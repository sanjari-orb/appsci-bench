#!/usr/bin/env python
"""Score one build conversation against a task's elicitation rubric, standalone.

Of the facts the agent could only get by ASKING the build-time stakeholder, what share did it actually
get? This is the hand-operated entry to that diagnostic: it needs no artifact, no deployment and no
dataset, only a rubric and a conversation. So every run already on disk can be scored without
re-running anything -- which is the point, because the conversations that matter most were recorded
before this metric existed.

Two input shapes, matching the two ways a build conversation gets recorded:

    # a benchmark run: the user-sim event log inside the run directory
    python scripts/score_elicitation.py --task order_to_cash --run runs/order_to_cash_20260811_121805

    # a human-baseline attempt: a captured terminal transcript
    python scripts/score_elicitation.py --task airline_rebooking \
        --transcript human_baseline/attempts/airline_rebooking-l3-human-20260806-183947/logs/transcript.md

Scoring a human attempt is what gives a machine number something real to be read against. An abstract
1.0 is not a reference point; what a person actually got out of the same stakeholder is.

Exit status is about whether the tool could run, never about the score: a rubric that will not load or
a flag combination that makes no sense exits non-zero, while a legitimately low score -- or a run that
cannot be measured at all -- exits zero. A diagnostic that fails the shell on a bad result would get
wrapped in `|| true` and stop being read.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from appsci_bench.core.spec import load_task_spec  # noqa: E402
from appsci_bench.eval.build_transcript import (  # noqa: E402
    read_run_dir,
    read_transcript_file,
)
from appsci_bench.eval.datatypes import (  # noqa: E402
    STATUS_SCORED,
    ElicitationResult,
    EvalReport,
)
from appsci_bench.eval.elicitation import score_elicitation  # noqa: E402
from appsci_bench.eval.elicitation_rubric import load_rubric, validate_rubric  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Score a build conversation against a task's elicitation rubric.")
    ap.add_argument("--task", required=True,
                    help="task id (or a path to its spec.yaml)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run", type=pathlib.Path,
                    help="a finished run directory (reads its events-usersim.jsonl)")
    src.add_argument("--transcript", type=pathlib.Path,
                    help="a captured conversation transcript, or an attempt directory holding one")
    ap.add_argument("--update-report", action="store_true",
                    help="fold the result into the run's EXISTING <run>/eval/report.{json,html} "
                         "instead of leaving them untouched (--run only)")
    ap.add_argument("--html", type=pathlib.Path,
                    help="write a standalone page carrying only this diagnostic; for --transcript, "
                         "where there is no report to update")
    ap.add_argument("--no-write", action="store_true",
                    help="print only; do not write elicitation.json")
    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.update_report and args.run is None:
        # There is no report to fold into: a captured transcript is a conversation, not a scored run.
        return _usage_error("--update-report needs --run; use --html with --transcript")

    spec = load_task_spec(args.task)
    rubric = load_rubric(spec)             # raises on a declared-but-broken rubric: a task bug
    if rubric is not None:
        for problem in validate_rubric(rubric, (spec.evaluation_data or {}).get("objectives") or []):
            print(f"warning: {problem}", file=sys.stderr)

    source = args.run if args.run is not None else args.transcript
    transcript = read_run_dir(source) if args.run is not None else read_transcript_file(source)
    result = score_elicitation(rubric, transcript, task_id=spec.task_id)

    _print_result(result, spec.task_id, source)
    if not args.no_write:
        _write_json(result, args.run, source)
    if args.update_report:
        _update_run_report(result, args.run)
    if args.html:
        _write_standalone_html(result, spec.task_id, args.html)
    return 0


def _usage_error(message: str) -> int:
    print(f"usage error: {message}", file=sys.stderr)
    return 2


def _print_result(result: ElicitationResult, task_id: str, source: pathlib.Path) -> None:
    """The summary a person reads. Leads with the status, because on two of the four statuses there are
    no rates at all -- and an unmeasurable run silently printing 0.00 is exactly the misreading the whole
    status design exists to prevent. `no_exchanges` is NOT one of them: an agent that never contacted the
    stakeholder scores a real zero, and it prints as a number like any other."""
    print(f"\ntask       {task_id}")
    print(f"source     {source}")
    print(f"status     {result.status}")
    print(f"exchanges  {result.num_exchanges}")
    print(f"items      {result.total_items}")
    if result.error_items:
        # The rates are over the judged items only, so the denominator has to be visible next to them.
        print(f"judged     {result.judged_items}  ({result.error_items} could not be judged and are "
              f"excluded, not counted against the agent)")

    if result.status == STATUS_SCORED or result.elicited_rate is not None:
        denom = (f"  (of {result.judged_items} judged)" if result.error_items else "")
        print(f"\n  rates{denom}")
        print(f"\n  elicited              {_pct(result.elicited_rate)}")
        print(f"  asked, not elicited   {_pct(result.asked_not_elicited_rate)}")
        print(f"  never asked           {_pct(result.missed_rate)}")
    else:
        print("\n  no rates -- nothing was measured (this is NOT a score of zero)")

    if result.items:
        print("\n  per item:")
        width = max(len(i.item_id) for i in result.items)
        for item in result.items:
            print(f"    {item.item_id:<{width}}  {item.verdict}")
            detail = item.error or item.evidence
            if detail:
                print(f"    {'':<{width}}  > {_one_line(detail)}")

    for warning in result.warnings:
        print(f"\n  note: {warning}")
    print()


def _one_line(text: str, limit: int = 96) -> str:
    """Squash a quote onto one terminal line. The full text is in the JSON and the HTML -- this is the
    at-a-glance view, and a multi-line quote per item would bury the table it belongs to."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _write_json(result: ElicitationResult, run: pathlib.Path | None,
                source: pathlib.Path) -> None:
    base = run if run is not None else (source if source.is_dir() else source.parent)
    path = base / "elicitation.json"
    path.write_text(json.dumps(result.to_dict(), indent=2))
    print(f"wrote {path}")


