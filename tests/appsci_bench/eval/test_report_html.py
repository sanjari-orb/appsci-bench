"""Unit tests for the single-file HTML eval report (report_html.render_html + persistence)."""
from __future__ import annotations

from appsci_bench.eval.datatypes import (
    ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, DataPointMetricResult,
    EvalReport, GenerationRecord,
)
from appsci_bench.eval.report_html import render_html
from appsci_bench.eval.store import TranscriptStore
from appsci_bench.eval import eval_system


def _build(tmp_path):
    store = TranscriptStore(tmp_path)
    # Case 1: multi-turn, a structured response json, and captured env state.
    store.save(GenerationRecord(
        "case_alpha", 0,
        conversation=[
            ConversationTurn(ArtifactAgentQuery(text="I want to cancel EHGLP3"),
                             ArtifactAgentOutput(text="I'm sorry, that cannot be cancelled.")),
            ConversationTurn(ArtifactAgentQuery(text="but a rep approved it"),
                             ArtifactAgentOutput(text="I must follow policy.",
                                                 json='{"actions": []}')),
        ],
        final_state={"env_state_hash": "abc123def"}))
    # Case 2: an errored generation unit.
    store.save(GenerationRecord("case_beta", 0, error="TimeoutError: query timed out"))

    results = [
        DataPointMetricResult("case_alpha", "task_success", 0, 1.0,
                              metadata={"db_ok": True, "communicate_ok": True,
                                        "agent_state_hash": "abc123def"}),
        DataPointMetricResult("case_alpha", "policy_compliance", 0, 0.9,
                              metadata={"reasoning": "Agent correctly refused and held firm."}),
        DataPointMetricResult("case_beta", "task_success", 0, 0.0,
                              error="generation failed"),
    ]
    views = {
        "per_metric": [
            {"objective": "task_success", "score": 0.5, "weight": 1.0, "positive": True},
            {"objective": "policy_compliance", "score": 0.9, "weight": 1.0, "positive": True},
        ],
        "per_data_point": [
            {"data_point_id": "case_alpha", "score": 0.95},
            {"data_point_id": "case_beta", "score": 0.0},
        ],
        "matrix": {},
    }
    report = EvalReport(task_id="airline_rebooking", results=results, views=views,
                        final_score=0.475, run_id="run_test_123", store_root=str(store.root))
    return report, store


def test_render_html_contains_scores_traces_and_reasoning(tmp_path):
    report, store = _build(tmp_path)
    html = render_html(report, store)

    # header + dashboard
    assert "airline_rebooking" in html and "run_test_123" in html and "0.4750" in html
    # both cases + their per-data-point scores
    assert "case_alpha" in html and "case_beta" in html
    # per-objective scores + judge reasoning + env state + turn text + structured output
    assert "task_success" in html and "policy_compliance" in html and "0.9000" in html
    assert "Agent correctly refused and held firm." in html
    assert "abc123def" in html                       # env_state_hash surfaced
    assert "I want to cancel EHGLP3" in html          # a conversation turn
    assert "TimeoutError: query timed out" in html    # errored case surfaced


def test_render_html_is_self_contained(tmp_path):
    report, store = _build(tmp_path)
    html = render_html(report, store).lower()
    assert "<script src" not in html and "<link" not in html
    assert 'href="http' not in html and "cdn" not in html


def test_render_html_escapes_injected_markup(tmp_path):
    store = TranscriptStore(tmp_path)
    store.save(GenerationRecord(
        "xss", 0,
        conversation=[ConversationTurn(ArtifactAgentQuery(text="hi"),
                                       ArtifactAgentOutput(text="<script>alert(1)</script>"))]))
    report = EvalReport(task_id="t", results=[
        DataPointMetricResult("xss", "m", 0, 1.0, metadata={"reasoning": "<img src=x onerror=1>"})],
        views={"per_data_point": [{"data_point_id": "xss", "score": 1.0}], "per_metric": []},
        final_score=1.0, run_id="r", store_root=str(store.root))
    html = render_html(report, store)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=1>" not in html


def test_write_report_artifacts_persists_json_and_html(tmp_path):
    report, store = _build(tmp_path)
    eval_system._write_report_artifacts(report, store)
    assert (store.root / "report.json").is_file()
    html = (store.root / "report.html").read_text()
    assert "airline_rebooking" in html and "case_alpha" in html


def test_eval_report_dict_roundtrip(tmp_path):
    report, _ = _build(tmp_path)
    back = EvalReport.from_dict(report.to_dict())
    assert back.task_id == report.task_id and back.final_score == report.final_score
    assert len(back.results) == len(report.results)
    assert back.results[0].metadata == report.results[0].metadata


# --- viewer v2: tabs, task index, headers, LLM trace ------------------------------------------

from appsci_bench.eval.report_html import render_html_multi


