"""Tracer routes backing the unified dashboard's native Eval tab + model-I/O expand, and a smoke
that the dashboard template renders the tab bar / native-eval / model-modal structure."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from appsci_bench.tracer.server import TracerServer


def _client(runs_dir):
    return TestClient(TracerServer(runs_dir=runs_dir, port=0)._create_app())


def _seed_eval(tmp_path, trace="tr"):
    ev = tmp_path / trace / "eval"
    (ev / "conversations").mkdir(parents=True)
    ev.joinpath("report.json").write_text(json.dumps({
        "task_id": "airline", "final_score": 0.5,
        "views": {"per_metric": [{"objective": "task_success", "score": 0.5, "weight": 1.0, "positive": True}],
                  "per_data_point": [{"data_point_id": "d1", "score": 0.5}], "matrix": {}},
        "results": [{"data_point_id": "d1", "objective": "task_success", "repeat_index": 0,
                     "score": 0.5, "metadata": {"reasoning": "ok"}, "error": None}],
        "warnings": [],
    }))
    ev.joinpath("conversations", "d1__r0.json").write_text(json.dumps({
        "data_point_id": "d1", "repeat_index": 0, "error": None, "final_state": {"env_state_hash": "abc"},
        "conversation": [{"query": {"text": "hi"}, "response": {"text": "hello", "trace": [
            {"kind": "assistant", "content": "thinking", "tool_calls": [{"name": "get", "arguments": {}}]}]}}],
    }))
    return trace


def test_eval_report_json_and_conversations_and_404(tmp_path):
    trace = _seed_eval(tmp_path)
    c = _client(tmp_path)
    rj = c.get(f"/api/eval/{trace}/report_json")
    assert rj.status_code == 200 and rj.json()["final_score"] == 0.5
    convs = c.get(f"/api/eval/{trace}/conversations").json()["conversations"]
    assert len(convs) == 1 and convs[0]["conversation"][0]["response"]["trace"][0]["kind"] == "assistant"
    assert c.get("/api/eval/missing/report_json").status_code == 404
    assert c.get("/api/eval/missing/conversations").json() == {"conversations": []}


def test_model_io_route_serves_full_call_and_404(tmp_path):
    trace = "tr"
    (tmp_path / trace).mkdir(parents=True)
    (tmp_path / trace / "model_calls-appsci_agent.jsonl").write_text(json.dumps({
        "call_id": "c1", "source": "appsci_agent", "caller": "agent_loop",
        "messages": [{"role": "user", "content": "full prompt text"}], "content": "full completion"}) + "\n")
    c = _client(tmp_path)
    ok = c.get(f"/api/model_io/{trace}?ref=c1")
    assert ok.status_code == 200 and ok.json()["messages"][0]["content"] == "full prompt text"
    assert c.get(f"/api/model_io/{trace}?ref=nope").status_code == 404


def test_model_io_route_serves_a_failed_calls_record_unchanged(tmp_path):
    """A failed call's record is a legitimately thinner shape and must survive the route intact.

    Since `model_call` started closing the span on the error path, the store contains records with
    `content` = "<model call failed: ...>" and NO usage, tool_calls or cost -- a shape that did not
    exist before. It must not be dropped, coerced or 500'd on the way to the modal: the prompt inside
    it is the only surviving copy of that conversation, and for a mini-swe step it is the conversation
    the sub-agent view resolves from.

    Nulls are asserted as nulls on purpose. Zero-filling `usage` here would make a rate-limited call
    look like a real one that used no tokens, and the cost rollup would silently price it at $0.
    """
    trace = "tr"
    (tmp_path / trace).mkdir(parents=True)
    (tmp_path / trace / "model_calls-mini_swe_agent.jsonl").write_text(json.dumps({
        "call_id": "dead1", "source": "mini_swe_agent", "caller": "mini_swe", "name": "mini-swe",
        "model": "claude-opus-4-8",
        "messages": [{"role": "system", "content": "you are mini-swe"},
                     {"role": "user", "content": "explore the environment"}],
        "content": "<model call failed: RateLimitError: gateway per-user rate limit exceeded>",
        "tool_calls": [], "usage": None, "cost_usd": None, "duration_ms": 12.5,
        "span_id": "dead1", "parent_span_id": "p1", "timestamp": "2026-08-07T18:34:29+00:00",
        "context": None,
    }) + "\n")
    body = _client(tmp_path).get(f"/api/model_io/{trace}?ref=dead1")
    assert body.status_code == 200
    call = body.json()
    assert call["usage"] is None and call["cost_usd"] is None
    assert call["tool_calls"] == []
    assert "rate limit" in call["content"]
    # The prompt is the payload that matters and the reason the record is written at all.
    assert [m["role"] for m in call["messages"]] == ["system", "user"]
    assert call["messages"][1]["content"] == "explore the environment"


def test_model_io_route_serves_a_tool_call_only_completion(tmp_path):
    """`content` is null whenever the assistant turn was pure tool calls -- common in the build loop
    (2 of the 9 agent_loop calls in run airline_rebooking_20260806_171519). The route must pass the
    null through so the modal can render its own '(no text)' rather than the view erroring on it."""
    trace = "tr"
    (tmp_path / trace).mkdir(parents=True)
    (tmp_path / trace / "model_calls-appsci_agent.jsonl").write_text(json.dumps({
        "call_id": "tc1", "source": "appsci_agent", "caller": "agent_loop", "content": None,
        "messages": [{"role": "user", "content": "build it"}],
        "tool_calls": [{"name": "mini_swe_agent", "arguments": {"task": "write server.py"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }) + "\n")
    call = _client(tmp_path).get(f"/api/model_io/{trace}?ref=tc1").json()
    assert call["content"] is None
    assert call["tool_calls"][0]["name"] == "mini_swe_agent"


def test_dashboard_model_modal_handles_an_unresolvable_ref(tmp_path):
    """The template must have a path for a ref the store cannot resolve.

    Every trace recorded before `model_call` existed still contains dangling refs -- 50 of 96 in
    runs/airline_rebooking_20260807_091549 -- so the modal cannot assume the fetch succeeds. It used to
    substitute a bare '(model call not found)' with empty messages, which rendered as a blank box and
    read as "the full I/O view is broken". `openModelIo` now takes the row it was clicked from so the
    modal can name the call and show the request-side preview the event still carries.

    A template smoke, not a behaviour test: the behaviour is Alpine's and only a browser can prove it.
    This pins the wiring that a refactor would quietly drop.
    """
    (tmp_path / "tr").mkdir(parents=True)
    html = _client(tmp_path).get("/?trace_id=tr").text
    assert "unrecordedCall(ref, node)" in html
    assert "openModelIo(row.node.ref, row.node)" in html, "the build row must pass its node"
    assert "modelModal.call.unrecorded" in html, "the modal needs the explanatory branch"
    assert "mio-warn" in html
    # Alpine is served from our own origin, pinned -- not a floating version from a third-party CDN
    # fetched into a page that carries an auth cookie.
    assert "unpkg.com" not in html
    assert "/static/alpine-3.15.12.min.js" in html


def test_dashboard_renders_unified_tabs(tmp_path):
    (tmp_path / "tr").mkdir(parents=True)
    html = _client(tmp_path).get("/?trace_id=tr").text
    for marker in ('class="tabs"', "switchTab('eval')", "eval-native", "loadEvalData",
                   "openModelIo", "modelModal", "INPUT MESSAGES"):
        assert marker in html, marker


def test_agent_fs_routes_list_read_and_404(tmp_path):
    trace = "tr"
    fs = tmp_path / trace / "agent_fs" / "workspace"
    fs.mkdir(parents=True)
    fs.joinpath("report.txt").write_text("exploration findings")
    c = _client(tmp_path)
    listing = c.get(f"/api/agent_fs/{trace}").json()
    assert listing["exists"] is True
    assert any(f["path"].endswith("workspace/report.txt") for f in listing["files"])
    got = c.get(f"/api/agent_fs/{trace}/file", params={"path": "workspace/report.txt"})
    assert got.status_code == 200 and got.json()["content"] == "exploration findings"
    # missing snapshot -> exists False (not an error); bad path -> 404
    assert c.get("/api/agent_fs/missing").json()["exists"] is False
    assert c.get(f"/api/agent_fs/{trace}/file", params={"path": "nope.txt"}).status_code == 404


def test_dashboard_renders_agent_files_tab(tmp_path):
    (tmp_path / "tr").mkdir(parents=True)
    html = _client(tmp_path).get("/?trace_id=tr").text
    for marker in ("switchTab('agentfs')", "loadAgentFs", "viewAgentFsFile", "activeTab==='agentfs'"):
        assert marker in html, marker


# --- the elicitation diagnostic in the Eval tab -------------------------------------------------
#
# The number is on the report, so a run's own trace is the first place anyone would look for it. It sits
# BELOW the objectives table and outside it: it measures the build conversation rather than the artifact,
# and anything inside that table reads as part of FINAL SCORE.

def _dashboard(tmp_path, trace):
    return _client(tmp_path).get(f"/?trace_id={trace}").text


def test_the_report_json_route_carries_the_elicitation_block(tmp_path):
    """The route serves the report as written, so the diagnostic needs no separate endpoint -- but that
    is worth pinning, because the tab reads this and nothing else."""
    trace = _seed_eval(tmp_path)
    report = json.loads((tmp_path / trace / "eval" / "report.json").read_text())
    report["elicitation"] = {
        "task_id": "airline", "status": "scored", "total_items": 3, "judged_items": 3,
        "elicited_rate": 2 / 3, "asked_not_elicited_rate": 1 / 3, "missed_rate": 0.0,
        "error_items": 0, "num_exchanges": 4, "warnings": [],
        "items": [{"item_id": "a", "verdict": "elicited"}, {"item_id": "b", "verdict": "elicited"},
                  {"item_id": "c", "verdict": "asked_not_elicited"}],
    }
    (tmp_path / trace / "eval" / "report.json").write_text(json.dumps(report))
    served = _client(tmp_path).get(f"/api/eval/{trace}/report_json").json()
    assert served["elicitation"]["status"] == "scored"
    assert len(served["elicitation"]["items"]) == 3


def test_the_eval_tab_gives_the_diagnostic_its_own_section(tmp_path):
    """Its own section, not a row in the objectives table: it scores the BUILD conversation rather than
    the artifact, and anything rendered among the objectives eventually gets read as one of them."""
    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    assert '<section class="eval-dash elic"' in page
    table = page.split('<table class="metrics">')[1].split("</table>")[0]
    assert "elic" not in table, "the diagnostic leaked into the objectives table"
    assert "not part of the score above" in page


def test_the_eval_tab_mirrors_the_generated_report_section(tmp_path):
    """Two views of one run must agree. The section here carries the same four things the report's does:
    headline rate, the three-way breakdown, the counts, and a folded per-item table."""
    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    sec = page.split('<section class="eval-dash elic"')[1].split("</section>")[0]
    assert "ELICITED RATE" in sec
    for label in ("elicited", "asked, not elicited", "never asked"):
        assert label in sec, f"the breakdown omits {label!r}"
    assert "elicitationSub()" in sec, "the denominator is not shown beside the rate"
    assert '<details class="elic-fold"' in sec


def test_the_per_item_table_is_folded_and_has_four_columns(tmp_path):
    """Folded because the judge's reasoning is long enough to push the rest of the run off the screen,
    and the rates are what anyone opened the section for."""
    import re

    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    sec = page.split('<section class="eval-dash elic"')[1].split("</section>")[0]
    fold = sec.split('<details class="elic-fold"')[1]
    assert " open" not in fold[:40], "the fold ships expanded"
    headers = re.findall(r"<th>(.*?)</th>", sec, re.DOTALL)
    assert len(headers) == 4, headers
    assert "rubric item" in headers[0] and "the fact" in headers[0]
    assert "verdict" in headers[1] and "evidence" in headers[2] and "reasoning" in headers[3]


def test_each_row_shows_the_item_its_fact_and_its_verdict(tmp_path):
    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    sec = page.split('<section class="eval-dash elic"')[1].split("</section>")[0]
    for binding in ("it.item_id", "it.fact", "it.evidence", "it.error || it.reasoning"):
        assert binding in sec, f"the table does not bind {binding}"
    assert "verdictLabel(it.verdict)" in sec and "verdictBand(it.verdict)" in sec


def test_the_four_verdicts_read_as_words_not_as_enum_values(tmp_path):
    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    body = page.split("verdictLabel(v) {")[1].split("},")[0]
    for phrase in ("asked, not elicited", "never asked", "not judged"):
        assert phrase in body, f"{phrase!r} is not spelled out"
    # An unjudged item must never be relabelled as one the agent failed to ask about.
    assert "'never asked'" in body and "missed:" in body


def test_a_status_that_measured_nothing_shows_a_word_instead_of_a_rate(tmp_path):
    """`no_rubric` and `unmeasurable` are not a rate of zero. `no_exchanges` IS one, and must still read
    as a number -- that distinction is the reason the four statuses exist."""
    import re

    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    sec = page.split('<section class="eval-dash elic"')[1].split("</section>")[0]
    assert 'x-show="elicitationRate() === null"' in sec, "no word shown where there is no rate"
    assert 'x-show="elicitationRate() !== null"' in sec, "no number shown where there is one"
    # Join the adjacent string fragments first: these messages are wrapped across `'...' + '...'` in the
    # source, so searching the raw text would test the line breaks rather than what the user reads.
    why = re.sub(r"'\s*\+\s*'", "", page.split("elicitationWhy() {")[1].split("},")[0])
    assert "not a zero" in why, "the unmeasurable case does not say it is not a zero"
    assert "a real zero" in why, "the no-exchanges case does not say it IS a zero"
    assert "no_rubric" in why


def test_the_section_hides_entirely_on_a_report_without_the_diagnostic(tmp_path):
    # Older reports predate the field; an empty section would read as a broken diagnostic.
    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    assert 'x-show="elicitation()"' in page


def test_warnings_on_the_diagnostic_are_rendered_in_its_own_section(tmp_path):
    """A note saying items went unjudged changes how the rate should be read, so it belongs beside the
    rate rather than in the objectives' warning list."""
    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    sec = page.split('<section class="eval-dash elic"')[1].split("</section>")[0]
    assert "elicitation().warnings" in sec


