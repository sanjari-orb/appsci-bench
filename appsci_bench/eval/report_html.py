"""Render one self-contained HTML report for an eval run: an overview stats dashboard plus every
case's step-by-step trace (conversation turns, structured output / env state, and each objective's
score + judge reasoning). No external assets -- inline <style>, native <details>/<summary>, and a
tiny inline filter script -- so the single file opens anywhere and works in CI.

Inputs are the same artifacts the run already produces: the `EvalReport` (scores + judge metadata in
`results[].metadata`, aggregate `views`) and the run's `TranscriptStore` (per-case conversation turns
+ `final_state`). See `report.py` for the text/JSON renderers.
"""
from __future__ import annotations

import html
import json
import pathlib
from collections import defaultdict
from typing import Any

from .datatypes import (
    OPTIMIZATION_STATUS_DISABLED,
    OPTIMIZATION_STATUS_NOT_ATTEMPTED,
    OPTIMIZATION_STATUS_SCORED,
    OPTIMIZATION_STATUS_UNMEASURABLE,
    ROLLBACK_REVERTED,
    STATUS_NO_EXCHANGES,
    STATUS_NO_STAKEHOLDER,
    STATUS_NO_RUBRIC,
    STATUS_SCORED,
    STATUS_UNMEASURABLE,
    STOP_REASON_UNVERIFIED_CHANGE,
    ElicitationResult,
    EvalReport,
    OptimizationCall,
    OptimizationResult,
)
from .store import TranscriptStore


def _esc(value: Any) -> str:
    """HTML-escape any value (model/judge output is untrusted -- everything dynamic goes through here)."""
    return html.escape("" if value is None else str(value))


def _fmt(score: Any) -> str:
    return f"{score:.4f}" if isinstance(score, (int, float)) else "n/a"


def _signed(value: Any) -> str:
    """A change, with its sign always shown. `+0.0000` and `-0.0000` both beat a bare `0.0000`, which
    reads as "no change" when it may be a change too small to show at this precision."""
    return f"{value:+.4f}" if isinstance(value, (int, float)) else "n/a"


def _band(score: Any) -> str:
    """CSS band for a 0-1 score (color coding)."""
    if not isinstance(score, (int, float)):
        return "na"
    if score >= 0.8:
        return "good"
    if score >= 0.4:
        return "warn"
    return "bad"


def _bar(score: Any) -> str:
    pct = int(round(score * 100)) if isinstance(score, (int, float)) else 0
    return (f'<div class="bar"><div class="bar-fill {_band(score)}" style="width:{pct}%"></div>'
            f'<span class="bar-label">{_fmt(score)}</span></div>')


