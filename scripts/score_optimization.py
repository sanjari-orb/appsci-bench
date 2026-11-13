#!/usr/bin/env python
"""Count what one run's optimization loop did, standalone -- for runs that are already finished.

How many times did the agent's optimize_agent loop run? Did it stop early because the scores had
saturated, or because it ran out of cycles? How many of its redeploys failed to build? How many
durable candidates (checkpoints with a `deployed` outcome) did it leave behind? And where did its own
internal-eval score start, peak and end? Those questions are answerable from records every run
already wrote, so this needs no artifact, no deployment, no dataset and no model -- which is the
point: the runs that matter most finished before the metric existed, and re-running them is not an
option.

When the run also holds an eval report, the benchmark's own score for it is printed beside the loop's
final one. They are two different measurements of one artifact (the loop scores its own generated
cases under its own metric names), so the number between them is labelled a distance and never an
error.

NOTHING IS JUDGED HERE, despite the name (which matches its siblings, `score_elicitation.py` and
`score_redteam_trace.py`, so the three are found together). Every number is counted out of the run's
own trace, so two passes over the same run give the same answer and cost nothing.

Usage:
    # one run on this machine
    python scripts/score_optimization.py --run runs/order_to_cash_20260818_173200

    # every run under a directory, printed as a table
    python scripts/score_optimization.py --runs-dir runs

    # fold each result into the run's existing eval report as well
    python scripts/score_optimization.py --runs-dir runs --update-report

    # a sweep's numbers for a notebook
    python scripts/score_optimization.py --runs-dir runs --csv optimization.csv

Each scored run gets `<run>/optimization.json` (skip with --no-write). Runs whose eval report already
carries the diagnostic are re-counted anyway: it is cheap, and the alternative is a flag nobody
remembers to pass after a change to how the loop is read.

Exit status is about whether the tool could run, never about what it found: an unreadable run is
reported and exits zero, a bad flag combination exits non-zero. A diagnostic that failed the shell on
a low number would get wrapped in `|| true` and stop being read.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from appsci_bench.eval.build_optimization import (  # noqa: E402
    AGENT_EVENTS_FILENAME,
    read_optimization,
)
from appsci_bench.eval.datatypes import (  # noqa: E402
    OPTIMIZATION_STATUS_SCORED,
    EvalReport,
    OptimizationResult,
)

#: Where the result lands beside the run, matching `elicitation.json`'s placement.
OUTPUT_FILENAME = "optimization.json"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Count one or more finished runs' optimization loops.",
        # Off deliberately: an abbreviation that becomes ambiguous when a flag is added later turns a
        # working command in someone's notes into an error, and `--run` vs `--runs-dir` are one
        # character apart with very different blast radii.
        allow_abbrev=False,
    )
    ap.add_argument("--run", type=pathlib.Path, action="append", default=[],
                    help="a finished run directory; repeatable")
    ap.add_argument("--runs-dir", type=pathlib.Path,
                    help="score every run directory under here (one level deep)")
    ap.add_argument("--task", default="",
                    help="task id to record on the result; taken from the run's own events when "
                         "omitted, which is what a sweep over mixed tasks wants")
    ap.add_argument("--update-report", action="store_true",
                    help=f"fold each result into the run's EXISTING <run>/eval/report.{{json,html}} "
                         f"as well as writing {OUTPUT_FILENAME}")
    ap.add_argument("--no-write", action="store_true",
                    help=f"print only; do not write {OUTPUT_FILENAME}")
    ap.add_argument("--csv", type=pathlib.Path,
                    help="also write one row per run here, for a notebook or a sweep table")
    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    runs = _resolve_runs(args.run, args.runs_dir)
    if isinstance(runs, int):
        return runs

    results: list[tuple[pathlib.Path, OptimizationResult]] = []
    for run in runs:
        result = read_optimization(run, task_id=args.task)
        results.append((run, result))
        if not args.no_write:
            _write_json(result, run)
        if args.update_report:
            _update_run_report(result, run)

    if len(results) == 1:
        _print_one(*results[0])
    else:
        _print_table(results)
    if args.csv:
        _write_csv(results, args.csv)
    return 0


def _resolve_runs(explicit: list[pathlib.Path],
                  runs_dir: pathlib.Path | None) -> list[pathlib.Path] | int:
    """Which run directories to read, or a non-zero exit for a request that cannot be served.

    A directory is taken as a run when it holds the agent's event log -- the one file this diagnostic
    cannot do without. Selecting by that rather than by directory name means a half-fetched run, a
    README or an artifact directory sitting under `runs/` is skipped instead of being reported as a
    run with no optimization.
    """
    if not explicit and runs_dir is None:
        return _usage_error("pass --run <dir> (repeatable) or --runs-dir <dir>")

    runs = list(explicit)
    missing = [r for r in runs if not (r / AGENT_EVENTS_FILENAME).is_file()]
    for run in missing:
        # Named explicitly, so this is worth saying out loud rather than skipping: the caller believes
        # that directory is a run. It is still scored, and reports itself unmeasurable.
        print(f"note: {run} has no {AGENT_EVENTS_FILENAME} -- it will report as unmeasurable",
              file=sys.stderr)

    if runs_dir is not None:
        if not runs_dir.is_dir():
            return _usage_error(f"--runs-dir {runs_dir} is not a directory")
        found = sorted(d for d in runs_dir.iterdir()
                       if d.is_dir() and (d / AGENT_EVENTS_FILENAME).is_file())
        if not found:
            print(f"nothing to score: no directory under {runs_dir} holds "
                  f"{AGENT_EVENTS_FILENAME}", file=sys.stderr)
        runs.extend(d for d in found if d not in runs)
    return runs


def _usage_error(message: str) -> int:
    print(f"usage error: {message}", file=sys.stderr)
    return 2


# --- output ---------------------------------------------------------------------------------------

def _print_one(run: pathlib.Path, result: OptimizationResult) -> None:
    """The summary a person reads for a single run.

    Leads with the status, because on three of the four there is nothing to count and a bare "0
    optimization runs" would read the same on all of them -- including on the ablation arm, where the
    route did not exist, and on a run whose trace we simply could not read.
    """
    print(f"\nrun        {run}")
    print(f"task       {result.task_id or '(unknown)'}")
    print(f"status     {result.status}")
    if result.status != OPTIMIZATION_STATUS_SCORED:
        print(f"\n  nothing was counted -- {_status_blurb(result)}")
        _print_warnings(result)
        return

    allowed = "no ceiling" if result.cycles_allowed is None else result.cycles_allowed
    spent = "unknown" if result.cycles_used is None else result.cycles_used
    print(f"\n  optimization runs     {result.calls} call(s) of optimize_agent")
    print(f"  measured              {result.scored_calls} scored the candidate, "
          f"{result.failed_calls} measured nothing")
    print(f"  cycles                {spent} spent of {allowed}")
    _print_internal_eval(run, result)
    print(f"\n  stopped because       {result.stop_reason}")
    print(f"  saturated             {_bool(result.saturated)}")
    print(f"  early stop on sat.    {_bool(result.early_stopped_on_saturation)}")
    print(f"  budget exhausted      {_bool(result.budget_exhausted)}")
    print(f"\n  deploys attempted     {result.deploys_attempted}")
    print(f"  deploys succeeded     {result.deploys_succeeded}")
    print(f"  deploys failed        {result.deploys_failed}")
    _print_rollbacks(result)
    if result.checkpoints_valid is None:
        print(f"\n  checkpoints           not counted -- {result.checkpoint_note}")
    else:
        print(f"\n  valid checkpoints     {result.checkpoints_valid} from optimization "
              f"(of {result.checkpoints_in_run} in the run)")
        print(f"  unhealthy / failed    {result.checkpoints_unhealthy} / "
              f"{result.checkpoints_failed}")
        if result.deploys_outside_loop:
            print(f"  deploys outside loop  {result.deploys_outside_loop}")
            print("    > the agent deployed these itself while the loop was in play; the "
                  "keep-or-roll-back rule does not cover them")
        _print_reconciliation(result)

    if result.call_details:
        print("\n  per call:")
        for call in result.call_details:
            score = "n/a" if call.headline_metric is None else f"{call.headline_metric:.3f}"
            change = ("measured only" if not call.changed else
                      "changed + redeployed" if call.redeployed else
                      "changed, would not build (rolled back)")
            print(f"    {call.ordinal:>3}  {str(call.status or '-'):<22} score {score:<7} {change}")
            # What this call decided about the PREVIOUS call's change, on its own line rather than in
            # the change column: the two are about different candidates, and one column holding both
            # read as though a call had kept its own edit.
            if call.previous_change_kept is not None:
                verdict = ("kept the previous change" if call.previous_change_kept
                           else f"rolled the previous change back ({call.rollback})")
                print(f"         · {verdict}")
            detail = call.deploy_error or call.error
            if detail:
                print(f"         > {detail}")
    _print_warnings(result)
    print()


def _print_rollbacks(result: OptimizationResult) -> None:
    """What the loop did with its own changes once the next call had measured them.

    Silent when it reached no verdicts at all: a pair of zeros would read as every change having been
    judged and rejected, when the truth is that nothing was ever judged.
    """
    judged = result.changes_kept + result.changes_rolled_back
    if not judged:
        return
    print(f"\n  changes kept          {result.changes_kept} of {judged} judged")
    print(f"  changes rolled back   {result.changes_rolled_back}")
    print(f"  roll-back redeploys   {result.rollback_redeploys}")
    if result.rollbacks_failed:
        print(f"  roll-backs that did not take  {result.rollbacks_failed}")
        print("    > the code the loop decided against is what stayed running")


def _print_reconciliation(result: OptimizationResult) -> None:
    """Why the deploy tally and the checkpoint tally differ: a restore is a redeploy, not a change.

    Printed only when there is something to reconcile. With no roll-backs the two already agree and
    the line would be noise.
    """
    if not result.rollback_redeploys:
        return
    expected = result.deploys_succeeded + result.rollback_redeploys
    line = (f"{result.deploys_succeeded} redeploy(s) of a change + {result.rollback_redeploys} "
            f"roll-back redeploy(s) = {expected}")
    if expected == result.checkpoints_valid:
        print(f"    > {line}, which is the valid checkpoint count")
    else:
        print(f"    > {line}, against {result.checkpoints_valid} valid checkpoint(s) -- "
              f"{abs(result.checkpoints_valid - expected)} unaccounted for")


def _print_internal_eval(run: pathlib.Path, result: OptimizationResult) -> None:
    """The loop's own scores, and the benchmark's verdict next to them when the run has one.

    Best is printed beside final because a loop can end below its own high-water mark: a change is
    kept when it beats the PREVIOUS measurement, not the best one.
    """
    if result.internal_eval_final is None:
        print("\n  internal eval         no call reported a score, so the trajectory is unknown")
        return

    best = f"{result.internal_eval_best:.3f}"
    if result.internal_eval_best_call is not None:
        best += f" (call {result.internal_eval_best_call})"
    change = ("one measurement only" if result.internal_eval_change is None
              else f"{result.internal_eval_change:+.3f}")
    print(f"\n  internal eval first   {result.internal_eval_first:.3f}")
    print(f"  internal eval best    {best}")
    print(f"  internal eval final   {result.internal_eval_final:.3f}")
    print(f"  internal eval change  {change}")
    if len(result.internal_eval_all_scores) > 1:
        print("  internal eval scores  "
              + " -> ".join(f"{s:.3f}" for s in result.internal_eval_all_scores))
    # Gated on the CANDIDATE differing, not on the numbers. A lower final on the same bytes is the
    # eval re-measuring one program; warning on that was wrong four times out of six in the sweep.
    if result.ended_below_best:
        where = (f" ({result.internal_eval_best_checkpoint})"
                 if result.internal_eval_best_checkpoint else "")
        print(f"    > ended BELOW its own best on a DIFFERENT candidate{where}: the keep rule only "
              f"protects a change the LOOP made, so a deploy the agent made itself can lose the "
              f"high-water candidate")
    elif (result.ended_below_best is False and result.internal_eval_best is not None
            and result.internal_eval_final < result.internal_eval_best):
        print("    > below its best but on the SAME candidate: the loop re-measured one program and "
              "its own eval moved")

    external = _benchmark_score(run)
    if external is not None:
        print(f"  benchmark score       {external:.3f} "
              f"({result.internal_eval_final - external:+.3f} from the loop's final)")
        print("    > a distance, not an error: the loop scores its own generated cases under its "
              "own metric names, the benchmark scores the task's objectives")


def _benchmark_score(run: pathlib.Path) -> float | None:
    """This run's own task score, or None when it has no readable eval report.

    Best-effort on purpose. A build-only cell never reaches an eval, so the absence is ordinary and
    must cost only the comparison line. Bools are excluded from the numeric check because `True` would
    otherwise format as a score of 1.
    """
    try:
        report = json.loads((run / "eval" / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    score = report.get("final_score") if isinstance(report, dict) else None
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def _print_warnings(result: OptimizationResult) -> None:
    for warning in result.warnings:
        print(f"\n  note: {warning}")


def _print_table(results: list[tuple[pathlib.Path, OptimizationResult]]) -> None:
    """One line per run, plus the rollup a sweep is actually after.

    The rollup counts runs, not cycles: "3 of 11 runs stopped early on saturation" is the claim, and
    summing cycles across runs of different budgets would not be one.
    """
    # `default` because zero runs is an ORDINARY call, not a broken one: `--runs-dir runs` against a
    # sweep directory that has not been populated yet is the first thing anyone types, and
    # `_resolve_runs` deliberately lets it through with a note on stderr rather than a usage error.
    # Without the default this crashed there, which contradicts this script's own contract that exit
    # status is about whether the tool could run. The table then degenerates to its header and the
    # "0 run(s) read" line, which is the honest rendering of an empty sweep.
    width = max((len(r.name) for r, _ in results), default=len("run"))
    print(f"\n{'run':<{width}}  {'status':<13} {'calls':>5} {'cycles':>6} "
          f"{'dep ok':>6} {'dep bad':>7} {'rb':>3} {'ckpt':>5} {'int.end':>8} {'int.chg':>8}  "
          f"stopped because")
    for run, result in results:
        cycles = "-" if result.cycles_used is None else result.cycles_used
        ckpt = "-" if result.checkpoints_valid is None else result.checkpoints_valid
        # "-", not 0.000: a loop that reported no score and one that scored zero are different runs.
        final = ("-" if result.internal_eval_final is None
                 else f"{result.internal_eval_final:.3f}")
        change = ("-" if result.internal_eval_change is None
                  else f"{result.internal_eval_change:+.3f}")
        print(f"{run.name:<{width}}  {result.status:<13} {result.calls:>5} {cycles:>6} "
              f"{result.deploys_succeeded:>6} {result.deploys_failed:>7} "
              f"{result.changes_rolled_back:>3} {ckpt:>5} "
              f"{final:>8} {change:>8}  "
              f"{result.stop_reason or '-'}")

    scored = [r for _, r in results if r.status == OPTIMIZATION_STATUS_SCORED]
    print(f"\n{len(results)} run(s) read, {len(scored)} with an optimization loop")
    if scored:
        early = sum(1 for r in scored if r.early_stopped_on_saturation)
        budget = sum(1 for r in scored if r.budget_exhausted)
        failed = sum(1 for r in scored if r.deploys_failed)
        print(f"  {early} stopped early on saturation (budget still unspent)")
        print(f"  {budget} ran out of optimization budget")
        print(f"  {failed} had at least one redeploy that would not build")
        print(f"  {sum(r.deploys_succeeded for r in scored)} redeploy(s) succeeded, "
              f"{sum(r.deploys_failed for r in scored)} failed, in total")
        rolled = [r for r in scored if r.changes_rolled_back]
        if rolled:
            stuck = sum(1 for r in scored if r.rollbacks_failed)
            print(f"  {len(rolled)} rolled back at least one change, "
                  f"{sum(r.changes_rolled_back for r in scored)} roll-back(s) in total; "
                  f"{stuck} had a roll-back that did not take")
        counted = [r for r in scored if r.checkpoints_valid is not None]
        if counted:
            print(f"  {sum(r.checkpoints_valid for r in counted)} valid checkpoint(s) from "
                  f"optimization across {len(counted)} run(s) that keep checkpoints")
            # The claim the roll-back counts exist to support: a restore is a redeploy that is not a
            # change, so once it is counted the two tallies close. Reported as a count of runs where
            # they do NOT, so a future divergence is loud rather than inferred from the table.
            off = [r for r in counted
                   if r.deploys_succeeded + r.rollback_redeploys != r.checkpoints_valid]
            print(f"  {len(counted) - len(off)} of {len(counted)} reconcile: successful redeploys "
                  f"plus roll-back redeploys equals the valid checkpoint count")
        # Over the runs that measured at all: a run with no score is not a run that did not regress.
        outside = [r for r in scored if r.deploys_outside_loop]
        if outside:
            print(f"  {len(outside)} deployed outside the loop at least once "
                  f"({sum(r.deploys_outside_loop for r in outside)} deploy(s) in total), which the "
                  f"keep-or-roll-back rule does not cover")
        measured = [r for r in scored if r.internal_eval_final is not None
                    and r.internal_eval_best is not None]
        if measured:
            # Split, because the two are different findings. Only the first is about the artifact;
            # the second is the loop's own eval returning a different number for the same program.
            worse = sum(1 for r in measured if r.ended_below_best)
            noise = sum(1 for r in measured
                        if r.ended_below_best is False
                        and r.internal_eval_final < r.internal_eval_best)
            improved = sum(1 for r in measured
                           if r.internal_eval_change is not None and r.internal_eval_change > 0)
            print(f"  {improved} of {len(measured)} run(s) with a measured trajectory improved on "
                  f"their own eval")
            print(f"  {worse} ended on a WORSE candidate than their best; {noise} scored below their "
                  f"best on the same candidate (the eval re-measuring, not the loop losing ground)")
    print()


def _status_blurb(result: OptimizationResult) -> str:
    return {
        "disabled": "this run had no optimization route (the ablation arm)",
        "not_attempted": "the agent had the route and never called it (a real zero about the agent)",
        "unmeasurable": "the run's trace could not be read, so the loop is unknown (NOT a run "
                        "without optimization)",
    }.get(result.status, result.status)


def _bool(value: bool | None) -> str:
    """Three-valued on purpose: unknown is not False. A run whose last call never reported back does
    not tell us whether its trajectory had saturated."""
    return "unknown" if value is None else ("yes" if value else "no")


def _write_json(result: OptimizationResult, run: pathlib.Path) -> None:
    path = run / OUTPUT_FILENAME
    try:
        path.write_text(json.dumps(result.to_dict(), indent=2))
    except OSError as e:
        print(f"note: could not write {path}: {e}", file=sys.stderr)
        return
    print(f"wrote {path}")


def _write_csv(results: list[tuple[pathlib.Path, OptimizationResult]],
               dest: pathlib.Path) -> None:
    """One row per run, and one value per column -- the per-call detail is in each optimization.json,
    and a CSV that tried to carry it would need a second table nobody asked for. The internal-eval
    columns are the run-level summary for the same reason: the per-call scores are the second table."""
    columns = ["run", "task_id", "status", "optimization_enabled", "calls", "scored_calls",
               "failed_calls", "cycles_used", "cycles_allowed", "saturated",
               "early_stopped_on_saturation", "budget_exhausted", "stop_reason",
               "deploys_attempted", "deploys_succeeded", "deploys_failed",
               "changes_kept", "changes_rolled_back", "rollback_redeploys", "rollbacks_failed",
               "internal_eval_first", "internal_eval_best", "internal_eval_best_call",
               "internal_eval_best_checkpoint",
               "internal_eval_final", "internal_eval_change", "internal_eval_all_scores",
               "ended_below_best",
               "checkpoints_valid",
               "checkpoints_unhealthy", "checkpoints_failed", "checkpoints_in_run",
               "deploys_before_loop", "deploys_outside_loop"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for run, result in results:
            row = result.to_dict()
            # The one list in an otherwise scalar table, pipe-joined rather than written as a Python
            # repr: a cell reading "[0.5, 0.7]" needs its own parser and would break on a comma in a
            # CSV that a spreadsheet re-quotes.
            row["internal_eval_all_scores"] = "|".join(
                f"{s:.6f}" for s in result.internal_eval_all_scores)
            writer.writerow({c: (run.name if c == "run" else row.get(c)) for c in columns})
    print(f"wrote {dest}")


def _update_run_report(result: OptimizationResult, run: pathlib.Path) -> None:
    """Fold the result into the run's own report instead of adding a second page.

    A reviewer opens one page per run, and this section only means something with the objectives on
    the page beside it. Found relative to the run directory, NOT via the report's own `store_root`:
    that field records a per-run temp directory that no longer exists once the run is over.
    """
    report_json = run / "eval" / "report.json"
    if not report_json.is_file():
        print(f"note: no report to update at {report_json} -- skipping --update-report",
              file=sys.stderr)
        return
    report = EvalReport.from_dict(json.loads(report_json.read_text()))
    report.optimization = result
    report_json.write_text(json.dumps(report.to_dict(), indent=2))

    from appsci_bench.eval.report_html import render_html
    from appsci_bench.eval.store import TranscriptStore

    # Re-rendered with the run's transcripts, NOT from the report alone: the per-case conversation
    # turns come from the store, so rendering without one silently strips them and this "update" would
    # shrink the report it was asked to add a section to.
    report_html = run / "eval" / "report.html"
    store_root = report_json.parent
    store = TranscriptStore(str(store_root)) if (store_root / "conversations").is_dir() else None
    if store is None:
        print(f"note: no conversations/ beside {report_json} -- the re-rendered report will not "
              f"carry per-case transcripts", file=sys.stderr)
    report_html.write_text(render_html(report, store))
    print(f"updated {report_json}\nupdated {report_html}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
