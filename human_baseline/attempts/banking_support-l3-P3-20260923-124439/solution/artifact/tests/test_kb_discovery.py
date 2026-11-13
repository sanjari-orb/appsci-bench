"""Tests for the read-only KB-discovery mode of diagnostic.py.

Two guarantees matter here and are asserted directly:
  1. `_guard_readonly` structurally forbids anything but the three read-only retrieval tools, and any
     shell command that could write, redirect, or chain -- so discovery cannot log a verification,
     escalate, unlock, or perform any write.
  2. `build_kb_report` drives ONLY those retrieval endpoints over the documented wire shape, with the
     bearer header, and bounds result size.
"""
from __future__ import annotations

import httpx
import pytest

import config
import diagnostic
import tools as tools_mod
from fake_tools import Recorder, make_client


# --- _guard_readonly -------------------------------------------------------

def test_guard_allows_the_three_retrieval_tools():
    # None of these raise.
    diagnostic._guard_readonly("KB_search_bm25", {"query": "unlock"})
    diagnostic._guard_readonly("KB_search_dense", {"query": "unlock"})
    diagnostic._guard_readonly("shell", {"command": "grep -rniE -m 5 unlock ."})


def test_guard_allows_regex_alternation_inside_quotes():
    # A '|' inside a quoted grep pattern is a regex alternation, not a shell pipe -- allowed.
    diagnostic._guard_readonly("shell", {"command": "grep -rniE -m 5 'clock|current time' ."})
    diagnostic._guard_readonly("shell", {"command": "grep -rniE -m 5 -e clock -e 'current time' ."})


def test_guard_allows_every_hardcoded_discovery_command():
    # Whatever build_kb_report / build_schema_report will actually run must pass the guard.
    for command in diagnostic._SHELL_COMMANDS:
        diagnostic._guard_readonly("shell", {"command": command})
    for command in diagnostic._SCHEMA_SHELL_COMMANDS:
        diagnostic._guard_readonly("shell", {"command": command})


def test_guard_allows_exact_tool_name_greps_without_false_positives():
    # Underscored tool names must not trip word-boundary matches for rm/ln/sed/etc.
    for lead in diagnostic._SCHEMA_TOOL_LEADS:
        diagnostic._guard_readonly("shell", {"command": f"grep -rliF {lead} ."})
    # A doc filename containing 'sed'/'rm' as a substring is fine (word boundaries).
    diagnostic._guard_readonly("shell", {"command": "cat kb/used_cases.md"})
    diagnostic._guard_readonly("shell", {"command": "cat kb/format_rules.md"})


def test_guard_rejects_non_retrieval_tools():
    for name in ("unlock_action", "log_verification", "escalate", "file_dispute", "get_account"):
        with pytest.raises(AssertionError):
            diagnostic._guard_readonly(name, {})


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf .",                 # not a read-only binary
        "echo hi",                  # not in the allowlist at all
        "sed -i s/a/b/ file",       # sed can write in place
        "cat a > b",                # output redirection
        "cat a >> b",               # append redirection
        "ls; rm -rf /",             # command chaining
        "cat a && rm b",            # command chaining
        "grep x . | tee out.txt",   # pipe + tee (write)
        "find . -exec rm {} ;",     # chaining/exec + rm
        "cat $(rm x)",              # command substitution
    ],
)
def test_guard_rejects_write_or_chaining_shell(command):
    with pytest.raises(AssertionError):
        diagnostic._guard_readonly("shell", {"command": command})


# --- _truncate -------------------------------------------------------------

def test_truncate_bounds_long_string():
    out = diagnostic._truncate("x" * 10_000, max_str=100)
    assert out.startswith("x" * 100)
    assert "more chars" in out


def test_truncate_bounds_long_list_and_recurses():
    out = diagnostic._truncate([{"t": "y" * 500} for _ in range(50)], max_str=20, max_items=3)
    assert isinstance(out, list)
    assert len(out) == 4  # 3 items + 1 elision marker
    assert "more items" in out[-1]
    assert out[0]["t"].endswith("more chars)")


# --- build_kb_report -------------------------------------------------------

