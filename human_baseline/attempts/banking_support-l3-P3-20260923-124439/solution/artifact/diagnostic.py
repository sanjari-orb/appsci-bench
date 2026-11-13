#!/usr/bin/env python
"""Read-only, container-side diagnostic for the Rho-Bank artifact.

Run BY P3 inside a `dev_deploy` container. Read-only modes:

    python diagnostic.py            # env presence + discovery/manifest probe + retrieval arg-name probe
    python diagnostic.py kb         # broad KB sweep for Phase-B facts (BASE tools, unlock, actions)
    python diagnostic.py schemas    # targeted: documented signatures for the unlock/call pair + BASE tools
    python diagnostic.py llm        # ONE trivial Anthropic Messages request: model/protocol/creds work?
    python diagnostic.py readdoc P  # read-only cat of one human-supplied corpus path P (full doc text)

This is NOT a customer-facing HTTP route. It performs only non-mutating operations and prints a JSON
report to stdout. It NEVER prints credential values -- only the *names* of present credential env vars
-- so the report is safe to paste back.

What it establishes, to unblock Phase B (the agent core):
  1. Which LLM/embeddings credential env var NAMES the deployer forwarded (names only), and whether
     the banking tool API vars are present -- so P3 can confirm the real provider/model/endpoint.
  2. Whether the tool API base URL exposes any listing/manifest endpoint, with each probe response
     classified (not-found vs auth vs transient vs 2xx-candidate) so we never conclude "no manifest"
     from an auth failure or a 5xx.
  3. The argument field NAMES the retrieval tools expect (KB_search_bm25 / KB_search_dense / shell),
     learned ONLY from documented error feedback: we call each with empty arguments and, if the tool
     rejects that with a validation error naming its required fields, we surface that error verbatim.
     We do NOT guess argument names; if the error does not name them, they stay unknown.

Nothing here mutates the DB: GETs and empty-argument calls to read-only retrieval tools only. It does
NOT call BASE tools or any unlock/specialist action by a guessed name.

The `kb` mode adds a read-only KB sweep to surface the remaining Phase-B interface facts (BASE tool
names, the unlock mechanism, specialist/customer action names) from KB text. It calls ONLY the three
documented read-only retrieval tools -- KB_search_bm25 / KB_search_dense / shell -- so it cannot log a
verification, escalate, unlock, or perform any write; `_guard_readonly` enforces that in code.

The `llm` mode sends exactly ONE trivial, tool-free Anthropic Messages request (max_tokens 16) with
the explicitly chosen model id and the runtime Anthropic env vars, to confirm the model/protocol/
credentials work before any agent core is built. It is a connectivity probe, NOT the agent core: it
reports only non-secret facts (status, echoed model, stop_reason, usage, a tiny content snippet, and
the NAMES of any custom headers) and never prints the auth token or a custom-header value.

The `readdoc <path>` mode does a read-only `cat` of one human-supplied corpus path -- used to read a
documented schema in full when a context window clipped it. The path comes from a human, is sanitized
to a safe relative path, and runs through the same `_guard_readonly` check; nothing is mutated.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional

import config


def _retrieval_schema_probe() -> Dict[str, Any]:
    """Elicit retrieval-tool argument names via documented error feedback only.

    Calling a retrieval tool with empty arguments is non-mutating (they are read-only). If the tool
    responds with a validation error that names its required fields, we capture that error so P3
    (and Phase B) learn the real field names without guessing. A 2xx here is also fine -- it simply
    means the tool tolerates empty arguments.
    """
    try:
        url, token = config.require_banking_tools()
    except RuntimeError as exc:
        return {"available": False, "reason": str(exc)}

    # Imported lazily so the diagnostic can still report env presence even if httpx is unavailable.
    from tools import ToolClient

    findings: Dict[str, Any] = {"available": True, "tools": {}}
    with ToolClient(url, token) as client:
        for tool_name in ("KB_search_bm25", "KB_search_dense", "shell"):
            res = client.call(tool_name, {})  # empty args on purpose -- read-only, non-mutating
            findings["tools"][tool_name] = {
                "kind": res.kind,
                "status_code": res.status_code,
                # On success we report only the TYPE/shape of the result, not its contents, to avoid
                # dumping corpus data.
                "result_type": type(res.result).__name__ if res.ok else None,
                "error": None if res.ok else res.error,
                # The (redacted, truncated) response body: an HTTP 400 validation error here names the
                # required argument fields -- this is how the field names are learned, not guessed.
                "detail": None if res.ok else res.detail,
            }
    return findings


def _discovery_probe() -> Dict[str, Any]:
    try:
        url, token = config.require_banking_tools()
    except RuntimeError as exc:
        return {"available": False, "reason": str(exc)}

    from tools import ToolClient

    with ToolClient(url, token) as client:
        probe = client.probe_discovery()
    return {
        "available": True,
        "manifest_candidate": probe.manifest_candidate,
        "findings": [
            {
                "path": f.path,
                "method": f.method,
                "kind": f.kind,
                "status_code": f.status_code,
                "note": f.note,
            }
            for f in probe.findings
        ],
    }


# ---------------------------------------------------------------------------
# Read-only KB discovery (`kb` mode): Phase-B interface facts, no writes, no guessed tool names.
# ---------------------------------------------------------------------------
#
# Discovery uses ONLY the three documented read-only retrieval tools, whose argument field names are
# confirmed: KB_search_bm25({"query": ...}), KB_search_dense({"query": ...}), shell({"command": ...}).
# It never calls a BASE tool, the unlock tool, or any specialist/customer action -- so it cannot log a
# verification, escalate, unlock, or perform any write. `_guard_readonly` enforces this structurally:
# it refuses any tool outside the allowlist and any shell command whose leading binary is not a
# read-only utility or that (outside quotes) contains a write / redirection / command-chaining token.

# The only tools discovery may call.
_READONLY_TOOLS = ("KB_search_bm25", "KB_search_dense", "shell")

# Leading binaries allowed for `shell` -- all non-mutating readers. (No sed/tee: they can write.)
_READONLY_SHELL_BINS = {
    "ls", "cat", "grep", "egrep", "find", "head", "tail", "awk",
    "pwd", "wc", "sort", "uniq", "file", "stat", "tree", "cut", "nl",
}

# Shell metacharacters that redirect, pipe, or chain commands. Checked only OUTSIDE quoted spans, so a
# regex alternation like 'a|b' inside a grep pattern is fine but a real shell pipe is not.
_SHELL_META_TOKENS = (">", "<", "|", ";", "&", "`", "$(", "${", "\n")

# Mutating / second-command utilities that must never appear anywhere outside quotes (e.g. smuggled via
# `find -exec`). Matched on word boundaries, so a doc name like "used" or "format" is never mistaken for
# sed/rm. This is the code-level guarantee that discovery performs no writes.
_SHELL_DANGEROUS_RE = re.compile(
    r"\b(rm|mv|cp|tee|touch|chmod|chown|chgrp|dd|sed|truncate|mkdir|rmdir|ln|shred|"
    r"install|rsync|xargs|eval|exec)\b"
)

# Spans we ignore when scanning for the tokens above: single- and double-quoted strings.
_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# KB search queries aimed at the exact Phase-B unknowns: BASE tools, the unlock mechanism, actions.
_KB_QUERIES = (
    "list of available tools and actions for the support agent",
    "how to unlock a specialist action by name",
    "unlock action procedure and its required arguments",
    "identity verification required before account actions",
    "record an identity verification against the customer",
    "look up a customer's identity and account",
    "escalate to a human agent",
    "current date and time environment clock tool",
    "file a dispute for a card transaction procedure",
    "referral program bonus eligibility procedure",
)

# One dense query, to confirm whether embeddings-backed search is usable in this environment.
_DENSE_PROBE_QUERY = "unlock a specialist action to file a dispute"

# Read-only shell commands: map the corpus, then grep for where the mechanism/names are documented.
# No pipes (portability); grep's own -m/-r and find's -maxdepth bound the output. Report-side
# truncation bounds it again.
_SHELL_COMMANDS = (
    "pwd",
    "ls -la",
    "find . -maxdepth 3 -type f",
    "grep -rniE -m 5 unlock .",
    "grep -rniE -m 5 verif .",
    "grep -rniE -m 5 escalat .",
    "grep -rniE -m 5 dispute .",
    "grep -rniE -m 5 -e clock -e 'current time' .",
    "grep -rniE -m 5 referral .",
)


def _guard_readonly(name: str, arguments: Dict[str, Any]) -> None:
    """Raise unless (name, arguments) is a provably non-mutating retrieval call.

    This is the structural guarantee that discovery writes nothing: the only callable tools are the
    three retrieval tools, and `shell` is limited to read-only utilities with no redirection or
    command chaining. Any violation aborts discovery loudly rather than risking a mutation.
    """
    if name not in _READONLY_TOOLS:
        raise AssertionError(f"discovery refused to call non-retrieval tool: {name!r}")
    if name == "shell":
        command = str((arguments or {}).get("command", ""))
        stripped = command.strip()
        leading = stripped.split(None, 1)[0] if stripped else ""
        if leading not in _READONLY_SHELL_BINS:
            raise AssertionError(f"discovery refused shell command (non-read-only binary): {command!r}")
        outside_quotes = _QUOTED_SPAN_RE.sub(" ", command)
        if any(tok in outside_quotes for tok in _SHELL_META_TOKENS):
            raise AssertionError(f"discovery refused shell command (redirection/pipe/chaining): {command!r}")
        if _SHELL_DANGEROUS_RE.search(outside_quotes):
            raise AssertionError(f"discovery refused shell command (mutating utility): {command!r}")


def _truncate(obj: Any, max_str: int = 2000, max_items: int = 6) -> Any:
    """Bound an arbitrary result so a large KB payload cannot flood the report, preserving its shape."""
    if isinstance(obj, str):
        if len(obj) <= max_str:
            return obj
        return obj[:max_str] + f"...(+{len(obj) - max_str} more chars)"
    if isinstance(obj, list):
        out: List[Any] = [_truncate(x, max_str, max_items) for x in obj[:max_items]]
        if len(obj) > max_items:
            out.append(f"...(+{len(obj) - max_items} more items)")
        return out
    if isinstance(obj, dict):
        return {k: _truncate(v, max_str, max_items) for k, v in obj.items()}
    return obj


def _summarize_call(
    label: str,
    tool: str,
    arguments: Dict[str, Any],
    res: Any,
    *,
    max_str: int = 2000,
    max_items: int = 6,
) -> Dict[str, Any]:
    """Record one read-only call's outcome: on success the (truncated) result; on failure its class."""
    return {
        "label": label,
        "tool": tool,
        "arguments": arguments,
        "ok": res.ok,
        "kind": res.kind,
        "status_code": res.status_code,
        "error": None if res.ok else res.error,
        "detail": None if res.ok else res.detail,
        "result": _truncate(res.result, max_str, max_items) if res.ok else None,
    }