def _load_conversations(report: EvalReport, store: TranscriptStore | None) -> dict[str, list[dict]]:
    """data_point_id -> [conversation doc per repeat], from the store or report.store_root."""
    root = None
    if store is not None:
        root = store.root
    elif report.store_root:
        root = pathlib.Path(report.store_root)
    by_dp: dict[str, list[dict]] = defaultdict(list)
    if not root:
        return by_dp
    conv_dir = pathlib.Path(root) / "conversations"
    if not conv_dir.is_dir():
        return by_dp
    for path in sorted(conv_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        by_dp[doc.get("data_point_id", path.stem)].append(doc)
    for docs in by_dp.values():
        docs.sort(key=lambda d: d.get("repeat_index", 0))
    return by_dp


def _turn_html(idx: int, query: dict, response: dict) -> str:
    q_text = query.get("text") or query.get("json") or ""
    r_text = response.get("text") or ""
    parts = [f'<div class="turn"><div class="turn-idx">turn {idx}</div>']
    parts.append(f'<div class="msg user"><span class="who">user</span>'
                 f'<div class="content">{_esc(q_text)}</div></div>')
    parts.append(f'<div class="msg agent"><span class="who">agent</span>'
                 f'<div class="content">{_esc(r_text) or "<em>(no text)</em>"}</div></div>')
    # The artifact's internal reasoning + tool calls for this turn, if it self-reported a trace.
    if response.get("trace"):
        parts.append(_trace_html(response["trace"]))
    # A structured response payload is where an artifact reports actions / tool calls, when it does.
    if response.get("json"):
        parts.append('<div class="struct"><span class="who">structured output / actions</span>'
                     f'<pre>{_esc(response["json"])}</pre></div>')
    files = (response.get("files") or []) + (query.get("files") or [])
    if files:
        names = ", ".join(_esc(f.get("file_name")) for f in files)
        parts.append(f'<div class="files">files: {names}</div>')
    parts.append("</div>")
    return "".join(parts)


def _trace_html(trace: list) -> str:
    """Render an artifact's self-reported internal LLM trace (reasoning + tool calls + results)."""
    if not trace:
        return ""
    steps = []
    for s in trace:
        kind = str(s.get("kind", "assistant"))
        row = [f'<div class="tstep"><span class="tkind {_esc(kind)}">{_esc(kind.replace("_", " "))}</span>']
        if s.get("content"):
            row.append(f'<span class="tcontent">{_esc(s["content"])}</span>')
        for tc in (s.get("tool_calls") or []):
            args = tc.get("arguments")
            args = json.dumps(args) if isinstance(args, (dict, list)) else args
            row.append(f'<div class="tcontent">&rarr; {_esc(tc.get("name"))}({_esc(args)})</div>')
        if s.get("result") is not None:
            res = s["result"]
            res = json.dumps(res, indent=2) if isinstance(res, (dict, list)) else res
            name = f'{_esc(s.get("name"))} &rArr; ' if s.get("name") else ""
            row.append(f'<div class="tcontent">{name}</div><pre>{_esc(res)}</pre>')
        row.append("</div>")
        steps.append("".join(row))
    return (f'<details class="trace"><summary>agent internals &middot; llm trace '
            f'({len(trace)} steps)</summary>{"".join(steps)}</details>')


def _metadata_html(metadata: dict) -> str:
    if not metadata:
        return ""
    parts = []
    # Judge reasoning (and similar prose) shown as a readable block; the rest as key/value rows.
    for key in ("reasoning", "explanation", "note"):
        if metadata.get(key):
            parts.append(f'<div class="reasoning"><span class="who">{_esc(key)}</span>'
                         f'<div class="content">{_esc(metadata[key])}</div></div>')
    rows = []
    for k, v in metadata.items():
        if k in ("reasoning", "explanation", "note"):
            continue
        val = json.dumps(v) if isinstance(v, (dict, list)) else v
        rows.append(f'<tr><td class="k">{_esc(k)}</td><td class="v">{_esc(val)}</td></tr>')
    if rows:
        parts.append(f'<table class="kv">{"".join(rows)}</table>')
    return "".join(parts)


def _objective_html(result: dict) -> str:
    score = result.get("score")
    err = result.get("error")
    head = (f'<div class="obj-head"><span class="obj-name">{_esc(result.get("objective"))}</span>'
            f'<span class="chip {_band(score)}">{_fmt(score)}</span>'
            f'{f"<span class=chip-err>error</span>" if err else ""}</div>')
    body = ""
    if err:
        body += f'<div class="err">{_esc(err)}</div>'
    body += _metadata_html(result.get("metadata") or {})
    return f'<div class="obj">{head}{body}</div>'


def _case_html(idx: int, dp: str, dp_score: Any, docs: list[dict], results: list[dict]) -> str:
    errored = any(d.get("error") for d in docs) or any(r.get("error") for r in results)
    summary = (f'<summary data-dp="{_esc(dp)}"><span class="idx">#{idx}</span>'
               f'<span class="chip {_band(dp_score)}">{_fmt(dp_score)}</span>'
               f'<span class="dp">{_esc(dp)}</span>'
               f'{"<span class=chip-err>error</span>" if errored else ""}</summary>')
    body = []
    for doc in docs:
        if doc.get("repeat_index"):
            body.append(f'<div class="repeat">repeat {_esc(doc.get("repeat_index"))}</div>')
        if doc.get("error"):
            body.append(f'<div class="err">generation error: {_esc(doc["error"])}</div>')
        conv = doc.get("conversation") or []
        if conv:
            body.append('<div class="section-label">Conversation</div>')
            for i, turn in enumerate(conv):
                body.append(_turn_html(i, turn.get("query") or {}, turn.get("response") or {}))
        else:
            body.append('<div class="muted">no conversation captured</div>')
        fs = doc.get("final_state")
        body.append('<div class="section-label">Environment state (post-run)</div>')
        if fs:
            body.append(f'<pre class="env">{_esc(json.dumps(fs, indent=2))}</pre>')
        else:
            body.append('<div class="muted">no env state captured (reset seam off, or artifact '
                        'reports no tool calls)</div>')
    if results:
        body.append('<div class="section-label">Objectives</div>')
        for r in sorted(results, key=lambda r: (r.get("objective") or "", r.get("repeat_index") or 0)):
            body.append(_objective_html(r))
    return f'<details class="case">{summary}<div class="case-body">{"".join(body)}</div></details>'


# Wording per status. The whole point of having five is that a reader can tell them apart at a glance:
# NO_EXCHANGES is a real zero, UNMEASURABLE is no score at all, NO_STAKEHOLDER is a different run
# altogether, and a bare "0.00" would render all three identically. So an unscored status prints no
# rate.
_ELICITATION_STATUS = {
    STATUS_SCORED: ("scored", "of the facts only the stakeholder held, this share came back"),
    STATUS_NO_RUBRIC: ("no rubric", "this task ships no elicitation rubric, so nothing was measured"),
    STATUS_NO_EXCHANGES: ("no conversation",
                          "the agent never contacted the stakeholder \u2014 a real zero, not a gap"),
    STATUS_UNMEASURABLE: ("unmeasurable",
                          "the build conversation could not be read \u2014 NO score, which is not a zero"),
    STATUS_NO_STAKEHOLDER: ("no stakeholder",
                            "the no-user arm \u2014 there was nobody to ask and the requirements "
                            "were handed over as files, so there is nothing to score here"),
}

_VERDICT_LABEL = {
    "elicited": ("elicited", "good"),
    "asked_not_elicited": ("asked, not elicited", "warn"),
    "missed": ("never asked", "bad"),
    "error": ("not judged", "na"),
}


def _elicitation_html(el: ElicitationResult | None) -> str:
    """The elicitation diagnostic, in its own section.

    Deliberately NOT in the objectives table and NOT in the score matrix. It measures the build
    conversation rather than the artifact, and anything rendered alongside the objectives eventually
    gets read as one of them -- so it gets its own heading, its own styling, and its own explanation of
    what the number means.
    """
    if el is None:
        return ""
    label, blurb = _ELICITATION_STATUS.get(el.status, (el.status, ""))
    scored = el.elicited_rate is not None
    if el.error_items and scored:
        # Only said when it is true. A standing caveat about a judged subset, on a run where every item
        # WAS judged, pointing at a count that is not shown, is the kind of note readers learn to skip.
        blurb += " \u2014 over the items that could be judged, see the count below"

    headline = (f'<div class="final-num {_band(el.elicited_rate)}">{_fmt(el.elicited_rate)}</div>'
                if scored else
                f'<div class="final-num na elic-nostat">{_esc(label)}</div>')

    breakdown = ""
    if scored:
        breakdown = (
            f'<span class="good">{_pct(el.elicited_rate)} elicited</span>'
            f'<span class="warn">{_pct(el.asked_not_elicited_rate)} asked, not elicited</span>'
            f'<span class="bad">{_pct(el.missed_rate)} never asked</span>')

    # The denominator travels with the rate, always. Unjudged items are excluded from it, so it varies
    # between runs -- and a rate over three judged items is not the same claim as one over fifty.
    judged = (f'<span>{el.judged_items} of {el.total_items} items judged</span>'
              if el.error_items else f'<span>{el.total_items} rubric items</span>')
    counts = (judged
              + f'<span>{el.num_exchanges} exchanges</span>'
              + f'<span>status: {_esc(label)}</span>')
    if el.error_items:
        counts += (f'<span class="na">{el.error_items} not judged &mdash; excluded, '
                   f'not counted against the agent</span>')

    # The id plus the fact it stands for. An id alone -- `check_order_first_failure_wins` -- does not tell a
    # reader what the verdict was ABOUT, which is the one thing they need to judge whether they agree with
    # it. The fact is what "this" refers to in "did the agent ask about this?".
    rows = "".join(
        f'<tr><td class="elic-id"><span class="elic-id-name">{_esc(i.item_id)}:</span>'
        f'{f"<span class=elic-fact>{_esc(i.fact.strip())}</span>" if i.fact else ""}</td>'
        f'<td><span class="chip {_VERDICT_LABEL.get(i.verdict, (i.verdict, "na"))[1]}">'
        f'{_esc(_VERDICT_LABEL.get(i.verdict, (i.verdict, "na"))[0])}</span></td>'
        f'<td class="elic-q">{_esc(i.evidence)}</td>'
        f'<td class="elic-r">{_esc(i.error or i.reasoning)}</td></tr>'
        for i in el.items)
    # Folded away by default, and it is the judge reasoning that makes it necessary: a fifty-item rubric
    # pushes everything below it off the screen, so the rates -- the reason anyone opened the section --
    # stop being the first thing read. The rates and the breakdown stay outside the fold; only the table
    # goes in. Native <details>, like the case traces elsewhere in this report, so it needs no script and
    # survives being emailed as a file.
    table = ""
    if rows:
        table = (
            f'<details class="elic-fold"><summary>the {len(el.items)} per-item verdicts, with the '
            f'evidence each one was based on</summary>'
            f'<table class="elic-items"><thead><tr>'
            f'<th>rubric item &mdash; the fact the agent had to obtain by asking</th><th>verdict</th>'
            f'<th>evidence quoted from the conversation</th><th>judge reasoning</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></details>')

    warns = ""
    if el.warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in el.warnings)
        warns = f'<div class="warnings"><b>notes</b><ul>{items}</ul></div>'

    return f"""
    <h2 class="section-hd elic-hd">Elicitation diagnostic &mdash; {_esc(el.task_id)}</h2>
    <section class="dashboard elic">
      <div class="final">{headline}<div class="final-lbl">ELICITED RATE</div></div>
      <div class="elic-blurb">{_esc(blurb)}
        <em>Diagnostic only &mdash; not part of the score above.</em></div>
      <div class="counts">{breakdown}</div>
      <div class="counts">{counts}</div>
      {warns}
      {table}
    </section>"""


