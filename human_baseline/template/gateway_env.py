"""Map gateway/ANTHROPIC_* host credentials onto the vars appsci_bench's eval subprocess needs.

Mirrors the setup steps in README.md's step 3 + OPENAI_* mapping, but as a pure function
over an env dict instead of shell exports -- so checkpoint.py/score_final.py can build a correct
subprocess env without requiring the human to `source` that script first in the same shell.
"""
from __future__ import annotations

import os

# Must equal artifact_agent_user_sim._NEAR_PARITY_ALIASES's sole member; a guard test asserts it.
# Duplicated rather than imported because this overlay ships inside the human-baseline template,
# which runs without appsci_bench importable.
_NEAR_PARITY_USERSIM_MODEL = "openai/openai-gpt-5.2"


def gateway_eval_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return env overlay vars derived from ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN, if present.

    Absent when those two aren't set (e.g. a first-party ANTHROPIC_API_KEY/OPENAI_API_KEY setup
    already works with no mapping) -- callers merge this over a copy of the host env so an
    explicitly-set var (e.g. a real OPENAI_API_KEY) always wins.
    """
    env = os.environ if env is None else env
    base_url = env.get("ANTHROPIC_BASE_URL")
    auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not base_url or not auth_token:
        return {}

    overlay = {
        "MODEL_NAME": env.get("MODEL_NAME", "anthropic/claude-opus-4-8"),
        "MODEL_BASE_URL": base_url,
        "MODEL_API_KEY": auth_token,
        "APPSCI_AGENT_BASE_URL": base_url,
        "APPSCI_AGENT_API_KEY": auth_token,
    }
    # The eval-time artifact-agent-user-sim is pinned to an openai/ model id (see
    # appsci_bench/eval/artifact_agent_user_sim.py) -- gateway serves it via an OpenAI-compatible
    # endpoint under its own alias, not the first-party id.
    if not env.get("OPENAI_API_KEY"):
        overlay["OPENAI_API_KEY"] = auth_token
        overlay["OPENAI_BASE_URL"] = f"{base_url}/v1"
        overlay["ARTIFACT_USERSIM_MODEL"] = env.get("ARTIFACT_USERSIM_MODEL", _NEAR_PARITY_USERSIM_MODEL)
    return overlay


def build_eval_subprocess_env() -> dict[str, str]:
    """Host env + the gateway overlay, ready to pass as subprocess.run(..., env=...)."""
    merged = dict(os.environ)
    merged.update(gateway_eval_env(merged))
    return merged