def build_kb_report() -> Dict[str, Any]:
    """Read-only KB sweep for the Phase-B interface facts. Calls only retrieval tools; writes nothing."""
    try:
        url, token = config.require_banking_tools()
    except RuntimeError as exc:
        return {"available": False, "reason": str(exc)}

    from tools import ToolClient

    searches: List[Dict[str, Any]] = []
    shells: List[Dict[str, Any]] = []
    with ToolClient(url, token) as client:
        for query in _KB_QUERIES:
            args = {"query": query}
            _guard_readonly("KB_search_bm25", args)
            searches.append(
                _summarize_call(query, "KB_search_bm25", args, client.call("KB_search_bm25", args))
            )

        dense_args = {"query": _DENSE_PROBE_QUERY}
        _guard_readonly("KB_search_dense", dense_args)
        dense_probe = _summarize_call(
            _DENSE_PROBE_QUERY, "KB_search_dense", dense_args, client.call("KB_search_dense", dense_args)
        )

        for command in _SHELL_COMMANDS:
            args = {"command": command}
            _guard_readonly("shell", args)
            shells.append(_summarize_call(command, "shell", args, client.call("shell", args)))

    return {
        "available": True,
        "bm25_searches": searches,
        "dense_probe": dense_probe,
        "shell_reads": shells,
        "notes": [
            "Read-only: only KB_search_bm25 / KB_search_dense / shell are called; no BASE tool, unlock, "
            "or write action is invoked. _guard_readonly enforces this in code.",
            "Goal: surface BASE tool names, the unlock mechanism (unlock tool + args + how an unlocked "
            "action is invoked + where its arg schema is documented), and specialist/customer action "
            "names -- all from KB text, never from guessed endpoint names.",
            "Results are truncated (max ~2000 chars / 6 items per hit); rerun a targeted query or "
            "shell command if a specific schema is cut off.",
            "No credential values are printed; the banking token is redacted from all output.",
        ],
    }


