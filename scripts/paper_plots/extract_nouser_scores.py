"""MMS@3 per cell for the no-user arm, from the campaign run log, through score_power.py.

The no-user arm gives the builder its requirements as documents instead of a simulated stakeholder
to ask. Its runs are logged one row per eval trial in `data/job_results/nouser_campaign_20260924.csv` (an export
of the campaign's run log, kept as the record). Each arm there is named like
`crm-nouser-3x1-opus48-opt5`: task, 3 builds x 1 trial, model, optimization off (`noopt`) or on
(`opt5`). A few arms from the first no-user runs predate that naming; LEGACY_ARMS says what they
are.

Steps:
  1. keep rows marked exactly VALID; every INVALID row (a cancelled or failed build, a trial cancelled
     mid-run, a run from before the crm_insights database fix) is ignored;
  2. group each cell's trials by build, and keep the LATEST valid build in each build slot (b0, b1,
     b2). A slot is re-run when its first attempt was invalid; where an earlier attempt was valid
     too, the later complete set is kept and the earlier one is marked superseded, never mixed in;
  3. run score_power.py on each cell's builds (one --build per slot) and read back its JSON;
  4. check every trial's score in score_power's eval report against the run log's task accuracy,
     and stop if they differ, since then one of the two does not describe the same run.

Writes two intermediate tables, both in data/:

  nouser_trials.csv                every VALID row, with the build slot, and whether it was used
                                   (and why not, if not);
  results_nouser_score_pow_k.csv   one row per task x model x optimization arm, in the same columns
                                   as results_score_pow_k.csv, so data.load_results reads it.

score_power.py reads each eval report from the local runs directory or the S3 run mirror, so this
needs AWS credentials:

    AWS_PROFILE=appsci-bench .venv/bin/python scripts/paper_plots/extract_nouser_scores.py
    .venv/bin/python scripts/paper_plots/extract_nouser_scores.py --profile appsci-bench [CSV]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import MODELS, NOUSER_RESULTS_CSV, TASKS  # noqa: E402

HERE = Path(__file__).resolve().parent
CAMPAIGN_CSV = HERE / "data" / "job_results" / "nouser_campaign_20260924.csv"
TRIALS_CSV = HERE / "data" / "nouser_trials.csv"
SCORE_POWER = HERE.parent / "score_power.py"

# The task and model slots of an arm name, mapped to the repo's ids.
ARM_TASKS = {
    "airline": "airline_rebooking",
    "banking": "banking_support",
    "cci": "customer_call_insights",
    "compliance": "compliance_review",
    "crm": "crm_insights",
    "o2c": "order_to_cash",
    "slides": "slides_generator",
}
ARM_MODELS = {"opus48": "claude-opus-4-8", "opus5": "claude-opus-5"}
ARM_OPT = {"noopt": False, "opt5": True}
ARM = re.compile(r"(?P<task>[a-z0-9]+)-nouser-(?P<builds>\d+)x(?P<trials>\d+)-(?P<model>[a-z0-9]+)"
                 r"-(?P<opt>noopt|opt5)")


@dataclass(frozen=True)
class ArmSpec:
    task: str
    model: str
    opt_enabled: bool
    planned_builds: int
    fills_slot: int | None = None  # a one-build re-run standing in for this slot of a 3-build arm


# Arms from the first no-user runs (2026-09-21), named before the campaign's naming scheme and
# without the optimization setting in the name. All of them ran with the optimization layer ON:
# every build's run manifest lists the optimizer's score_history.json, which an optimization-off
# build does not write (checked build by build on 2026-09-25, against a known on and off build).
# The 1x1 airline arm re-ran the 3x1 arm's cancelled b2; its one build is named b0 but fills b2.
LEGACY_ARMS = {
    "no-user 3x1 o2c opus-5": ArmSpec("order_to_cash", "claude-opus-5", True, 3),
    "no-user 3x1 o2c opus-4.8": ArmSpec("order_to_cash", "claude-opus-4-8", True, 3),
    "no-user 3x1 airline opus-5": ArmSpec("airline_rebooking", "claude-opus-5", True, 3),
    "no-user 1x1 airline opus-5 (b2 replacement, 4 lanes)":
        ArmSpec("airline_rebooking", "claude-opus-5", True, 3, fills_slot=2),
}
# The run log's Task column holds either the repo id or the results sheet's name for the task.
SHEET_TASK_NAMES = {
    "Airline": "airline_rebooking",
    "O2C": "order_to_cash",
    "Customer-Call Insights": "customer_call_insights",
    "Compliance": "compliance_review",
    "Slides": "slides_generator",
    "CRM Insights": "crm_insights",
    "Banking": "banking_support",
}
# A no-user build id: <task>_<model>_b<slot>_nouser_<YYYYMMDD_HHMMSS>. The timestamp orders re-runs
# of one slot; it sorts correctly as text because every field is fixed width.
BUILD_ID = re.compile(r"(?P<prefix>.+)_b(?P<slot>\d+)_nouser_(?P<stamp>\d{8}_\d{6})")
VALID = "VALID"
# score_power's scores come from the eval report, the run log's from the same report rounded to 4
# places; anything past rounding means the two are not the same run.
ACCURACY_TOLERANCE = 0.00051
OUT_FIELDS = ["task", "model", "opt_enabled", "k_builds", "score_pow_k_mean", "score_pow_k_std",
              "score_pow_k_random_trial", "worst_k3", "trial_mean", "trial_std", "note"]
TRIAL_FIELDS = ["task", "model", "opt_enabled", "arm", "build_slot", "build_id", "eval_run_id",
                "task_accuracy", "used", "why_not_used"]


@dataclass(frozen=True)
class Trial:
    task: str
    model: str
    opt_enabled: bool
    arm: str
    planned_builds: int
    slot: int
    stamp: str
    build_id: str
    eval_run_id: str
    accuracy: float


def _accuracy(raw: str, row_no: int) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"row {row_no}: task accuracy {raw!r} is not a number") from exc
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"row {row_no}: task accuracy {value} is outside [0, 1]")
    return value


def arm_spec(arm: str, row_no: int) -> ArmSpec:
    """What an arm name means: a campaign name like crm-nouser-3x1-opus48-opt5, or a listed legacy one."""
    if arm in LEGACY_ARMS:
        return LEGACY_ARMS[arm]
    m = ARM.fullmatch(arm)
    if not m or m["task"] not in ARM_TASKS or m["model"] not in ARM_MODELS:
        raise ValueError(f"row {row_no}: arm {arm!r} is not <task>-nouser-<B>x<T>-<model>-<opt> "
                         f"with a known task and model, nor a listed legacy arm")
    return ArmSpec(ARM_TASKS[m["task"]], ARM_MODELS[m["model"]], ARM_OPT[m["opt"]],
                   int(m["builds"]))


def parse_row(row: dict[str, str], row_no: int) -> Trial:
    """One VALID row as a Trial. Any field that disagrees with another stops the run."""
    arm = row["Arm"].strip()
    spec = arm_spec(arm, row_no)
    task, model = spec.task, spec.model
    if task not in TASKS or model not in MODELS:
        raise ValueError(f"row {row_no}: {task}/{model} is not a task and model data.py knows")
    row_task = row["Task"].strip()
    if SHEET_TASK_NAMES.get(row_task, row_task) != task or row["Model"].strip() != model:
        raise ValueError(f"row {row_no}: arm {arm!r} says {task}/{model} but the row says "
                         f"{row_task}/{row['Model'].strip()}")
    build_id, run_id = row["Build ID"].strip(), row["Eval Run ID"].strip()
    b = BUILD_ID.fullmatch(build_id)
    if not b or b["prefix"] != f"{task}_{model}":
        raise ValueError(f"row {row_no}: build id {build_id!r} does not name {task}_{model}")
    slot = int(b["slot"])
    if spec.fills_slot is not None:
        if slot != 0:
            raise ValueError(f"row {row_no}: a one-build re-run should be b0, got {build_id!r}")
        slot = spec.fills_slot
    # The trial's run id repeats the build's prefix and slot: <task>_<model>_b<slot>.t<n>_nouser_...
    if not run_id.startswith(f"{task}_{model}_b{b['slot']}.t"):
        raise ValueError(f"row {row_no}: eval run {run_id!r} does not belong to build {build_id!r}")
    if not row["Task Accuracy"].strip():
        # A valid build whose eval never ran would be an empty bucket (scored 0) in score_power.
        # None exist today; one appearing is a decision to make, not something to default.
        raise ValueError(f"row {row_no}: marked VALID but has no task accuracy")
    return Trial(task=task, model=model, opt_enabled=spec.opt_enabled, arm=arm,
                 planned_builds=spec.planned_builds, slot=slot, stamp=b["stamp"],
                 build_id=build_id, eval_run_id=run_id,
                 accuracy=_accuracy(row["Task Accuracy"].strip(), row_no))


def load_valid(path: Path) -> list[Trial]:
    trials: list[Trial] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            if not any((v or "").strip() for v in row.values()):
                continue  # a blank line a spreadsheet export leaves behind
            if row["Valid/Invalid"].strip() != VALID:
                continue
            t = parse_row(row, row_no)
            if t.eval_run_id in seen:
                raise ValueError(f"row {row_no}: eval run {t.eval_run_id!r} appears twice")
            seen.add(t.eval_run_id)
            trials.append(t)
    if not trials:
        raise ValueError(f"{path} has no VALID rows")
    return trials


Cell = tuple[str, str, bool]  # task, model, opt_enabled


def select_builds(trials: list[Trial]) -> tuple[dict[Cell, list[list[Trial]]], dict[str, str]]:
    """Per cell, the trials of the latest valid build in each slot, slots in order.

    Returns (cell -> one list of trials per slot, build id -> why it was not used).
    """
    by_cell: dict[Cell, dict[int, dict[str, list[Trial]]]] = {}
    planned_by_cell: dict[Cell, set[int]] = {}
    for t in trials:
        cell = (t.task, t.model, t.opt_enabled)
        by_cell.setdefault(cell, {}).setdefault(t.slot, {}).setdefault(t.build_id, []).append(t)
        planned_by_cell.setdefault(cell, set()).add(t.planned_builds)
    chosen: dict[Cell, list[list[Trial]]] = {}
    dropped: dict[str, str] = {}
    for cell, slots in by_cell.items():
        # A cell can draw on more than one arm (a re-run of one slot), but they must plan the
        # same number of builds, or k would mean different things across its slots.
        if len(planned_by_cell[cell]) != 1:
            raise ValueError(f"{cell} mixes arms planning {sorted(planned_by_cell[cell])} builds")
        (planned,) = planned_by_cell[cell]
        if len(slots) > planned or max(slots) >= planned:
            raise ValueError(f"{cell} has build slots {sorted(slots)} but its arm plans {planned}")
        buckets = []
        for slot in sorted(slots):
            builds = slots[slot]
            latest = max(builds, key=lambda bid: builds[bid][0].stamp)
            for bid in builds:
                if bid != latest:
                    dropped[bid] = f"superseded by the later valid build in slot b{slot}"
            buckets.append(sorted(builds[latest], key=lambda t: t.eval_run_id))
        chosen[cell] = buckets
    return chosen, dropped


def run_score_power(buckets: list[list[Trial]], env: dict[str, str]) -> dict:
    """score_power.py's JSON for one cell. A failure stops the run with score_power's own message."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "score_power.json"
        argv = [sys.executable, str(SCORE_POWER)]
        for bucket in buckets:
            argv += ["--build", *[t.eval_run_id for t in bucket]]
        argv += ["--json", str(out)]
        done = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
        if done.returncode != 0:
            raise RuntimeError(f"score_power.py exited {done.returncode}:\n{done.stderr.strip()}")
        return json.loads(out.read_text(encoding="utf-8"))


