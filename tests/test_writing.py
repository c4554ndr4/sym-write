import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.configuration import APP_DIR, load_profile
from backend.main import create_app
from backend.providers import GenerationError, ProviderClient, clean_output
from backend.writing import CompletionRequest, complete


@pytest.fixture
def profile():
    return load_profile(APP_DIR / "config/config.yaml")


class LocalContext:
    mode = "semantic"
    fingerprint = "test"

    def warmup(self):
        pass

    def search(self, text):
        return [
            {
                "id": "evidence:1",
                "title": "Approved note",
                "kind": "fact",
                "text": "The violet notebook has three sections.",
                "score": 0.8,
            }
        ]


class RecordingClient:
    def __init__(self, failing=(), synthesis_failure=False):
        self.calls = []
        self.failing = failing
        self.synthesis_failure = synthesis_failure
        self.active = 0
        self.max_active = 0

    async def generate(self, stage, system, prompt):
        payload = json.loads(prompt)
        index = len(self.calls)
        self.calls.append((stage, system, payload))
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        try:
            await asyncio.sleep(0)
            if index in self.failing or (
                self.synthesis_failure and "candidates" in payload
            ):
                raise GenerationError("Synthetic provider failure")
            return (
                "A refined continuation."
                if "candidates" in payload
                else f"Candidate number {index + 1}."
            )
        finally:
            self.active -= 1


def request(**kwargs):
    text = "I am considering how retrieval should preserve uncertainty."
    return CompletionRequest(
        text=text,
        cursor=len(text),
        request_id="test",
        profile_version="0" * 64,
        **kwargs,
    )


async def test_retrieval_reaches_both_stages_and_candidates_are_parallel(profile):
    client = RecordingClient()
    result = await complete(request(), profile, LocalContext(), client)
    assert len(client.calls) == 6
    assert client.max_active == 5
    assert all(
        call[2]["sources"][0]["text"] == "The violet notebook has three sections."
        for call in client.calls
    )
    assert client.calls[0][0] == profile.models.candidate
    assert client.calls[-1][0] == profile.models.synthesis
    assert client.calls[-1][2]["candidates"] == [
        f"Candidate number {i}." for i in range(1, 6)
    ]
    assert result["suggestions"][0]["label"] == "Refined"
    assert len(result["suggestions"]) == 6
    assert result["synthesis_status"] == "complete"


async def test_partial_failure_never_enters_synthesis(profile):
    client = RecordingClient(failing=(1, 3))
    result = await complete(request(), profile, LocalContext(), client)
    assert result["candidate_count"] == 3
    assert result["failed_candidates"] == 2
    assert "failed" in result["warning"]
    assert all("failure" not in c for c in client.calls[-1][2]["candidates"])


async def test_all_failures_are_errors_not_prose(profile):
    client = RecordingClient(failing=range(5))
    with pytest.raises(GenerationError, match="Synthetic provider failure"):
        await complete(request(), profile, LocalContext(), client)
    assert len(client.calls) == 5


async def test_synthesis_failure_keeps_explicitly_labelled_candidates(profile):
    result = await complete(
        request(), profile, LocalContext(), RecordingClient(synthesis_failure=True)
    )
    assert result["synthesis_status"] == "failed"
    assert all(s["stage"] == "candidate" for s in result["suggestions"])
    assert "Refinement failed" in result["warning"]


async def test_explore_skips_synthesis_and_length_reaches_prompt(profile):
    client = RecordingClient()
    result = await complete(
        request(mode="explore", length="sentence"), profile, LocalContext(), client
    )
    assert len(client.calls) == 5
    assert "15–35" in client.calls[0][2]["length"]
    assert result["models"]["synthesis"] is None


async def test_cursor_context_preserves_suffix_and_unicode(profile):
    text = "An idea 🌱 continues here. Keep this ending."
    req = CompletionRequest(
        text=text,
        cursor=text.index(" Keep"),
        request_id="test",
        profile_version="0" * 64,
    )
    client = RecordingClient()
    await complete(req, profile, LocalContext(), client)
    assert client.calls[0][2]["before_cursor"] == "An idea 🌱 continues here."
    assert client.calls[0][2]["after_cursor"] == " Keep this ending."


@pytest.mark.parametrize(
    "value", [None, "", "<think>unclosed", "<think>private trace</think>", "x" * 6001]
)
def test_reject_empty_or_reasoning_only_output(value):
    with pytest.raises(GenerationError):
        clean_output(value)


