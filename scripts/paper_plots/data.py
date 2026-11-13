"""Load the benchmark results table and hold the display names and orders used across figures.

The table (`data/results_score_pow_k.csv`) is a cleaned copy of the "AppSci Bench Results (score^3
scores)" sheet: one row per task x model x optimization arm. Blank cells mean the cell has no result
yet. Loading checks every value, so a bad edit to the CSV fails the plot instead of drawing it.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

RESULTS_CSV = Path(__file__).resolve().parent / "data" / "results_score_pow_k.csv"
# The same columns for the no-user arm (requirements given as documents, no stakeholder to ask),
# written by extract_nouser_scores.py. Only the models the arm was run on have rows, so read it with
# load_results, not grid.
NOUSER_RESULTS_CSV = Path(__file__).resolve().parent / "data" / "results_nouser_score_pow_k.csv"

# Column order in figures, and the label shown for each task.
TASKS: dict[str, str] = {
    "airline_rebooking": "τ-bench: Airlines",
    "order_to_cash": "Order to cash",
    "customer_call_insights": "Customer call insights",
    "compliance_review": "Compliance review",
    "slides_generator": "Slides generator",
    "crm_insights": "CRM insights",
    "banking_support": "τ-bench: Banking",
}

# For tight spaces (radar spokes, small panels).
TASKS_SHORT: dict[str, str] = {
    "airline_rebooking": "τ-Airlines",
    "order_to_cash": "O2C",
    "customer_call_insights": "Calls",
    "compliance_review": "Compliance",
    "slides_generator": "Slides",
    "crm_insights": "CRM",
    "banking_support": "τ-Banking",
}

MODELS: dict[str, str] = {
    "claude-opus-5": "Claude Opus 5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "gpt-5.5": "GPT-5.5",
    "gpt-oss-120b": "GPT-OSS-120B",
    "kimi-k3": "Kimi K3",
}

# For tight legends.
MODELS_SHORT: dict[str, str] = {
    "claude-opus-5": "Opus 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-4-5": "Haiku 4.5",
    "gpt-5.5": "GPT-5.5",
    "gpt-oss-120b": "GPT-OSS-120B",
    "kimi-k3": "Kimi K3",
}

# The k every cell is meant to have. Fewer means some builds did not finish (a timeout, the turn
# limit) and were dropped rather than scored as zero; figures mark those cells.
FULL_BUILDS = 3


@dataclass(frozen=True)
class Result:
    task: str
    model: str
    opt_enabled: bool
    k_builds: int | None
    score: float | None  # MMS@k (mean minimum score @ k), mean over resampled trial picks
    score_std: float | None
    trial_mean: float | None  # plain mean of trial scores, for reference
    trial_std: float | None
    note: str


def _optional_float(raw: str, field: str, row_no: int, *, lo: float, hi: float) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"row {row_no}: {field}={raw!r} is not a number") from exc
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"row {row_no}: {field}={value} is outside [{lo}, {hi}]")
    return value


def _optional_int(raw: str, field: str, row_no: int, *, lo: int, hi: int) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        raise ValueError(f"row {row_no}: {field}={raw!r} is not a whole number in [{lo}, {hi}]")
    return int(raw)


def load_results(path: Path = RESULTS_CSV) -> list[Result]:
    results: list[Result] = []
    seen: set[tuple[str, str, bool]] = set()
    with path.open(newline="", encoding="utf-8") as f:
        # Row numbers count the header as row 1, so they match what a spreadsheet shows.
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            task, model = row["task"].strip(), row["model"].strip()
            if task not in TASKS:
                raise ValueError(f"row {row_no}: unknown task {task!r}")
            if model not in MODELS:
                raise ValueError(f"row {row_no}: unknown model {model!r}")
            opt_raw = row["opt_enabled"].strip().lower()
            if opt_raw not in ("true", "false"):
                raise ValueError(f"row {row_no}: opt_enabled must be true or false, got {opt_raw!r}")
            opt = opt_raw == "true"
            key = (task, model, opt)
            if key in seen:
                raise ValueError(f"row {row_no}: duplicate row for {key}")
            seen.add(key)

            score = _optional_float(row["score_pow_k_mean"], "score_pow_k_mean", row_no, lo=0, hi=1)
            k = _optional_int(row["k_builds"], "k_builds", row_no, lo=1, hi=FULL_BUILDS)
            if score is not None and k is None:
                raise ValueError(f"row {row_no}: has a score but no k_builds")
            results.append(Result(
                task=task,
                model=model,
                opt_enabled=opt,
                k_builds=k,
                score=score,
                score_std=_optional_float(row["score_pow_k_std"], "score_pow_k_std", row_no, lo=0, hi=1),
                trial_mean=_optional_float(row["trial_mean"], "trial_mean", row_no, lo=0, hi=1),
                trial_std=_optional_float(row["trial_std"], "trial_std", row_no, lo=0, hi=1),
                note=row["note"].strip(),
            ))
    return results


def grid(
    results: list[Result], *, opt_enabled: bool
) -> dict[tuple[str, str], Result]:
    """(model, task) -> result for one arm. Every model x task pair is present, scored or not."""
    cells = {(r.model, r.task): r for r in results if r.opt_enabled == opt_enabled}
    missing = [(m, t) for m in MODELS for t in TASKS if (m, t) not in cells]
    if missing:
        raise ValueError(f"results table has no row for {missing}")
    return cells


def shared_tasks(cells: dict[tuple[str, str], Result]) -> list[str]:
    """Tasks every model has a score on, in TASKS order.

    Averages across models are taken over these only, so a model with a missing cell is neither
    helped nor hurt by the gap.
    """
    shared = [t for t in TASKS if all(cells[(m, t)].score is not None for m in MODELS)]
    if not shared:
        raise ValueError("no task has a score for every model, so there is nothing fair to average")
    return shared


def mean_score(cells: dict[tuple[str, str], Result], model: str, tasks: list[str]) -> float:
    if not tasks:
        raise ValueError("cannot average over zero tasks")
    scores = [cells[(model, t)].score for t in tasks]
    if any(s is None for s in scores):
        raise ValueError(f"{model} has no score on one of {tasks}")
    return sum(scores) / len(scores)  # type: ignore[arg-type]


def rank_models(cells: dict[tuple[str, str], Result]) -> list[str]:
    """Models best first, by mean score over the shared tasks. Ties keep the MODELS order."""
    tasks = shared_tasks(cells)
    return sorted(MODELS, key=lambda m: -mean_score(cells, m, tasks))


def rank_tasks(cells: dict[tuple[str, str], Result]) -> list[str]:
    """Tasks easiest first, by mean score over the models that have a result there.

    Every task in TASKS is returned; one with no scores at all sorts last. Ties keep the TASKS order.
    """
    def task_mean(task: str) -> float:
        scores = [cells[(m, task)].score for m in MODELS if cells[(m, task)].score is not None]
        return sum(scores) / len(scores) if scores else -1.0  # type: ignore[arg-type]

    return sorted(TASKS, key=lambda t: -task_mean(t))


# --- Requirement elicitation vs task accuracy -------------------------------------------------
#
# `data/elicitation_trials.csv` is a trimmed copy of the per-trial run log sheet (see
# import_elicitation_sheet.py): one row per eval trial, with its build's elicitation score. It is
# the optimization-enabled arm only.

ELICITATION_CSV = Path(__file__).resolve().parent / "data" / "elicitation_trials.csv"
# Marks that count as valid. "VALID*" is a trial the sheet keeps with a caveat (some cases errored).
VALID_STATUSES = frozenset({"VALID", "VALID*"})
# A metric's name in the breakdown: lowercase words joined by underscores.
_METRIC_NAME = re.compile(r"[a-z][a-z0-9_]*")


def parse_breakdown(raw: str, where: str) -> dict[str, float | None] | None:
    """The sheet's "Broken Down Accuracy" cell as {metric: score}.

    The cell is "name score name score ...", for example
    "policy_compliance 0.621 safety_no_fabrication 0.520". A score of DROPPED means the metric was
    not scored on that trial and maps to None. A blank cell, or a bare number (the sheet writes the
    accuracy alone when an eval produced no per-metric scores), has no breakdown: None. Anything
    else is an error naming `where`, so a malformed cell stops the run instead of losing a metric.
    """
    tokens = raw.split()
    if not tokens:
        return None
    if len(tokens) == 1:
        try:
            float(tokens[0])
        except ValueError:
            raise ValueError(f"{where}: breakdown {raw!r} is not 'name score' pairs") from None
        return None
    if len(tokens) % 2:
        raise ValueError(f"{where}: breakdown {raw!r} has a name without a score")
    metrics: dict[str, float | None] = {}
    for name, score in zip(tokens[::2], tokens[1::2], strict=True):
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError(f"{where}: {name!r} in the breakdown is not a metric name")
        if name in metrics:
            raise ValueError(f"{where}: metric {name!r} appears twice in the breakdown")
        if score == "DROPPED":
            metrics[name] = None
            continue
        try:
            value = float(score)
        except ValueError:
            raise ValueError(f"{where}: {name}={score!r} is not a number") from None
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{where}: {name}={value} is outside [0, 1]")
        metrics[name] = value
    return metrics


@dataclass(frozen=True)
class Trial:
    task: str
    model: str
    build_id: str
    eval_run_id: str
    status: str  # VALID, VALID*, or INVALID
    elicited: float | None  # share of the build's hidden requirements the agent drew out
    accuracy: float | None  # the trial's task accuracy; blank when the eval never ran
    # Per-metric scores from the eval (see parse_breakdown); None when the sheet has none.
    metrics: dict[str, float | None] | None = None
    build_error: str = ""  # first line of the sheet's "Build Error" cell; empty when the build was clean
    build_tokens: int | None = None  # tokens the build phase used; the sheet repeats it per trial


# Capability tiers, weakest first, for grouping models where 7 separate ones would be too few
# points each.
MODEL_TIERS: dict[str, tuple[str, ...]] = {
    "Weak": ("claude-haiku-4-5", "gpt-oss-120b"),
    "Medium": ("claude-sonnet-4-6", "gpt-5.5"),
    "Strong": ("kimi-k3", "claude-opus-4-8", "claude-opus-5"),
}


@dataclass(frozen=True)
class BuildPoint:
    """One build: its elicitation score and its mean task accuracy over its scored trials."""
    task: str
    model: str
    build_id: str
    elicited: float
    accuracy: float
    n_trials: int


def load_trials(path: Path = ELICITATION_CSV) -> list[Trial]:
    trials: list[Trial] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            task, model = row["task"].strip(), row["model"].strip()
            if task not in TASKS:
                raise ValueError(f"row {row_no}: unknown task {task!r}")
            if model not in MODELS:
                raise ValueError(f"row {row_no}: unknown model {model!r}")
            status = row["status"].strip()
            if status not in VALID_STATUSES | {"INVALID"}:
                raise ValueError(f"row {row_no}: unknown status {status!r}")
            build_id = row["build_id"].strip()
            if not build_id:
                raise ValueError(f"row {row_no}: no build_id")
            run_id = row["eval_run_id"].strip()
            if run_id:  # a build whose eval never ran has no run id; any real one appears once
                if run_id in seen:
                    raise ValueError(f"row {row_no}: duplicate eval run {run_id!r}")
                seen.add(run_id)
            trials.append(Trial(
                task=task, model=model, build_id=build_id, eval_run_id=run_id, status=status,
                elicited=_optional_float(row["elicited"], "elicited", row_no, lo=0, hi=1),
                accuracy=_optional_float(row["task_accuracy"], "task_accuracy", row_no, lo=0, hi=1),
                # Older copies of the table have no breakdown column; treat that as no breakdown.
                metrics=parse_breakdown(row.get("breakdown") or "", f"row {row_no}"),
                build_error=(row.get("build_error") or "").strip(),
                build_tokens=_optional_int(row.get("build_tokens") or "", "build_tokens", row_no,
                                           lo=0, hi=10**12),
            ))
    return trials


def usable_trials(trials: list[Trial]) -> list[Trial]:
    """Valid trials that have both an elicitation score and a task accuracy.

    A valid build whose eval never ran has no accuracy. It is left out rather than scored 0: the
    question is how elicitation relates to accuracy, and there is no accuracy to relate.
    """
    return [t for t in trials
            if t.status in VALID_STATUSES and t.elicited is not None and t.accuracy is not None]


def elicitation_builds(trials: list[Trial]) -> list[BuildPoint]:
    """One point per build over the given trials, in first-seen order.

    Averaging per build gives each build one vote, so a build with three trials does not outweigh
    one with a single trial. Elicitation belongs to the build, but the sheet repeats it on every
    trial and a few builds carry slightly different values across their trials, so it is averaged
    the same way.
    """
    builds: dict[tuple[str, str, str], list[Trial]] = {}
    for t in trials:
        if t.elicited is None or t.accuracy is None:
            raise ValueError(f"trial {t.eval_run_id or t.build_id} is missing a value")
        builds.setdefault((t.model, t.task, t.build_id), []).append(t)
    return [
        BuildPoint(
            task=task, model=model, build_id=build_id,
            elicited=sum(t.elicited for t in ts) / len(ts),  # type: ignore[misc]
            accuracy=sum(t.accuracy for t in ts) / len(ts),  # type: ignore[misc]
            n_trials=len(ts),
        )
        for (model, task, build_id), ts in builds.items()
    ]


# --- Build reliability ------------------------------------------------------------------------

# How a valid build ended. A build is valid when at least one of its rows is; builds whose every row
# is INVALID were infrastructure failures and are left out.
USABLE, BROKEN, NO_ARTIFACT = "usable", "broken", "no_artifact"
# Build-error text that means the builder never handed over an artifact: it did not submit, or ran
# out of time, turns, or context first. Any other build error means an artifact was handed over but
# could not be used (failed the API conformance check, or crashed on start).
_NO_ARTIFACT_MARKERS = ("no submission", "timeout", "turn limit", "context len")


@dataclass(frozen=True)
class BuildOutcome:
    task: str
    model: str
    build_id: str
    outcome: str  # USABLE, BROKEN, or NO_ARTIFACT
    error: str  # the build error text, empty for a clean build


def build_outcomes(trials: list[Trial]) -> list[BuildOutcome]:
    """One outcome per valid build, in first-seen order.

    Usable: no build error and at least one valid trial with a score. A build with a build error is
    a failure even when the sheet gives its trials a score: those scores are the 0 the sheet writes
    for a build that produced nothing to evaluate. A build with no error but no scored trial (its
    eval could not run the artifact) counts as broken.
    """
    rows: dict[tuple[str, str, str], list[Trial]] = {}
    for t in trials:
        rows.setdefault((t.model, t.task, t.build_id), []).append(t)
    out = []
    for (model, task, build_id), ts in rows.items():
        valid = [t for t in ts if t.status in VALID_STATUSES]
        if not valid:
            continue
        errors = sorted({t.build_error for t in valid if t.build_error})
        error = "; ".join(errors)
        if error:
            lowered = error.lower()
            outcome = (NO_ARTIFACT if any(m in lowered for m in _NO_ARTIFACT_MARKERS)
                       else BROKEN)
        else:
            outcome = USABLE if any(t.accuracy is not None for t in valid) else BROKEN
        out.append(BuildOutcome(task, model, build_id, outcome, error))
    return out


def mean_build_tokens(trials: list[Trial]) -> tuple[dict[str, float], list[str]]:
    """Each model's build-phase tokens per build: per task, the mean over its valid builds (failed
    ones too, since they spent tokens); then the mean over the tasks every model has a count on, each
    task weighted equally. Returns (model -> tokens, the tasks used).

    The sheet repeats a build's count on each of its trials; differing counts within one build mean
    a bad edit, and are an error rather than something to average away.
    """
    per_build: dict[tuple[str, str, str], set[int]] = {}
    for t in trials:
        if t.status in VALID_STATUSES and t.build_tokens is not None:
            per_build.setdefault((t.model, t.task, t.build_id), set()).add(t.build_tokens)
    per_cell: dict[tuple[str, str], list[int]] = {}
    for (model, task, build_id), counts in per_build.items():
        if len(counts) > 1:
            raise ValueError(f"build {build_id} has different token counts on its trials: {counts}")
        per_cell.setdefault((model, task), []).append(next(iter(counts)))
    tasks = [t for t in TASKS if all((m, t) in per_cell for m in MODELS)]
    if not tasks:
        raise ValueError("no task has a build token count for every model")
    return ({m: sum(sum(per_cell[(m, t)]) / len(per_cell[(m, t)]) for t in tasks) / len(tasks)
             for m in MODELS}, tasks)


def pareto_front(points: dict[str, tuple[float, float]]) -> list[str]:
    """Keys of `points` (cost, score) that no other point dominates, ordered by cost.

    A point is dominated when another costs no more and scores no less, and is strictly better on
    at least one of the two. Points tied on both dominate neither, so all of them are kept.
    """
    front = []
    for key, (cost, score) in points.items():
        dominated = any(oc <= cost and os >= score and (oc < cost or os > score)
                        for other, (oc, os) in points.items() if other != key)
        if not dominated:
            front.append(key)
    return sorted(front, key=lambda k: points[k])


# The internal-vs-external analysis (scripts/analysis/internal_eval_consistency): for each build, the
# agent's own score and the held-out score of every program it deployed. Claude Opus 4.8 only.
CONSISTENCY_MODEL = "claude-opus-4-8"
CONSISTENCY_DIR = (Path(__file__).resolve().parents[1] / "analysis" / "internal_eval_consistency"
                   / "results" / CONSISTENCY_MODEL)

# How one optimization step reads. The step is a move to a program the build had not deployed
# before (the upstream `decisions`). A held-out move smaller than one std of its noise (the
# upstream "within noise" rule) is no real change; anything larger is a real change.
STEP_AGREED = "agreed"  # real change, and the agent's own score moved the same way
STEP_DISAGREED = "disagreed"  # real change, and its own score moved the other way
STEP_MISSED = "missed"  # real change, and its own score did not move at all
STEP_FALSE_UP = "false_improvement"  # no real change, but its own score went up
STEP_NO_CHANGE = "no_change"  # no real change, and its own score did not go up
STEP_GROUPS = (STEP_AGREED, STEP_DISAGREED, STEP_MISSED, STEP_FALSE_UP, STEP_NO_CHANGE)


def classify_step(internal_delta: float, external_delta: float, noise_sd: float) -> str:
    """One step's group. `noise_sd` is one std of the held-out measurement."""
    if not all(math.isfinite(v) for v in (internal_delta, external_delta, noise_sd)):
        raise ValueError(f"step has a non-finite value: {internal_delta}, {external_delta}, "
                         f"{noise_sd}")
    if noise_sd < 0:
        raise ValueError(f"noise sd {noise_sd} is negative")
    if abs(external_delta) < noise_sd:
        return STEP_FALSE_UP if internal_delta > 0 else STEP_NO_CHANGE
    if internal_delta == 0:
        return STEP_MISSED
    return STEP_AGREED if internal_delta * external_delta > 0 else STEP_DISAGREED