def _report_with_trace(tmp_path, task="airline_rebooking"):
    store = TranscriptStore(tmp_path)
    trace = [
        {"kind": "assistant", "step": 1, "content": "Verify eligibility first.",
         "tool_calls": [{"name": "get_user_details", "arguments": {"user_id": "emma_kim_9957"}}]},
        {"kind": "tool_result", "step": 1, "name": "get_user_details", "result": {"membership": "gold"}},
        {"kind": "final", "step": 2, "content": "Sorry, that can't be cancelled."},
    ]
    store.save(GenerationRecord(
        f"{task}_0", 0,
        conversation=[ConversationTurn(ArtifactAgentQuery(text="cancel EHGLP3"),
                                       ArtifactAgentOutput(text="Sorry.", trace=trace))],
        final_state={"env_state_hash": "abc123"}))
    store.save(GenerationRecord(
        f"{task}_1", 0,
        conversation=[ConversationTurn(ArtifactAgentQuery(text="hello"),
                                       ArtifactAgentOutput(text="hi"))]))  # no trace on this turn
    report = EvalReport(
        task_id=task,
        results=[DataPointMetricResult(f"{task}_0", "task_success", 0, 1.0, metadata={"db_ok": True}),
                 DataPointMetricResult(f"{task}_1", "task_success", 0, 0.0)],
        views={"per_metric": [{"objective": "task_success", "score": 0.5, "weight": 1.0, "positive": True}],
               "per_data_point": [{"data_point_id": f"{task}_0", "score": 1.0},
                                  {"data_point_id": f"{task}_1", "score": 0.0}]},
        final_score=0.5, run_id=f"run_{task}", store_root=str(store.root))
    return report, store


def test_headers_and_task_index(tmp_path):
    report, store = _report_with_trace(tmp_path)
    html = render_html(report, store)
    assert "Aggregated metrics" in html and "Per-task transcripts" in html
    assert "#0" in html and "#1" in html                     # numbered cases


def test_chips_keep_white_text_not_scoped_globally(tmp_path):
    # Regression: the .good/.warn/.bad text-color rules must be scoped to .counts so chips
    # (class "chip good") don't get green text on a green background.
    report, store = _report_with_trace(tmp_path)
    html = render_html(report, store)
    assert ".counts .good{color" in html
    assert ".chip{" in html and "color:#fff" in html


def test_llm_trace_panel_rendered_and_escaped(tmp_path):
    report, store = _report_with_trace(tmp_path)
    html = render_html(report, store)
    assert "llm trace" in html                               # the trace <details> label
    assert "get_user_details" in html                        # tool call name
    assert "emma_kim_9957" in html                           # tool call arguments
    assert "Verify eligibility first." in html               # assistant reasoning
    assert "membership" in html                              # tool result


def test_multi_task_tabs(tmp_path):
    r1, s1 = _report_with_trace(tmp_path / "a", task="airline_rebooking")
    r2, s2 = _report_with_trace(tmp_path / "b", task="order_to_cash")
    html = render_html_multi([(r1, s1), (r2, s2)])
    assert html.count('class="tab') >= 2
    assert "airline_rebooking" in html and "order_to_cash" in html
    assert 'class="task-panel' in html
    low = html.lower()
    assert "<script src" not in low and "<link" not in low and "cdn" not in low


def test_output_trace_roundtrips_and_excluded_from_sim_view():
    out = ArtifactAgentOutput(text="x", trace=[{"kind": "final", "content": "done"}])
    assert ArtifactAgentOutput.from_dict(out.to_dict()).trace == out.trace
    assert "trace" not in out.sim_view()                     # debug-only, hidden from the user-sim


def test_store_persists_trace(tmp_path):
    store = TranscriptStore(tmp_path)
    tr = [{"kind": "final", "content": "done"}]
    path = store.save(GenerationRecord("d", 0, conversation=[
        ConversationTurn(ArtifactAgentQuery(text="q"), ArtifactAgentOutput(text="a", trace=tr))]))
    import json as _j
    doc = _j.loads(path.read_text())
    assert doc["conversation"][0]["response"]["trace"] == tr


# --- the elicitation diagnostic section -------------------------------------------------------
#
# The section has one job beyond showing numbers: it must not be mistakable for a scored objective, and
# it must render the four statuses distinguishably. A bare "0.00" for "we could not read the log" and a
# real 0.00 for "the agent asked nothing" are opposite findings, so the unscored statuses print a word
# instead of a number.

def _el(**overrides):
    from appsci_bench.eval.datatypes import ElicitationResult, ItemResult
    base = {
        "task_id": "demo_task", "status": "scored", "total_items": 3, "judged_items": 3,
        "elicited_rate": 1 / 3, "asked_not_elicited_rate": 1 / 3, "missed_rate": 1 / 3,
        "num_exchanges": 9,
        "items": [
            ItemResult(item_id="fault_taxonomy", verdict="elicited",
                       fact="The six fault_type values, spelled exactly as the finance team spells them.",
                       evidence="sim > all six values", reasoning="listed in full"),
            ItemResult(item_id="price_tolerance", verdict="asked_not_elicited",
                       fact="The dollar-impact threshold below which a price difference is not a fault.",
                       evidence="sim > I would have to check", reasoning="declined"),
            ItemResult(item_id="dedup_window", verdict="missed",
                       fact="How a duplicate invoice is identified and within what window.",
                       reasoning="never came up"),
        ]}
    base.update(overrides)
    return ElicitationResult(**base)