def _pct(rate: Any) -> str:
    return f"{rate * 100:.0f}%" if isinstance(rate, int | float) else "n/a"


# Wording per status, for the same reason the elicitation table exists: the four are only worth having
# if a reader can tell them apart without opening the JSON. `disabled` is the ablation arm and
# `not_attempted` is an agent that had the tool and never called it -- one is a fact about the
# experiment, the other about the agent.
_OPTIMIZATION_STATUS = {
    OPTIMIZATION_STATUS_SCORED: ("ran", "the build's optimize_agent loop, counted out of its trace"),
    OPTIMIZATION_STATUS_DISABLED: (
        "route off", "this run had no optimization route (the ablation arm): no eval_generator and "
                     "no optimize_agent, so there was nothing to run"),
    OPTIMIZATION_STATUS_NOT_ATTEMPTED: (
        "never called", "the agent had the optimization route and never called it — a real zero "
                        "about the agent, not a gap in the measurement"),
    OPTIMIZATION_STATUS_UNMEASURABLE: (
        "unmeasurable", "the build's trace could not be read, so the loop is unknown — NOT a run "
                        "without optimization"),
}

#: How the stopping reason reads on the page. The pair that matters: a loop that flattened while it
#: still had budget had nothing left to find; one that ran out of cycles established nothing either way.
_STOP_REASON_LABEL = {
    "saturated": ("saturated — stopped early, with budget still unspent", "good"),
    "budget_exhausted": ("budget spent — the cycle allowance ran out, not the ideas", "warn"),
    "saturated_and_budget_exhausted": (
        "saturated and out of budget — both held, so neither is the sole reason", "warn"),
    "nothing_to_fix": ("nothing to fix — every internal-eval case passed", "good"),
    "unverified_change": (
        "left an unverified change — the submitted candidate is not the one any score refers to",
        "bad"),
    "run_ended_mid_call": (
        "the run ended inside an optimize_agent call — the loop did not stop, the run did, so the "
        "counts here are a floor", "bad"),
    "still_optimizing": ("not stopped — the agent moved on while the loop was still working",
                         "na"),
}