# Did the two evals change together? Direction-free: the internal score changed if it moved at all,
# the held-out score changed if it moved by at least one std of its noise (up or down).
CO_INTERNAL = "internal_changed"
CO_BOTH = "both_changed"
CO_BOTH_SAME = "both_changed_same_direction"
CO_HELD_OUT = "held_out_changed"
CO_MISSED = "held_out_changed_internal_flat"
CO_KEYS = (CO_INTERNAL, CO_BOTH, CO_BOTH_SAME, CO_HELD_OUT, CO_MISSED)


def co_change(internal_delta: float, external_delta: float, noise_sd: float) -> dict[str, bool]:
    """Which co-change counters one step adds to. Checks the numbers as classify_step does."""
    classify_step(internal_delta, external_delta, noise_sd)
    internal = internal_delta != 0
    held_out = abs(external_delta) >= noise_sd
    return {CO_INTERNAL: internal, CO_BOTH: internal and held_out,
            CO_BOTH_SAME: internal and held_out and internal_delta * external_delta > 0,
            CO_HELD_OUT: held_out, CO_MISSED: held_out and not internal}


@dataclass
class Steps:
    """Step counts for one task (or several, added together), with each build's change from its
    first to its final graded deploy on both evals."""
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STEP_GROUPS, 0))
    builds: int = 0
    internal_changes: list[float] = field(default_factory=list)  # one per build
    external_changes: list[float] = field(default_factory=list)
    co: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CO_KEYS, 0))

    @property
    def held_out_follows(self) -> float | None:
        """When the internal score changed, the share of steps where the held-out score changed
        too. None when the internal score never changed."""
        return self.co[CO_BOTH] / self.co[CO_INTERNAL] if self.co[CO_INTERNAL] else None

    @property
    def internal_misses(self) -> float | None:
        """When the held-out score changed, the share of steps where the internal score did not
        move. None when the held-out score never changed."""
        return self.co[CO_MISSED] / self.co[CO_HELD_OUT] if self.co[CO_HELD_OUT] else None

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def real(self) -> int:
        """Steps where the held-out score really changed."""
        return self.counts[STEP_AGREED] + self.counts[STEP_DISAGREED] + self.counts[STEP_MISSED]

    def share(self, group: str) -> float:
        return self.counts[group] / self.total if self.total else 0.0

    @property
    def agreement(self) -> float | None:
        """Share of real changes the self-grade got right: agreed / real changes, so a disagreed or
        missed change counts against it. None when there is no real change.

        Directional agreement over consecutive steps, not Kendall's tau: tau compares every pair
        of checkpoints, and upstream reports it per build (`kendall_tau_b` in stats.json)."""
        if not self.real:
            return None
        return self.counts[STEP_AGREED] / self.real

    @staticmethod
    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    @property
    def mean_internal_change(self) -> float | None:
        """Mean over builds of internal final - first; None with no build."""
        return self._mean(self.internal_changes)

    @property
    def mean_external_change(self) -> float | None:
        return self._mean(self.external_changes)

    def __add__(self, other: Steps) -> Steps:
        return Steps({g: self.counts[g] + other.counts[g] for g in STEP_GROUPS},
                     self.builds + other.builds,
                     self.internal_changes + other.internal_changes,
                     self.external_changes + other.external_changes,
                     {k: self.co[k] + other.co[k] for k in CO_KEYS})


