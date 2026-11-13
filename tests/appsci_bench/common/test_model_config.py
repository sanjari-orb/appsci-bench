"""Tests for the shared model config + LiteLLM call wrapper. No network -- litellm is mocked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from appsci_bench.common.model_config import (
    ModelConfig,
    ModelConfigError,
    complete,
    config_for,
    parse_custom_headers,
)


def _resp(content):
    """Build a minimal object shaped like a LiteLLM/OpenAI completion response."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TestModelConfigFromEnv:
    """from_env is provider-neutral: model + optional api_key + optional base_url, no validation."""

    def test_defaults(self) -> None:
        # Empty env -> default model, no explicit key/url (LiteLLM resolves the provider key itself).
        cfg = ModelConfig.from_env({})
        assert cfg.model == "anthropic/claude-opus-4-8"
        assert cfg.api_key is None
        assert cfg.base_url is None

    def test_barebone_first_party_leaves_key_to_litellm(self) -> None:
        # A first-party setup sets ANTHROPIC_API_KEY (read by LiteLLM), NOT MODEL_API_KEY.
        cfg = ModelConfig.from_env({"ANTHROPIC_API_KEY": "sk-ant"})
        assert cfg.api_key is None  # we don't forward it; LiteLLM picks it up from the env
        assert cfg.base_url is None

    def test_explicit_model_and_key_and_base_url(self) -> None:
        cfg = ModelConfig.from_env({
            "MODEL_NAME": "openai/gpt-5.5",
            "MODEL_API_KEY": "tok-123",
            "MODEL_BASE_URL": "https://gw/v1",
        })
        assert cfg.model == "openai/gpt-5.5"
        assert cfg.api_key == "tok-123"
        assert cfg.base_url == "https://gw/v1"

    def test_gateway_or_cortex_via_generic_vars(self) -> None:
        # gateway/any proxy is just a base_url + bearer token -- no provider-specific code path.
        cfg = ModelConfig.from_env({
            "MODEL_API_KEY": "gateway-jwt",
            "MODEL_BASE_URL": "https://gateway.example.com",
        })
        assert cfg.api_key == "gateway-jwt"
        assert cfg.base_url == "https://gateway.example.com"

    def test_blank_values_normalize_to_none(self) -> None:
        cfg = ModelConfig.from_env({"MODEL_NAME": "  ", "MODEL_API_KEY": "", "MODEL_BASE_URL": "  "})
        assert cfg.model == "anthropic/claude-opus-4-8"  # blank model falls back to default
        assert cfg.api_key is None
        assert cfg.base_url is None

    def test_custom_headers_parsed_from_env(self) -> None:
        cfg = ModelConfig.from_env({
            "MODEL_CUSTOM_HEADERS": "X-Gateway-User: someone@example.com\nX-Client: 1.0.33",
        })
        assert cfg.custom_headers == {
            "X-Gateway-User": "someone@example.com",
            "X-Client": "1.0.33",
        }

    def test_no_custom_headers_var_leaves_none(self) -> None:
        # None (not {}) so `complete` can omit the litellm parameter entirely.
        assert ModelConfig.from_env({}).custom_headers is None

    def test_unparseable_custom_headers_leave_none(self) -> None:
        assert ModelConfig.from_env({"MODEL_CUSTOM_HEADERS": "garbage"}).custom_headers is None


