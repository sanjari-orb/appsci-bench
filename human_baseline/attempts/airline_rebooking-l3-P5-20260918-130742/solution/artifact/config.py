"""Runtime configuration: environment endpoint, LLM credentials, and the fixed clock.

Everything here is read from the process environment so the artifact can be deployed without a code
change. Defaults target the local bench setup (`airline-workspace-1` on `bench-network`), which is the
only way the task environment is reachable -- its port is not published to the host.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# The policy's fixed "current time". Every time-based rule (the 24-hour cancellation window, whether a
# flight has already been flown) is judged against this, never against real-world now.
NOW_ISO = "2024-05-15T15:00:00"

# Default env endpoint. The task environment listens on 8080 inside its container and is NOT published
# to the host, so it is addressed by container name over the shared docker network.
DEFAULT_ENV_URL = "http://airline-workspace-1:8080"
DEFAULT_ENV_TOKEN = "airline-tool-token-local"

DEFAULT_MODEL = "claude-sonnet-5"


def _first_env(*names: str) -> str | None:
    """Return the first non-empty value among `names`, else None."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class Config:
    env_url: str
    env_token: str
    model: str
    anthropic_api_key: str | None
    anthropic_base_url: str | None
    max_tool_steps: int
    log_level: str

    @property
    def llm_available(self) -> bool:
        return bool(self.anthropic_api_key)


def load_config() -> Config:
    """Build the config from the environment.

    The LLM key is looked up under several names because the deploying harness's exact variable name
    is not guaranteed; a missing key is not fatal here -- `/health` must still pass, and the failure is
    logged loudly by the caller.
    """
    return Config(
        env_url=(_first_env("AIRLINE_ENV_URL", "AIRLINE_ENV_BASE_URL") or DEFAULT_ENV_URL).rstrip("/"),
        env_token=_first_env("AIRLINE_ENV_TOKEN") or DEFAULT_ENV_TOKEN,
        model=_first_env("AGENT_MODEL", "ANTHROPIC_MODEL", "MODEL") or DEFAULT_MODEL,
        anthropic_api_key=_first_env(
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY", "LLM_API_KEY"
        ),
        anthropic_base_url=_first_env("ANTHROPIC_BASE_URL", "LLM_BASE_URL"),
        max_tool_steps=int(_first_env("AGENT_MAX_TOOL_STEPS") or 24),
        log_level=_first_env("LOG_LEVEL") or "INFO",
    )