def test_only_completion_is_returned():
    assert (
        clean_output("<think>discard</think><continue>New prose.</continue>")
        == "New prose."
    )


async def test_stage_providers_use_their_own_endpoint_and_key(profile, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-router")
    seen = []

    def handler(req):
        seen.append(
            (str(req.url), req.headers["authorization"], json.loads(req.content))
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "A real-shaped response."},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ProviderClient(http)
        await client.generate(profile.models.candidate, "system", "prompt")
        stage = profile.models.synthesis.model_copy(
            update={"provider": "groq", "model": "test-groq-model"}
        )
        await client.generate(stage, "system", "prompt")
    assert seen[0][0] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen[0][1] == "Bearer test-router"
    assert seen[1][0] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen[1][1] == "Bearer test-groq"
    assert seen[1][2]["model"] == "test-groq-model"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_provider_errors_never_expose_body_or_key(status, profile, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "never-show-this-key")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(status, text="private provider body")
        )
    ) as http:
        with pytest.raises(GenerationError) as error:
            await ProviderClient(http).generate(profile.models.candidate, "s", "p")
    assert "private provider body" not in str(error.value)
    assert "never-show-this-key" not in str(error.value)


async def test_missing_key_does_not_fall_back_to_mock(profile, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    async with httpx.AsyncClient() as http:
        with pytest.raises(GenerationError, match="Set OPENROUTER_API_KEY"):
            await ProviderClient(http).generate(profile.models.candidate, "s", "p")


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": [{"message": {}}]},
        {
            "choices": [
                {"message": {"content": "unfinished"}, "finish_reason": "length"}
            ]
        },
        {"choices": [{"message": {"content": None}}]},
    ],
)
async def test_malformed_and_truncated_responses_fail(response, profile, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))
    ) as http:
        with pytest.raises(GenerationError):
            await ProviderClient(http).generate(profile.models.candidate, "s", "p")


def test_http_profile_binding_and_local_origin(profile, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    recorder = RecordingClient()
    from backend.access import AccessSettings

    app = create_app(
        profile,
        LocalContext(),
        recorder,
        settings=AccessSettings(database=tmp_path / "access.db"),
    )
    with TestClient(app) as http:
        http.headers["X-CSRF-Token"] = http.get("/api/access").json()["csrf_token"]
        status = http.get("/api/status").json()
        assert status["profile_id"] == profile.id
        body = request().model_dump()
        assert http.post("/api/complete", json=body).status_code == 409
        assert not recorder.calls
        body["profile_version"] = status["profile_version"]
        assert (
            http.post(
                "/api/complete",
                json=body,
                headers={"Origin": "https://outside.example"},
            ).status_code
            == 403
        )
        response = http.post("/api/complete", json=body)
        assert response.status_code == 200
        assert response.json()["suggestions"][0]["text"] == "A refined continuation."
        assert http.get("/", headers={"Host": "outside.example"}).status_code == 400


def test_http_validation_rejects_bad_cursor_before_model(profile, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    recorder = RecordingClient()
    with TestClient(create_app(profile, LocalContext(), recorder)) as http:
        body = request().model_dump()
        body["cursor"] = 1000
        assert http.post("/api/complete", json=body).status_code == 422
        assert not recorder.calls


def test_boundary_echo_is_removed_without_rewriting_new_prose():
    from backend.writing import remove_boundary_echo

    assert (
        remove_boundary_echo(
            "If there is a choice, then", "then the writer should see it."
        )
        == "the writer should see it."
    )
    assert (
        remove_boundary_echo(
            "I would begin an evaluation by",
            "I would begin an evaluation by asking writers what they changed.",
        )
        == "asking writers what they changed."
    )
    assert (
        remove_boundary_echo("I think that", "that assumption is worth examining.")
        == "that assumption is worth examining."
    )
    assert (
        remove_boundary_echo("It is a choice.", "A choice can have consequences.")
        == "A choice can have consequences."
    )
    with pytest.raises(GenerationError):
        remove_boundary_echo("Entire draft", "Entire draft")


def test_boundary_echo_preserves_sentence_and_clause_punctuation():
    from backend.writing import remove_boundary_echo

    assert (
        remove_boundary_echo("The question is whether", "Whether? I am not sure.")
        == "Whether? I am not sure."
    )
    assert (
        remove_boundary_echo("I said no. No", "No. No one agreed.")
        == "No. No one agreed."
    )
    assert remove_boundary_echo("I think no, I", "No I would not.") == "No I would not."