def test_the_section_is_styled_as_a_different_kind_of_thing(tmp_path):
    css = _client(tmp_path).get("/static/styles.css").text
    assert ".eval-dash.elic" in css
    block = css.split(".eval-dash.elic")[1][:200]
    assert "dashed" in block, "the section is not visually distinct from the metrics panel"
    assert "table.elic-items" in css
    assert ".elic-fold > summary" in css
    # The fold names its action and changes it when open, so it reads as a control.
    assert "Show " in css and "Hide " in css


# --- which eval-phase model calls the tab is willing to file -------------------------------------
#
# The tab files each eval-side model call under the case that produced it. The elicitation judge has no
# case -- it scores the run's one build conversation -- so it exposed two separate gaps: a catch-all list
# that showed its calls as raw prompt/completion blocks, and a source the tab did not recognise as
# eval-phase at all. Both are guarded here because both were invisible until someone opened the tab.

def test_the_eval_tab_has_no_raw_call_fallback_list(tmp_path):
    """A call with no case tag renders nowhere, deliberately.

    The tab used to end in a catch-all list for calls carrying no case, written for runs recorded before
    case tagging existed. The elicitation judge then landed in it -- correctly untagged, wrongly labelled
    as a legacy leftover -- and showed 12 raw call blocks whose evidence and reasoning the elicitation
    section already presents per rubric item. The list is gone rather than relabelled: it would have been
    a second copy of content the reader already has.
    """
    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    # Each of the three things the deleted block actually was, named directly. Two earlier versions of
    # this guard were wrong in opposite directions: one searched the page ABOVE the elicitation section
    # while the list sat at the very bottom below the per-case transcripts, so it could not fire at all;
    # the other banned the word "untagged" anywhere on the page, which a comment explaining the removal
    # then tripped. Testing for the markup, the helper and the heading catches a reintroduction without
    # forbidding anyone from writing about it.
    assert "eval-mcalls" not in page, "the fallback section's own class is back"
    assert "evalUntaggedCalls" not in page, "the fallback's helper is back"
    assert "MODEL I/O" not in page, "an uppercase model-I/O heading is the fallback's own"


