import json
import sqlite3

import pytest
import yaml
from pydantic import ValidationError

from backend.configuration import APP_DIR, Profile, Source, load_profile
from backend.retrieval import Retriever, cosine
from profiles import create_profile, migrate_profile, write_profile


@pytest.fixture
def profile():
    return load_profile(APP_DIR / "config/config.yaml")


def test_private_profile_roundtrip_is_portable_and_does_not_overwrite(tmp_path):
    writing = tmp_path / "notes.md"
    writing.write_text("A private note about retrieval and uncertainty.")
    original = create_profile(
        'Name with "quotes"\nand a newline', [writing], ["An approved fact."]
    )
    output = tmp_path / "private.yaml"
    write_profile(original, output)
    restored = load_profile(output)
    assert original == restored
    assert not restored.synthetic
    assert restored.sources[0].text == writing.read_text()
    assert (output.stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        write_profile(original, output)
    assert original == load_profile(output)


def test_invalid_profile_does_not_create_output(tmp_path):
    with pytest.raises(ValidationError):
        create_profile("empty", [], [])
    assert not list(tmp_path.iterdir())


def test_migration_preserves_selected_text_only(tmp_path):
    path = tmp_path / "old.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "user": {
                    "name": "A researcher",
                    "facts": ["A fact."],
                    "writing_samples": ["Original words."],
                },
                "old_key": "not copied",
            }
        )
    )
    new = migrate_profile(path)
    assert [s.text for s in new.sources] == ["A fact.", "Original words."]
    assert "not copied" not in new.model_dump_json()
    assert new.id


def test_profile_identity_is_stable_across_source_edits(profile, tmp_path):
    data = profile.model_dump()
    data["sources"] = list(data["sources"]) + [
        Source(
            id="new", title="New note", kind="writing", text="Different writing."
        ).model_dump()
    ]
    edited = Profile.model_validate(data)
    assert edited.id == profile.id
    assert (
        Retriever(edited, tmp_path, "lexical").fingerprint
        != Retriever(profile, tmp_path, "lexical").fingerprint
    )


def test_independent_profiles_get_different_identity(tmp_path):
    a = create_profile("A", [], ["Same fact."])
    b = create_profile("B", [], ["Same fact."])
    assert a.id != b.id


def test_lexical_retrieval_order_budget_and_empty_query(profile, tmp_path):
    retriever = Retriever(profile, tmp_path, "lexical")
    assert retriever.search("") == []
    results = retriever.search("fiddle duel second draft", top_k=2, budget=350)
    assert results[0]["source_id"] == "avalon-revision-notes"
    assert len(results) <= 2
    assert sum(len(r["text"]) for r in results) <= 350
    assert retriever.search("zxqxzqzzzz") == []


class FakeEncoder:
    def __init__(self):
        self.encodes = 0

    def passage_embed(self, texts):
        self.encodes += 1
        return (
            [1.0 if i == j % 384 else 0 for i in range(384)]
            for j, _ in enumerate(texts)
        )

    def query_embed(self, query):
        return iter([[0.0, 1.0] + [0.0] * 382])


def test_semantic_retrieval_and_cache_reuse(profile, tmp_path):
    encoder = FakeEncoder()
    retriever = Retriever(profile, tmp_path, encoder=encoder)
    result = retriever.search("synthetic query")
    assert result[0]["source_id"] == "avalon-revision-notes"
    assert result[0]["score"] == 1
    assert encoder.encodes == 1
    second = Retriever(profile, tmp_path, encoder=encoder)
    assert second.search("synthetic query") == result
    assert encoder.encodes == 1


def test_corrupt_embedding_dimensions_fail_explicitly(profile, tmp_path):
    retriever = Retriever(profile, tmp_path, encoder=FakeEncoder())
    retriever.warmup()
    with sqlite3.connect(retriever.db_path) as db:
        db.execute(
            "UPDATE vectors SET payload=?",
            (json.dumps([[0.0]] * len(profile.sources)),),
        )
    with pytest.raises(ValueError, match="Embedding cache"):
        Retriever(profile, tmp_path, encoder=FakeEncoder()).warmup()


def test_zero_vector_is_not_relevant():
    assert cosine([0, 0], [1, 0]) == 0


def test_duplicate_source_ids_rejected(profile):
    data = profile.model_dump()
    data["sources"] = list(data["sources"]) + [data["sources"][0]]
    with pytest.raises(ValidationError, match="unique"):
        Profile.model_validate(data)