def check_against_log(cell: Cell, buckets: list[list[Trial]], result: dict) -> None:
    """score_power's per-trial scores must be the run log's task accuracies, build by build."""
    if len(result["builds"]) != len(buckets):
        raise ValueError(f"{cell}: score_power returned {len(result['builds'])} builds, "
                         f"asked for {len(buckets)}")
    for bucket, build in zip(buckets, result["builds"], strict=True):
        if build["run_ids"] != [t.eval_run_id for t in bucket]:
            raise ValueError(f"{cell}: score_power scored {build['run_ids']}, asked for "
                             f"{[t.eval_run_id for t in bucket]}")
        for t, score in zip(bucket, build["scores"], strict=True):
            if abs(t.accuracy - score) > ACCURACY_TOLERANCE:
                raise ValueError(f"{cell}: {t.eval_run_id} scores {score:.4f} in its eval report "
                                 f"but {t.accuracy:.4f} in the run log")


def result_row(cell: Cell, buckets: list[list[Trial]], result: dict) -> dict[str, str]:
    task, model, opt = cell
    n_trials = sorted({len(b) for b in buckets})
    note = f"{len(buckets)} builds x {'/'.join(map(str, n_trials))} trial(s), {result['n_examples']} cases"
    if result["warnings"]:
        note += "; score_power warned: " + " | ".join(result["warnings"])
    return {
        "task": task, "model": model, "opt_enabled": "true" if opt else "false",
        "k_builds": str(result["k"]),
        "score_pow_k_mean": f"{result['score']:.4f}",
        "score_pow_k_std": f"{result['std']:.4f}",
        "score_pow_k_random_trial": f"{result['random_pick']:.4f}",
        "worst_k3": f"{result['worst']:.4f}",
        "trial_mean": f"{result['average']:.4f}",
        "trial_std": f"{result['average_std_across_builds']:.4f}",
        "note": note,
    }


