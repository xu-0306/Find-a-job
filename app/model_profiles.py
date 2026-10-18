"""Saved model connections share the same validation as active service settings."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from .store import Input

Protocol = Literal["auto", "responses", "chat_completions", "anthropic_messages"]
CONNECTION_FIELDS = ("llm_base_url", "llm_model", "llm_protocol")
MAX_PROFILES = 50


class LLMSettings(Input):
    llm_base_url: str | None = Field(default=None, max_length=2000)
    llm_model: str | None = Field(default=None, max_length=200)
    llm_protocol: Protocol | None = None

    @field_validator("llm_base_url")
    @classmethod
    def valid_url(cls, value):
        if value:
            try:
                parsed = urlsplit(value)
                valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
                parsed.port
            except ValueError:
                valid = False
            if not valid or any(char.isspace() for char in value):
                raise ValueError("需為不含帳密的 HTTP 或 HTTPS 網址")
        return value


class ProfileInput(LLMSettings):
    name: str = Field(min_length=1, max_length=80)
    llm_base_url: str = Field(min_length=1, max_length=2000)
    llm_model: str = Field(min_length=1, max_length=200)
    llm_protocol: Protocol = "auto"
    llm_api_key: str = Field(default="", max_length=4096)


class StoredProfile(ProfileInput):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    llm_api_key: str = Field(min_length=1, max_length=4096)


class ProfileCollection(Input):
    items: list[StoredProfile] = Field(default_factory=list, max_length=MAX_PROFILES)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("模型設定 ID 重複")
        return self


def profile_view(profile, settings, key):
    # A profile is identified by its UUID, never its model or provider name.
    return {**{field: profile[field] for field in ("id", "name", *CONNECTION_FIELDS)},
            "llm_api_key": {"configured": bool(profile["llm_api_key"]), "mask": "••••••••"},
            "is_active": all(profile[field] == settings.get(field) for field in CONNECTION_FIELDS)
                         and profile["llm_api_key"] == key}
