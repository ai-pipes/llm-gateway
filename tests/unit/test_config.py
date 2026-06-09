import os
import pytest
import textwrap
from pathlib import Path
from pydantic import ValidationError
from gateway.config import load_config, Config


@pytest.fixture
def config_file(tmp_path):
    content = textwrap.dedent("""
        gateway:
          host: "0.0.0.0"
          port: 9000

        auth:
          module: "gateway.middleware.auth.StaticKeyAuthProvider"
          config:
            keys:
              "sk-dev":
                user_id: "dev"
                team_id: "engineering"

        adapters:
          - name: openai
            type: openai_compatible
            base_url: "https://api.openai.com/v1"
            auth:
              token_env: OPENAI_API_KEY
            default: true

        sanitizers:
          input: []
          output: []

        audit:
          type: stdout
    """)
    p = tmp_path / "gateway.yaml"
    p.write_text(content)
    return str(p)


def test_load_config_parses_gateway_section(config_file, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = load_config(config_file)
    assert config.gateway.host == "0.0.0.0"
    assert config.gateway.port == 9000


def test_load_config_parses_auth_section(config_file, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = load_config(config_file)
    assert config.auth.module == "gateway.middleware.auth.StaticKeyAuthProvider"
    assert "sk-dev" in config.auth.config["keys"]


def test_load_config_parses_adapters(config_file, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = load_config(config_file)
    assert len(config.adapters) == 1
    assert config.adapters[0].name == "openai"
    assert config.adapters[0].default is True


def test_load_config_env_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET_KEY", "sk-from-env")
    content = textwrap.dedent("""
        gateway:
          host: "0.0.0.0"
          port: 8080

        auth:
          module: "gateway.middleware.auth.StaticKeyAuthProvider"
          config:
            keys:
              "${MY_SECRET_KEY}":
                user_id: "alice"
                team_id: "eng"

        adapters: []
        sanitizers:
          input: []
          output: []
        audit:
          type: stdout
    """)
    p = tmp_path / "gateway.yaml"
    p.write_text(content)
    config = load_config(str(p))
    assert "sk-from-env" in config.auth.config["keys"]
    assert "${MY_SECRET_KEY}" not in config.auth.config["keys"]


def test_load_config_missing_env_var_raises(tmp_path):
    content = textwrap.dedent("""
        gateway:
          host: "0.0.0.0"
          port: 8080
        auth:
          module: "gateway.middleware.auth.StaticKeyAuthProvider"
          config: {}
        adapters:
          - name: openai
            type: openai_compatible
            base_url: "${MISSING_VAR}/v1"
            auth:
              token_env: OPENAI_API_KEY
        sanitizers:
          input: []
          output: []
        audit:
          type: stdout
    """)
    p = tmp_path / "gateway.yaml"
    p.write_text(content)
    with pytest.raises(ValueError, match="MISSING_VAR"):
        load_config(str(p))


def test_file_audit_config_requires_path(tmp_path):
    content = textwrap.dedent("""
        auth:
          module: "gateway.middleware.auth.StaticKeyAuthProvider"
          config: {}
        adapters: []
        sanitizers:
          input: []
          output: []
        audit:
          type: file
    """)
    p = tmp_path / "gateway.yaml"
    p.write_text(content)
    with pytest.raises(ValidationError):
        load_config(str(p))


def test_stdout_audit_config_default(tmp_path):
    content = textwrap.dedent("""
        auth:
          module: "gateway.middleware.auth.StaticKeyAuthProvider"
          config: {}
        adapters: []
        sanitizers:
          input: []
          output: []
    """)
    p = tmp_path / "gateway.yaml"
    p.write_text(content)
    config = load_config(str(p))
    assert config.audit.type == "stdout"


def test_body_logging_disabled_by_default_stdout():
    data = {
        "auth": {"module": "gateway.middleware.auth.StaticKeyAuthProvider", "config": {"keys": {}}},
        "audit": {"type": "stdout"},
    }
    from gateway.config import Config
    config = Config.model_validate(data)
    assert config.audit.body_logging.enabled is False


def test_body_logging_can_be_enabled_on_stdout():
    data = {
        "auth": {"module": "gateway.middleware.auth.StaticKeyAuthProvider", "config": {"keys": {}}},
        "audit": {"type": "stdout", "body_logging": {"enabled": True}},
    }
    from gateway.config import Config
    config = Config.model_validate(data)
    assert config.audit.body_logging.enabled is True


def test_body_logging_disabled_by_default_file():
    data = {
        "auth": {"module": "gateway.middleware.auth.StaticKeyAuthProvider", "config": {"keys": {}}},
        "audit": {"type": "file", "path": "/tmp/audit.jsonl"},
    }
    from gateway.config import Config
    config = Config.model_validate(data)
    assert config.audit.body_logging.enabled is False


def test_body_logging_on_plugin_audit():
    data = {
        "auth": {"module": "gateway.middleware.auth.StaticKeyAuthProvider", "config": {"keys": {}}},
        "audit": {
            "type": "plugin",
            "module": "some.backend.Backend",
            "body_logging": {"enabled": True},
        },
    }
    from gateway.config import Config
    config = Config.model_validate(data)
    assert config.audit.body_logging.enabled is True
