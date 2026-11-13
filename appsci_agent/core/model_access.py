"""How the artifact reaches a language model -- stated, so it never has to be discovered.

The artifact the agent builds is itself an agent, so it needs model access, and the harness already
provides it: the same credential environment variables the meta-agent runs with are injected into the
artifact's container when it is deployed. Nothing used to SAY so, which made reading the harness's own
source (its LLM client, its gateway wiring) the rational way to find out -- work that is not part of
the task, and that the sub-agent can no longer do anyway (/app is root-only; see
appsci_agent/tools/mini_swe_agent.py). This module is the replacement: the facts, in the prompt, for
both the meta-agent and its coding sub-agent.

Values are never rendered -- only variable NAMES -- so the brief can go into a prompt and a trace
without carrying a token with it.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover -- import cycle: config imports nothing from here
    from .config import AgentConfig

# Credential variables the harness injects into the artifact's container at deploy time. This MIRRORS
# `LLM_CREDENTIAL_ENV_VARS` in appsci_bench/orchestrator/deployer.py, which is the source of truth --
# duplicated rather than imported because the agent image deliberately does not ship `appsci_bench`
# (see appsci_agent/Dockerfile). Keep the two lists in sync; a name only present there would be a
# credential the artifact receives but is never told about, which is the gap this module exists to
# close.
ARTIFACT_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "APPSCI_AGENT_API_KEY",
    "APPSCI_AGENT_BASE_URL",
    "APPSCI_AGENT_CUSTOM_HEADERS",
    # Fireworks, when the agent under test is served from there. Both spellings LiteLLM accepts, since
    # the artifact needs whichever the operator set. Without these, a Fireworks run with no other
    # listed var set would tell the agent nothing about model access while its artifact does receive
    # the key -- exactly the gap this module exists to close.
    "FIREWORKS_API_KEY",
    "FIREWORKS_AI_API_KEY",
)


def present_credential_env_vars(env: dict[str, str] | None = None) -> list[str]:
    """Names of the credential variables actually set, in declared order.

    A name is reported only when its value is non-empty: an exported-but-blank variable is the same
    as absent to every client library, and listing it would send the artifact author after a
    credential that is not there.
    """
    source = os.environ if env is None else env
    return [name for name in ARTIFACT_CREDENTIAL_ENV_VARS if (source.get(name) or "").strip()]


def model_access_brief(config: "AgentConfig", env: dict[str, str] | None = None) -> str:
    """The model-access section for the agent's system prompt and its sub-agent's task brief.

    Returns "" when no credential variable is set at all -- there is then nothing true to say, and a
    section listing no way to reach a model would only invite the agent to go looking for one.
    """
    present = present_credential_env_vars(env)
    if not present:
        return ""

    source = os.environ if env is None else env
    lines = [
        "# Model Access",
        "",
        "Your artifact may call a language model. Credentials come from the environment: the same "
        "variables set here in the build environment are injected into your artifact's container "
        "when it is deployed. Read them at runtime with `os.environ`.",
        "",
        f"- **Model id available**: `{config.model}`",
        "- **Credential variables set here** (values not shown; the same set reaches your "
        "artifact): " + ", ".join(f"`{name}`" for name in present),
    ]

    if (source.get("APPSCI_AGENT_BASE_URL") or "").strip():
        lines.append(
            "- `APPSCI_AGENT_BASE_URL` is a company model gateway, with `APPSCI_AGENT_API_KEY` as "
            "its token. Point your client's base URL and key at that pair rather than at a "
            "provider's default endpoint."
        )
    if (source.get("APPSCI_AGENT_CUSTOM_HEADERS") or "").strip():
        lines.append(
            "- `APPSCI_AGENT_CUSTOM_HEADERS` holds `Name: Value` header lines, one per line, that "
            "the gateway requires for attribution. Parse it and send those headers with every "
            "model request; requests without them can be rejected."
        )

    lines += [
        "- Any client library is fine (for example `litellm`, `openai`, `anthropic`) as long as your "
        "artifact's `requirements.txt` declares it.",
        "- **Never hardcode a key, token, or endpoint** in your code, your `Dockerfile`, or a config "
        "file. A baked-in credential is a leak, and it will be wrong at deploy time.",
        "- The build environment has these same variables, so you can smoke-test your model client "
        "here before you deploy.",
    ]
    return "\n".join(lines)