def _render(el):
    return render_html(EvalReport(task_id="demo_task", final_score=0.8, elicitation=el))


def test_the_section_shows_the_headline_rate_and_every_item():
    html = _render(_el())
    assert "Elicitation diagnostic" in html
    assert "0.3333" in html                      # the headline rate
    for item_id in ("fault_taxonomy", "price_tolerance", "dedup_window"):
        assert item_id in html
    for label in ("elicited", "asked, not elicited", "never asked"):
        assert label in html
    assert "3 rubric items" in html and "9 exchanges" in html


def test_the_section_says_it_is_not_part_of_the_score():
    # The one line that stops a reader averaging it with the objectives.
    html = _render(_el())
    assert "not part of the score" in html


def test_the_evidence_and_reasoning_are_shown_for_the_audit_trail():
    html = _render(_el())
    assert "all six values" in html
    assert "I would have to check" in html
    assert "declined" in html


def test_unmeasurable_shows_the_word_and_no_rate():
    # The failure this guards: "unmeasurable" rendered as 0.00 reads as "the agent asked nothing",
    # which is a different run with a real score.
    html = _render(_el(status="unmeasurable", elicited_rate=None,
                       asked_not_elicited_rate=None, missed_rate=None, items=[],
                       warnings=["the build conversation could not be read"]))
    assert "unmeasurable" in html.lower()
    section = html.split("Elicitation diagnostic")[1].split("Per-task transcripts")[0]
    assert "0.00" not in section and "0.0000" not in section
    assert "0%" not in section


def test_no_rubric_shows_the_word_and_no_rate():
    html = _render(_el(status="no_rubric", elicited_rate=None, asked_not_elicited_rate=None,
                       missed_rate=None, total_items=0, items=[]))
    section = html.split("Elicitation diagnostic")[1].split("Per-task transcripts")[0]
    assert "no rubric" in section.lower()
    assert "0.0000" not in section


def test_no_exchanges_shows_a_real_zero():
    # The other side of the same coin: this one IS a zero, and must read as one.
    from appsci_bench.eval.datatypes import ItemResult
    html = _render(_el(status="no_exchanges", elicited_rate=0.0, asked_not_elicited_rate=0.0,
                       missed_rate=1.0, num_exchanges=0,
                       items=[ItemResult(item_id="a", verdict="missed")]))
    section = html.split("Elicitation diagnostic")[1].split("Per-task transcripts")[0]
    assert "0.0000" in section
    assert "100% never asked" in section
    assert "no conversation" in section.lower()


def test_the_four_statuses_render_differently_from_each_other():
    sections = {}
    for status, rates in (("scored", (0.5, 0.25, 0.25)), ("no_exchanges", (0.0, 0.0, 1.0)),
                          ("no_rubric", (None, None, None)), ("unmeasurable", (None, None, None))):
        html = _render(_el(status=status, elicited_rate=rates[0],
                           asked_not_elicited_rate=rates[1], missed_rate=rates[2]))
        sections[status] = html.split("Elicitation diagnostic")[1].split("Per-task")[0]
    assert len(set(sections.values())) == 4


def test_items_not_judged_are_shown_as_such_rather_than_as_misses():
    from appsci_bench.eval.datatypes import ItemResult
    html = _render(_el(error_items=1, items=[
        ItemResult(item_id="broken", verdict="error", error="JudgeParseError('garbled')")]))
    row = html.split('class="elic-items"')[1].split("<tbody>")[1].split("</tbody>")[0]
    assert "not judged" in row
    assert "garbled" in row                # the error text is surfaced, not swallowed
    assert "never asked" not in row        # NOT relabelled as a miss
    assert "1 not judged" in html          # and counted separately in the summary line


def test_the_denominator_is_shown_whenever_items_went_unjudged():
    """The rates are over the judged items only, so the denominator varies between runs -- a rate over
    three judged items is not the same claim as one over fifty. It has to be impossible to read the
    number without it."""
    from appsci_bench.eval.datatypes import ItemResult
    html = _render(_el(total_items=20, judged_items=17, error_items=3,
                       elicited_rate=0.7059, asked_not_elicited_rate=0.1765, missed_rate=0.1176,
                       items=[ItemResult(item_id="a", verdict="elicited")]))
    section = html.split("Elicitation diagnostic")[1].split("Per-task")[0]
    assert "17 of 20 items judged" in section
    assert "excluded" in section and "not counted against the agent" in section


def test_no_denominator_fuss_when_every_item_was_judged():
    section = _render(_el()).split("Elicitation diagnostic")[1].split("Per-task")[0]
    assert "3 rubric items" in section
    assert "judged" not in section.replace("items judged", "")    # no needless caveat


def test_warnings_on_the_diagnostic_are_rendered():
    html = _render(_el(warnings=["2 of 3 items could not be judged"]))
    assert "2 of 3 items could not be judged" in html