def _stop_reason_also(opt: OptimizationResult) -> str:
    """Whatever the stopping reason outranked, shown beside it.

    `unverified_change` wins over saturation and a spent budget because the tool's own status field
    resolves it that way, and because it is a claim about the candidate rather than about the
    trajectory (see `build_optimization._stop_reason`). Those two can still have held -- on the last
    cycle of a budget-bounded run they usually both do -- and the reason chip is the only place on
    this page either of them would otherwise appear. So they are shown alongside rather than lost to
    the reason that outranked them.
    """
    if opt.stop_reason != STOP_REASON_UNVERIFIED_CHANGE:
        return ""
    held = []
    if opt.saturated:
        held.append("the scores had also saturated")
    if opt.budget_exhausted:
        held.append("the cycle budget was also spent")
    if not held:
        return ""
    return f'<span class="warn">{_esc(" and ".join(held))}</span>' 


def _optimization_html(opt: OptimizationResult | None, final_score: Any = None) -> str:
    """The optimization diagnostic, in its own section beside the elicitation one.

    Counts first, because the number a reader wants first is how many times the loop ran and the claim
    next to it is why it stopped. The loop's own scores come after them, and `final_score` -- the
    benchmark's own verdict on the same run -- is passed in so the two can be shown together. It is
    passed rather than stored on the diagnostic on purpose: the diagnostic is readable from a
    build-only run that never reached an eval, and a field for a number that is usually absent there
    would invite reading its absence as a result.

    Deliberately mirrors the tracer's Eval tab (`appsci_bench/tracer/templates/dashboard.html`) so the
    two views of one run agree.
    """
    if opt is None:
        return ""
    label, blurb = _OPTIMIZATION_STATUS.get(opt.status, (opt.status, ""))
    ran = opt.status == OPTIMIZATION_STATUS_SCORED

    headline = (f'<div class="final-num">{opt.calls}</div>' if ran
                else f'<div class="final-num na elic-nostat">{_esc(label)}</div>')

    stop = ""
    if ran and opt.stop_reason:
        text, band = _STOP_REASON_LABEL.get(opt.stop_reason, (opt.stop_reason, "na"))
        stop = f'<span class="{band}">{_esc(text)}</span>' + _stop_reason_also(opt)

    counts = ""
    if ran:
        allowed = "no ceiling" if opt.cycles_allowed is None else str(opt.cycles_allowed)
        # "unknown", not 0: a loop whose calls never reported back spent an unknown number of cycles.
        spent = "an unknown number of" if opt.cycles_used is None else str(opt.cycles_used)
        counts = (
            f'<span>{_esc(spent)} cycle(s) spent of {_esc(allowed)}</span>'
            f'<span>{opt.scored_calls} call(s) measured the candidate</span>'
            + (f'<span class="bad">{opt.failed_calls} call(s) measured nothing</span>'
               if opt.failed_calls else "")
        )

    internal = ""
    if ran:
        internal = _internal_eval_spans(opt)

    benchmark = ""
    if ran and opt.internal_eval_final is not None and isinstance(final_score, (int, float)):
        gap = opt.internal_eval_final - final_score
        benchmark = (
            f'<span class="na">the loop ended at {_fmt(opt.internal_eval_final)} on its own eval; '
            f'this run scored {_fmt(final_score)} on the benchmark</span>'
            f'<span class="na">{_signed(gap)} between them &mdash; a distance, not an error: the two '
            f'score different things</span>')

    deploys = ""
    if ran and opt.deploys_attempted:
        deploys = (
            f'<span class="good">{opt.deploys_succeeded} redeploy(s) succeeded</span>'
            f'<span class="{"bad" if opt.deploys_failed else "na"}">'
            f'{opt.deploys_failed} would not build</span>')
    elif ran:
        deploys = '<span class="na">the loop changed the candidate 0 times, so it deployed nothing</span>'

    rollbacks = _rollback_spans(opt) if ran else ""

    checkpoints = ""
    if ran:
        if opt.checkpoints_valid is None:
            checkpoints = f'<span class="na">checkpoints: {_esc(opt.checkpoint_note or "not counted")}</span>'
        else:
            checkpoints = (
                f'<span class="good">{opt.checkpoints_valid} valid checkpoint(s) from optimization'
                f'</span>'
                f'<span class="na">{opt.checkpoints_unhealthy} unhealthy, {opt.checkpoints_failed} '
                f'failed, of {opt.checkpoints_in_run} in the run</span>'
                + _outside_loop_span(opt)
                + _reconciliation_span(opt))

    rows = "".join(
        f'<tr><td class="elic-id"><span class="elic-id-name">call {c.ordinal}</span></td>'
        f'<td>{_esc(c.status or ("no result -- the run ended inside this call" if c.unfinished else "n/a"))}</td>'
        f'<td>{_fmt(c.headline_metric)}</td>'
        f'<td>{_esc(_call_change_label(c))}</td>'
        f'<td>{_esc(_call_verdict_label(c))}</td>'
        f'<td class="elic-r">{_esc(c.checkpoint or "")}</td>'
        f'<td class="elic-r">{_esc(c.deploy_error or c.error or "")}</td></tr>'
        for c in opt.call_details)
    table = ""
    if rows:
        table = (
            f'<details class="elic-fold"><summary>the {len(opt.call_details)} call(s), each with what '
            f'it scored and what it changed</summary>'
            f'<table class="elic-items"><thead><tr><th>call</th><th>status</th>'
            f'<th>internal-eval score</th><th>change</th><th>verdict on the previous change</th>'
            f'<th>checkpoint scored</th>'
            f'<th>error</th></tr></thead><tbody>{rows}</tbody></table></details>')

    warns = ""
    if opt.warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in opt.warnings)
        warns = f'<div class="warnings"><b>notes</b><ul>{items}</ul></div>'

    return f"""
    <h2 class="section-hd elic-hd opt-hd">Optimization diagnostic &mdash; {_esc(opt.task_id)}</h2>
    <section class="dashboard elic opt">
      <div class="final">{headline}<div class="final-lbl">OPTIMIZATION RUNS</div></div>
      <div class="elic-blurb">{_esc(blurb)}
        <em>Diagnostic only &mdash; not part of the score above.</em></div>
      <div class="counts">{stop}</div>
      <div class="counts">{counts}</div>
      <div class="counts">{internal}</div>
      <div class="counts">{benchmark}</div>
      <div class="counts">{deploys}</div>
      <div class="counts">{rollbacks}</div>
      <div class="counts">{checkpoints}</div>
      {warns}
      {table}
    </section>"""


