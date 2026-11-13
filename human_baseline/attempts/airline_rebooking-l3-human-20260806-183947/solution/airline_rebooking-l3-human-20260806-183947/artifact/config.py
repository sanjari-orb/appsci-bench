"""Runtime configuration, read from the environment with verified defaults.

The defaults are the values confirmed to work against the task environment container
(DNS alias `workspace` on the `bench-network` docker network), so the agent runs without
any extra configuration. Every value can be overridden by an environment variable.

ANTHROPIC_API_KEY has no default on purpose: a missing key is a startup error, not a
silent fallback that fails later mid-conversation.
"""
from __future__ import annotations

import os

# --- reference clock -------------------------------------------------------------------------
# The task environment's "current time" is fixed at this instant (policy.md); every
# time-relative rule (the 24h cancellation window, whether a flight has already flown) is
# judged against it. Single source of truth: policy.py derives its NOW from this, and
# prompt.py quotes it in the system prompt, so the three cannot drift apart.
REFERENCE_TIME = "2024-05-15T15:00:00"
REFERENCE_TIME_DISPLAY = "2024-05-15 15:00:00 EST"

# --- task environment (the airline tool API the agent acts against) -------------------------
ENV_BASE_URL = os.environ.get("ENV_BASE_URL", "http://workspace:8080")
ENV_TOOL_TOKEN = os.environ.get("ENV_TOOL_TOKEN", "airline-tool-token-local")
ENV_TIMEOUT_SECONDS = float(os.environ.get("ENV_TIMEOUT_SECONDS", "20"))

# --- LLM ------------------------------------------------------------------------------------
MODEL = os.environ.get("MODEL", "claude-sonnet-4-5")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "2048"))

# Maximum internal tool calls per /respond turn. The loop is only allowed to exit by
# producing user-facing text, so this bounds a model that keeps calling tools forever.
MAX_TOOL_ITERS = int(os.environ.get("MAX_TOOL_ITERS", "20"))

# Maximum customer turns in one conversation, counted from the incoming transcript. Distinct from
# MAX_TOOL_ITERS: this bounds how long a single conversation may run overall, not how many tool
# calls happen within one turn.
MAX_USER_TURNS = int(os.environ.get("MAX_USER_TURNS", "10"))

# If the same policy violation fires this many times in one turn, stop looping and return a
# conservative refusal rather than letting a stubborn model burn the whole iteration budget.
MAX_REPEATED_VIOLATIONS = int(os.environ.get("MAX_REPEATED_VIOLATIONS", "3"))

# --- policy switches ------------------------------------------------------------------------
# Cancellation via the insurance branch requires the reason to be health/weather -- a fact only
# the customer can state and no tool can verify. False (the chosen default) means the agent
# transfers to a human instead of acting on an uncorroborated reason; True means it takes the
# customer at their word. Flip after observing real traffic. See policy.py::check_cancel.
TRUST_STATED_REASON = os.environ.get("TRUST_STATED_REASON", "false").lower() == "true"


def anthropic_api_key() -> str:
    """Return the Anthropic API key, raising a clear error if it is not configured."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. The agent cannot run its reasoning loop without it."
        )
    return key
