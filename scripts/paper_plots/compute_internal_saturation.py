"""When each model's own internal eval stops giving a signal, per model and per task (optimization on).

During a build the optimize loop scores each candidate on an internal eval the agent wrote itself. One
ROUND is one of those scorings: a baseline or an attempted change, one record in the build's
`.appsci/optimize/score_history.json`. Two ways a build's eval can stop rewarding further work:

  SATURATED  the first round whose headline score is at least 0.99: the eval is at its ceiling and
             has no room left to show a gain;
  PLATEAUED  the round the build stopped improving: the round it saturated, or the round that set its
             final best if no later round beat that by 0.01 (the margin the optimize loop itself
             requires before keeping a change), whichever comes first. Going flat needs at least one
             round after the best; a build whose best is its last round was still improving.

Every saturated build is therefore also plateaued: an eval at its ceiling cannot reward anything more.

Steps:
  1. the builds are the paper's: unique build IDs from data/elicitation_trials.csv with at least one
     VALID or VALID* trial, so each build counts once however many eval trials it has;
  2. each build's rounds come from data/internal_eval_rounds.csv, which `--fetch` rebuilds from S3; a
     build with no recorded rounds is listed there but left out of everything below, not counted as
     unsaturated;
  3. per build, the round it saturated at and the round it plateaued at, -1 for never, written to
     figures/internal_saturation/internal_saturation_per_build.csv;
  4. per group (model, task, model x task), computed by reading that per-build CSV back: the share of
     builds that saturated and that plateaued; how many were saturated at round 1, before any change;
     and the mean round, over only the builds that reached it.

    python scripts/paper_plots/compute_internal_saturation.py           # tables + CSVs
    python scripts/paper_plots/compute_internal_saturation.py --latex   # also print LaTeX tables
    python scripts/paper_plots/compute_internal_saturation.py --fetch   # rebuild the rounds table (AWS)

prints the tables and writes the per-build CSV and figures/internal_saturation/internal_saturation.csv
(one row per group, with a `grouping` column of model, task, model_task or all).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import MODELS, TASKS, VALID_STATUSES, Trial, load_trials  # noqa: E402

ROUNDS_CSV = Path(__file__).resolve().parent / "data" / "internal_eval_rounds.csv"
# style.FIGURES_DIR, spelled out: importing style pulls in plotly, and this script draws nothing, so
# it (and its tests) must not need a plotting library installed.
OUT_DIR = Path(__file__).resolve().parent / "figures" / "internal_saturation"
PER_BUILD_CSV = OUT_DIR / "internal_saturation_per_build.csv"
OUT_CSV = OUT_DIR / "internal_saturation.csv"

SATURATED = 0.99
# The optimize loop keeps a change only when it beats the score to beat by this much
# (appsci_agent/tools/optimize_agent.py, MIN_IMPROVEMENT), so a smaller gain is not progress to it.
MIN_GAIN = 0.01
# Written for "never" in the per-build CSV's round columns.
NEVER = -1
BUCKET = "<your-bucket>"
HISTORY_KEY = "runs/{build_id}/agent_fs/appsci_agent/workspace/.appsci/optimize/score_history.json"
# A build ID becomes part of an S3 key, so it is checked against the shape run IDs actually have
# before it is used, rather than trusted because it came from our own sheet.
_BUILD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")

ROUNDS_FIELDS = ["model", "task", "build_id", "n_rounds", "scores", "minutes", "cases"]


# --- the build set ---------------------------------------------------------------------------------

def valid_builds(trials: list[Trial]) -> list[tuple[str, str, str]]:
    """(model, task, build_id) for every build with at least one valid trial, each once, sorted."""
    seen: dict[tuple[str, str, str], bool] = {}
    for t in trials:
        key = (t.model, t.task, t.build_id)
        seen[key] = seen.get(key, False) or t.status in VALID_STATUSES
    return sorted(k for k, ok in seen.items() if ok)


# --- fetching (the only step that needs AWS) -------------------------------------------------------

def rounds_from_history(history: dict) -> tuple[list[float], list[float], list[int]]:
    """(headline scores, minutes since the first round, case counts), one entry per scored round.

    A record with no numeric headline score is not a round -- nothing was measured -- and is skipped
    rather than read as 0. Minutes are None-free only when every scored round carries a timestamp;
    otherwise the list is empty and the minutes column stays blank for that build.
    """
    records = [r for r in (history.get("records") or [])
               if isinstance(r, dict) and isinstance(r.get("headline_metric"), (int, float))]
    scores = [float(r["headline_metric"]) for r in records]
    stamps = [r.get("timestamp") for r in records]
    minutes: list[float] = []
    if stamps and all(isinstance(s, (int, float)) for s in stamps):
        minutes = [(s - stamps[0]) / 60 for s in stamps]
    cases = [len(r.get("cases") or []) for r in records]
    return scores, minutes, cases


def fetch_history(build_id: str, profile: str | None) -> dict | None:
    """The build's score_history.json from S3, or None when it has none. Other failures raise."""
    if not _BUILD_ID.match(build_id):
        raise ValueError(f"build id {build_id!r} does not look like a run id")
    env = {**os.environ, **({"AWS_PROFILE": profile} if profile else {})}
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "score_history.json"
        proc = subprocess.run(
            ["aws", "s3", "cp", f"s3://{BUCKET}/{HISTORY_KEY.format(build_id=build_id)}", str(dest),
             "--only-show-errors"],
            capture_output=True, text=True, env=env, check=False,
        )
        if proc.returncode != 0:
            # A missing object is an answer (the build recorded no rounds); anything else -- expired
            # credentials, no network -- must stop the rebuild rather than write a table of blanks.
            if "404" in proc.stderr or "Not Found" in proc.stderr or "does not exist" in proc.stderr:
                return None
            raise RuntimeError(f"fetching {build_id}: {proc.stderr.strip()}")
        return json.loads(dest.read_text(encoding="utf-8"))