def _rollback_spans(opt: OptimizationResult) -> str:
    """How many of the loop's changes survived being measured, and what the rejections cost.

    A roll-back that did not take gets its own span, because it is the only one of these counts that
    changes what the run submitted: the code the loop decided against is what stayed running.
    """
    judged = opt.changes_kept + opt.changes_rolled_back
    if not judged:
        # Silent rather than "0 kept, 0 rolled back": a loop whose changes were never measured reached
        # no verdicts at all, and a pair of zeros would read as "every change was judged and none
        # survived".
        return ""

    kept = (f'<span class="{"good" if opt.changes_kept else "na"}">{opt.changes_kept} of {judged} '
            f'change(s) kept</span>')
    rolled = (f'<span class="warn">{opt.changes_rolled_back} rolled back</span>'
              if opt.changes_rolled_back else "")
    restored = (f'<span class="na">{opt.rollback_redeploys} roll-back redeploy(s)</span>'
                if opt.rollback_redeploys else "")
    stuck = (f'<span class="bad">{opt.rollbacks_failed} roll-back(s) did not take &mdash; the '
             f'rejected code stayed running</span>' if opt.rollbacks_failed else "")
    return kept + rolled + restored + stuck


def _outside_loop_span(opt: OptimizationResult) -> str:
    """Deploys the agent made itself while the loop was in play.

    Worth its own chip rather than a footnote on the checkpoint count: these sit outside the
    keep-or-roll-back rule, which only restores a change the loop made, so they are the one way a run
    can lose a candidate the loop had already accepted.
    """
    if not opt.deploys_outside_loop:
        return ""
    return (f'<span class="warn">{opt.deploys_outside_loop} deploy(s) the agent made outside the '
            f'loop &mdash; the keep-or-roll-back rule does not cover these</span>')


def _reconciliation_span(opt: OptimizationResult) -> str:
    """Why the deploy tally and the checkpoint tally differ, in the one line that settles it.

    A restore is a redeploy that is not a change, so it leaves a checkpoint that the deploy counts do
    not explain. Rendered only when there is something to reconcile: with no roll-backs the two numbers
    already agree and the sentence would be noise.
    """
    if opt.checkpoints_valid is None or not opt.rollback_redeploys:
        return ""
    expected = opt.deploys_succeeded + opt.rollback_redeploys
    line = (f'{opt.deploys_succeeded} redeploy(s) of a change + {opt.rollback_redeploys} roll-back '
            f'redeploy(s) = {expected}')
    if expected == opt.checkpoints_valid:
        return f'<span class="na">{line}, which is the valid checkpoint count</span>'
    # Stated, not hidden: the residual is a checkpoint the loop did not account for, which is exactly
    # what this line exists to make visible.
    return (f'<span class="warn">{line}, against {opt.checkpoints_valid} valid checkpoint(s) '
            f'&mdash; {abs(opt.checkpoints_valid - expected)} unaccounted for</span>')


def _internal_eval_spans(opt: OptimizationResult) -> str:
    """Where the loop's own score started, peaked and ended.

    The below-best case gets its own span rather than a footnote because it is the finding: the loop
    keeps a change when it beats the PREVIOUS measurement, not the best one, so it can finish holding
    a candidate it had already scored higher than. A reader seeing only the final number would have no
    way to know that happened.
    """
    if opt.internal_eval_final is None:
        # Not 0.0000: no call came back with a score, which is not the same as a loop that scored zero.
        return ('<span class="na">internal eval: no call reported a score, so the loop\'s own '
                'trajectory is unknown</span>')

    best = f'<span>best {_fmt(opt.internal_eval_best)}'
    if opt.internal_eval_best_call is not None:
        best += f' (call {opt.internal_eval_best_call})'
    best += "</span>"

    change = (f'<span class="{"good" if opt.internal_eval_change > 0 else "warn"}">'
              f'{_signed(opt.internal_eval_change)} over the loop</span>'
              if opt.internal_eval_change is not None
              else '<span class="na">one measurement only, so there is no change to report</span>')

    # Only when the two scores were taken on DIFFERENT candidates. A lower final on the same bytes is
    # the eval re-measuring, not the loop losing ground, and warning on it fired four false alarms out
    # of six in the sweep. See `build_optimization._apply_below_best`.
    below = ""
    if opt.ended_below_best:
        where = (f' (checkpoint {_esc(opt.internal_eval_best_checkpoint)})'
                 if opt.internal_eval_best_checkpoint else "")
        below = (f'<span class="warn">ended BELOW its own best on a DIFFERENT candidate{where} '
                 f'&mdash; the loop\'s keep rule only protects a change the LOOP made, so a deploy the '
                 f'agent made itself can lose the high-water candidate</span>')
    elif (opt.ended_below_best is False and opt.internal_eval_best is not None
          and opt.internal_eval_final < opt.internal_eval_best):
        below = ('<span class="na">the final score is below the best, but on the SAME candidate '
                 '&mdash; the loop re-measured one program and its own eval moved</span>')

    return (f'<span>internal eval: first {_fmt(opt.internal_eval_first)}</span>'
            f'{best}'
            f'<span>final {_fmt(opt.internal_eval_final)}</span>'
            f'{change}{below}{_trajectory_span(opt)}')


