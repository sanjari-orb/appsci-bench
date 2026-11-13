"""Config tests: names-not-values exposure, presence booleans, port handling, redaction."""
from __future__ import annotations

import pytest

import config


def test_present_credential_names_returns_names_not_values(monkeypatch):
    secret = "XXXXXXXXXXXXXXXXXXXXXXXX"
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", secret)
    names = config.present_credential_env_names()
    assert "FAKE_PROVIDER_API_KEY" in names
    # The value must never appear.
    assert all(secret not in n for n in names)


def test_present_credential_names_excludes_banking_vars(monkeypatch):
    monkeypatch.setenv("BANKING_TOOLS_TOKEN", "tok")
    monkeypatch.setenv("BANKING_TOOLS_URL", "http://x")
    names = config.present_credential_env_names()
    assert "BANKING_TOOLS_TOKEN" not in names
    assert "BANKING_TOOLS_URL" not in names


def test_banking_env_presence_booleans_only(monkeypatch):
    monkeypatch.setenv("BANKING_TOOLS_URL", "http://tools")
    monkeypatch.delenv("BANKING_TOOLS_TOKEN", raising=False)
    presence = config.banking_env_presence()
    assert presence == {"BANKING_TOOLS_URL": True, "BANKING_TOOLS_TOKEN": False}


def test_banking_tools_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("BANKING_TOOLS_URL", "http://tools.test/")
    assert config.banking_tools_url() == "http://tools.test"


def test_require_banking_tools_raises_when_missing(monkeypatch):
    monkeypatch.delenv("BANKING_TOOLS_URL", raising=False)
    monkeypatch.delenv("BANKING_TOOLS_TOKEN", raising=False)
    with pytest.raises(RuntimeError) as exc:
        config.require_banking_tools()
    assert "BANKING_TOOLS_URL" in str(exc.value)
    assert "BANKING_TOOLS_TOKEN" in str(exc.value)


def test_port_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert config.port() == config.DEFAULT_PORT


def test_port_reads_env(monkeypatch):
    monkeypatch.setenv("PORT", "12345")
    assert config.port() == 12345