def test_a_report_without_the_diagnostic_omits_the_section_entirely():
    html = render_html(EvalReport(task_id="demo_task", final_score=0.8))
    assert "Elicitation diagnostic" not in html
    assert "ELICITED RATE" not in html


def test_evidence_containing_html_is_escaped():
    """The evidence is model output quoting agent text -- untrusted twice over. A rubric item id, an
    error string and the judge's reasoning are all model- or author-supplied too."""
    from appsci_bench.eval.datatypes import ItemResult
    html = _render(_el(items=[ItemResult(
        item_id="xss_item",
        verdict="elicited",
        evidence='<script>alert("evidence")</script> & <img src=x onerror=1>',
        reasoning="<b>bold</b> reasoning")]))
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html
    assert "<img src=x" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_a_hostile_item_id_error_and_task_id_are_escaped():
    from appsci_bench.eval.datatypes import ItemResult
    html = _render(_el(task_id="<i>task</i>", items=[ItemResult(
        item_id="<b>id</b>", verdict="error", error="<script>boom</script>")]))
    for raw in ("<i>task</i>", "<b>id</b>", "<script>boom"):
        assert raw not in html
    assert "&lt;script&gt;boom" in html


def test_an_unknown_status_or_verdict_does_not_break_rendering():
    # Forward compatibility: a report written by a newer version must still render.
    from appsci_bench.eval.datatypes import ItemResult
    html = _render(_el(status="some_future_status", elicited_rate=None,
                       asked_not_elicited_rate=None, missed_rate=None,
                       items=[ItemResult(item_id="a", verdict="some_future_verdict")]))
    assert "some_future_status" in html and "some_future_verdict" in html


def test_elicitation_json_is_written_alongside_the_report(tmp_path):
    import json

    from appsci_bench.eval.store import TranscriptStore

    store = TranscriptStore(tmp_path)
    report = EvalReport(task_id="demo_task", final_score=0.8, elicitation=_el())
    eval_system._write_report_artifacts(report, store)
    path = store.root / "elicitation.json"
    assert path.is_file()
    # Standalone and complete, so a consumer wanting only the diagnostic need not parse the report.
    payload = json.loads(path.read_text())
    assert payload["status"] == "scored"
    assert [i["item_id"] for i in payload["items"]] == [
        "fault_taxonomy", "price_tolerance", "dedup_window"]


def test_no_elicitation_json_when_there_is_no_diagnostic(tmp_path):
    from appsci_bench.eval.store import TranscriptStore

    store = TranscriptStore(tmp_path)
    eval_system._write_report_artifacts(EvalReport(task_id="demo_task", final_score=0.8), store)
    assert (store.root / "report.json").is_file()
    assert not (store.root / "elicitation.json").exists()


# --- the per-item table is folded away by default ---------------------------------------------
#
# A fifty-item rubric with a paragraph of judge reasoning per item pushes the whole rest of the report off
# the screen, and the rates -- the reason anyone opened the section -- stop being the first thing read. So
# the table starts closed. What stays out of the fold is exactly what a reader wants at a glance.

def _section(html: str) -> str:
    return html.split("Elicitation diagnostic")[1].split("Per-task transcripts")[0]


def _outside_the_fold(html: str) -> str:
    """The section with the fold's contents removed -- what a reader sees before clicking anything."""
    section = _section(html)
    if '<details class="elic-fold"' not in section:
        return section
    before, rest = section.split('<details class="elic-fold"', 1)
    return before + rest.split("</details>", 1)[1]


def test_the_item_table_starts_collapsed():
    html = _render(_el())
    assert '<details class="elic-fold"' in html
    # No `open` attribute: closed on first paint, with no script needed to get there.
    fold = _section(html).split('<details class="elic-fold"')[1][:40]
    assert "open" not in fold, f"the fold ships expanded: {fold!r}"


def test_the_rate_and_the_breakdown_stay_outside_the_fold():
    visible = _outside_the_fold(_render(_el()))
    assert "0.3333" in visible and "ELICITED RATE" in visible
    assert "33% elicited" in visible
    assert "33% asked, not elicited" in visible
    assert "33% never asked" in visible
    assert "not part of the score above" in visible


def test_the_items_and_their_reasoning_are_inside_the_fold():
    html = _render(_el())
    visible = _outside_the_fold(html)
    for item_id in ("fault_taxonomy", "price_tolerance", "dedup_window"):
        assert item_id not in visible, f"{item_id} is outside the fold"
        assert item_id in _section(html)
    # The judge's prose is what made the section long, so it must be the part that folds.
    assert "listed in full" not in visible
    assert "I would have to check" not in visible


def test_the_fold_says_how_many_items_it_holds():
    # A closed fold that does not say what is behind it invites nobody to open it.
    summary = _section(_render(_el())).split("<summary>")[1].split("</summary>")[0]
    assert "3 per-item verdicts" in summary
    assert "evidence" in summary