def _order(task: str, model: str, opt: bool) -> tuple[int, int, bool]:
    return list(TASKS).index(task), list(MODELS).index(model), not opt


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("campaign_csv", nargs="?", type=Path, default=CAMPAIGN_CSV,
                   help=f"the campaign run log export (default: {CAMPAIGN_CSV.name})")
    p.add_argument("--profile", help="AWS profile for the run mirror (default: $AWS_PROFILE)")
    args = p.parse_args()
    if not args.campaign_csv.is_file():
        sys.exit(f"no such file: {args.campaign_csv}")
    env = dict(os.environ)
    if args.profile:
        env["AWS_PROFILE"] = args.profile
    if not env.get("AWS_PROFILE"):
        # Without a profile the aws CLI finds no credentials and score_power aborts on the first
        # fetch; saying so up front is clearer than its mirror error.
        sys.exit("set AWS_PROFILE or pass --profile: score_power.py reads eval reports from S3")

    trials = load_valid(args.campaign_csv)
    chosen, dropped = select_builds(trials)

    rows = []
    for cell in sorted(chosen, key=lambda c: _order(*c)):
        buckets = chosen[cell]
        print(f"{cell[0]} / {cell[1]} / opt {'on' if cell[2] else 'off'}: "
              f"{len(buckets)} builds ...", file=sys.stderr)
        result = run_score_power(buckets, env)
        check_against_log(cell, buckets, result)
        rows.append(result_row(cell, buckets, result))

    with NOUSER_RESULTS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with TRIALS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRIAL_FIELDS)
        writer.writeheader()
        for t in sorted(trials, key=lambda t: (*_order(t.task, t.model, t.opt_enabled), t.slot,
                                               t.build_id, t.eval_run_id)):
            writer.writerow({
                "task": t.task, "model": t.model, "opt_enabled": "true" if t.opt_enabled else "false",
                "arm": t.arm, "build_slot": f"b{t.slot}", "build_id": t.build_id,
                "eval_run_id": t.eval_run_id, "task_accuracy": f"{t.accuracy:.4f}",
                "used": "false" if t.build_id in dropped else "true",
                "why_not_used": dropped.get(t.build_id, ""),
            })

    print(f"wrote {NOUSER_RESULTS_CSV} ({len(rows)} cells) and {TRIALS_CSV} ({len(trials)} valid "
          f"trials, {sum(1 for t in trials if t.build_id in dropped)} superseded)", file=sys.stderr)


if __name__ == "__main__":
    main()