def test_port_raises_on_non_integer(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-number")
    with pytest.raises(RuntimeError):
        config.port()


def test_redact_replaces_token(monkeypatch):
    monkeypatch.setenv("BANKING_TOOLS_TOKEN", "secret-tok")
    assert "secret-tok" not in config.redact("prefix secret-tok suffix")
    assert "<redacted>" in config.redact("prefix secret-tok suffix")


def test_redact_noop_without_token(monkeypatch):
    monkeypatch.delenv("BANKING_TOOLS_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("APPSCI_AGENT_CUSTOM_HEADERS", raising=False)
    assert config.redact("nothing to hide") == "nothing to hide"


# --- agent model id (explicit non-secret default; override honored first) -------------------------

def _clear_model_vars(monkeypatch):
    for var in config._AGENT_MODEL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_agent_model_defaults_to_chosen_id(monkeypatch):
    _clear_model_vars(monkeypatch)
    assert config.agent_model() == config.DEFAULT_AGENT_MODEL == "claude-opus-4-8"
    assert config.agent_model_source() == f"default:{config.DEFAULT_AGENT_MODEL}"


@pytest.mark.parametrize("var", config._AGENT_MODEL_VARS)
def test_agent_model_honors_injected_override(monkeypatch, var):
    _clear_model_vars(monkeypatch)
    monkeypatch.setenv(var, "some-other-model")
    assert config.agent_model() == "some-other-model"
    assert config.agent_model_source() == var


# --- anthropic runtime accessors ------------------------------------------------------------------

def test_anthropic_base_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.test/")
    assert config.anthropic_base_url() == "https://proxy.test"


def test_anthropic_base_url_none_when_unset(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert config.anthropic_base_url() is None


def test_anthropic_auth_token_none_when_empty(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    assert config.anthropic_auth_token() is None


def test_anthropic_custom_headers_parses_json_object(monkeypatch):
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", '{"x-route": "abc", "x-team": 7}')
    assert config.anthropic_custom_headers() == {"x-route": "abc", "x-team": "7"}
    assert config.anthropic_custom_headers_error() is None


def test_anthropic_custom_headers_parses_single_raw_line(monkeypatch):
    # The actual runtime format: one "Name: value" header line.
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", "X-Route: abc123value")
    assert config.anthropic_custom_headers() == {"X-Route": "abc123value"}
    assert config.anthropic_custom_headers_error() is None


def test_anthropic_custom_headers_value_may_contain_colons(monkeypatch):
    # Only the first ':' separates name from value; the value keeps the rest verbatim.
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", "X-Target: https://host:8443/path")
    assert config.anthropic_custom_headers() == {"X-Target": "https://host:8443/path"}
    assert config.anthropic_custom_headers_error() is None


def test_anthropic_custom_headers_parses_multiple_raw_lines(monkeypatch):
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", "X-Route: abc\nX-Team: seven")
    assert config.anthropic_custom_headers() == {"X-Route": "abc", "X-Team": "seven"}
    assert config.anthropic_custom_headers_error() is None


def test_anthropic_custom_headers_malformed_line_surfaces_error_not_silent(monkeypatch):
    # A present value with no ':' must NOT silently become {} -- the error must be surfaced.
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", "not-a-header-line")
    assert config.anthropic_custom_headers() == {}
    err = config.anthropic_custom_headers_error()
    assert err is not None
    assert "APPSCI_AGENT_CUSTOM_HEADERS" in err


def test_anthropic_custom_headers_empty_name_surfaces_error(monkeypatch):
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", ": value-with-no-name")
    assert config.anthropic_custom_headers() == {}
    assert config.anthropic_custom_headers_error() is not None


def test_anthropic_custom_headers_bad_json_surfaces_error(monkeypatch):
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", '{"x-route": ')
    assert config.anthropic_custom_headers() == {}
    err = config.anthropic_custom_headers_error()
    assert err is not None and "JSON" in err


def test_anthropic_custom_headers_json_non_object_surfaces_error(monkeypatch):
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", '["a", "b"]')
    assert config.anthropic_custom_headers() == {}
    assert config.anthropic_custom_headers_error() is not None


def test_anthropic_custom_headers_absent_is_empty_no_error(monkeypatch):
    monkeypatch.delenv("APPSCI_AGENT_CUSTOM_HEADERS", raising=False)
    assert config.anthropic_custom_headers() == {}
    assert config.anthropic_custom_headers_error() is None


def test_require_anthropic_returns_pair(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    assert config.require_anthropic() == ("https://proxy.test", "tok")


def test_require_anthropic_raises_when_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError) as exc:
        config.require_anthropic()
    assert "ANTHROPIC_BASE_URL" in str(exc.value)
    assert "ANTHROPIC_AUTH_TOKEN" in str(exc.value)


def test_anthropic_env_presence_booleans_only(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", '{"x-route": "abc"}')
    presence = config.anthropic_env_presence()
    assert presence == {
        "ANTHROPIC_BASE_URL": True,
        "ANTHROPIC_AUTH_TOKEN": False,
        "APPSCI_AGENT_CUSTOM_HEADERS": True,
    }
    # Booleans only -- no value leaks through.
    assert set(presence.values()) <= {True, False}


# --- redaction covers all secret sources ----------------------------------------------------------

def test_redact_scrubs_anthropic_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-secret-123")
    out = config.redact("Authorization: Bearer anthropic-secret-123 failed")
    assert "anthropic-secret-123" not in out
    assert "<redacted>" in out


def test_redact_scrubs_custom_header_values(monkeypatch):
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", '{"x-route": "routing-secret-value"}')
    out = config.redact("sent x-route: routing-secret-value upstream")
    assert "routing-secret-value" not in out
    assert "<redacted>" in out


def test_redact_scrubs_raw_line_header_value(monkeypatch):
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", "X-Route: routing-secret-value")
    out = config.redact("sent X-Route: routing-secret-value upstream")
    assert "routing-secret-value" not in out
    assert "<redacted>" in out


def test_redact_scrubs_malformed_header_value_defensively(monkeypatch):
    # Empty name -> parsing drops it, but the value must STILL be scrubbed from any diagnostic/error.
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", ": routing-secret-value")
    monkeypatch.delenv("BANKING_TOOLS_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    out = config.redact("error mentioned routing-secret-value in the body")
    assert "routing-secret-value" not in out
    assert "<redacted>" in out


def test_redact_leaves_short_values_alone(monkeypatch):
    # A short header value (< _REDACT_MIN_LEN) must not blank out benign text that contains it.
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", '{"x-env": "prod"}')
    monkeypatch.delenv("BANKING_TOOLS_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert config.redact("deploying to prod now") == "deploying to prod now"