#: How many scores the trajectory span prints before eliding the middle. A ten-call loop would
#: otherwise push the row wider than the section.
_TRAJECTORY_SHOWN = 6


def _trajectory_span(opt: OptimizationResult) -> str:
    """Every score the loop measured, in order, so the summary above can be checked against it.

    Elided in the middle rather than truncated at the end: the last number is the one the other spans
    call `final`, so dropping it would make the two disagree on a long run.
    """
    scores = opt.internal_eval_all_scores
    if len(scores) < 2:
        # One score is already printed three times by the spans above; zero has its own message there.
        return ""
    if len(scores) <= _TRAJECTORY_SHOWN:
        shown = " &rarr; ".join(_fmt(s) for s in scores)
    else:
        head = " &rarr; ".join(_fmt(s) for s in scores[: _TRAJECTORY_SHOWN - 1])
        shown = f"{head} &rarr; &hellip; &rarr; {_fmt(scores[-1])}"
    return f'<span class="na">{shown}</span>'


def _call_change_label(call: OptimizationCall) -> str:
    """What one call did to the candidate, in words rather than two booleans.

    "changed but did not redeploy" is the case worth spelling out: the edit was written, the rebuild
    failed, and the tool put the previous source back -- so the cycle cost a change and produced no
    candidate.
    """
    if not call.changed:
        return "measured only"
    if call.redeployed:
        return "changed + redeployed"
    return "changed, would not build (rolled back)"


def _call_verdict_label(call: OptimizationCall) -> str:
    """What this call decided about the PREVIOUS call's change.

    Blank when there was nothing pending to judge -- the first call, and any call after one that
    changed nothing. A blank cell says that honestly; "kept: no" would read as a rejection.
    """
    if call.previous_change_kept is None:
        return ""
    if call.previous_change_kept:
        return "kept the previous change"
    if call.rollback == ROLLBACK_REVERTED:
        return "rolled the previous change back"
    if call.rollback:
        # The three words that mean the restore did not happen. Named rather than summarised: which
        # one it was is the difference between "no snapshot existed" and "the restore would not
        # redeploy", and both leave the rejected code running.
        return f"rolled back, but {call.rollback} -- the rejected code stayed running"
    return "rolled the previous change back"


def _dashboard_html(report: EvalReport) -> str:
    views = report.views or {}
    per_metric = views.get("per_metric") or []
    per_dp = views.get("per_data_point") or []
    n = len(per_dp)
    errored = sum(1 for r in report.results if r.error)
    bands = {"good": 0, "warn": 0, "bad": 0, "na": 0}
    for dp in per_dp:
        bands[_band(dp.get("score"))] += 1

    metric_rows = "".join(
        f'<tr><td>{_esc(m.get("objective"))}</td><td>{_bar(m.get("score"))}</td>'
        f'<td class="num">{_esc(m.get("weight"))}</td>'
        f'<td>{"+" if m.get("positive", True) else "-"}</td></tr>'
        for m in per_metric)

    dp_chips = "".join(
        f'<span class="chip {_band(dp.get("score"))}" title="{_esc(dp.get("data_point_id"))}">'
        f'{_fmt(dp.get("score"))}</span>' for dp in per_dp)

    warns = ""
    if report.warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in report.warnings)
        warns = f'<div class="warnings"><b>warnings</b><ul>{items}</ul></div>'

    return f"""
    <h2 class="section-hd">Aggregated metrics &mdash; {_esc(report.task_id)}</h2>
    <section class="dashboard">
      <div class="final"><div class="final-num {_band(report.final_score)}">{_fmt(report.final_score)}</div>
        <div class="final-lbl">FINAL SCORE</div></div>
      <div class="counts">
        <span>{n} cases</span><span>{errored} errored</span>
        <span class="good">{bands['good']} &ge;0.8</span>
        <span class="warn">{bands['warn']} 0.4&ndash;0.8</span>
        <span class="bad">{bands['bad']} &lt;0.4</span>
        <span class="na">{bands['na']} n/a</span>
      </div>
      <table class="metrics"><thead><tr><th>objective</th><th>score</th><th>weight</th><th>dir</th></tr></thead>
        <tbody>{metric_rows}</tbody></table>
      <div class="dist"><b>per-case scores</b><div class="chips">{dp_chips}</div></div>
      {warns}
    </section>"""


