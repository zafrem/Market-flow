import logging
import os

import pytest

from marketflow.settings import Settings, load_settings

TOML = """
[database]
url = "sqlite:///data/x.db"
[llm]
mock = false
[sources.kamis]
api_key = "file-key"
api_id = "file-id"
[sources.garak]
url = "https://garak.example/api"
api_key = "g-secret"
"""


def write(tmp_path, body=TOML, mode=0o600):
    p = tmp_path / "settings.toml"
    p.write_text(body)
    os.chmod(p, mode)
    return p


def test_defaults_when_no_file_and_no_env(tmp_path):
    s = load_settings(tmp_path / "missing.toml", env={})
    assert s == Settings() and not s.kamis_enabled and not s.garak_enabled and s.mock_llm


def test_reads_tokens_from_file(tmp_path):
    s = load_settings(write(tmp_path), env={})
    assert (s.kamis_api_key, s.kamis_api_id, s.garak_url) == ("file-key", "file-id", "https://garak.example/api")
    assert s.kamis_enabled and s.garak_enabled and s.mock_llm is False


def test_env_overrides_file(tmp_path):
    s = load_settings(write(tmp_path), env={"KAMIS_API_KEY": "env-key", "MOCK_LLM": "true"})
    assert s.kamis_api_key == "env-key" and s.kamis_api_id == "file-id" and s.mock_llm is True


def test_settings_path_from_env(tmp_path):
    p = write(tmp_path)
    assert load_settings(env={"MARKETFLOW_SETTINGS": str(p)}).kamis_api_key == "file-key"


def test_partial_credentials_do_not_enable_kamis(tmp_path):
    p = write(tmp_path, '[sources.kamis]\napi_key = "only-key"\n')
    assert not load_settings(p, env={}).kamis_enabled


def test_invalid_toml_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="invalid settings file"):
        load_settings(write(tmp_path, "[broken"), env={})


def test_tokens_never_appear_in_repr(tmp_path):
    s = load_settings(write(tmp_path), env={})
    assert "file-key" not in repr(s) and "g-secret" not in repr(s) and "***" in repr(s)


def test_warns_when_file_is_world_readable(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        load_settings(write(tmp_path, mode=0o644), env={})
    assert "chmod 600" in caplog.text


def test_example_file_is_valid_and_has_no_secrets():
    from marketflow.settings import ROOT
    s = load_settings(ROOT / "config" / "settings.example.toml", env={})
    assert not s.kamis_enabled and not s.garak_enabled


def test_vision_uses_an_openai_key_from_file_or_env_and_never_leaks(tmp_path):
    assert not load_settings(tmp_path / "missing.toml", env={}).vision_enabled
    p = write(tmp_path, '[vision]\nopenai_api_key = "sk-file-secret"\nopenai_model = "gpt-test"\n')
    s = load_settings(p, env={})
    assert s.vision_enabled and s.openai_model == "gpt-test"
    assert load_settings(p, env={"OPENAI_API_KEY": " sk-env-secret ", "OPENAI_MODEL": "m2"}).openai_api_key == "sk-env-secret"
    assert "sk-file-secret" not in repr(s) and "sk-env-secret" not in repr(load_settings(p, env={"OPENAI_API_KEY": "sk-env-secret"}))