def test_every_model_call_source_the_eval_phase_declares_is_known_to_the_tab(tmp_path):
    """The tab must recognise each source eval-phase code tags its model calls with.

    A source it does not recognise is not merely missing from the Eval tab: the call falls through to the
    build timeline and renders as a stray top-level row after the last build turn. That has now happened
    twice, to the eval-time artifact user sim and then to the elicitation judge, because tagging a new
    source and teaching the tab about it are edits in two different layers with nothing joining them.

    Derived from the source of truth rather than a hardcoded list, so the next one is covered on the day
    it is written: eval-phase code declares its override as a module-level `_MODEL_CALL_SOURCE`.
    """
    import pathlib
    import re

    import appsci_bench.eval as eval_pkg
    from shared.observability.events import Source

    root = pathlib.Path(eval_pkg.__file__).resolve().parent
    declared = {
        str(getattr(Source, m.group(1)))
        for path in root.rglob("*.py")
        for m in re.finditer(r"^_MODEL_CALL_SOURCE\s*=\s*Source\.([A-Z_]+)", path.read_text(), re.M)
    }
    assert declared, "found no eval-phase model-call source overrides to check"

    page = _dashboard(tmp_path, _seed_eval(tmp_path))
    known = page.split("const isEval")[1].split(";")[0]
    missing = sorted(s for s in declared if f"'{s}'" not in known)
    assert not missing, f"the Eval tab does not classify these as eval-phase: {missing}"
