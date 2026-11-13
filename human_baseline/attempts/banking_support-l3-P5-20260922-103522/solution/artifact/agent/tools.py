"""Native Claude tool schemas -- BASE TOOLS ONLY.

This module is the structural enforcement of P3 / R-19: the model is handed schemas for the 17 base
agent tools plus the 3 retrieval tools, and *nothing else*. No specialist ``*_NNNN`` action tool is
ever pre-registered here. The only route to a specialist action is a KB procedure that names it,
reached at runtime through ``unlock_discoverable_agent_tool`` -> ``call_discoverable_agent_tool``.

Signatures are taken verbatim from EXPLORATION.md §5.1-5.2 (all confirmed against the stakeholder
briefing and one live call per tool).
"""
from __future__ import annotations

from typing import Any

_STR = {"type": "string"}


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
        },
    }


# --- Retrieval (base, read-only over the corpus; EXPLORATION.md §5.1) ---------------------------

_RETRIEVAL = [
    _tool(
        "KB_search_bm25",
        "Keyword search over the knowledge base. PRIMARY retrieval path. Returns ranked "
        "{id, score, content}. Use this (and shell/grep) to find the Internal: procedure doc that "
        "names the action tool for a situation.",
        {"query": _STR, "k": {"type": "integer", "description": "max results, default 10"}},
        ["query"],
    ),
    _tool(
        "KB_search_dense",
        "Embedding search over the knowledge base. Degrades gracefully with no results when no "
        "embeddings key is set (the case in this environment) -- treat any results as a bonus, "
        "never depend on it. Prefer KB_search_bm25 / shell.",
        {"query": _STR, "k": {"type": "integer", "description": "max results, default 10"}},
        ["query"],
    ),
    _tool(
        "shell",
        "Read-only shell (grep, cat, ls) over the corpus exported as files. Useful to grep the "
        "Internal: procedure docs for an action-tool name.",
        {"command": _STR},
        ["command"],
    ),
]

# --- Base agent tools (14; EXPLORATION.md §5.2) --------------------------------------------------

_BASE = [
    _tool("get_user_information_by_id", "Look up a customer by user_id. Returns the record with the "
          "four verification fields (date_of_birth, email, phone_number, address) plus name/user_id.",
          {"user_id": _STR}, ["user_id"]),
    _tool("get_user_information_by_email", "Look up a customer by email.", {"email": _STR}, ["email"]),
    _tool("get_user_information_by_name", "Look up a customer by name (case sensitive).",
          {"customer_name": _STR}, ["customer_name"]),
    _tool("get_credit_card_accounts_by_user", "List a user's credit card accounts.",
          {"user_id": _STR}, ["user_id"]),
    _tool("get_credit_card_transactions_by_user", "List a user's credit card transactions.",
          {"user_id": _STR}, ["user_id"]),
    _tool("get_referrals_by_user", "List a user's referrals.", {"user_id": _STR}, ["user_id"]),
    _tool("get_current_time", "Current time. Use its value for log_verification's time_verified.",
          {}, []),
    _tool("log_verification",
          "Write ONE verification record after two fields match and BEFORE the account action. All "
          "seven args come from the looked-up user record (not customer assertions); time_verified "
          "from get_current_time. Do not log twice in one conversation.",
          {"name": _STR, "user_id": _STR, "address": _STR, "email": _STR, "phone_number": _STR,
           "date_of_birth": _STR, "time_verified": _STR},
          ["name", "user_id", "address", "email", "phone_number", "date_of_birth", "time_verified"]),
    _tool("change_user_email", "Change a user's email.", {"user_id": _STR, "new_email": _STR},
          ["user_id", "new_email"]),
    _tool("transfer_to_human_agents",
          "Escalate to a human. Use ONLY when the KB names no applicable action. reason (optional) "
          "must be a value from the tool's closed enum (e.g. fraud_or_security_concern, "
          "account_closure_request, deceased_account_holder, legal_or_regulatory_matter, other) -- "
          "never free text.",
          {"summary": _STR, "reason": _STR}, ["summary"]),
    _tool("unlock_discoverable_agent_tool",
          "Unlock a specialist action tool by the exact name read from a KB procedure. Must be done "
          "before call_discoverable_agent_tool.",
          {"agent_tool_name": _STR}, ["agent_tool_name"]),
    _tool("call_discoverable_agent_tool",
          "Invoke a previously unlocked specialist action tool. `arguments` is a JSON-encoded STRING "
          "of the object the KB procedure requires, e.g. '{\"card_id\": \"abc\", \"pin\": \"2748\"}' "
          "-- pass a string, not an object.",
          {"agent_tool_name": _STR,
           "arguments": {"type": "string",
                         "description": "JSON-encoded object of the tool's arguments, e.g. "
                         "'{\"card_id\": \"abc\"}'. A JSON string, not an object."}},
          ["agent_tool_name"]),
    _tool("list_discoverable_agent_tools",
          "List tools that have been UNLOCKED this session. This is NOT a catalogue of what is right "
          "for a situation -- the situation->action mapping comes only from the KB procedure.",
          {}, []),
    _tool("give_discoverable_user_tool",
          "Hand a customer-side action to the customer so they can run it. Walk them through it to "
          "completion. `arguments` is a JSON-encoded STRING of the object the procedure requires "
          "(e.g. '{\"url\": \"https://...\"}'), not an object.",
          {"discoverable_tool_name": _STR,
           "arguments": {"type": "string",
                         "description": "JSON-encoded object of the tool's arguments. A JSON "
                         "string, not an object."}},
          ["discoverable_tool_name"]),
]

# Full set exposed to the model: 3 retrieval + 14 base = 17 base agent tools. No specialists.
TOOL_SCHEMAS: list[dict[str, Any]] = _RETRIEVAL + _BASE

# Names the loop is allowed to dispatch. Used only to guard the loop, not as an action catalogue.
BASE_TOOL_NAMES = frozenset(t["name"] for t in TOOL_SCHEMAS)
