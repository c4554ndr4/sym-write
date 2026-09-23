"""One bounded interactive interface for both supported model providers."""

import os
import re

import httpx

from .configuration import Stage

ENDPOINTS = {
    "openrouter": (
        "https://openrouter.ai/api/v1/chat/completions",
        "OPENROUTER_API_KEY",
    ),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
}


class GenerationError(Exception):
    """Safe message that may be displayed without provider bodies or keys."""


def key_name(stage: Stage, public=False):
    return "SYMWRITE_OPENROUTER_API_KEY" if public else ENDPOINTS[stage.provider][1]


def api_key(stage: Stage, public=False):
    return os.getenv(key_name(stage, public), "").strip()


def configured(stage: Stage, public=False):
    return bool(api_key(stage, public))


def clean_output(text):
    if not isinstance(text, str):
        raise GenerationError("The model returned an unreadable suggestion. Try again.")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    if re.search(r"</?think>", text, flags=re.I):
        raise GenerationError("The model returned an incomplete suggestion. Try again.")
    match = re.fullmatch(r"<continue>(.*?)</continue>", text, flags=re.S | re.I)
    if match:
        text = match.group(1).strip()
    if not text or len(text) > 6000:
        raise GenerationError(
            "The model returned an empty or oversized suggestion. Try again."
        )
    return text


class ProviderClient:
    def __init__(self, http: httpx.AsyncClient, public=False):
        self.http = http
        self.public = public

    async def generate(self, stage: Stage, system: str, prompt: str):
        url, _ = ENDPOINTS[stage.provider]
        name = key_name(stage, self.public)
        key = api_key(stage, self.public)
        if not key:
            raise GenerationError(
                f"Set {name} in your .env file, then restart SymWrite."
            )
        try:
            response = await self.http.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": stage.model,
                    "temperature": stage.temperature,
                    "max_tokens": stage.max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=35,
            )
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise GenerationError(
                    "The model reached its output limit. Try a shorter suggestion."
                )
            return clean_output(choice["message"]["content"])
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status in (401, 403):
                raise GenerationError(
                    f"{stage.provider.title()} rejected the API key or model access."
                ) from None
            if status == 429:
                raise GenerationError(
                    f"{stage.provider.title()} is rate limited. Wait a moment and try again."
                ) from None
            raise GenerationError(
                f"{stage.provider.title()} could not complete the request (HTTP {status}). Check the model and account."
            ) from None
        except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError):
            raise GenerationError(
                f"{stage.provider.title()} returned no usable response. Please try again."
            ) from None