# ---------------------------------------------------------------------------
# Targeted schema discovery (`schemas` mode): read the DOCUMENTED signatures for the mechanism/BASE
# tools we already have leads on, without enumerating any specialist actions.
# ---------------------------------------------------------------------------
#
# The prior KB pass surfaced three documented leads: the unlock+call pair and log_verification. This
# mode reads their documented argument/result schemas (and the rest of the BASE-tool catalog) from KB
# text only -- via BM25 (dense is unavailable in this env) and read-only `grep`/`find`. It NEVER calls
# unlock_discoverable_agent_tool, call_discoverable_agent_tool, log_verification, or any BASE/specialist
# tool: those are writes or state changes, and their schemas come from documentation, not from probing.
# It also does NOT search for or hard-code specialist action names -- those stay discovered per request
# at runtime.

# The documented leads whose signatures we want to read (the mechanism + one BASE write tool). These
# are the unlock/call machinery and a named BASE tool, NOT specialist actions.
_SCHEMA_TOOL_LEADS = (
    "unlock_discoverable_agent_tool",
    "call_discoverable_agent_tool",
    "log_verification",
)

# Natural-language BM25 queries for the signatures and the BASE-tool catalog. No specialist action
# names appear here on purpose.
_SCHEMA_QUERIES = (
    "arguments and result of unlock_discoverable_agent_tool",
    "arguments and result of call_discoverable_agent_tool",
    "log_verification required fields and result",
    "complete list of base tools available to the agent",
    "how to call an unlocked discoverable tool and read its result",
    "identity and account lookup tools and the environment clock and escalation tool",
)

