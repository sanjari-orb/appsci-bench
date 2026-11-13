"""Shared airline artifact-agent core: a tau2-`LLMAgent`-style tool-calling loop over the 14 airline
tools, following the airline policy. Used by BOTH the in-process artifact (tau2_style_agent.py) and
the locally-hosted FastAPI server (local_server/server.py) so there is one airline-agent
implementation. It acts on the task-environment via the airline tool-API over HTTP
(AIRLINE_TOOLS_URL / AIRLINE_TOOLS_TOKEN). Claude-only (the LLM client comes from appsci_agent).

NOTE (re: the SYSTEM prompt below): this is the *reference / local stand-in* artifact, NOT something
injected into the AppSci agent at build time. The AppSci agent authors its OWN system prompt while
building its artifact (verified in the e2e: it wrote its own ROLE/CRITICAL_RULES from scratch). This
module exists only as (a) a gold reference and (b) a hosted stand-in for the eventual 3-tier artifact
container -- it is never shared with, or seen by, the agent under test.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

from appsci_bench.core.spec import REPO_ROOT

_log = logging.getLogger("airline_artifact")
TASK_ID = "airline_rebooking"
# The policy + tool reference live with the user-sim's documents: they are what the stakeholder holds
# and the agent under test has to elicit (#100). This reference implementation reads them directly
# because it stands in for an agent that already did that elicitation.
_USERSIM_DOCS = REPO_ROOT / "tasks" / TASK_ID / "user_sim_docs" / "common"
_POLICY_PATH = _USERSIM_DOCS / "policy.md"
_TOOLS_REF = _USERSIM_DOCS / "tools_reference.json"
MAX_STEPS = 12
LLM_TIMEOUT_S = 120      # hard per-call timeout so a hung gateway request fails fast (not forever)

# Reference system prompt for the stand-in artifact only -- the AppSci agent writes its own (see the
# module note above); this is not handed to the agent under test.
SYSTEM = (
    "You are a customer-service airline agent. Help the user strictly according to the policy below. "
    "Use the provided tools to read and modify reservations; obtain explicit user confirmation before "
    "any write. If a request is not allowed by policy, refuse or transfer. When done, reply to the "
    "user with the outcome.\n\n<policy>\n{policy}\n</policy>"
)


def system_prompt() -> str:
    return SYSTEM.format(policy=_POLICY_PATH.read_text())


def tool_schemas() -> list[dict]:
    """The 14 airline tools as OpenAI function schemas (drop our `tool_type` extension)."""
    tools = json.loads(_TOOLS_REF.read_text())
    return [{"type": "function", "function": t["function"]} for t in tools]


def build_client():
    """The Claude LLM client (appsci_agent AgentConfig -> gateway/anthropic)."""
    from appsci_agent.core.llm_client import LLMClient
    from appsci_agent.core.config import AgentConfig
    return LLMClient(AgentConfig.from_env())


def call_tool(name: str, arguments: dict) -> str:
    """Invoke one airline tool over the task-env tool-API; returns the result text (or an ERROR string)."""
    url = os.environ.get("AIRLINE_TOOLS_URL")
    token = os.environ.get("AIRLINE_TOOLS_TOKEN")
    if not url:
        return "ERROR: no tool endpoint bound"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.post(f"{url}/{name}", json={"arguments": arguments or {}}, headers=headers, timeout=20.0)
        if r.status_code >= 400:
            return f"ERROR: {r.json().get('detail', r.text)}"
        return json.dumps(r.json().get("result"), default=str)[:4000]
    except httpx.HTTPError as e:
        return f"ERROR: {e!r}"


def run_chat(client, messages: list[dict], schemas: list[dict] | None = None,
             *, collect_trace: bool = False):
    """Run the bounded tool-calling loop over `messages` (must already include the system prompt);
    returns the final assistant text. With `collect_trace=True` returns `(text, trace)`, where trace
    is a list of step dicts (assistant reasoning + tool_calls, tool_results, final) for the eval
    viewer to render -- so users can debug the agent's internal decisions."""
    schemas = schemas if schemas is not None else tool_schemas()
    last_text = ""
    trace: list | None = [] if collect_trace else None
    if collect_trace:
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        if sys_msg:
            trace.append({"kind": "system", "content": sys_msg.get("content", "")})
    for step in range(1, MAX_STEPS + 1):
        _log.info("  LLM call (step %d/%d, model=%s) ...", step, MAX_STEPS, getattr(client, "model", "?"))
        resp = client.chat(messages=messages, tools=schemas, timeout=LLM_TIMEOUT_S)
        if resp.tool_calls:
            _log.info("  -> tool calls: %s", ", ".join(tc.name for tc in resp.tool_calls))
            if collect_trace:
                trace.append({"kind": "assistant", "step": step, "content": resp.content or "",
                              "tool_calls": [{"name": tc.name, "arguments": tc.arguments}
                                             for tc in resp.tool_calls]})
            messages.append(client.format_assistant_message_with_tool_calls(
                content=resp.content, tool_calls=resp.tool_calls))
            for tc in resp.tool_calls:
                result = call_tool(tc.name, tc.arguments)
                messages.append(client.format_tool_result(tool_call_id=tc.id, result=result))
                if collect_trace:
                    trace.append({"kind": "tool_result", "step": step, "name": tc.name,
                                  "result": result})
            continue
        last_text = resp.content or last_text
        _log.info("  -> final reply (%d chars)", len(last_text))
        if collect_trace:
            trace.append({"kind": "final", "step": step, "content": last_text})
        break
    return (last_text, trace) if collect_trace else last_text


def respond_to_task(client, intended_task: str, history: list[dict] | None = None,
                    schemas: list[dict] | None = None) -> str:
    """Convenience: build [system, *history, user] and run the loop -> final assistant text."""
    messages = [{"role": "system", "content": system_prompt()}, *(history or []),
                {"role": "user", "content": intended_task}]
    return run_chat(client, messages, schemas)
