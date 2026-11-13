"""Tests for the eval metrics math and report (pure, offline -- no DB, stubbed respond)."""
from __future__ import annotations

from solution.eval import run_eval


def test_compute_metrics_basic():
    m = {"n": 4, "tp": 1, "fp": 1, "fn": 1, "tn": 1, "per_fault": {}, "missing_in_db": []}
    metrics = run_eval.compute_metrics(m)
    assert metrics["flag_accuracy"] == 0.5
    assert metrics["precision"] == 0.5
    assert metrics["recall"] == 0.5
    assert metrics["f1"] == 0.5
    assert metrics["false_positives"] == 1
    assert metrics["false_negatives"] == 1


def test_compute_metrics_divide_by_zero_guarded():
    """All-zero confusion cells return 0.0, not a ZeroDivisionError."""
    m = {"n": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0, "per_fault": {}, "missing_in_db": []}
    metrics = run_eval.compute_metrics(m)
    assert metrics == {
        "flag_accuracy": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "false_positives": 0,
        "false_negatives": 0,
    }


def test_score_buckets_and_missing(monkeypatch):
    """score() builds the confusion matrix, per-fault buckets, and missing-in-DB list."""
    examples = [
        {"invoice_number": "INV-DUP", "expected_flagged": True, "fault_type": "DUPLICATE"},
        {"invoice_number": "INV-CLEAN", "expected_flagged": False, "fault_type": "CLEAN"},
        {"invoice_number": "INV-PRICE", "expected_flagged": True, "fault_type": "PRICE_DELTA"},
        {"invoice_number": "INV-GONE", "expected_flagged": True, "fault_type": "DUPLICATE"},
    ]

    # Stub respond(): DUP flagged; CLEAN not; PRICE_DELTA missed (FN); GONE not-found.
    def fake_predict(number):
        if number == "INV-DUP":
            return True, "duplicate of X"
        if number == "INV-CLEAN":
            return False, "matches PO"
        if number == "INV-PRICE":
            return False, "matches PO"  # not implemented yet -> false negative
        return True, "invoice INV-GONE not found in the database"

    monkeypatch.setattr(run_eval, "_predict", fake_predict)
    m = run_eval.score(examples)

    assert m["tp"] == 2  # INV-DUP and INV-GONE both flagged & expected-flagged
    assert m["fn"] == 1  # INV-PRICE missed
    assert m["tn"] == 1  # INV-CLEAN
    assert m["fp"] == 0
    assert m["missing_in_db"] == ["INV-GONE"]
    assert m["per_fault"]["DUPLICATE"]["n"] == 2
    assert m["per_fault"]["PRICE_DELTA"]["flagged_correct"] == 0


def test_format_report_contains_key_lines():
    m = {"n": 1, "tp": 1, "fp": 0, "fn": 0, "tn": 0,
         "per_fault": {"DUPLICATE": {"n": 1, "flagged_correct": 1}}, "missing_in_db": []}
    report = run_eval.format_report(m, "somepath")
    assert "F1-score" in report
    assert "false negatives: 0" in report
    assert "DUPLICATE" in report
