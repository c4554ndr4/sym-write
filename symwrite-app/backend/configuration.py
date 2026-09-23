"""Validated, immutable-per-process writing profiles; no secrets in the profile."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

APP_DIR = Path(__file__).resolve().parents[1]
ROOT = APP_DIR.parent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stage(StrictModel):
    provider: Literal["openrouter", "groq"] = "openrouter"
    model: str = Field(min_length=1, max_length=150)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=400, ge=64, le=1500)


class Models(StrictModel):
    candidate: Stage
    synthesis: Stage
    candidate_count: int = Field(default=5, ge=1, le=5)


class Source(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    title: str = Field(min_length=1, max_length=150)
    kind: Literal["writing", "fact"]
    text: str = Field(min_length=1, max_length=5000)


class Profile(StrictModel):
    version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=100)
    synthetic: bool = False
    style: str = Field(min_length=1, max_length=1500)
    models: Models
    sources: tuple[Source, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be unique")
        return self


def load_profile(path: Path) -> Profile:
    if path.stat().st_size > 1_500_000:
        raise ValueError("Profile is too large")
    return Profile.model_validate(yaml.safe_load(path.read_text()))