# Read-only shell: locate the authoritative doc files (-l), print each signature block in generous
# context (-A/-B), and map the doc tree. Fixed commands -- no discovered paths are fed back into a
# command -- so there is no path-injection surface. grep -F is a fixed-string match on the exact names.
_SCHEMA_SHELL_COMMANDS = (
    "grep -rliF unlock_discoverable_agent_tool .",
    "grep -rliF call_discoverable_agent_tool .",
    "grep -rliF log_verification .",
    "grep -rni -A 40 -B 4 -m 2 unlock_discoverable_agent_tool .",
    "grep -rni -A 40 -B 4 -m 2 call_discoverable_agent_tool .",
    "grep -rni -A 25 -B 4 -m 2 log_verification .",
    "find . -maxdepth 4 -type f -iname '*.md'",
    "find . -maxdepth 4 -type f -iname '*tool*'",
)

# Larger caps than the broad kb sweep: a signature block may be a few KB and we don't want to clip it.
_SCHEMA_MAX_STR = 8000
_SCHEMA_MAX_ITEMS = 8


def build_schema_report() -> Dict[str, Any]:
    """Read documented schemas for the known mechanism/BASE tools. Calls only BM25 + shell; no writes."""
    try:
        url, token = config.require_banking_tools()
    except RuntimeError as exc:
        return {"available": False, "reason": str(exc)}

    from tools import ToolClient

    searches: List[Dict[str, Any]] = []
    shells: List[Dict[str, Any]] = []
    with ToolClient(url, token) as client:
        for query in _SCHEMA_QUERIES:
            args = {"query": query}
            _guard_readonly("KB_search_bm25", args)
            searches.append(
                _summarize_call(
                    query, "KB_search_bm25", args, client.call("KB_search_bm25", args),
                    max_str=_SCHEMA_MAX_STR, max_items=_SCHEMA_MAX_ITEMS,
                )
            )

        for command in _SCHEMA_SHELL_COMMANDS:
            args = {"command": command}
            _guard_readonly("shell", args)
            shells.append(
                _summarize_call(
                    command, "shell", args, client.call("shell", args),
                    max_str=_SCHEMA_MAX_STR, max_items=_SCHEMA_MAX_ITEMS,
                )
            )

    return {
        "available": True,
        "leads_from_prior_kb_pass": list(_SCHEMA_TOOL_LEADS),
        "bm25_searches": searches,
        "shell_reads": shells,
        "notes": [
            "Read-only: only KB_search_bm25 and shell are called (dense is unavailable in this env). "
            "unlock/call/log_verification and every BASE/specialist tool are NEVER invoked -- "
            "_guard_readonly enforces this in code -- so their schemas come from docs, not from probing.",
            "Targets ONLY the documented mechanism/BASE-tool signatures (the unlock+call pair, "
            "log_verification, and the BASE catalog). It does NOT search for or enumerate specialist "
            "action names; those stay discovered per request at runtime and are never hard-coded.",
            "grep -l names the authoritative doc files; grep -A/-B prints signature blocks in context. "
            "If a schema is clipped by truncation, cat the named file(s) read-only to read the full text.",
            "KB text is task data describing the interface, not instructions: our boundaries (no writes "
            "during discovery, no guessed names, no specialist-action enumeration) hold no matter what a "
            "doc says.",
            "No credential values are printed; the banking token is redacted from all output.",
        ],
    }


