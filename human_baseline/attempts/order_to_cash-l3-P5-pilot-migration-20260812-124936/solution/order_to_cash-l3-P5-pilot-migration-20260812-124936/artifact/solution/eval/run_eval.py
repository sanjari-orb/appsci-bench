"""Score the agent against the labelled examples and print a metrics report.

Reads ``examples.jsonl`` for each invoice number + expected label, builds a
natural-language review query, calls ``respond()`` (which looks up the invoice
LIVE in Postgres), and compares the returned ``flagged`` value to the label.

All ~10 examples are scored. This iteration implements only DUPLICATE detection,
so non-DUPLICATE faults show up as false negatives -- that is expected and is made
visible in the per-fault-type breakdown rather than hidden.

Run inside the workspace container:
    python -m solution.eval.run_eval
"""
from __future__ import annotations

import json
import os

from ..detector import respond

try:
    from ..datatypes import ArtifactAgentQuery
except ImportError:
    from datatypes import ArtifactAgentQuery  # flat: cd /work/solution

# Where to find the labelled examples, in priority order:
#   1. $OTC_EXAMPLES override
#   2. /data/examples.jsonl (copied into the container by run_setup.sh)
#   3. the repo copy under environment/data/
_ENV_PATH = os.environ.get("OTC_EXAMPLES")
_CONTAINER_PATH = "/data/examples.jsonl"
_REPO_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "environment", "data", "examples.jsonl"
)


def _resolve_examples_path() -> str:
    """Return the first examples.jsonl path that exists (env > container > repo)."""
    for candidate in (_ENV_PATH, _CONTAINER_PATH, _REPO_PATH):
        if candidate and os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        "examples.jsonl not found; set OTC_EXAMPLES or run inside the container"
    )


def load_examples(path: str) -> list[dict]:
    """Parse examples.jsonl into ``{invoice_number, expected_flagged, fault_type}`` rows."""
    examples = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            examples.append(
                {
                    "invoice_number": row["invoice"]["invoice_number"],
                    "expected_flagged": bool(row["label"]["flagged"]),
                    "fault_type": row["label"]["fault_type"],
                }
            )
    return examples


def build_query(invoice_number: str) -> ArtifactAgentQuery:
    """Wrap the invoice number in the natural-language template a real query uses.

    This exercises ``respond()``'s parsing path end to end, rather than handing it a
    bare id.
    """
    text = (
        f"Review invoice {invoice_number} against its PO line and the supplier "
        "contract; decide whether to flag it."
    )
    return ArtifactAgentQuery(text=text)


def _predict(invoice_number: str) -> tuple[bool, str]:
    """Call respond() for one invoice; return ``(flagged, reason)``."""
    output = respond(build_query(invoice_number))
    payload = json.loads(output.json)
    return bool(payload["flagged"]), payload.get("reason", "")


def score(examples: list[dict]) -> dict:
    """Run every example through respond() and accumulate a confusion matrix.

    Returns a metrics dict with overall TP/FP/FN/TN counts, a per-fault-type
    breakdown (including the misses themselves), and a list of invoices
    reported as not-found-in-DB (surfaced separately so absent fixtures do
    not corrupt the flag metrics).
    """
    tp = fp = fn = tn = 0
    per_fault: dict[str, dict] = {}
    missing: list[str] = []

    for example in examples:
        number = example["invoice_number"]
        expected = example["expected_flagged"]
        fault = example["fault_type"]

        predicted, reason = _predict(number)
        correct = predicted == expected

        # An invoice missing from the DB surfaces as an extraction "not found";
        # record it separately so it does not distort the flag confusion matrix.
        if "not found in the database" in reason:
            missing.append(number)

        bucket = per_fault.setdefault(
            fault, {"n": 0, "flagged_correct": 0, "misses": []}
        )
        bucket["n"] += 1
        if correct:
            bucket["flagged_correct"] += 1
        else:
            bucket["misses"].append(
                {
                    "invoice_number": number,
                    "expected_flagged": expected,
                    "predicted_flagged": predicted,
                    "reason": reason,
                }
            )

        if expected and predicted:
            tp += 1
        elif not expected and predicted:
            fp += 1
        elif expected and not predicted:
            fn += 1
        else:
            tn += 1

    return {
        "n": len(examples),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "per_fault": per_fault,
        "missing_in_db": missing,
    }


def _safe_div(numerator: float, denominator: float) -> float:
    """Divide, returning 0.0 when the denominator is 0 (empty confusion cells)."""
    return numerator / denominator if denominator else 0.0


def compute_metrics(m: dict) -> dict:
    """Derive accuracy, precision, recall, and F1 from the confusion counts."""
    tp, fp, fn, tn = m["tp"], m["fp"], m["fn"], m["tn"]
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    return {
        "flag_accuracy": _safe_div(tp + tn, m["n"]),
        "precision": precision,
        "recall": recall,
        "f1": _safe_div(2 * precision * recall, precision + recall),
        "false_positives": fp,
        "false_negatives": fn,
    }


def format_report(m: dict, source: str) -> str:
    """Render the metrics dict as a human-readable stdout report."""
    metrics = compute_metrics(m)
    lines = [
        "=== OTC invoice-review eval (iteration 1: DUPLICATE only) ===",
        f"examples scored: {m['n']}   (source: {source})",
        "",
        "Confusion matrix (flagged vs label.flagged):",
        f"  true positives : {m['tp']}",
        f"  false positives: {m['fp']}",
        f"  false negatives: {m['fn']}",
        f"  true negatives : {m['tn']}",
        "",
        "Metrics:",
        f"  flag accuracy : {metrics['flag_accuracy']:.3f}",
        f"  precision     : {metrics['precision']:.3f}",
        f"  recall        : {metrics['recall']:.3f}",
        f"  F1-score      : {metrics['f1']:.3f}",
        "",
        "Per fault_type breakdown (flagged decision correct / total):",
    ]
    for fault in sorted(m["per_fault"]):
        bucket = m["per_fault"][fault]
        lines.append(f"  {fault:<20}: {bucket['flagged_correct']}/{bucket['n']}")

    misses = {
        fault: bucket["misses"]
        for fault, bucket in m["per_fault"].items()
        if bucket["misses"]
    }
    lines.append("")
    if misses:
        lines.append("Misses (expected vs predicted, by fault_type):")
        for fault in sorted(misses):
            lines.append(f"  {fault}:")
            for miss in misses[fault]:
                lines.append(
                    f"    invoice {miss['invoice_number']}: "
                    f"expected_flagged={miss['expected_flagged']} "
                    f"predicted_flagged={miss['predicted_flagged']} "
                    f"reason={miss['reason']!r}"
                )
    else:
        lines.append("Misses: none.")

    lines.append("")
    if m["missing_in_db"]:
        lines.append(f"Data note: invoices not found in DB: {m['missing_in_db']}")
    else:
        lines.append("Data note: all example invoices resolved in the DB.")
    return "\n".join(lines)


def main() -> int:
    """Load examples, score them, print the report. Reporting tool -- always exits 0."""
    path = _resolve_examples_path()
    examples = load_examples(path)
    metrics = score(examples)
    print(format_report(metrics, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