def _json_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: {value!r} is not a number")
    return float(value)


def load_steps(results_dir: Path = CONSISTENCY_DIR) -> dict[str, Steps]:
    """Steps per task, in TASKS order, from each task's self_grade_vs_real/stats.json.

    A task with no result file is reported and left out, as is one with no steps. A build with steps
    but no measured noise is an error: without it "no real change" is undefined.

    First and final are the build's first and last deploys that carry BOTH scores (upstream
    `internal_first`/`_last`, `external_first`/`_last`). The submitted copy never carries an internal
    score, so "final" is the last deploy the agent graded, not necessarily the one it submitted.
    """
    out = {}
    for task in TASKS:
        path = results_dir / task / "self_grade_vs_real" / "stats.json"
        if not path.is_file():
            print(f"no internal-vs-external result for {task}")
            continue
        series = json.loads(path.read_text(encoding="utf-8")).get("series")
        if not isinstance(series, dict):
            raise ValueError(f"{path} has no series")
        steps = Steps()
        for run_id, s in series.items():
            where = f"{task} {run_id}"
            decisions = s.get("decisions") or []
            if not decisions:
                continue
            sd = s.get("external_noise_sd")
            if sd is None:
                raise ValueError(f"{where}: external noise was not measured, so 'no real "
                                 "change' is undefined")
            sd = _json_number(sd, where)
            steps.builds += 1
            first_last = [_json_number(s.get(k), where) for k in
                          ("internal_first", "internal_last", "external_first", "external_last")]
            steps.internal_changes.append(first_last[1] - first_last[0])
            steps.external_changes.append(first_last[3] - first_last[2])
            for d in decisions:
                internal = _json_number(d.get("internal_delta"), where)
                external = _json_number(d.get("external_delta"), where)
                steps.counts[classify_step(internal, external, sd)] += 1
                for key, hit in co_change(internal, external, sd).items():
                    steps.co[key] += hit
        if steps.total:
            out[task] = steps
    return out
