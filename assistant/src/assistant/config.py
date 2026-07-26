"""Configuration loading and validation."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """The loaded config is not ready to run the service."""


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # Telegram
    telegram_bot_token: str = ""
    allowed_user_ids: list[int] = Field(default_factory=list)
    # Fallback chat for proactive sends until one is learned from an incoming message
    default_chat_id: int | None = None

    # Copilot — alias → model id map, and the alias used by default
    default_model: str = "sonnet"
    models: dict[str, str] = Field(default_factory=lambda: {"sonnet": "claude-sonnet-5"})

    # GitHub Models (voice transcription) — optional; a fine-grained PAT
    # with the `models: read` permission, read from GITHUB_TOKEN
    github_token: str = ""

    # Web research (optional) — base URL of a SearXNG instance; empty disables
    # the research tool entirely
    searxng_url: str = ""

    # Assistant. The path defaults are relative to the working directory, which
    # suits a local checkout; a packaged deployment points them at its own
    # mounts via VAULT_PATH / STATE_DIR rather than having them baked in here.
    timezone: str = "UTC"
    vault_path: Path = Path("vault")
    state_dir: Path = Path("state")
    history_size: int = 40

    @field_validator("vault_path", "state_dir", mode="before")
    @classmethod
    def expand_path(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    def validate_for_run(self) -> None:
        """Check everything the service needs before starting; raise ConfigError listing all problems."""
        problems: list[str] = []

        if not self.telegram_bot_token.strip():
            problems.append(
                "telegram.bot_token is not set — create a bot with https://t.me/BotFather "
                "and set it in config.toml or via TELEGRAM_BOT_TOKEN"
            )
        if not self.allowed_user_ids:
            problems.append(
                "telegram.allowed_user_ids is empty — add your Telegram user id "
                "in config.toml or via ALLOWED_USER_IDS, otherwise the bot ignores everyone"
            )
        try:
            ZoneInfo(self.timezone)
        except Exception:
            problems.append(
                f"assistant.timezone {self.timezone!r} is not a valid IANA timezone "
                "(e.g. 'Europe/Madrid')"
            )
        if self.default_model not in self.models:
            problems.append(
                f"copilot.default_model {self.default_model!r} is not a key of copilot.models "
                f"({', '.join(sorted(self.models)) or 'empty'}) — add it under [copilot.models] "
                "in config.toml"
            )
        from .copilot import OAUTH_TOKEN_FILENAME

        oauth_token = self.state_dir / OAUTH_TOKEN_FILENAME
        if not oauth_token.exists():
            problems.append(
                f"no GitHub OAuth token at {oauth_token} — run `assistant auth` first"
            )

        if problems:
            raise ConfigError(
                "Not ready to run:\n" + "\n".join(f"  - {p}" for p in problems)
            )


# (TOML section, key) → Config field name.
_TOML_FIELDS = (
    ("telegram", "bot_token", "telegram_bot_token"),
    ("telegram", "allowed_user_ids", "allowed_user_ids"),
    ("telegram", "default_chat_id", "default_chat_id"),
    ("copilot", "default_model", "default_model"),
    ("copilot", "models", "models"),
    ("web", "searxng_url", "searxng_url"),
    ("assistant", "timezone", "timezone"),
    ("assistant", "vault_path", "vault_path"),
    ("assistant", "state_dir", "state_dir"),
    ("assistant", "history_size", "history_size"),
)

# Env var → Config field name. Applied after the TOML file so env always wins.
_ENV_FIELDS = {
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "DEFAULT_CHAT_ID": "default_chat_id",
    "DEFAULT_MODEL": "default_model",
    "SEARXNG_URL": "searxng_url",
    "TIMEZONE": "timezone",
    "VAULT_PATH": "vault_path",
    "STATE_DIR": "state_dir",
    "HISTORY_SIZE": "history_size",
}


def load_config(config_path: Path | None = None) -> Config:
    """Load config from TOML file, with env-var overrides."""
    data: dict = {}

    # Find config file
    if config_path is None:
        candidates = [
            Path("config.toml"),
            Path(__file__).parent.parent.parent / "config.toml",
        ]
        for c in candidates:
            if c.exists():
                config_path = c
                break

    if config_path and config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        # Flatten nested TOML sections into the flat keys Config expects. Only
        # keys actually present are copied, so every default lives exactly once
        # — on the model above — instead of being restated here.
        for section, key, field_name in _TOML_FIELDS:
            value = raw.get(section, {}).get(key)
            if value is not None:
                data[field_name] = value

    for env_key, field_name in _ENV_FIELDS.items():
        val = os.environ.get(env_key)
        if val is not None:
            data[field_name] = val

    # ALLOWED_USER_IDS is a comma-separated list of integers, e.g. "123456,789012"
    allowed_ids_env = os.environ.get("ALLOWED_USER_IDS")
    if allowed_ids_env is not None:
        try:
            data["allowed_user_ids"] = [
                int(uid.strip()) for uid in allowed_ids_env.split(",") if uid.strip()
            ]
        except ValueError:
            raise ConfigError(
                f"ALLOWED_USER_IDS must be a comma-separated list of numeric Telegram "
                f"user ids, got {allowed_ids_env!r}"
            ) from None

    return Config(**data)
