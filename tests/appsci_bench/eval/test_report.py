"""Unit tests: report rendering (text + JSON)."""
from __future__ import annotations

import json

from appsci_bench.eval.aggregate import aggregate
from appsci_bench.eval.report import render_json, render_text
from appsci_bench.eval.datatypes import DataPointMetricResult, Objective


def _report():
    results = [DataPointMetricResult("d1", "acc", 0, 0.9, {}),
               DataPointMetricResult("d1", "cost", 0, 0.2, {}, error="boom")]
    objs = [Objective("acc", "x.acc", True, 1.0), Objective("cost", "x.cost", False, 1.0)]
    return aggregate("mytask", results, objs, run_id="run_abc")


def test_render_text_has_headline_and_metrics():
    text = render_text(_report())
    assert "mytask" in text and "run_abc" in text
    assert "FINAL SCORE:" in text
    assert "acc" in text and "cost" in text
    assert "errors: 1 of 2" in text


def test_render_json_roundtrips():
    doc = json.loads(render_json(_report()))
    assert doc["task_id"] == "mytask" and doc["run_id"] == "run_abc"
    assert "final_score" in doc and "views" in doc
    assert len(doc["results"]) == 2