def _update_run_report(result: ElicitationResult, run: pathlib.Path) -> None:
    """Fold the result into the run's own report instead of adding a second page.

    A reviewer opens one page per run. And the section's whole job is to not read like a scored
    objective, which only means something with the objectives on the page beside it.

    The report is found relative to the run directory, NOT via the report's own `store_root`: that
    field records a per-run temp directory that no longer exists once the run is over.
    """
    report_json = run / "eval" / "report.json"
    if not report_json.is_file():
        print(f"note: no report to update at {report_json} -- skipping --update-report",
              file=sys.stderr)
        return
    report = EvalReport.from_dict(json.loads(report_json.read_text()))
    report.elicitation = result
    report_json.write_text(json.dumps(report.to_dict(), indent=2))

    from appsci_bench.eval.report_html import render_html
    from appsci_bench.eval.store import TranscriptStore

    # Re-render with the run's transcripts, NOT from the report alone: the per-case conversation turns
    # come from the store, so rendering without one silently strips them and this "update" would shrink
    # the report it was asked to add a section to. The store lives beside the report because the
    # orchestrator copied it there; the report's own `store_root` still names the temp dir it was
    # scored in, which is long gone.
    report_html = run / "eval" / "report.html"
    store_root = report_json.parent
    store = TranscriptStore(str(store_root)) if (store_root / "conversations").is_dir() else None
    if store is None:
        print(f"note: no conversations/ beside {report_json} -- the re-rendered report will not "
              f"carry per-case transcripts", file=sys.stderr)
    report_html.write_text(render_html(report, store))
    print(f"updated {report_json}\nupdated {report_html}")


def _write_standalone_html(result: ElicitationResult, task_id: str,
                           dest: pathlib.Path) -> None:
    """A page carrying only the diagnostic, for input that has no report of its own."""
    from appsci_bench.eval.report_html import render_html

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_html(EvalReport(task_id=task_id, elicitation=result)))
    print(f"wrote {dest}")


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.3f}  ({rate * 100:.0f}%)"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