_CSS = """
:root{--good:#2e7d32;--warn:#b26a00;--bad:#c62828;--na:#7a7d78;--bg:#e9e9e3;--panel:#fbfbf7;--fg:#181a17;--mut:#5c5f58;--line:#b7b9b0;--line2:#d7d9cf;--accent:#1f4e5f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.35 ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
header{padding:6px 12px;background:var(--panel);border-bottom:2px solid var(--accent);display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
header h1{margin:0;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.14em}
header .meta{color:var(--mut);font-size:11px}
main{max-width:1500px;margin:0 auto;padding:10px 12px}
/* KPI strip */
.dashboard{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.final{display:flex;align-items:stretch;border:1px solid var(--line);background:var(--panel)}
.final .final-num{font-size:26px;font-weight:700;line-height:1;padding:8px 14px;border-right:1px solid var(--line);display:flex;align-items:center}
.final .final-lbl{align-self:center;padding:0 14px;font-size:11px;letter-spacing:.14em;color:var(--mut)}
.counts{display:flex;flex-wrap:wrap;border:1px solid var(--line);background:var(--panel)}
.counts span{padding:4px 12px;border-right:1px solid var(--line2);color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:3px 8px;border-bottom:1px solid var(--line2);vertical-align:top}
table.metrics{border:1px solid var(--line);background:var(--panel)}
table.metrics th{background:#efefe8;text-transform:uppercase;font-size:10px;letter-spacing:.1em;color:var(--mut);border-bottom:1px solid var(--line)}
.num{text-align:right}
.bar{position:relative;background:#dfe0d8;border:1px solid var(--line2);border-radius:0;height:15px;min-width:160px}.bar-fill{height:100%}.bar-label{position:absolute;right:5px;top:0;font-size:11px;color:#fff;text-shadow:0 1px 1px rgba(0,0,0,.5)}
.dist{border:1px solid var(--line);background:var(--panel);padding:6px 8px}.dist b{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--mut)}
.chips{display:flex;gap:2px;flex-wrap:wrap;margin-top:5px}
.chip{display:inline-block;min-width:46px;text-align:center;padding:1px 5px;border-radius:0;font-size:11px;color:#fff}
.chip.good,.bar-fill.good{background:var(--good)}.chip.warn,.bar-fill.warn{background:var(--warn)}.chip.bad,.bar-fill.bad{background:var(--bad)}.chip.na,.bar-fill.na{background:var(--na)}
.counts .good{color:var(--good);background:none}.counts .warn{color:var(--warn);background:none}.counts .bad{color:var(--bad);background:none}.counts .na{color:var(--na);background:none}
.chip-err{background:var(--bad);color:#fff;border-radius:0;padding:1px 5px;font-size:10px;margin-left:6px;text-transform:uppercase;letter-spacing:.06em}
.warnings{background:#fdf4dd;border:1px solid var(--warn);padding:6px 10px}.warnings ul{margin:4px 0 0;padding-left:16px}
.filter{width:100%;padding:5px 8px;margin:4px 0 10px;background:var(--panel);border:1px solid var(--line);border-radius:0;color:var(--fg);font:12px ui-monospace,Menlo,monospace}
.case{background:var(--panel);border:1px solid var(--line);border-radius:0;margin:0 0 -1px;padding:0 10px}
.case>summary{cursor:pointer;padding:5px 0;display:flex;align-items:center;gap:10px;list-style:none}
.case>summary::-webkit-details-marker{display:none}
.case>summary .dp{font-weight:700}
.case[open]{border-left:3px solid var(--accent)}
.case-body{padding:2px 0 10px;border-top:1px solid var(--line2)}
.section-label{color:var(--accent);text-transform:uppercase;font-size:10px;letter-spacing:.12em;margin:10px 0 4px;border-bottom:1px solid var(--line2);padding-bottom:2px;font-weight:700}
.turn{border-left:2px solid var(--line2);padding-left:8px;margin:5px 0}.turn-idx{color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.08em}
.msg{margin:2px 0}.who{display:inline-block;font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
.msg.user .who{color:#1f6feb}.msg.agent .who{color:var(--good)}
.msg .content{white-space:pre-wrap;background:#fff;border:1px solid var(--line2);border-radius:0;padding:5px 8px;margin-top:1px}
.msg.user .content{background:#e8f1fd;border:1px solid #bcd6f7;border-left:3px solid #1f6feb}
.msg.agent .content{background:#e9f5ec;border:1px solid #bfe0c6;border-left:3px solid var(--good)}
pre{white-space:pre-wrap;word-break:break-word;background:#f3f3ec;border:1px solid var(--line2);border-radius:0;padding:5px 8px;margin:2px 0;font:12px/1.4 ui-monospace,Menlo,monospace;overflow:auto}
.reasoning .content{white-space:pre-wrap;background:#fdf4dd;border:1px solid var(--line2);border-left:3px solid var(--warn);border-radius:0;padding:5px 8px;margin-top:1px}
.obj{border:1px solid var(--line2);border-radius:0;padding:5px 8px;margin:4px 0;background:#fff}.obj-head{display:flex;align-items:center;gap:8px}.obj-name{font-weight:700}
table.kv{margin-top:4px;border:1px solid var(--line2)}table.kv td{padding:2px 8px}table.kv td.k{color:var(--mut);width:180px;background:#f3f3ec}
.err{color:var(--bad);white-space:pre-wrap}.muted{color:var(--mut);font-style:italic}
.repeat{color:var(--mut);font-size:11px;margin-top:6px;text-transform:uppercase;letter-spacing:.06em}
/* tabs */
.tabs{display:flex;flex-wrap:wrap;gap:0;border:1px solid var(--line);border-bottom:none;background:var(--panel)}
.tab{padding:6px 14px;border-right:1px solid var(--line2);cursor:pointer;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;background:none;border-top:0;border-left:0;border-bottom:2px solid transparent;font-family:inherit}
.tab:hover{color:var(--fg)}
.tab.active{color:var(--fg);font-weight:700;border-bottom:2px solid var(--accent);background:#eef3f2}
.tab .ts{color:var(--mut);font-weight:400;margin-left:6px}
.task-panel{display:none}.task-panel.active{display:block}
/* section headers */
.section-hd{margin:12px 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);border-bottom:2px solid var(--accent);padding-bottom:3px}
.idx{color:var(--mut);font-weight:700;min-width:34px}
/* llm trace */
.trace{margin:4px 0 2px}.trace>summary{cursor:pointer;color:var(--accent);font-size:10px;text-transform:uppercase;letter-spacing:.1em;list-style:none;padding:3px 0}
.trace>summary::-webkit-details-marker{display:none}
.tstep{border-left:2px solid var(--line2);padding:2px 0 2px 8px;margin:3px 0}
.tstep .tkind{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:#fff;padding:0 5px;border-radius:0;margin-right:6px}
.tkind.assistant{background:#6f42c1}.tkind.tool_call{background:var(--accent)}.tkind.tool_result{background:var(--mut)}.tkind.final{background:var(--good)}.tkind.system{background:#57606a}
.tstep .tcontent{white-space:pre-wrap;margin-top:2px}
/* elicitation diagnostic -- deliberately unlike the objectives dashboard so it cannot read as a metric */
.elic-hd{border-left:4px solid #6f42c1;padding-left:8px}
.dashboard.elic{border-style:dashed;background:#f6f4fa}
/* the optimization diagnostic is the same KIND of section, in teal, so two dashed panels in a row do
   not read as one */
.elic-hd.opt-hd{border-left-color:#1f7a74}
.dashboard.elic.opt{background:#f1f7f6}
.dashboard.elic.opt .elic-blurb em{color:#1f7a74}
.elic .final-num.elic-nostat{font-size:15px;letter-spacing:.04em;text-transform:uppercase}
.elic-blurb{grid-column:1/-1;color:var(--mut);font-size:11px;line-height:1.5}
.elic-blurb em{display:block;color:#6f42c1;font-style:normal;text-transform:uppercase;font-size:9px;letter-spacing:.1em;margin-top:3px}
table.elic-items{grid-column:1/-1;width:100%;border-collapse:collapse;font-size:11px}
table.elic-items th,table.elic-items td{border-bottom:1px solid var(--line2);padding:3px 6px;text-align:left;vertical-align:top}
td.elic-id{max-width:26em}
td.elic-id .elic-id-name{font-family:ui-monospace,monospace;white-space:nowrap;font-weight:700}
td.elic-id .elic-fact{display:block;margin-top:2px;color:var(--mut);font-weight:400;white-space:normal}
td.elic-q,td.elic-r{white-space:pre-wrap}
td.elic-r{color:var(--mut)}
.elic-fold{grid-column:1/-1;width:100%;margin-top:2px}
/* Looks like a control, not like a heading: bordered, filled, with a verb that changes on open. A small
   uppercase label with only a triangle beside it reads as decoration, and a closed section nobody knows
   to click is the same as a section that is not there. */
.elic-fold>summary{cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-size:11px;
  color:#4c2d8a;background:#ece7f7;border:1px solid #b9a7e0;padding:5px 10px;list-style:none;
  user-select:none}
.elic-fold>summary:hover{background:#ded4f2;border-color:#8f77c9}
.elic-fold>summary:focus-visible{outline:2px solid #6f42c1;outline-offset:1px}
.elic-fold>summary::-webkit-details-marker{display:none}
.elic-fold>summary::before{content:"\25b8  Show ";font-weight:700}
.elic-fold[open]>summary::before{content:"\25be  Hide ";font-weight:700}
.elic-fold[open]>summary{background:#ded4f2}
.elic-fold table.elic-items{margin-top:8px}
"""