def write_rounds(builds: list[tuple[str, str, str]], profile: str | None, path: Path = ROUNDS_CSV) -> None:
    rows = []
    for model, task, build_id in builds:
        history = fetch_history(build_id, profile)
        scores, minutes, cases = rounds_from_history(history) if history else ([], [], [])
        rows.append({
            "model": model, "task": task, "build_id": build_id, "n_rounds": len(scores),
            # Full precision: the table is compared against a threshold, and rounding a 0.98996 to
            # "0.9900" would record a build as saturated that never was.
            "scores": ";".join(repr(s) for s in scores),
            # Four places, not two: a mean over a handful of builds lands on a display rounding boundary
            # often enough that two-place storage moved published cells by 0.1.
            "minutes": ";".join(f"{m:.4f}" for m in minutes),
            "cases": ";".join(str(c) for c in cases),
        })
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ROUNDS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path} ({len(rows)} builds, {sum(1 for r in rows if r['n_rounds'])} with rounds)")


# --- reading the rounds (no network) ---------------------------------------------------------------

@dataclass(frozen=True)
class Build:
    model: str
    task: str
    build_id: str
    scores: tuple[float, ...]
    minutes: tuple[float, ...]
    cases: tuple[int, ...]


def _split(raw: str, cast, field: str, row_no: int) -> tuple:
    try:
        return tuple(cast(x) for x in raw.split(";")) if raw.strip() else ()
    except ValueError as exc:
        raise ValueError(f"row {row_no}: bad {field} {raw!r}") from exc