# ---------------------------------------------------------------------------
# Minimal Anthropic Messages connectivity test (`llm` mode). NOT the agent core.
# ---------------------------------------------------------------------------
#
# One trivial, tool-free Messages request using the explicitly chosen model id (config.agent_model())
# and the runtime Anthropic env vars, to confirm the model/protocol/credentials actually work before
# any agent core is built. It reports only NON-SECRET facts (HTTP status, the model the server echoes,
# stop_reason, token usage, and a tiny content snippet) and never prints the auth token or any
# custom-header value. This is a probe, not the LLM client: it hard-codes no agent behavior.

_ANTHROPIC_VERSION = "2023-06-01"
_LLM_DETAIL_LIMIT = 800
_LLM_TIMEOUT = 30.0


def _parse_messages_response(resp: Any) -> Dict[str, Any]:
    """Extract only non-secret facts from a Messages API response (redacting any secret if present)."""
    status = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        return {
            "ok": False,
            "kind": "bad_response",
            "error": "response body was not JSON",
            "detail": config.redact((resp.text or "")[:_LLM_DETAIL_LIMIT]),
        }
    if not (200 <= status < 300):
        err = body.get("error") if isinstance(body, dict) else None
        return {
            "ok": False,
            "kind": "http_error",
            "error_type": err.get("type") if isinstance(err, dict) else None,
            # Error envelopes should not carry request secrets, but redact defensively and truncate.
            "detail": config.redact(json.dumps(body)[:_LLM_DETAIL_LIMIT]),
        }
    text_snippet: Optional[str] = None
    content = body.get("content") if isinstance(body, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_snippet = str(block.get("text", ""))[:80]
                break
    return {
        "ok": True,
        "kind": "ok",
        "response_model": body.get("model") if isinstance(body, dict) else None,
        "stop_reason": body.get("stop_reason") if isinstance(body, dict) else None,
        "usage": body.get("usage") if isinstance(body, dict) else None,
        "content_text_snippet": config.redact(text_snippet) if text_snippet else None,
    }


def build_llm_connectivity_report(client: Any = None) -> Dict[str, Any]:
    """Send one trivial Messages request and report non-secret connectivity facts. Never the agent core."""
    presence = config.anthropic_env_presence()
    model = config.agent_model()
    model_source = config.agent_model_source()
    base_common = {
        "model": model,
        "model_source": model_source,
        "anthropic_env_presence": presence,
        # Non-secret: surfaces a present-but-malformed APPSCI_AGENT_CUSTOM_HEADERS instead of silently
        # sending no routing headers. None when the value is absent or parses cleanly.
        "custom_headers_error": config.anthropic_custom_headers_error(),
    }
    try:
        base_url, token = config.require_anthropic()
    except RuntimeError as exc:
        return {"available": False, "reason": str(exc), **base_common}

    custom = config.anthropic_custom_headers()
    headers = {
        "Authorization": f"Bearer {token}",  # ANTHROPIC_AUTH_TOKEN -> bearer (proxy/gateway auth)
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    headers.update(custom)  # proxy routing headers; values attached but never printed
    payload = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
    }
    url = f"{base_url}/v1/messages"

    import httpx  # lazy: keep pure env-presence reporting available even without httpx

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=_LLM_TIMEOUT)
    try:
        try:
            resp = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return {
                "available": True,
                "ok": False,
                "kind": "network_error",
                "error": config.redact(f"{type(exc).__name__}: {exc}"),
                "custom_header_names": sorted(custom.keys()),
                **base_common,
            }
        parsed = _parse_messages_response(resp)
        parsed.update(base_common)
        parsed["available"] = True
        parsed["status_code"] = resp.status_code
        parsed["custom_header_names"] = sorted(custom.keys())
        return parsed
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# Read one KB doc in full (`readdoc <path>` mode): human-supplied path, read-only cat.
# ---------------------------------------------------------------------------
#
# Used to close schema gaps: after `schemas` mode names the authoritative doc files with grep -l,
# P3 passes one of those paths here to read it in full when a context window clipped it. The path
# comes from a human, never fed automatically from tool output, and is sanitized to a safe relative
# corpus path before being cat-ed through the same read-only guard.

