"""Tests for config loading and run-readiness validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from assistant.config import Config, ConfigError, load_config


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


def _config(state_dir: Path, **overrides) -> Config:
    defaults = {
        "telegram_bot_token": "123456:ABC-token",
        "allowed_user_ids": [42],
        "state_dir": state_dir,
        "vault_path": state_dir.parent / "vault",
    }
    return Config(**{**defaults, **overrides})


def _write_oauth_token(state_dir: Path) -> None:
    (state_dir / "oauth_token").write_text("gho_token")


# ------------------------------------------------------------------
# Lenient loading
# ------------------------------------------------------------------

def test_load_config_with_empty_toml_does_not_raise(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("")

    cfg = load_config(cfg_file)

    assert cfg.telegram_bot_token == ""
    assert cfg.allowed_user_ids == []


def test_load_config_without_file_does_not_raise(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.toml")

    assert cfg.telegram_bot_token == ""


def test_github_token_read_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_abc")

    cfg = load_config(tmp_path / "does-not-exist.toml")

    assert cfg.github_token == "github_pat_abc"


def test_github_token_defaults_to_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    cfg = load_config(tmp_path / "does-not-exist.toml")

    assert cfg.github_token == ""


def test_validate_passes_without_github_token(state_dir: Path) -> None:
    """Voice transcription is optional — no GITHUB_TOKEN must not block startup."""
    _write_oauth_token(state_dir)

    _config(state_dir, github_token="").validate_for_run()  # should not raise


# ------------------------------------------------------------------
# validate_for_run
# ------------------------------------------------------------------

def test_validate_passes_when_everything_is_ready(state_dir: Path) -> None:
    _write_oauth_token(state_dir)

    _config(state_dir).validate_for_run()  # should not raise


def test_validate_rejects_missing_bot_token(state_dir: Path) -> None:
    _write_oauth_token(state_dir)
    cfg = _config(state_dir, telegram_bot_token="  ")

    with pytest.raises(ConfigError, match="bot_token"):
        cfg.validate_for_run()


def test_validate_rejects_empty_allowed_user_ids(state_dir: Path) -> None:
    _write_oauth_token(state_dir)
    cfg = _config(state_dir, allowed_user_ids=[])

    with pytest.raises(ConfigError, match="allowed_user_ids"):
        cfg.validate_for_run()


def test_validate_rejects_invalid_timezone(state_dir: Path) -> None:
    _write_oauth_token(state_dir)
    cfg = _config(state_dir, timezone="Mars/Olympus_Mons")

    with pytest.raises(ConfigError, match="timezone"):
        cfg.validate_for_run()


def test_validate_rejects_missing_oauth_token(state_dir: Path) -> None:
    cfg = _config(state_dir)

    with pytest.raises(ConfigError, match="assistant auth"):
        cfg.validate_for_run()


# ------------------------------------------------------------------
# Model selection config
# ------------------------------------------------------------------

def test_models_map_and_default_model_from_toml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[copilot]\n'
        'default_model = "opus"\n'
        '\n'
        '[copilot.models]\n'
        'sonnet = "claude-sonnet-4.6"\n'
        'opus = "claude-opus-41"\n'
    )

    cfg = load_config(cfg_file)

    assert cfg.default_model == "opus"
    assert cfg.models == {"sonnet": "claude-sonnet-4.6", "opus": "claude-opus-41"}


def test_models_default_when_absent(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.toml")

    assert cfg.default_model == "sonnet"
    assert cfg.models == {"sonnet": "claude-sonnet-5"}


def test_default_model_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_MODEL", "opus")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[copilot]\ndefault_model = "sonnet"\n')

    cfg = load_config(cfg_file)

    assert cfg.default_model == "opus"


def test_default_chat_id_from_toml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[telegram]\ndefault_chat_id = 555\n')

    cfg = load_config(cfg_file)

    assert cfg.default_chat_id == 555


def test_default_chat_id_defaults_to_none(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.toml")

    assert cfg.default_chat_id is None


def test_default_chat_id_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_CHAT_ID", "999")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[telegram]\ndefault_chat_id = 555\n')

    cfg = load_config(cfg_file)

    assert cfg.default_chat_id == 999


def test_fourget_url_from_toml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[web]\nfourget_url = "http://fourget"\n')

    cfg = load_config(cfg_file)

    assert cfg.fourget_url == "http://fourget"


def test_fourget_url_defaults_to_empty(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.toml")

    assert cfg.fourget_url == ""


def test_fourget_url_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOURGET_URL", "http://other:9999")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[web]\nfourget_url = "http://fourget"\n')

    cfg = load_config(cfg_file)

    assert cfg.fourget_url == "http://other:9999"


def test_validate_rejects_default_model_not_in_models(state_dir: Path) -> None:
    _write_oauth_token(state_dir)
    cfg = _config(state_dir, default_model="opus")  # models defaults to {"sonnet": ...}

    with pytest.raises(ConfigError, match="default_model"):
        cfg.validate_for_run()


# ------------------------------------------------------------------
# Paths and partial TOML sections
# ------------------------------------------------------------------

def test_paths_default_relative_to_the_working_directory(tmp_path: Path) -> None:
    """Deployment layouts belong in the image, not in the code's defaults."""
    cfg = load_config(tmp_path / "does-not-exist.toml")

    assert cfg.vault_path == (Path.cwd() / "vault").resolve()
    assert cfg.state_dir == (Path.cwd() / "state").resolve()