def load_rounds(path: Path = ROUNDS_CSV) -> list[Build]:
    """Every build with at least one recorded round. Builds with none are listed in the file only."""
    builds = []
    with path.open(newline="", encoding="utf-8") as f:
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            model, task = row["model"].strip(), row["task"].strip()
            if model not in MODELS:
                raise ValueError(f"row {row_no}: unknown model {model!r}")
            if task not in TASKS:
                raise ValueError(f"row {row_no}: unknown task {task!r}")
            scores = _split(row["scores"], float, "scores", row_no)
            minutes = _split(row["minutes"], float, "minutes", row_no)
            cases = _split(row["cases"], int, "cases", row_no)
            if len(scores) != int(row["n_rounds"]):
                raise ValueError(f"row {row_no}: n_rounds {row['n_rounds']} but {len(scores)} scores")
            if minutes and len(minutes) != len(scores):
                raise ValueError(f"row {row_no}: {len(minutes)} minutes for {len(scores)} rounds")
            if any(not 0 <= s <= 1 for s in scores):
                raise ValueError(f"row {row_no}: a score outside [0, 1]")
            if scores:
                builds.append(Build(model, task, row["build_id"].strip(), scores, minutes, cases))
    return builds


# --- per build: when did it saturate, and when did it stop improving ------------------------------

def saturated_at(scores: tuple[float, ...]) -> int:
    """The 1-based round that first scored at least SATURATED, or NEVER."""
    return next((i for i, s in enumerate(scores, 1) if s >= SATURATED), NEVER)


def flat_from(scores: tuple[float, ...]) -> int:
    """The 1-based round that set the build's final best, if the score never rose again after it.

    Rising means beating the best score so far by at least MIN_GAIN, the same comparison the optimize
    loop makes before it keeps a change. At least one round has to have run after the best one: a
    build whose best is its last round was still improving when it ended, so it is NEVER rather than
    assumed flat.
    """
    best, best_at = None, NEVER
    for i, s in enumerate(scores, 1):
        if best is None or s >= best + MIN_GAIN - 1e-12:
            best, best_at = s, i
    return best_at if best_at < len(scores) else NEVER


def plateaued_at(scores: tuple[float, ...]) -> int:
    """The round the build stopped improving: whichever comes first of saturating and going flat.

    Saturation counts because an eval at 0.99 or above has no room left to show a gain of MIN_GAIN, so
    from that round on the loop cannot be rewarded for anything. Going flat is the same outcome below
    the ceiling. So "plateaued" and "stopped improving" are one measure, and every saturated build is
    also plateaued.
    """
    rounds = [r for r in (saturated_at(scores), flat_from(scores)) if r != NEVER]
    return min(rounds) if rounds else NEVER


PER_BUILD_FIELDS = ["model", "task", "build_id", "n_rounds", "baseline", "best",
                    "saturated_at_round", "plateaued_at_round", "minutes_to_saturate", "median_suite_size"]


def per_build_rows(builds: list[Build]) -> list[dict]:
    rows = []
    for b in builds:
        sat = saturated_at(b.scores)
        minutes = b.minutes[sat - 1] if sat != NEVER and len(b.minutes) >= sat else None
        rows.append({
            "model": b.model, "task": b.task, "build_id": b.build_id, "n_rounds": len(b.scores),
            "baseline": f"{b.scores[0]:.4f}", "best": f"{max(b.scores):.4f}",
            "saturated_at_round": sat, "plateaued_at_round": plateaued_at(b.scores),
            "minutes_to_saturate": "" if minutes is None else f"{minutes:.2f}",
            "median_suite_size": f"{statistics.median(b.cases):g}" if b.cases else "",
        })
    return rows