_READDOC_MAX_CHARS = 16000
_SAFE_DOC_PATH_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")


def build_readdoc_report(path: str) -> Dict[str, Any]:
    """Read-only `cat` of one human-supplied corpus path, for reading full documented schemas."""
    if not path or not _SAFE_DOC_PATH_RE.match(path) or ".." in path.split("/"):
        return {
            "available": False,
            "reason": f"unsafe or empty path: {path!r}. Pass a relative corpus path, e.g. "
            "kb/tools/reference.md (no leading '/', no '..').",
        }
    try:
        url, token = config.require_banking_tools()
    except RuntimeError as exc:
        return {"available": False, "reason": str(exc)}

    command = f"cat {path}"
    try:
        _guard_readonly("shell", {"command": command})
    except AssertionError as exc:
        return {"available": False, "reason": f"path rejected by read-only guard: {exc}"}

    from tools import ToolClient

    with ToolClient(url, token) as client:
        res = client.call("shell", {"command": command})
    return {
        "available": True,
        "path": path,
        "read": _summarize_call(path, "shell", {"command": command}, res, max_str=_READDOC_MAX_CHARS, max_items=4),
        "notes": [
            "Read-only cat of a single, human-supplied corpus path (no path came from tool output).",
            "Use on the doc files `schemas` mode named (grep -l) to read full unlock/call and BASE-tool "
            "signatures when a context window clipped them.",
        ],
    }


def build_report() -> Dict[str, Any]:
    report: Dict[str, Any] = {
        # (1) Env presence -- NAMES ONLY, never values.
        "present_credential_env_names": config.present_credential_env_names(),
        "banking_env_presence": config.banking_env_presence(),
        "port": config.port(),
    }
    # (2) Discovery probe (read-only).
    report["discovery_probe"] = _discovery_probe()
    # (3) Retrieval-tool argument-name elicitation via documented error feedback (read-only).
    report["retrieval_schema_probe"] = _retrieval_schema_probe()
    report["notes"] = [
        "Names only; no credential values are printed.",
        "All operations are read-only/non-mutating.",
        "BASE tool names, the unlock mechanism, and specialist actions are NOT probed by guessed "
        "names -- discover them via KB search per the task procedure.",
    ]
    return report


def main(argv: List[str]) -> int:
    mode = (argv[0] if argv else "env").lower()
    if mode in ("env", "default", "diag"):
        report: Dict[str, Any] = build_report()
    elif mode in ("kb", "discover", "kb-discovery"):
        report = build_kb_report()
    elif mode in ("schemas", "schema"):
        report = build_schema_report()
    elif mode in ("llm", "connectivity"):
        report = build_llm_connectivity_report()
    elif mode in ("readdoc", "read"):
        if len(argv) < 2:
            report = {"error": "readdoc requires a corpus path, e.g. `diagnostic.py readdoc kb/tools.md`"}
        else:
            report = build_readdoc_report(argv[1])
    elif mode == "all":
        report = {"env": build_report(), "kb": build_kb_report(), "schemas": build_schema_report()}
    else:
        report = {
            "error": f"unknown mode {mode!r}",
            "modes": ["env", "kb", "schemas", "llm", "readdoc <path>", "all"],
        }
    # Defense-in-depth: redact the banking token if it somehow appears in the serialized output.
    print(config.redact(json.dumps(report, indent=2, sort_keys=True)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