def test_the_fold_reads_as_a_control_and_names_the_action():
    """A closed section nobody knows to click is the same as a section that is not there. The affordance is
    CSS-only on purpose -- no script, so it survives the report being saved or emailed as a file."""
    css = _render(_el())
    # The verb, and the fact that it changes when the fold opens.
    assert 'Show ' in css and 'Hide ' in css
    assert '.elic-fold[open]>summary::before' in css
    # Control-like styling rather than heading-like: something to click, a hover state, a focus ring.
    for rule in ("cursor:pointer", "border:1px solid", "background:", ":hover", ":focus-visible"):
        assert rule in css.split(".elic-fold>summary")[1][:400] or rule in css, f"missing {rule}"


def test_the_fold_is_reachable_by_keyboard():
    """`<details>`/`<summary>` is focusable and toggles on Enter natively. Pinning that the marker is
    hidden by CSS rather than by replacing the element with a div, which would lose that for free."""
    html = _render(_el())
    assert "<details" in html and "<summary>" in html
    assert "webkit-details-marker" in html      # hidden, not removed


def test_warnings_stay_outside_the_fold():
    # A note saying items went unjudged changes how the rate is read, so it cannot be hidden behind it.
    visible = _outside_the_fold(_render(_el(warnings=["2 of 3 items could not be judged"])))
    assert "2 of 3 items could not be judged" in visible


def test_the_judged_denominator_stays_outside_the_fold():
    from appsci_bench.eval.datatypes import ItemResult
    visible = _outside_the_fold(_render(_el(
        total_items=20, judged_items=17, error_items=3,
        items=[ItemResult(item_id="a", verdict="elicited")])))
    assert "17 of 20 items judged" in visible


def test_a_status_with_no_items_needs_no_fold():
    html = _render(_el(status="unmeasurable", elicited_rate=None, asked_not_elicited_rate=None,
                       missed_rate=None, items=[]))
    assert '<details class="elic-fold"' not in html


def test_the_fold_does_not_collide_with_the_case_filter_script():
    """The report's filter script expands and collapses `details.case` while typing. This fold must not be
    in that set, or filtering cases would pop the rubric table open."""
    html = _render(_el())
    assert "details.case" in html                      # the script still targets only cases
    assert "details.elic-fold" not in html


