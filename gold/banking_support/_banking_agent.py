"""Shared banking artifact-agent core: a tool-calling loop over the banking tool set.

The gold reference for `banking_support`, and the localhost stand-in for the artifact CONTAINER. It
acts on the task environment over the tool-API (BANKING_TOOLS_URL / BANKING_TOOLS_TOKEN), exactly the
path a submitted artifact uses -- no oracle access, no admin routes, no privileged shortcut. What it
demonstrates is the skill the task actually grades: WIRING the provided retrieval into a grounded,
identity-verified, correct transactional agent.

Why it is not an oracle replayer (airline's gold is one): banking cases are driven by a paraphrasing
user simulator over several turns, so an HTTP artifact cannot reliably recognise which case it is in.
Deterministic wiring validation is done separately, by a local smoke driver that replays a golden
trajectory through the live tool API and scores it -- so this module is free to be a real agent.

NOTE on the system prompt below: this is the REFERENCE artifact's own prompt, authored here. It is
never injected into, shared with, or seen by the AppSci agent under test -- that agent writes its own
while building its artifact. Nothing in this file is mounted into any container the agent can read.

Claude-only (the LLM client comes from appsci_agent).
"""
from __future__ import annotations

import json
import logging
import os

import httpx

_log = logging.getLogger("banking_artifact")

TASK_ID = "banking_support"
MAX_STEPS = 16          # retrieval-then-act needs more steps than airline's read-then-write
LLM_TIMEOUT_S = 120     # hard per-call ceiling so a hung gateway fails fast rather than forever
TOOL_TIMEOUT_S = 60     # a dense KB search can be slow on a cold embeddings cache

#: The reference artifact's own operating instructions. Deliberately states the POLICY SHAPE (ground
#: every claim, verify before acting, discover specialised tools through the KB, transfer when the KB
#: does not cover it) without hardcoding any specific rule, fee or eligibility threshold -- those must
#: come from retrieval at run time, which is the whole point of the task.
SYSTEM = (
    "You are a customer-service agent for Rho-Bank. Help the customer with accounts, cards, "
    "referrals, disputes and product questions.\n\n"
    "HOW TO WORK, in order:\n"
    "1. SEARCH BEFORE YOU SPEAK. The bank's rules, product terms, fees and eligibility live in a "
    "knowledge base you must search -- start with KB_search_bm25 (keyword) and use grep for exact "
    "strings. Never state a fee, rate, term, eligibility rule or capability you have not just read "
    "in a document. If the knowledge base does not cover the request, say so and transfer to a "
    "human -- do not guess, and do not invent a policy.\n"
    "2. THE DOCUMENTS NAME THE TOOLS. Handling a request often requires a specialised tool that is "
    "not in your base tool list. The knowledge base names it. Unlock it with "
    "unlock_discoverable_agent_tool, then call it with call_discoverable_agent_tool. If the tool "
    "belongs to the customer rather than to you, hand it over with give_discoverable_user_tool and "
    "tell them to use it.\n"
    "3. VERIFY BEFORE YOU TOUCH AN ACCOUNT. Identify the customer and record it with "
    "log_verification before any action that reads or changes their account data. The tools do NOT "
    "enforce this; you do.\n"
    "4. SOME ACTIONS ARE THE CUSTOMER'S. When the customer holds the tool (applying for a card, "
    "submitting a referral or a transaction), advise them clearly and let them act -- do not try to "
    "do it for them.\n"
    "5. Be concise and concrete. Quote the figures you retrieved. When you are done, reply to the "
    "customer with the outcome."
)


def system_prompt() -> str:
    return SYSTEM


def _tools_url() -> str | None:
    return os.environ.get("BANKING_TOOLS_URL")


