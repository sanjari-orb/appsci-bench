"""Runtime configuration for the Rho-Bank agent artifact.

Reads configuration from the environment set by the deployer. Deliberately does *not* hard-code any
provider, model id, endpoint, or credential var name: the deployer forwards whatever LLM credential
env vars are available, and `dev_deploy` merges task-specific agent env. We detect what is present at
runtime rather than assuming a fixed name.

Rules honored here:
  * Credential *values* are never returned by the introspection helpers and never logged. Only the
    *names* of present env vars are exposed, so a diagnostic can report which credentials the
    deployer forwarded without leaking secrets.
  * Missing required config raises an explicit error instead of falling back to a silent default that
    would fabricate behavior.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

# Env var names that carry the banking tool API coordinates (documented in TASK_BRIEF.yaml).
BANKING_TOOLS_URL_VAR = "BANKING_TOOLS_URL"
BANKING_TOOLS_TOKEN_VAR = "BANKING_TOOLS_TOKEN"

# Anthropic Messages runtime coordinates (confirmed present by P3 in the build environment).
ANTHROPIC_BASE_URL_VAR = "ANTHROPIC_BASE_URL"
ANTHROPIC_AUTH_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
ANTHROPIC_CUSTOM_HEADERS_VAR = "APPSCI_AGENT_CUSTOM_HEADERS"

# The deployed-agent model id, chosen explicitly by P3 (claude-opus-4-8 via Anthropic Messages).
# It is a NON-SECRET configuration value, not a guessed default: no model-name variable is injected at
# runtime, so this explicit id is the artifact default. `agent_model()` still honors an injected
# override first, so we never rely solely on an unprovided env var.
DEFAULT_AGENT_MODEL = "claude-opus-4-8"

# Optional env vars that, if the deployer ever injects one, override the model id (checked in order).
_AGENT_MODEL_VARS = ("AGENT_MODEL", "MODEL_NAME", "ANTHROPIC_MODEL")

# Default port the scaffold Dockerfile/deployer conventionally publishes when $PORT is unset. The
# deployer normally injects $PORT; we only fall back so a local import/smoke test does not crash.
DEFAULT_PORT = 8080

# Substrings that, in an env var *name*, suggest it carries an LLM/embeddings credential or endpoint.
# Used ONLY to report present var *names* (never values) so P3 can confirm what the deployer
# forwards. This is intentionally broad and provider-agnostic; it hard-codes no single provider.
_CREDENTIAL_NAME_PATTERNS = [
    r"API_KEY",
    r"_KEY$",
    r"_TOKEN$",
    r"SECRET",
    r"ANTHROPIC",
    r"OPENAI",
    r"AZURE_OPENAI",
    r"GOOGLE",
    r"GEMINI",
    r"VERTEX",
    r"COHERE",
    r"MISTRAL",
    r"BEDROCK",
    r"AWS_",
    r"HUGGINGFACE",
    r"HF_",
    r"EMBED",  # embeddings key for KB_search_dense
    r"LLM",
    r"MODEL",
    r"BASE_URL",
    r"API_BASE",
    r"ENDPOINT",
]

_CREDENTIAL_NAME_RE = re.compile("|".join(_CREDENTIAL_NAME_PATTERNS), re.IGNORECASE)

# Names we must never echo even as a "present name" would be misleading — but note we still only ever
# expose names, never values, so no denylist of values is needed here.


def banking_tools_url() -> Optional[str]:
    """Return the banking tool API base URL, or None if not set in the environment."""
    value = os.environ.get(BANKING_TOOLS_URL_VAR)
    if value is None:
        return None
    return value.rstrip("/")


def banking_tools_token() -> Optional[str]:
    """Return the banking tool API bearer token, or None if not set.

    The returned value is a secret: callers must use it only as an Authorization header and must
    never log it.
    """
    return os.environ.get(BANKING_TOOLS_TOKEN_VAR)


def require_banking_tools() -> tuple[str, str]:
    """Return (url, token) for the banking tool API, raising if either is missing.

    Used by code paths that genuinely need to reach the tools. We raise instead of defaulting so a
    misconfiguration surfaces as an honest error rather than silent, fabricated behavior.
    """
    url = banking_tools_url()
    token = banking_tools_token()
    missing = []
    if not url:
        missing.append(BANKING_TOOLS_URL_VAR)
    if not token:
        missing.append(BANKING_TOOLS_TOKEN_VAR)
    if missing:
        raise RuntimeError(
            "Missing required banking tool API configuration: "
            + ", ".join(missing)
            + ". The deployer is expected to inject these; not set in this environment."
        )
    return url, token  # type: ignore[return-value]


def port() -> int:
    """Return the port to bind, from $PORT, defaulting only for local smoke tests."""
    raw = os.environ.get("PORT")
    if raw is None or raw == "":
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"PORT is set but not an integer: {raw!r}") from exc


def present_credential_env_names() -> List[str]:
    """Return the sorted NAMES of env vars that look like LLM/embeddings credentials or endpoints.

    Names only — never values. This lets a diagnostic report which credentials the deployer forwarded
    into the container so P3 can confirm the real provider/model/endpoint, without ever exposing a
    secret. The banking tool API vars are reported separately (see `banking_env_presence`).
    """
    names = [
        name
        for name in os.environ
        if _CREDENTIAL_NAME_RE.search(name)
        and name not in (BANKING_TOOLS_URL_VAR, BANKING_TOOLS_TOKEN_VAR)
    ]
    return sorted(names)


def banking_env_presence() -> dict:
    """Report whether the banking tool API vars are present (booleans only, never values)."""
    return {
        BANKING_TOOLS_URL_VAR: banking_tools_url() is not None,
        BANKING_TOOLS_TOKEN_VAR: banking_tools_token() is not None,
    }


def agent_model() -> str:
    """Return the deployed-agent model id: an injected override if present, else the chosen default.

    The default is the explicit, non-secret id P3 chose (claude-opus-4-8). Honoring an override
    first means we do not depend solely on an unprovided env var.
    """
    for var in _AGENT_MODEL_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return DEFAULT_AGENT_MODEL


def agent_model_source() -> str:
    """Report where the model id came from (an env var name, or the explicit default) — no secrets."""
    for var in _AGENT_MODEL_VARS:
        if os.environ.get(var):
            return var
    return f"default:{DEFAULT_AGENT_MODEL}"


def anthropic_base_url() -> Optional[str]:
    """Return the Anthropic Messages base URL (trailing slash stripped), or None if unset."""
    value = os.environ.get(ANTHROPIC_BASE_URL_VAR)
    if not value:
        return None
    return value.rstrip("/")


def anthropic_auth_token() -> Optional[str]:
    """Return the Anthropic auth token (a secret; use only as an Authorization header, never log)."""
    return os.environ.get(ANTHROPIC_AUTH_TOKEN_VAR) or None


def _parse_custom_headers(raw: str) -> Tuple[Dict[str, str], Optional[str]]:
    """Parse APPSCI_AGENT_CUSTOM_HEADERS into (headers, error).

    Accepts either the raw HTTP header-line form -- one or more ``Name: value`` lines, newline
    separated (the actual runtime format) -- or a JSON object of name/value pairs (retained for
    convenience). When the value is present but cannot be fully parsed, returns whatever was parsed
    plus a NON-SECRET error string (header *names* or counts only, never values) so the caller can
    surface the problem instead of silently dropping a present value.
    """
    text = raw.strip()
    if not text:
        return {}, None

    # JSON form: only when it clearly looks like JSON (starts with '{' or '[').
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            return {}, "APPSCI_AGENT_CUSTOM_HEADERS looks like JSON but is not valid JSON."
        if not isinstance(data, dict):
            return {}, "APPSCI_AGENT_CUSTOM_HEADERS JSON must be an object of header name/value pairs."
        return {str(k): str(v) for k, v in data.items()}, None

    # Raw header-line form: one or more "Name: value" lines.
    headers: Dict[str, str] = {}
    bad_lines = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" not in stripped:
            bad_lines += 1
            continue
        name, value = stripped.split(":", 1)
        name = name.strip()
        if not name:
            bad_lines += 1
            continue
        headers[name] = value.strip()

    if bad_lines:
        # Report only the count -- never the offending name/value text.
        return headers, (
            f"APPSCI_AGENT_CUSTOM_HEADERS had {bad_lines} line(s) not in 'Name: value' form."
        )
    if not headers:
        return {}, "APPSCI_AGENT_CUSTOM_HEADERS is set but contained no 'Name: value' header."
    return headers, None


def anthropic_custom_headers() -> Dict[str, str]:
    """Parse APPSCI_AGENT_CUSTOM_HEADERS into a header dict (best effort).

    These are proxy/routing headers whose values are secret-ish; callers attach them to requests but
    must never print the values. A malformed value yields the best-effort parse here; use
    `anthropic_custom_headers_error()` to detect (and surface) a present-but-malformed value rather
    than treating an empty dict as "no headers configured".
    """
    raw = os.environ.get(ANTHROPIC_CUSTOM_HEADERS_VAR)
    if not raw:
        return {}
    headers, _ = _parse_custom_headers(raw)
    return headers


def anthropic_custom_headers_error() -> Optional[str]:
    """Return a NON-SECRET description if APPSCI_AGENT_CUSTOM_HEADERS is present but malformed, else None.

    Lets a diagnostic report a present-but-unparseable header value instead of silently dropping it.
    The message never contains a header name's value.
    """
    raw = os.environ.get(ANTHROPIC_CUSTOM_HEADERS_VAR)
    if not raw:
        return None
    _, error = _parse_custom_headers(raw)
    return error


def require_anthropic() -> Tuple[str, str]:
    """Return (base_url, auth_token) for the Anthropic Messages API, raising if either is missing.

    Custom headers are optional and are not required here. We raise instead of defaulting so a
    misconfiguration surfaces as an honest error.
    """
    base = anthropic_base_url()
    token = anthropic_auth_token()
    missing = []
    if not base:
        missing.append(ANTHROPIC_BASE_URL_VAR)
    if not token:
        missing.append(ANTHROPIC_AUTH_TOKEN_VAR)
    if missing:
        raise RuntimeError(
            "Missing required Anthropic runtime configuration: "
            + ", ".join(missing)
            + ". The deployer is expected to inject these; not set in this environment."
        )
    return base, token  # type: ignore[return-value]


def anthropic_env_presence() -> Dict[str, bool]:
    """Report whether the Anthropic runtime vars are present (booleans only, never values)."""
    return {
        ANTHROPIC_BASE_URL_VAR: bool(os.environ.get(ANTHROPIC_BASE_URL_VAR)),
        ANTHROPIC_AUTH_TOKEN_VAR: bool(os.environ.get(ANTHROPIC_AUTH_TOKEN_VAR)),
        ANTHROPIC_CUSTOM_HEADERS_VAR: bool(os.environ.get(ANTHROPIC_CUSTOM_HEADERS_VAR)),
    }


# Only redact values at least this long, so short/common header values cannot blank out benign text.
_REDACT_MIN_LEN = 6


def _secret_values() -> List[str]:
    """Collect the secret STRINGS currently in the environment, for defense-in-depth redaction."""
    values: List[str] = []
    for token in (banking_tools_token(), anthropic_auth_token()):
        if token:
            values.append(token)
    # Custom-header values. We add the parsed values AND defensive fallbacks so that a value which is
    # present but could not be structured (malformed line, empty name, invalid JSON) is still scrubbed:
    #   * the whole raw env string, and
    #   * for the raw-line form, the text after the first ':' (the value portion), even when parsing
    #     dropped it because the name was empty.
    raw = os.environ.get(ANTHROPIC_CUSTOM_HEADERS_VAR)
    if raw:
        headers, _ = _parse_custom_headers(raw)
        values.extend(v for v in headers.values() if v)
        values.append(raw.strip())
        if raw.strip()[:1] not in "{[" and ":" in raw:
            remainder = raw.split(":", 1)[1].strip()
            if remainder:
                values.append(remainder)
    return [v for v in values if len(v) >= _REDACT_MIN_LEN]


def redact(text: str) -> str:
    """Best-effort redaction of known secret values if any appear in a string to be logged/printed.

    Covers the banking token, the Anthropic auth token, and custom-header values. Defense-in-depth
    only: secrets should never be placed in a loggable string in the first place.
    """
    if not text:
        return text
    for secret in _secret_values():
        if secret in text:
            text = text.replace(secret, "<redacted>")
    return text