def _echo_kb_handler(request: httpx.Request) -> httpx.Response:
    # Return a small KB-shaped envelope regardless of tool; content is irrelevant to these assertions.
    return httpx.Response(
        200,
        json={"result": {"hits": [{"path": "kb/policy.md", "text": "unlock the action by name"}]}},
    )


def _patch_toolclient(monkeypatch, recorder: Recorder) -> None:
    """Make build_kb_report's ToolClient(url, token) run over an in-memory MockTransport."""
    monkeypatch.setattr(config, "require_banking_tools", lambda: ("http://tools.test", "tok"))
    shared = make_client(_echo_kb_handler, recorder)
    real_cls = tools_mod.ToolClient

    def factory(url, token, **kwargs):
        return real_cls(url, token, client=shared)

    monkeypatch.setattr(tools_mod, "ToolClient", factory)


def test_build_kb_report_calls_only_readonly_endpoints(monkeypatch):
    rec = Recorder()
    _patch_toolclient(monkeypatch, rec)

    report = diagnostic.build_kb_report()

    assert report["available"] is True
    # Only the three retrieval endpoints are ever hit -- never a BASE tool, unlock, or write action.
    paths = {c["path"] for c in rec.calls}
    assert paths <= {"/KB_search_bm25", "/KB_search_dense", "/shell"}
    assert "/KB_search_bm25" in paths and "/shell" in paths

    expected = len(diagnostic._KB_QUERIES) + 1 + len(diagnostic._SHELL_COMMANDS)
    assert len(rec.calls) == expected


def test_build_kb_report_uses_documented_wire_shape_and_bearer(monkeypatch):
    rec = Recorder()
    _patch_toolclient(monkeypatch, rec)

    diagnostic.build_kb_report()

    for c in rec.calls:
        assert c["method"] == "POST"
        assert c["authorization"] == "Bearer tok"
        assert set(c["body"].keys()) == {"arguments"}
        # Confirmed retrieval arg names only.
        arg_keys = set(c["body"]["arguments"].keys())
        assert arg_keys in ({"query"}, {"command"})


def test_build_kb_report_reports_missing_config_without_calling(monkeypatch):
    def raise_missing():
        raise RuntimeError("BANKING_TOOLS_URL and BANKING_TOOLS_TOKEN must be set")

    monkeypatch.setattr(config, "require_banking_tools", raise_missing)
    report = diagnostic.build_kb_report()
    assert report["available"] is False
    assert "BANKING_TOOLS_URL" in report["reason"]


# --- build_schema_report ---------------------------------------------------

def test_build_schema_report_calls_only_bm25_and_shell(monkeypatch):
    rec = Recorder()
    _patch_toolclient(monkeypatch, rec)

    report = diagnostic.build_schema_report()

    assert report["available"] is True
    paths = {c["path"] for c in rec.calls}
    # BM25 + shell only. Dense is not called (reported unavailable in the build env), and no BASE tool,
    # unlock/call, or log_verification endpoint is ever hit.
    assert paths <= {"/KB_search_bm25", "/shell"}
    assert "/KB_search_dense" not in paths
    for forbidden in ("/unlock_discoverable_agent_tool", "/call_discoverable_agent_tool",
                      "/log_verification"):
        assert forbidden not in paths

    expected = len(diagnostic._SCHEMA_QUERIES) + len(diagnostic._SCHEMA_SHELL_COMMANDS)
    assert len(rec.calls) == expected


def test_schema_report_leads_do_not_include_specialist_actions():
    # The mode targets the mechanism + BASE tools only; it must not bake in specialist action names.
    assert set(diagnostic._SCHEMA_TOOL_LEADS) == {
        "unlock_discoverable_agent_tool",
        "call_discoverable_agent_tool",
        "log_verification",
    }
    haystack = " ".join(diagnostic._SCHEMA_QUERIES + diagnostic._SCHEMA_SHELL_COMMANDS).lower()
    # A few representative specialist-ish names that must NOT be pre-enumerated anywhere in this mode.
    for leaked in ("file_dispute", "open_savings", "close_account", "issue_refund", "order_card"):
        assert leaked not in haystack
