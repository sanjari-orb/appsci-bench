"""Copy the per-trial run log sheet into data/elicitation_trials.csv, keeping only what figures use.

The sheet ("AppSci Bench Results (with optimize_agent())") has one row per eval trial, with the
build's requirement-elicitation scores, the trial's task accuracy, a validity mark, and long free
text (notes, pasted logs). This keeps the ids, the validity mark, the elicited share, the task
accuracy, the per-metric breakdown (which holds the safety scores), the first line of the build
error, and the build-phase token count, renamed to the repo's task and model ids. Nothing is
filtered here: the plot scripts drop invalid rows themselves, so the rule is visible where the
figure is drawn.

    python scripts/paper_plots/import_elicitation_sheet.py "path/to/sheet export.csv"

Fully blank rows are skipped. Any other row the script cannot map (unknown task or model, a build id
that names a different task or model, a value outside [0, 1]) stops the import with its row number.
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data import ELICITATION_CSV, MODELS, TASKS, parse_breakdown  # noqa: E402

SHEET_TASKS = {
    "Airline": "airline_rebooking",
    "Airlines": "airline_rebooking",  # a few rows of the optimization-disabled sheet
    "O2C": "order_to_cash",
    "Customer-Call Insights": "customer_call_insights",
    "Compliance": "compliance_review",
    "Slides": "slides_generator",
    "CRM Insights": "crm_insights",
    "Banking": "banking_support",
}
STATUSES = ("VALID", "VALID*", "INVALID")
OUT_FIELDS = ["task", "model", "build_id", "eval_run_id", "status", "elicited", "task_accuracy",
              "breakdown", "build_error", "build_tokens"]
# The build error cell can hold a pasted log; only its first line, cut to this length, is kept.
BUILD_ERROR_MAX = 120


def _breakdown(raw: str, row_no: int) -> str:
    """The per-metric scores, with whitespace collapsed; empty when the sheet has none."""
    parse_breakdown(raw, f"row {row_no}")
    return " ".join(raw.split())


def _tokens(raw: str, row_no: int) -> str:
    """The sheet's token count ("1732.4k", "2.1M", or a plain number) as a whole number of tokens;
    empty when the sheet has none."""
    raw = raw.strip().replace(",", "")
    if not raw:
        return ""
    scale = {"k": 1_000, "m": 1_000_000}.get(raw[-1].lower(), 1)
    number = raw[:-1] if scale != 1 else raw
    try:
        value = float(number) * scale
    except ValueError as exc:
        raise ValueError(f"row {row_no}: build tokens {raw!r} is not a count like 1732.4k") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"row {row_no}: build tokens {raw!r} is not a count")
    return str(round(value))


def _fraction(raw: str, field: str, row_no: int) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"row {row_no}: {field}={raw!r} is not a number") from exc
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"row {row_no}: {field}={value} is outside [0, 1]")
    return raw


def convert(sheet: Path) -> list[dict[str, str]]:
    out = []
    with sheet.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # The sheet's header has a trailing space on "Model "; match on stripped names.
        fields = {name.strip(): name for name in reader.fieldnames or [] if name}
        needed = ("Model", "Task", "Build ID", "Eval Run ID", "Valid/Invalid", "Elicited",
                  "Task Accuracy", "Broken Down Accuracy", "Build Error",
                  "Build Phase Tokens Used")
        missing = [n for n in needed if n not in fields]
        if missing:
            raise ValueError(f"{sheet.name} has no column {missing}")
        for row_no, row in enumerate(reader, start=2):
            if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                continue
            def get(name: str, row: dict = row) -> str:
                return (row[fields[name]] or "").strip()

            task = SHEET_TASKS.get(get("Task"))
            if task is None:
                raise ValueError(f"row {row_no}: unknown task {get('Task')!r}")
            # "fireworks_ai/accounts/fireworks/models/kimi-k3" and "openai/gpt-5.5" -> the last part;
            # "kimi k3" -> "kimi-k3".
            model = "-".join(get("Model").rsplit("/", 1)[-1].split())
            if model not in MODELS:
                raise ValueError(f"row {row_no}: unknown model {get('Model')!r}")
            build_id = get("Build ID")
            if not build_id.startswith(f"{task}_{model}_"):
                raise ValueError(f"row {row_no}: build {build_id!r} is not a {task} {model} build")
            status = get("Valid/Invalid").upper()
            if status not in STATUSES:
                raise ValueError(f"row {row_no}: validity {status!r} is not one of {STATUSES}")
            out.append({
                "task": task,
                "model": model,
                "build_id": build_id,
                "eval_run_id": get("Eval Run ID"),
                "status": status,
                "elicited": _fraction(get("Elicited"), "Elicited", row_no),
                "task_accuracy": _fraction(get("Task Accuracy"), "Task Accuracy", row_no),
                # Parsed here only to reject a malformed cell now; the loader parses it again.
                "breakdown": _breakdown(get("Broken Down Accuracy"), row_no),
                "build_error": (get("Build Error").splitlines() or [""])[0].strip()[:BUILD_ERROR_MAX],
                "build_tokens": _tokens(get("Build Phase Tokens Used"), row_no),
            })
    return out


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: import_elicitation_sheet.py <sheet export.csv>")
    sheet = Path(sys.argv[1])
    if not sheet.is_file():
        sys.exit(f"no such file: {sheet}")
    rows = convert(sheet)
    rows.sort(key=lambda r: (list(TASKS).index(r["task"]), list(MODELS).index(r["model"]),
                             r["build_id"], r["eval_run_id"]))
    with ELICITATION_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {ELICITATION_CSV}")


if __name__ == "__main__":
    main()