_JS = """
// tab switching
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
  const id=t.getAttribute('data-task');
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===t));
  document.querySelectorAll('.task-panel').forEach(p=>p.classList.toggle('active',p.getAttribute('data-task')===id));
}));
// per-panel case filter
document.querySelectorAll('.filter').forEach(f=>f.addEventListener('input',()=>{
  const q=f.value.toLowerCase();
  f.closest('.task-panel').querySelectorAll('details.case').forEach(d=>{
    const s=d.querySelector('summary');
    d.style.display=(s&&s.getAttribute('data-dp')||'').toLowerCase().includes(q)?'':'none';});}));
"""


def _task_panel(report: EvalReport, store: TranscriptStore | None, active: bool) -> str:
    """One task's panel: aggregated-metrics dashboard + filter + numbered per-case transcripts."""
    convs = _load_conversations(report, store)
    results_by_dp: dict[str, list[dict]] = defaultdict(list)
    for r in report.results:
        results_by_dp[r.data_point_id].append(r.to_dict())
    dp_score = {row.get("data_point_id"): row.get("score")
                for row in (report.views or {}).get("per_data_point", [])}
    dps = sorted(set(results_by_dp) | set(convs))          # stable union of report + transcript dps
    cases = "".join(
        _case_html(i, dp, dp_score.get(dp), convs.get(dp, []), results_by_dp.get(dp, []))
        for i, dp in enumerate(dps))
    return f"""<div class="task-panel{' active' if active else ''}" data-task="{_esc(report.task_id)}">
{_dashboard_html(report)}
{_elicitation_html(report.elicitation)}
{_optimization_html(report.optimization, report.final_score)}
<h2 class="section-hd">Per-task transcripts &mdash; {_esc(report.task_id)} ({len(dps)} cases)</h2>
<input class="filter" type="search" placeholder="filter cases by data_point_id&hellip;">
<section class="cases">{cases}</section>
</div>"""


def render_html_multi(items: list[tuple[EvalReport, TranscriptStore | None]]) -> str:
    """Render one self-contained HTML with a tab per task (dashboard + transcripts each)."""
    items = list(items)
    tabs = "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-task="{_esc(rep.task_id)}">'
        f'{_esc(rep.task_id)}<span class="ts">{_fmt(rep.final_score)}</span></button>'
        for i, (rep, _) in enumerate(items))
    panels = "".join(_task_panel(rep, store, active=(i == 0)) for i, (rep, store) in enumerate(items))
    title = items[0][0].task_id if len(items) == 1 else f"{len(items)} tasks"
    runs = " &nbsp;|&nbsp; ".join(f"{_esc(rep.task_id)}: {_esc(rep.run_id)}" for rep, _ in items)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>eval report: {_esc(title)}</title>
<style>{_CSS}</style></head>
<body>
<header><h1>Eval report &mdash; {_esc(title)}</h1>
<div class="meta">{runs}</div></header>
<nav class="tabs">{tabs}</nav>
<main>{panels}</main>
<script>{_JS}</script>
</body></html>"""


def render_html(report: EvalReport, store: TranscriptStore | None = None) -> str:
    """Render one eval run as a self-contained HTML string (single-task; one tab)."""
    return render_html_multi([(report, store)])