def test_the_section_is_well_formed_markup():
    """String assertions pass happily on a page with an unclosed tag, and the fold made the section's
    nesting deeper. So parse it: every element opened inside the diagnostic section has to close, in
    order, or a browser will silently swallow part of the report."""
    from html.parser import HTMLParser

    VOID = {"br", "hr", "img", "input", "meta", "link", "col", "source", "area", "base", "wbr"}

    class _Checker(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.problems = [], []

        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if not self.stack:
                self.problems.append(f"</{tag}> with nothing open")
            elif self.stack[-1] != tag:
                self.problems.append(f"</{tag}> closed while <{self.stack[-1]}> was open")
            else:
                self.stack.pop()

    from appsci_bench.eval.datatypes import ItemResult

    html = _render(_el(
        error_items=1, warnings=["1 of 4 items could not be judged"],
        items=[ItemResult(item_id="a", verdict="elicited", evidence="<b>quoted</b> & unsafe"),
               ItemResult(item_id="b", verdict="error", error="JudgeParseError('x')")]))
    # The <section> element itself, start tag to matching end tag -- slicing on the surrounding headings
    # would leave a half-open tag and the parser would blame the report for the test's cut.
    body = html.split('<section class="dashboard elic">', 1)[1].rsplit("</section>", 1)
    section = '<section class="dashboard elic">' + body[0] + "</section>"
    checker = _Checker()
    checker.feed(section)
    assert checker.problems == [], checker.problems
    assert checker.stack == [], f"unclosed elements: {checker.stack}"


def test_the_fold_nests_the_table_inside_the_details_element():
    # Not merely present in the same section: a table rendered as a sibling of the details would show at
    # all times and the fold would toggle nothing.
    fold = _section(_render(_el())).split('<details class="elic-fold"')[1].split("</details>")[0]
    assert "<table" in fold and "</table>" in fold
    assert "<summary>" in fold


# --- the rubric-item column explains what the verdict was about --------------------------------

def test_each_row_names_the_item_and_states_its_fact():
    """An id alone -- `check_order_first_failure_wins` -- does not tell a reader what the verdict was
    ABOUT, which is the one thing they need in order to judge whether they agree with it. The fact is what
    "this" refers to in "did the agent ask about this?"."""
    rows = _render(_el()).split('class="elic-items"')[1].split("<tbody>")[1].split("</tbody>")[0]
    assert "fault_taxonomy:" in rows                      # id, then its fact
    assert "The six fault_type values, spelled exactly" in rows
    assert "The dollar-impact threshold below which" in rows
    assert "How a duplicate invoice is identified" in rows


def test_the_column_header_says_what_the_column_holds():
    assert "the fact the agent had to obtain by asking" in _render(_el())


def test_a_facts_html_is_escaped_like_everything_else():
    from appsci_bench.eval.datatypes import ItemResult
    html = _render(_el(items=[ItemResult(
        item_id="a", verdict="elicited", fact="<script>alert('fact')</script> & more")]))
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html


def test_a_result_written_before_facts_were_carried_still_renders():
    """Backward compatibility with every report.json already on disk: no fact, so the row shows the id and
    nothing after it rather than an empty label or a crash."""
    from appsci_bench.eval.datatypes import ItemResult
    html = _render(_el(items=[ItemResult(item_id="legacy_item", verdict="missed")]))
    rows = html.split('class="elic-items"')[1].split("<tbody>")[1].split("</tbody>")[0]
    assert "legacy_item:" in rows
    assert "elic-fact" not in rows                        # no empty description block


# --- the optimization diagnostic section -------------------------------------------------------
#
# Same requirement as the elicitation section: it must not be mistakable for a scored objective, and a
# status that counted nothing must print a word rather than a zero. What is specific here is the
# stopping reason -- "saturated with budget left" and "ran out of cycles" are different findings, and
# this section is where a reader sees which one happened.

def _opt(**overrides):
    from appsci_bench.eval.datatypes import OptimizationCall, OptimizationResult
    base = {
        "task_id": "demo_task", "status": "scored", "optimization_enabled": True,
        "calls": 3, "scored_calls": 3, "failed_calls": 0, "cycles_used": 2, "cycles_allowed": 5,
        "configured_cycles_allowed": 5, "saturated": True, "budget_exhausted": False,
        "early_stopped_on_saturation": True, "stop_reason": "saturated",
        "deploys_attempted": 2, "deploys_succeeded": 1, "deploys_failed": 1,
        "changes_kept": 2, "changes_rolled_back": 0, "rollback_redeploys": 0,
        "rollbacks_failed": 0,
        "internal_eval_first": 0.4, "internal_eval_best": 0.55, "internal_eval_best_call": 2,
        "internal_eval_final": 0.55, "internal_eval_change": 0.15,
        "internal_eval_all_scores": [0.4, 0.55, 0.55],
        "checkpoints_valid": 1, "checkpoints_unhealthy": 0, "checkpoints_failed": 1,
        "checkpoints_in_run": 4,
        "call_details": [
            OptimizationCall(ordinal=1, status="optimizing", succeeded=True, headline_metric=0.4,
                             changed=True, redeployed=True, checkpoint="artifact-002"),
            OptimizationCall(ordinal=2, status="optimizing", succeeded=True, headline_metric=0.55,
                             changed=True, redeployed=False,
                             deploy_error="docker build failed: no module named pandas"),
            OptimizationCall(ordinal=3, status="saturated", succeeded=True, headline_metric=0.55),
        ]}
    base.update(overrides)
    return OptimizationResult(**base)


def _render_opt(opt, final_score=0.8):
    return render_html(EvalReport(task_id="demo_task", final_score=final_score, optimization=opt))


def test_the_optimization_section_leads_with_how_many_times_the_loop_ran():
    html = _render_opt(_opt())
    assert "Optimization diagnostic" in html
    assert "OPTIMIZATION RUNS" in html
    assert "2 cycle(s) spent of 5" in html


def test_the_optimization_section_says_it_is_not_part_of_the_score():
    assert "not part of the score above" in _render_opt(_opt())


def test_saturation_with_budget_left_reads_differently_from_a_spent_budget():
    """The two findings this section exists to keep apart."""
    assert "stopped early, with budget still unspent" in _render_opt(_opt())
    spent = _render_opt(_opt(saturated=False, early_stopped_on_saturation=False,
                             budget_exhausted=True, stop_reason="budget_exhausted"))
    assert "the cycle allowance ran out, not the ideas" in spent
    assert "stopped early" not in spent


def test_a_run_that_died_inside_a_call_does_not_read_as_a_loop_that_kept_going():
    """The opposite claim from the one the page used to make: "the agent moved on" says the agent
    chose to stop, and this run did not choose anything."""
    html = _render_opt(_opt(saturated=False, early_stopped_on_saturation=None,
                            stop_reason="run_ended_mid_call"))

    assert "the loop did not stop, the run did" in html
    assert "the agent moved on" not in html


def test_what_the_stopping_reason_outranked_is_shown_beside_it():
    """An unverified change wins over saturation and a spent budget, and the reason chip is the only
    place on this page either of those would otherwise appear -- so they are said alongside it rather
    than lost to it."""
    both = _render_opt(_opt(stop_reason="unverified_change", saturated=True,
                            budget_exhausted=True))

    assert "the submitted candidate is not the one any score refers to" in both
    assert "the scores had also saturated and the cycle budget was also spent" in both
    # And nothing is added when neither of them held, so the ordinary case reads unchanged.
    plain = _render_opt(_opt(stop_reason="unverified_change", saturated=False,
                             budget_exhausted=False))
    assert "also saturated" not in plain and "also spent" not in plain


def test_a_failed_redeploy_is_named_as_one_per_call():
    html = _render_opt(_opt())
    assert "1 redeploy(s) succeeded" in html and "1 would not build" in html
    assert "changed, would not build (rolled back)" in html
    assert "no module named pandas" in html
    # A call that changed nothing attempted no deploy, so it must not read as a failed one.
    assert "measured only" in html


def test_the_loops_own_trajectory_is_rendered_first_best_and_final():
    html = _render_opt(_opt())
    assert "internal eval: first 0.4000" in html
    assert "best 0.5500 (call 2)" in html
    assert "final 0.5500" in html
    assert "+0.1500 over the loop" in html


def test_a_loop_that_ended_on_a_worse_candidate_says_so():
    """The finding the final score alone would hide."""
    below = _render_opt(_opt(internal_eval_best=0.9, internal_eval_best_call=1,
                             internal_eval_final=0.55, internal_eval_change=-0.35,
                             ended_below_best=True,
                             internal_eval_best_checkpoint="artifact-001"))
    assert "ended BELOW its own best on a DIFFERENT candidate" in below
    assert "artifact-001" in below
    # And the ordinary case must not carry the warning.
    assert "ended BELOW its own best" not in _render_opt(_opt())


def test_a_lower_final_on_the_same_candidate_is_not_warned_about():
    """The eval re-measuring one program is not the loop losing ground, and warning on it fired four
    false alarms out of six in the sweep."""
    html = _render_opt(_opt(internal_eval_best=0.991, internal_eval_best_call=1,
                            internal_eval_final=0.990, internal_eval_change=-0.001,
                            ended_below_best=False))
    assert "ended BELOW its own best" not in html
    assert "on the SAME candidate" in html


def test_candidates_that_cannot_be_compared_are_not_warned_about_either():
    """`None` means the two could not be compared, which is not evidence that they differed."""
    html = _render_opt(_opt(internal_eval_best=0.9, internal_eval_final=0.55,
                            internal_eval_change=-0.35, ended_below_best=None))
    assert "ended BELOW its own best" not in html
    assert "on the SAME candidate" not in html


def test_deploys_the_agent_made_outside_the_loop_are_shown():
    html = _render_opt(_opt(deploys_outside_loop=4))
    assert "4 deploy(s) the agent made outside the loop" in html
    assert "keep-or-roll-back rule does not cover these" in html
    # A run where the loop was the only thing deploying says nothing.
    assert "outside the loop" not in _render_opt(_opt(deploys_outside_loop=0))


def test_a_loop_with_no_measured_score_says_so_rather_than_rendering_zero():
    html = _render_opt(_opt(internal_eval_first=None, internal_eval_best=None,
                            internal_eval_best_call=None, internal_eval_final=None,
                            internal_eval_change=None))
    assert "no call reported a score" in html
    assert "internal eval: first 0.0000" not in html


def test_a_single_measurement_reports_no_change():
    html = _render_opt(_opt(internal_eval_change=None))
    assert "one measurement only, so there is no change to report" in html


def test_the_benchmarks_own_score_is_shown_beside_the_loops_final_one():
    html = _render_opt(_opt())
    assert "the loop ended at 0.5500 on its own eval" in html
    assert "this run scored 0.8000 on the benchmark" in html
    assert "-0.2500 between them" in html
    # Never as an error or an agreement score: the two measure different things.
    assert "a distance, not an error" in html


def test_the_benchmark_comparison_is_absent_without_a_score():
    """A build-only run never reached an eval, and one number labelled as a comparison is worse than
    none."""
    html = _render_opt(_opt(), final_score=None)
    assert "on the benchmark" not in html
    # The loop's own trajectory still renders: it does not depend on the eval having run.
    assert "internal eval: first 0.4000" in html


def test_the_whole_trajectory_is_rendered_beside_the_summary():
    html = _render_opt(_opt())
    assert "0.4000 &rarr; 0.5500 &rarr; 0.5500" in html


def test_a_long_trajectory_is_elided_in_the_middle_not_at_the_end():
    """The last number is what the chip beside it calls `final`; dropping it would make the two
    disagree."""
    html = _render_opt(_opt(internal_eval_all_scores=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.99]))
    assert "&hellip;" in html
    assert "0.9900" in html
    assert "0.6000" not in html