class TestParseCustomHeaders:
    """The gateway header block (gateway's ANTHROPIC_CUSTOM_HEADERS) is parsed defensively: a
    malformed value must degrade to "no extra headers", never raise mid-run."""

    def test_multiple_lines(self) -> None:
        raw = "X-Gateway-User: someone@example.com\nX-Gateway-Client: 1.0.33-2606222233-94c1f6d"
        assert parse_custom_headers(raw) == {
            "X-Gateway-User": "someone@example.com",
            "X-Gateway-Client": "1.0.33-2606222233-94c1f6d",
        }

    def test_single_line(self) -> None:
        assert parse_custom_headers("X-Gateway-User: a@b.com") == {"X-Gateway-User": "a@b.com"}

    def test_splits_on_first_colon_only(self) -> None:
        # A value containing colons (a URL, a timestamp) must survive intact.
        assert parse_custom_headers("X-Origin: https://gw:8443/v1") == {
            "X-Origin": "https://gw:8443/v1",
        }

    def test_whitespace_is_stripped(self) -> None:
        assert parse_custom_headers("  X-A :  v  \n\n  X-B:v2  ") == {"X-A": "v", "X-B": "v2"}

    def test_line_without_colon_is_ignored(self) -> None:
        assert parse_custom_headers("nonsense\nX-A: v") == {"X-A": "v"}

    def test_malformed_only_yields_no_headers(self) -> None:
        assert parse_custom_headers("nonsense") == {}

    def test_blank_and_none(self) -> None:
        assert parse_custom_headers("") == {}
        assert parse_custom_headers("   \n  ") == {}
        assert parse_custom_headers(None) == {}

    def test_empty_value_is_kept_but_empty_name_is_not(self) -> None:
        assert parse_custom_headers("X-A:") == {"X-A": ""}
        assert parse_custom_headers(": v") == {}

    @pytest.mark.parametrize(
        "raw",
        [
            "Bad Name: v",          # a space -- the gateway var pasted with a stray word in it
            "X-A\tB: v",            # a tab
            "X-(A): v",             # a delimiter character, illegal in an HTTP token
            "X-Ünicode: v",         # non-ASCII, e.g. copied out of a wiki page
            "X-A;B: v",
            '"X-A": v',
        ],
    )
    def test_a_name_http_cannot_send_is_dropped(self, raw: str) -> None:
        """The docstring's promise is about the LLM CALL, not about this function returning.

        A name like `Bad Name` used to parse cleanly and then die in h11 when the request was
        serialised -- so every model call in an unattended run failed, which is precisely the outcome
        "degrade to no extra headers" exists to prevent. Dropping it here is what keeps the promise.
        """
        assert parse_custom_headers(raw) == {}
        assert parse_custom_headers(f"{raw}\nX-Good: keep") == {"X-Good": "keep"}

    def test_a_value_http_cannot_send_is_dropped(self) -> None:
        assert parse_custom_headers("X-A: bad\x00value\nX-Good: keep") == {"X-Good": "keep"}

    def test_everything_returned_is_actually_sendable(self) -> None:
        """Pins the rule to the layer that enforces it, so it can't drift into folklore."""
        h11 = pytest.importorskip("h11")
        raw = (
            "X-Gateway-User: someone@example.com\nBad Name: v\nX-Origin: https://gw:8443/v1\n"
            "X-Ünicode: v\nX-A: nul\x00here\nX-Client: 1.0.33-2606222233-94c1f6d\n"
        )
        parsed = parse_custom_headers(raw)
        assert parsed  # the good headers survived
        h11.Request(
            method="GET",
            target="/",
            headers=[(b"host", b"gw")] + [
                (k.encode("ascii"), v.encode("ascii")) for k, v in parsed.items()
            ],
        )


class TestConfigFor:
    """Per-role model var over the shared MODEL_* credentials (JUDGE_MODEL, APPSCI_USERSIM_MODEL)."""

    _GW = {"MODEL_NAME": "anthropic/claude-opus-4-8", "MODEL_API_KEY": "gw-key",
           "MODEL_BASE_URL": "https://gw/v1"}

    def test_role_var_overrides_the_model(self) -> None:
        cfg = config_for("JUDGE_MODEL", {**self._GW, "JUDGE_MODEL": "anthropic/claude-sonnet-4-5"})
        assert cfg.model == "anthropic/claude-sonnet-4-5"

    def test_credentials_are_the_shared_ones(self) -> None:
        # Deliberate: both roles on this helper are Claude-on-the-same-gateway, so a per-role
        # credential pair would be two more things to keep in sync for no gain.
        cfg = config_for("JUDGE_MODEL", {**self._GW, "JUDGE_MODEL": "anthropic/claude-sonnet-4-5"})
        assert cfg.api_key == "gw-key"
        assert cfg.base_url == "https://gw/v1"

    def test_falls_back_to_model_name_when_role_var_unset(self) -> None:
        cfg = config_for("JUDGE_MODEL", self._GW)
        assert cfg.model == "anthropic/claude-opus-4-8"

    def test_blank_role_var_is_treated_as_unset(self) -> None:
        cfg = config_for("JUDGE_MODEL", {**self._GW, "JUDGE_MODEL": "   "})
        assert cfg.model == "anthropic/claude-opus-4-8"

    def test_roles_are_independent_of_each_other(self) -> None:
        env = {**self._GW, "JUDGE_MODEL": "anthropic/claude-sonnet-4-5",
               "APPSCI_USERSIM_MODEL": "anthropic/claude-haiku-4-5-20251001"}
        assert config_for("JUDGE_MODEL", env).model == "anthropic/claude-sonnet-4-5"
        assert config_for("APPSCI_USERSIM_MODEL", env).model == "anthropic/claude-haiku-4-5-20251001"
        # ...and neither leaks into the shared default.
        assert ModelConfig.from_env(env).model == "anthropic/claude-opus-4-8"

    def test_default_model_applies_with_no_env_at_all(self) -> None:
        cfg = config_for("JUDGE_MODEL", {})
        assert cfg.model == "anthropic/claude-opus-4-8"
        assert cfg.api_key is None and cfg.base_url is None


