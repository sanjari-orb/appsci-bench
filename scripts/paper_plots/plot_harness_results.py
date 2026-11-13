#!/usr/bin/env python3
"""Compare native harness JSON results with AppSci-agent CSV results in a PNG.

Usage:
    python scripts/paper_plots/plot_harness_results.py
    python scripts/paper_plots/plot_harness_results.py --score-type average
    python scripts/paper_plots/plot_harness_results.py --input-dir /path/to/results --output table.png
    python scripts/paper_plots/plot_harness_results.py --appsci-csv /path/to/results.csv
    python scripts/paper_plots/plot_harness_results.py --input-dir DIR --appsci-optimization disabled \
        --native-optimization disabled   # both sides without optimization

Cells show MMS@3 (default) or average scores, with standard deviations, as percentages.
Average uses score_power.py's mean of per-build trial means and across-build spread;
the AppSci summary CSV supplies its corresponding Average/std columns.
The final Average column equally weights task means, without pooling task standard deviations.
This reads saved results only; it does not run evaluations or access AWS.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO / "scripts/_out/harness_smoke_score_power_20260923"
DEFAULT_CSV = Path.home() / "Downloads/AppSci Bench Results(score^3 scores).csv"
TASKS = (
    ("airline_rebooking", "Airline rebooking"),
    ("banking_support", "Banking support"),
    ("compliance_review", "Compliance review"),
    ("crm_insights", "CRM insights"),
    ("customer_call_insights", "Customer call insights"),
    ("order_to_cash", "Order to cash"),
    ("slides_generator", "Slides generator"),
)
HARNESSES = (
    ("claude_code", "Claude Code\nClaude Opus 4.8"),
    ("codex", "Codex\nGPT-5.5"),
)
ARMS = (
    ("appsci_opus", "Opus 4.8 + AppSci agent"),
    ("claude_code", "Opus 4.8 + Claude Code"),
    ("appsci_gpt", "GPT-5.5 + AppSci agent"),
    ("codex", "GPT-5.5 + Codex"),
)
CSV_TASKS = dict(zip(
    ("airlines", "banking", "compliance", "crm insights", "cci", "o2c", "slides"),
    (task for task, _ in TASKS),
    strict=True,
))
CSV_MODELS = {"opus 4.8": "appsci_opus", "gpt 5.5": "appsci_gpt"}


def checked_number(value, source: str) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{source}: expected a finite number between 0 and 1")
    return value


def score_fields(score_type: str) -> tuple[str, str, str, str]:
    """Native JSON mean/std and corresponding AppSci summary CSV columns."""
    if score_type == "mms":
        return "score", "std", "Score ^ k (mean)", "Score ^ k (std)"
    if score_type == "average":
        return "average", "average_std_across_builds", "Average", "std"
    raise ValueError(f"Unknown score type: {score_type!r}")


def load_appsci_results(path: Path, optimization: str, score_type: str = "mms") -> dict:
    results = {}
    _, _, mean_field, std_field = score_fields(score_type)
    opt_value = "true" if optimization == "enabled" else "false"
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            model = row["Model"].strip().lower()
            if (model not in CSV_MODELS
                    or row["Opt Enabled"].strip().lower() != opt_value):
                continue
            task = CSV_TASKS[row["Task"].strip().lower()]
            key = task, CSV_MODELS[model]
            if key in results:
                raise ValueError(f"{path}: duplicate selected result for {key}")
            if float(row["k (number of builds)"]) != 3:
                raise ValueError(f"{path}: {key} does not report K=3")
            raw_std = row[std_field].strip()
            results[key] = {
                "score": checked_number(row[mean_field], f"{path}: {key} {mean_field}"),
                "std": (None if raw_std.lower() in ("", "n/a", "na") else
                        checked_number(raw_std, f"{path}: {key} std")),
                "note": row.get("Notes", "").strip(),
            }
    missing = {(task, arm) for task, _ in TASKS for arm in CSV_MODELS.values()} - results.keys()
    if missing:
        raise ValueError(f"{path}: missing {optimization} results: {sorted(missing)}")
    return results


def load_results(input_dir: Path, score_type: str = "mms") -> dict:
    results = {}
    mean_field, std_field, _, _ = score_fields(score_type)
    for task, _ in TASKS:
        for harness, _ in HARNESSES:
            path = input_dir / f"{task}__{harness}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("k") != 3 or len(data.get("builds", [])) != 3:
                raise ValueError(f"{path}: expected K=3 with three build buckets")
            for field in (mean_field, std_field):
                value = data.get(field)
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not 0 <= value <= 1):
                    raise ValueError(f"{path}: invalid {field}: {value!r}")
            if data.get("warnings"):
                raise ValueError(f"{path}: resolve scoring warnings before plotting: "
                                 f"{data['warnings']}")
            data["score"], data["std"] = data[mean_field], data[std_field]
            data["note"] = ("One failed build is included as an all-zero bucket."
                            if any(not b["run_ids"] for b in data["builds"]) else "")
            results[task, harness] = data
    return results


def task_averages(results: dict) -> dict[str, float]:
    """Macro-average the selected metric, retaining zeros and using unrounded means."""
    return {
        arm: math.fsum(results[task, arm]["score"] for task, _ in TASKS) / len(TASKS)
        for arm, _ in ARMS
    }


def render(results: dict, output: Path, dpi: int, optimization: str,
           score_type: str = "mms", native_optimization: str = "enabled") -> None:
    score_fields(score_type)
    metric_label = "MMS@3" if score_type == "mms" else "Average"
    spread_label = "trial combinations" if score_type == "mms" else "build means"
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = []
    notes = []
    markers = ("*", "†", "‡", "§", "¶")
    # Compare at displayed precision so visually tied scores receive equal emphasis.
    best_scores = {
        task: max(round(100 * results[task, arm]["score"], 2) for arm, _ in ARMS)
        for task, _ in TASKS
    }
    averages = task_averages(results)
    best_average = max(round(100 * value, 2) for value in averages.values())
    for arm, label in ARMS:
        row = [label]
        for task, task_label in TASKS:
            data = results[task, arm]
            std = "n/a" if data["std"] is None else f"{100 * data['std']:.2f}"
            value = f"{100 * data['score']:.2f} ± {std}"
            if data["note"]:
                marker = markers[len(notes)] if len(notes) < len(markers) else str(len(notes) + 1)
                value += f" {marker}"
                notes.append(f"{marker} {label}, {task_label}: {data['note']}")
            row.append(value)
        rows.append(row)

    with plt.rc_context({"font.family": "STIXGeneral", "mathtext.fontset": "stix"}):
        fig, ax = plt.subplots(figsize=(20, 5.5 + max(0, len(notes) - 3) * 0.25))
        fig.patch.set_facecolor("white")
        fig.subplots_adjust(left=0.035, right=0.965, top=0.97, bottom=0.03)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_axis_off()
        ax.text(0.5, 0.965, f"[harness comparison | {metric_label} (%) | mean ± std]",
                ha="center", va="center", fontsize=12,
                fontfamily="DejaVu Sans", color="#666666")

        # Booktabs-style rules and typography: no cell boxes or alternating fills.
        ax.hlines(0.915, 0, 1, color="black", linewidth=1.5)
        ax.text(0.01, 0.840, "Model + harness", fontsize=16, weight="bold", va="center")
        task_start = 0.23
        width = (1 - task_start) / (len(TASKS) + 1)
        centers = [task_start + (i + 0.5) * width for i in range(len(TASKS) + 1)]
        headings = ("Airline\nrebooking", "Banking\nsupport", "Compliance\nreview",
                    "CRM\ninsights", "Customer call\ninsights", "Order to\ncash", "Slides\ngenerator",
                    "Average\n(all tasks)")
        for center, heading in zip(centers, headings, strict=True):
            ax.text(center, 0.840, heading, ha="center", va="center",
                    fontsize=14, weight="bold", linespacing=1.2)
        ax.hlines(0.764, 0, 1, color="black", linewidth=0.8)

        for index, row in enumerate(rows):
            y = 0.700 - index * 0.090
            ax.text(0.01, y, row[0], fontsize=15, va="center")
            arm = ARMS[index][0]
            for center, value, (task, _) in zip(centers[:-1], row[1:], TASKS, strict=True):
                is_best = round(100 * results[task, arm]["score"], 2) == best_scores[task]
                ax.text(center, y, value, fontsize=13, ha="center", va="center",
                        weight="bold" if is_best else "normal")
            ax.text(centers[-1], y, f"{100 * averages[arm]:.2f}", fontsize=13,
                    ha="center", va="center",
                    weight="bold" if round(100 * averages[arm], 2) == best_average else "normal")
        ax.hlines(0.565, 0, 1, color="black", linewidth=0.5)
        ax.hlines(0.370, 0, 1, color="black", linewidth=1.5)

        ax.text(0.01, 0.320,
                f"K = 3 builds. AppSci optimization: {optimization}. "
                f"Native results: optimization {native_optimization}. Values shown as percentages. "
                "Bold marks the highest mean in each column.",
                fontsize=11, va="center", color="#444444")
        ax.text(0.01, 0.275,
                "AppSci values retain the CSV's precision; n/a means std was not provided. "
                f"Std is over {spread_label}, not a confidence interval.",
                fontsize=11, va="center", color="#444444")
        ax.text(0.01, 0.230,
                f"Average (all tasks) = equal-weight mean of the {len(TASKS)} task means; "
                "no pooled standard deviation is reported.",
                fontsize=11, va="center", color="#444444")
        for i, note in enumerate(notes):
            ax.text(0.01, 0.180 - i * 0.043, note,
                    fontsize=10.5, va="center", color="#444444")
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output, format="png", dpi=dpi, facecolor="white")
        plt.close(fig)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-type", choices=("mms", "average"), default="mms",
                        help="Score to display: mms = MMS@3 (default); average = mean of build means")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT,
                        help="Directory containing TASK__HARNESS.json score results")
    parser.add_argument("--appsci-csv", type=Path, default=DEFAULT_CSV,
                        help="CSV containing AppSci-agent Opus 4.8 and GPT-5.5 results")
    parser.add_argument("--appsci-optimization", choices=("enabled", "disabled"),
                        default="enabled", help="Which AppSci CSV condition to display")
    parser.add_argument("--native-optimization", choices=("enabled", "disabled"),
                        default="enabled",
                        help="The optimization setting the native-harness JSON results were run with "
                             "(only labels the table; it does not pick files)")
    parser.add_argument("--output", type=Path,
                        help="PNG destination (default: INPUT_DIR/harness_results.png for mms; "
                             "INPUT_DIR/harness_results_average.png for average)")
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args(argv)
    if args.dpi <= 0:
        parser.error("--dpi must be positive")
    filename = "harness_results.png" if args.score_type == "mms" else "harness_results_average.png"
    output = args.output or args.input_dir / filename
    if output.suffix.lower() != ".png":
        parser.error("--output must end in .png")
    try:
        results = load_results(args.input_dir, args.score_type)
        results.update(load_appsci_results(args.appsci_csv, args.appsci_optimization,
                                          args.score_type))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    render(results, output, args.dpi, args.appsci_optimization, args.score_type,
           args.native_optimization)
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