def test_a_single_measurement_renders_no_trajectory():
    """It is already printed three times by the spans above it."""
    html = _render_opt(_opt(internal_eval_all_scores=[0.4], internal_eval_change=None))
    assert "&rarr;" not in html


def test_the_rollback_counts_are_rendered():
    html = _render_opt(_opt(changes_kept=3, changes_rolled_back=1, rollback_redeploys=1))
    assert "3 of 4 change(s) kept" in html
    assert "1 rolled back" in html
    assert "1 roll-back redeploy(s)" in html


def test_a_loop_that_reached_no_verdicts_renders_no_rollback_row():
    """"0 kept, 0 rolled back" would read as every change having been judged and rejected."""
    html = _render_opt(_opt(changes_kept=0, changes_rolled_back=0))
    assert "change(s) kept" not in html


def test_a_rollback_that_did_not_take_gets_its_own_warning():
    html = _render_opt(_opt(changes_kept=1, changes_rolled_back=1, rollback_redeploys=0,
                            rollbacks_failed=1))
    assert "1 roll-back(s) did not take" in html
    assert "the rejected code stayed running" in html


def test_the_reconciliation_line_closes_the_deploy_and_checkpoint_tallies():
    html = _render_opt(_opt(deploys_succeeded=4, deploys_failed=0, deploys_attempted=4,
                            changes_kept=3, changes_rolled_back=1, rollback_redeploys=1,
                            checkpoints_valid=5))
    assert "4 redeploy(s) of a change + 1 roll-back redeploy(s) = 5" in html
    assert "which is the valid checkpoint count" in html