class TestComplete:
    _CFG = ModelConfig(model="anthropic/claude-opus-4-8")

    def test_returns_message_content(self) -> None:
        with patch("appsci_bench.common.model_config.litellm.completion",
                   return_value=_resp("hello from the model")) as mock:
            out = complete([{"role": "user", "content": "hi"}], self._CFG)
        assert out == "hello from the model"
        mock.assert_called_once()

    def test_forwards_expected_kwargs(self) -> None:
        with patch("appsci_bench.common.model_config.litellm.completion",
                   return_value=_resp("ok")) as mock:
            complete([{"role": "user", "content": "hi"}], self._CFG)
        kwargs = mock.call_args.kwargs
        assert kwargs["model"] == "anthropic/claude-opus-4-8"
        assert kwargs["num_retries"] == 3
        # temperature is NOT sent -- left at the provider default (some models reject other values).
        assert "temperature" not in kwargs
        # api_key/base_url omitted entirely when None (let LiteLLM resolve them for first-party).
        assert "api_key" not in kwargs
        assert "base_url" not in kwargs
        # Same for gateway headers: the parameter is absent, not an empty dict, so a non-gateway
        # request is byte-identical to what it was before extra_headers existed.
        assert "extra_headers" not in kwargs

    def test_custom_headers_forwarded_when_set(self) -> None:
        cfg = ModelConfig(model="anthropic/claude-opus-4-8",
                          custom_headers={"X-Gateway-User": "a@b.com"})
        with patch("appsci_bench.common.model_config.litellm.completion",
                   return_value=_resp("ok")) as mock:
            complete([{"role": "user", "content": "hi"}], cfg)
        assert mock.call_args.kwargs["extra_headers"] == {"X-Gateway-User": "a@b.com"}

    def test_empty_custom_headers_are_not_forwarded(self) -> None:
        cfg = ModelConfig(model="anthropic/claude-opus-4-8", custom_headers={})
        with patch("appsci_bench.common.model_config.litellm.completion",
                   return_value=_resp("ok")) as mock:
            complete([{"role": "user", "content": "hi"}], cfg)
        assert "extra_headers" not in mock.call_args.kwargs

    def test_api_key_forwarded_when_set(self) -> None:
        cfg = ModelConfig(model="anthropic/claude-opus-4-8", api_key="tok-123")
        with patch("appsci_bench.common.model_config.litellm.completion",
                   return_value=_resp("ok")) as mock:
            complete([{"role": "user", "content": "hi"}], cfg)
        assert mock.call_args.kwargs["api_key"] == "tok-123"

    def test_base_url_forwarded_when_set(self) -> None:
        cfg = ModelConfig(model="openai/gpt-4o", api_key="k", base_url="https://gw/v1")
        with patch("appsci_bench.common.model_config.litellm.completion",
                   return_value=_resp("ok")) as mock:
            complete([{"role": "user", "content": "hi"}], cfg)
        assert mock.call_args.kwargs["base_url"] == "https://gw/v1"

    def test_json_object_hint_forwarded(self) -> None:
        with patch("appsci_bench.common.model_config.litellm.completion",
                   return_value=_resp("ok")) as mock:
            complete([{"role": "user", "content": "hi"}], self._CFG, json_object=True)
        assert mock.call_args.kwargs["response_format"] == {"type": "json_object"}

    def test_empty_content_raises(self) -> None:
        with patch("appsci_bench.common.model_config.litellm.completion", return_value=_resp("")):
            with pytest.raises(ModelConfigError, match="empty content"):
                complete([{"role": "user", "content": "hi"}], self._CFG)

    def test_malformed_response_raises(self) -> None:
        with patch("appsci_bench.common.model_config.litellm.completion",
                   return_value=SimpleNamespace(choices=[])):
            with pytest.raises(ModelConfigError, match="unexpected LiteLLM response shape"):
                complete([{"role": "user", "content": "hi"}], self._CFG)