def _headers() -> dict:
    token = os.environ.get("BANKING_TOOLS_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def tool_schemas() -> list[dict]:
    """The base agent tools as OpenAI function schemas, discovered from the LIVE tool API.

    Read from the environment rather than from a committed reference file, so the reference artifact
    cannot drift from the tools that actually exist. Falls back to a minimal hand-written set when no
    environment is bound (unit tests), so the loop stays testable creds-free and env-free.
    """
    url = _tools_url()
    if url:
        try:
            resp = httpx.get(f"{url}", headers=_headers(), timeout=20.0)
            if resp.status_code < 400:
                payload = resp.json()
                tools = payload.get("tools") if isinstance(payload, dict) else payload
                if tools:
                    return [{"type": "function", "function": t["function"] if "function" in t else t}
                            for t in tools]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            _log.info("tool API exposes no schema listing; using the built-in schema set")
    return _FALLBACK_SCHEMAS


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


_STR = {"type": "string"}

#: The tools this reference agent relies on. Names match the environment's toolkit; kept small on
#: purpose -- this is a reference, not an exhaustive binding of every tool the domain offers.
_FALLBACK_SCHEMAS = [
    _fn("KB_search_bm25", "Keyword-search the bank's knowledge base. Use this first, always.",
        {"query": _STR}, ["query"]),
    _fn("KB_search_dense", "Semantic search over the knowledge base (needs an embeddings key).",
        {"query": _STR}, ["query"]),
    _fn("grep", "Regex-search the knowledge base for an exact string.",
        {"pattern": _STR}, ["pattern"]),
    _fn("get_current_time", "The bank's current time.", {}, []),
    _fn("get_user_information_by_name", "Look a customer up by name.",
        {"name": _STR}, ["name"]),
    _fn("log_verification", "Record that you verified the customer's identity. Required before "
                          "any account action.",
        {"user_id": _STR, "verification_method": _STR}, ["user_id"]),
    _fn("unlock_discoverable_agent_tool", "Unlock a specialised tool the knowledge base named.",
        {"agent_tool_name": _STR}, ["agent_tool_name"]),
    _fn("call_discoverable_agent_tool", "Call a specialised tool you have unlocked.",
        {"agent_tool_name": _STR, "arguments": {"type": "object"}}, ["agent_tool_name"]),
    _fn("give_discoverable_user_tool", "Hand a specialised tool to the customer to use themselves.",
        {"user_tool_name": _STR}, ["user_tool_name"]),
    _fn("transfer_to_human_agents", "Hand off when the knowledge base does not cover the request.",
        {"summary": _STR, "reason": _STR}, ["summary"]),
]


def build_client():
    """The Claude LLM client (appsci_agent AgentConfig -> gateway/anthropic)."""
    from appsci_agent.core.config import AgentConfig
    from appsci_agent.core.llm_client import LLMClient
    return LLMClient(AgentConfig.from_env())


def call_tool(name: str, arguments: dict) -> str:
    """Invoke one tool over the task-env tool-API; returns result text (or an ERROR string).

    An error is RETURNED to the model rather than raised: a rejected call (missing verification, bad
    arguments, a tool not yet unlocked) is information the agent should react to, and a real artifact
    has to cope with it. Requestor is always the agent -- customer-held tools are the customer's to
    call, and this agent must not reach for them.
    """
    url = _tools_url()
    if not url:
        return "ERROR: no tool endpoint bound"
    try:
        r = httpx.post(f"{url}/{name}",
                       json={"arguments": arguments or {}, "requestor": "assistant"},
                       headers=_headers(), timeout=TOOL_TIMEOUT_S)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            return f"ERROR: {detail}"
        return json.dumps(r.json().get("result"), default=str)[:6000]
    except httpx.HTTPError as e:
        return f"ERROR: {e!r}"


def run_chat(client, messages: list[dict], schemas: list[dict] | None = None,
             *, collect_trace: bool = False):
    """Run the bounded tool-calling loop over `messages` (which must already carry the system
    prompt); returns the final assistant text, or `(text, trace)` when `collect_trace` is set.

    Mirrors the airline reference loop so the two golds stay comparable, and bounded by MAX_STEPS so a
    model that keeps searching without concluding cannot run forever.
    """
    schemas = schemas if schemas is not None else tool_schemas()
    last_text = ""
    trace: list | None = [] if collect_trace else None
    if collect_trace:
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        if sys_msg:
            trace.append({"kind": "system", "content": sys_msg.get("content", "")})

    for step in range(1, MAX_STEPS + 1):
        _log.info("  LLM call (step %d/%d, model=%s) ...",
                  step, MAX_STEPS, getattr(client, "model", "?"))
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
                                  "result": result[:2000]})
            continue
        last_text = resp.content or last_text
        if collect_trace:
            trace.append({"kind": "final", "step": step, "content": last_text})
        return (last_text, trace) if collect_trace else last_text

    # Step budget exhausted mid-tool-loop: return whatever the agent last said rather than nothing, so
    # the turn still produces a reply the evaluator can score.
    _log.warning("step budget (%d) exhausted before a final answer", MAX_STEPS)
    if collect_trace:
        trace.append({"kind": "final", "step": MAX_STEPS, "content": last_text,
                      "truncated": True})
        return last_text, trace
    return last_text


def respond(client, user_messages: list[dict], *, collect_trace: bool = False):
    """One /respond turn: prepend the system prompt to the caller's message history and run the loop."""
    messages = [{"role": "system", "content": system_prompt()}]
    messages.extend(m for m in user_messages if m.get("role") != "system")
    return run_chat(client, messages, collect_trace=collect_trace)