def write_per_build(rows: list[dict], path: Path = PER_BUILD_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PER_BUILD_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


@dataclass(frozen=True)
class BuildResult:
    """One row of the per-build CSV, as the tables read it back."""
    model: str
    task: str
    n_rounds: int
    saturated_at: int | None
    plateaued_at: int | None
    minutes_to_saturate: float | None
    suite_size: float | None


def _round(raw: str, field: str, row_no: int) -> int | None:
    value = int(raw)
    if value == NEVER:
        return None
    if value < 1:
        raise ValueError(f"row {row_no}: {field} {value} is neither a round nor {NEVER}")
    return value


def load_per_build(path: Path = PER_BUILD_CSV) -> list[BuildResult]:
    """The tables are computed from this file, so what they report is exactly what the CSV says."""
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for row_no, row in enumerate(csv.DictReader(f), start=2):
            n = int(row["n_rounds"])
            sat = _round(row["saturated_at_round"], "saturated_at_round", row_no)
            plat = _round(row["plateaued_at_round"], "plateaued_at_round", row_no)
            if (sat and sat > n) or (plat and plat > n):
                raise ValueError(f"row {row_no}: a round past the build's {n} rounds")
            out.append(BuildResult(
                model=row["model"], task=row["task"], n_rounds=n, saturated_at=sat, plateaued_at=plat,
                minutes_to_saturate=float(row["minutes_to_saturate"]) if row["minutes_to_saturate"] else None,
                suite_size=float(row["median_suite_size"]) if row["median_suite_size"] else None,
            ))
    return out


# --- per group -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Group:
    builds: int
    saturated: int
    at_round_1: int
    mean_rounds: float | None
    sd_rounds: float | None
    median_rounds: float | None
    mean_minutes: float | None
    plateaued: int
    mean_plateau_round: float | None
    sd_plateau_round: float | None
    rounds_per_build: float
    median_suite_size: float | None

    @property
    def share(self) -> float:
        return self.saturated / self.builds


def _mean_sd(values: list[int]) -> tuple[float | None, float | None]:
    return (statistics.mean(values) if values else None,
            statistics.stdev(values) if len(values) > 1 else None)


def summarize(results: list[BuildResult]) -> Group:
    if not results:
        raise ValueError("summarize needs at least one build")
    sat = [r.saturated_at for r in results if r.saturated_at]
    plat = [r.plateaued_at for r in results if r.plateaued_at]
    minutes = [r.minutes_to_saturate for r in results if r.minutes_to_saturate is not None]
    suite = [r.suite_size for r in results if r.suite_size is not None]
    mean_rounds, sd_rounds = _mean_sd(sat)
    mean_plat, sd_plat = _mean_sd(plat)
    return Group(
        builds=len(results),
        saturated=len(sat),
        at_round_1=sum(1 for r in sat if r == 1),
        mean_rounds=mean_rounds,
        sd_rounds=sd_rounds,
        median_rounds=statistics.median(sat) if sat else None,
        mean_minutes=statistics.mean(minutes) if minutes else None,
        plateaued=len(plat),
        mean_plateau_round=mean_plat,
        sd_plateau_round=sd_plat,
        rounds_per_build=statistics.mean(r.n_rounds for r in results),
        median_suite_size=statistics.median(suite) if suite else None,
    )


def groups(results: list[BuildResult]) -> list[tuple[str, str, str, Group]]:
    """(grouping, model, task, summary) in the paper's model and task order; empty groups omitted."""
    out = []
    for m in MODELS:
        if rs := [r for r in results if r.model == m]:
            out.append(("model", m, "", summarize(rs)))
    for t in TASKS:
        if rs := [r for r in results if r.task == t]:
            out.append(("task", "", t, summarize(rs)))
    for m in MODELS:
        for t in TASKS:
            if rs := [r for r in results if r.model == m and r.task == t]:
                out.append(("model_task", m, t, summarize(rs)))
    out.append(("all", "", "", summarize(results)))
    return out


# --- output ----------------------------------------------------------------------------------------

def _f(x: float | None, spec: str = ".1f") -> str:
    return "" if x is None else format(x, spec)


def write_csv(rows: list[tuple[str, str, str, Group]], path: Path = OUT_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["grouping", "model", "task", "builds", "saturated", "share_saturated",
                         "saturated_at_round_1", "mean_rounds_to_saturate", "sd_rounds_to_saturate",
                         "median_rounds_to_saturate", "mean_minutes_to_saturate", "plateaued",
                         "share_plateaued", "mean_plateau_round", "sd_plateau_round",
                         "mean_rounds_per_build", "median_suite_size"])
        for grouping, model, task, g in rows:
            writer.writerow([grouping, model, task, g.builds, g.saturated, f"{g.share:.4f}", g.at_round_1,
                             _f(g.mean_rounds, ".3f"), _f(g.sd_rounds, ".3f"), _f(g.median_rounds, "g"),
                             _f(g.mean_minutes, ".2f"), g.plateaued, f"{g.plateaued / g.builds:.4f}",
                             _f(g.mean_plateau_round, ".3f"), _f(g.sd_plateau_round, ".3f"),
                             f"{g.rounds_per_build:.3f}", _f(g.median_suite_size, "g")])


def _share(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)"


def _pm(mean: float | None, sd: float | None) -> str:
    return "-" if mean is None else f"{mean:.1f} ± {_f(sd) or '-'}"


def _cells(g: Group) -> list[str]:
    return [_share(g.saturated, g.builds), str(g.at_round_1), _pm(g.mean_rounds, g.sd_rounds),
            _f(g.median_rounds, "g") or "-", _f(g.mean_minutes) or "-",
            _share(g.plateaued, g.builds), _pm(g.mean_plateau_round, g.sd_plateau_round),
            _f(g.median_suite_size, "g") or "-"]


HEADER = ["Saturated", "At round 1", "Rounds to saturate", "Median", "Minutes", "Plateaued",
          "Plateau round", "Suite size"]


def print_tables(rows: list[tuple[str, str, str, Group]]) -> None:
    total = next(g for grouping, _, _, g in rows if grouping == "all")
    for grouping, label, names in (("model", "Model", MODELS), ("task", "Task", TASKS)):
        print(f"\n{label:<24}" + "".join(f"{h:>19}" for h in HEADER))
        for gp, model, task, g in rows:
            if gp == grouping:
                print(f"{names[model or task]:<24}" + "".join(f"{c:>19}" for c in _cells(g)))
        print(f"{'All':<24}" + "".join(f"{c:>19}" for c in _cells(total)))


def _tex(text: str) -> str:
    return (text.replace("τ", r"$\tau$").replace("%", r"\%").replace("&", r"\&")
            .replace(" ± ", r" $\pm$ "))


def print_latex(rows: list[tuple[str, str, str, Group]]) -> None:
    total = next(g for grouping, _, _, g in rows if grouping == "all")
    for grouping, label, names in (("model", "Model", MODELS), ("task", "Task", TASKS)):
        print(r"\begin{tabular}{l" + "r" * len(HEADER) + "}", r"\toprule", sep="\n")
        print(" & ".join([label, *HEADER]) + r" \\", r"\midrule", sep="\n")
        for gp, model, task, g in rows:
            if gp == grouping:
                print(" & ".join(_tex(c) for c in [names[model or task], *_cells(g)]) + r" \\")
        print(r"\midrule")
        print(" & ".join(_tex(c) for c in ["All", *_cells(total)]) + r" \\")
        print(r"\bottomrule", r"\end{tabular}", "", sep="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fetch", action="store_true",
                        help=f"rebuild {ROUNDS_CSV.name} from S3 before computing (needs AWS access)")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"),
                        help="AWS profile for --fetch (default: $AWS_PROFILE)")
    parser.add_argument("--latex", action="store_true", help="also print the two tables as LaTeX")
    args = parser.parse_args(argv)

    if args.fetch:
        write_rounds(valid_builds(load_trials()), args.profile)

    builds = load_rounds()
    if not builds:
        print(f"no build in {ROUNDS_CSV} has recorded rounds", file=sys.stderr)
        return 1
    write_per_build(per_build_rows(builds))
    rows = groups(load_per_build())
    print(f"Internal-eval saturation (first round scoring >= {SATURATED}) and plateaus (saturated, or "
          f"never again beaten by {MIN_GAIN}), optimization enabled, {len(builds)} valid builds with "
          "recorded rounds. Each round and minutes column averages over only the builds that reached "
          "that point.")
    print_tables(rows)
    if args.latex:
        print()
        print_latex(rows)
    write_csv(rows)
    print(f"\nwrote {PER_BUILD_CSV}\nwrote {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