def test_vault_path_and_state_dir_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "mounted-vault"))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "mounted-state"))
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[assistant]\nvault_path = "/from/toml"\nstate_dir = "/from/toml"\n')

    cfg = load_config(cfg_file)

    assert cfg.vault_path == tmp_path / "mounted-vault"
    assert cfg.state_dir == tmp_path / "mounted-state"


def test_history_size_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HISTORY_SIZE", "12")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[assistant]\nhistory_size = 40\n")

    cfg = load_config(cfg_file)

    assert cfg.history_size == 12


def test_keys_absent_from_a_present_section_keep_their_defaults(tmp_path: Path) -> None:
    """A section that sets one key must not blank out its siblings."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[assistant]\ntimezone = "Europe/Madrid"\n')

    cfg = load_config(cfg_file)

    assert cfg.timezone == "Europe/Madrid"
    assert cfg.history_size == 40


def test_allowed_user_ids_env_rejects_non_numeric_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOWED_USER_IDS", "123,not-a-number")

    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        load_config(tmp_path / "does-not-exist.toml")


def test_validate_reports_all_problems_at_once(state_dir: Path) -> None:
    cfg = _config(
        state_dir,
        telegram_bot_token="",
        allowed_user_ids=[],
        timezone="Nope/Nope",
    )

    with pytest.raises(ConfigError) as exc:
        cfg.validate_for_run()

    message = str(exc.value)
    assert "bot_token" in message
    assert "allowed_user_ids" in message
    assert "timezone" in message
    assert "assistant auth" in message


# ------------------------------------------------------------------
# Backup
# ------------------------------------------------------------------

def test_backup_is_disabled_by_default(state_dir: Path) -> None:
    cfg = _config(state_dir)

    assert cfg.backup_enabled is False
    assert cfg.backup_git_dir is None


def test_backup_toml_section(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'[backup]\nenabled = true\ngit_dir = "{tmp_path}/backups/vault.git"\n'
    )

    cfg = load_config(cfg_file)

    assert cfg.backup_enabled is True
    assert cfg.backup_git_dir == tmp_path / "backups" / "vault.git"


def test_backup_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[backup]\nenabled = false\n")
    monkeypatch.setenv("BACKUP_ENABLED", "true")
    monkeypatch.setenv("BACKUP_GIT_DIR", str(tmp_path / "elsewhere"))

    cfg = load_config(cfg_file)

    assert cfg.backup_enabled is True
    assert cfg.backup_git_dir == tmp_path / "elsewhere"


def test_validate_rejects_backup_git_dir_inside_the_vault(state_dir: Path) -> None:
    _write_oauth_token(state_dir)
    vault = state_dir.parent / "vault"
    cfg = _config(
        state_dir,
        backup_enabled=True,
        backup_git_dir=vault / ".git",
    )

    with pytest.raises(ConfigError, match="inside the vault"):
        cfg.validate_for_run()


def test_validate_requires_git_binary_when_backup_enabled(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_oauth_token(state_dir)
    monkeypatch.setattr("shutil.which", lambda name: None)
    cfg = _config(state_dir, backup_enabled=True)

    with pytest.raises(ConfigError, match="git"):
        cfg.validate_for_run()