def test_the_reconciliation_line_states_a_residual_rather_than_hiding_it():
    html = _render_opt(_opt(deploys_succeeded=4, deploys_failed=0, deploys_attempted=4,
                            changes_kept=3, changes_rolled_back=1, rollback_redeploys=1,
                            checkpoints_valid=7))
    assert "2 unaccounted for" in html


def test_no_reconciliation_line_without_a_rollback_to_explain():
    """With no roll-backs the two numbers already agree, so the sentence would be noise."""
    assert "which is the valid checkpoint count" not in _render_opt(_opt())


def test_the_per_call_table_names_the_verdict_on_the_previous_change():
    from appsci_bench.eval.datatypes import OptimizationCall

    html = _render_opt(_opt(call_details=[
        OptimizationCall(ordinal=1, status="awaiting_verification", succeeded=True,
                         headline_metric=0.4, changed=True, redeployed=True),
        OptimizationCall(ordinal=2, status="optimizing", succeeded=True, headline_metric=0.3,
                         previous_change_kept=False, rollback="reverted"),
        OptimizationCall(ordinal=3, status="optimizing", succeeded=True, headline_metric=0.5,
                         previous_change_kept=True),
        OptimizationCall(ordinal=4, status="optimizing", succeeded=True, headline_metric=0.5,
                         previous_change_kept=False, rollback="no_snapshot"),
    ]))
    assert "verdict on the previous change" in html
    assert "rolled the previous change back" in html
    assert "kept the previous change" in html
    assert "rolled back, but no_snapshot -- the rejected code stayed running" in html


def test_an_unknown_cycle_count_does_not_render_as_zero():
    html = _render_opt(_opt(cycles_used=None))
    assert "an unknown number of cycle(s) spent" in html
    assert "None cycle" not in html


def test_uncounted_checkpoints_show_the_reason_not_a_zero():
    html = _render_opt(_opt(checkpoints_valid=None, checkpoints_unhealthy=None,
                            checkpoints_failed=None, checkpoints_in_run=None,
                            checkpoint_note="this run has no checkpoint manifest"))
    assert "no checkpoint manifest" in html
    assert "valid checkpoint(s) from optimization" not in html


def test_the_three_unscored_statuses_print_a_word_rather_than_a_count():
    for status, phrase in (("disabled", "the ablation arm"),
                           ("not_attempted", "never called it"),
                           ("unmeasurable", "could not be read")):
        html = _render_opt(_opt(status=status, calls=0, scored_calls=0, cycles_used=None,
                                stop_reason=None, deploys_attempted=0, deploys_succeeded=0,
                                deploys_failed=0, call_details=[]))
        assert phrase in html
        assert "cycle(s) spent" not in html


def test_an_unknown_status_or_stop_reason_still_renders():
    """Forward compatibility: a report written by a newer reader must not render blank here. The two
    are checked apart because an unscored status prints no stopping reason -- there was no loop to
    stop."""
    assert "some_future_status" in _render_opt(_opt(status="some_future_status", calls=0))
    assert "some_future_reason" in _render_opt(_opt(stop_reason="some_future_reason"))


def test_a_deploy_error_is_escaped_like_everything_else():
    from appsci_bench.eval.datatypes import OptimizationCall
    html = _render_opt(_opt(call_details=[OptimizationCall(
        ordinal=1, status="optimizing", succeeded=True, changed=True, redeployed=False,
        deploy_error="<script>alert('x')</script>")]))
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html


def test_optimization_json_is_written_alongside_the_report(tmp_path):
    import json

    from appsci_bench.eval.store import TranscriptStore

    store = TranscriptStore(tmp_path)
    report = EvalReport(task_id="demo_task", final_score=0.8, optimization=_opt())
    eval_system._write_report_artifacts(report, store)
    path = store.root / "optimization.json"
    assert path.is_file()
    payload = json.loads(path.read_text())
    assert payload["calls"] == 3 and payload["stop_reason"] == "saturated"


def test_no_optimization_json_when_there_is_no_diagnostic(tmp_path):
    from appsci_bench.eval.store import TranscriptStore

    store = TranscriptStore(tmp_path)
    eval_system._write_report_artifacts(EvalReport(task_id="demo_task", final_score=0.8), store)
    assert not (store.root / "optimization.json").exists()
