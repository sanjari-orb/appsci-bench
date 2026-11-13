"""Environment resolution for the banking-support artifact.

Two independent endpoints are involved, reached through env vars injected into the artifact's
container at deploy time (never hardcoded -- see EXPLORATION.md §2.2, §8):

  * The **tool API** (banking + retrieval tools):  BANKING_TOOLS_URL / BANKING_TOOLS_TOKEN.
  * The **model endpoint** (Anthropic-compatible):  MODEL_BASE_URL / MODEL_API_KEY, with
    APPSCI_AGENT_* and ANTHROPIC_* accepted as fallbacks.

Resolution fails loudly (RuntimeError) when a required var is missing, so a misconfigured deploy
surfaces immediately rather than at the first tool call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Opus is the driver model per the approved plan; overridable via MODEL_NAME at deploy time.
DEFAULT_MODEL = "claude-opus-4-8"


def _first_env(*names: str) -> str | None:
    """Return the first non-empty value among the given env var names."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _require(*names: str) -> str:
    value = _first_env(*names)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable (tried: {', '.join(names)})"
        )
    return value


@dataclass(frozen=True)
class Config:
    """Resolved endpoints and credentials for one process."""

    tools_url: str
    tools_token: str
    model_base_url: str
    model_api_key: str
    model_name: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            tools_url=_require("BANKING_TOOLS_URL").rstrip("/"),
            tools_token=_require("BANKING_TOOLS_TOKEN"),
            model_base_url=_require(
                "MODEL_BASE_URL", "APPSCI_AGENT_BASE_URL", "ANTHROPIC_BASE_URL"
            ).rstrip("/"),
            model_api_key=_require(
                "MODEL_API_KEY", "APPSCI_AGENT_API_KEY", "ANTHROPIC_AUTH_TOKEN"
            ),
            model_name=_first_env("MODEL_NAME") or DEFAULT_MODEL,
        )
