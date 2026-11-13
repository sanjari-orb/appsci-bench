"""Tests for the cross-task summary of the internal-vs-external results."""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import sys

_SCRIPT = (pathlib.Path(__file__).resolve().parents[2]
           / "scripts" / "analysis" / "internal_eval_consistency" / "summarize_results.py")
_MODNAME = "summarize_results_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODNAME, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = module
    spec.loader.exec_module(module)
    return module


MOD = _load()


def _series(tau, *, regret=0.0, correct_keep=0, wrong_reject=0):
    return {"kendall_tau_b": tau, "spearman": tau, "pearson": tau, "programs_built": 3, "n_deploys": 4,
            "internal_first": 0.5, "internal_last": 1.0, "external_first": 0.4, "external_last": 0.5,
            "external_noise_sd": 0.01, "regret": {"regret": regret},
            "loop_correct_keep": correct_keep, "loop_correct_reject": 0,
            "loop_wrong_keep": 0, "loop_wrong_reject": wrong_reject}


def _write(model_dir: pathlib.Path, task: str, series: dict, skipped=None):
    path = model_dir / task / "self_grade_vs_real" / "stats.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"series": series, "skipped": skipped or {}, "limit": None}))


def test_averages_are_over_builds_and_skip_undefined_tau(tmp_path):
    """An undefined tau-b is listed but kept out of the mean, and the count says so."""
    _write(tmp_path, "task_a", {"task_a_m_b0_l3_20260901_000000": _series(1.0),
                                "task_a_m_b1_l3_20260901_000001": _series(0.0)})
    _write(tmp_path, "task_b", {"task_b_m_b0_l3_20260902_000000": _series(None)})

    rows, _ = MOD.rows_for(tmp_path)
    text = MOD.render("m", rows, {})

    assert len(rows) == 3
    assert math.isnan(rows[2]["kendall_tau_b"])
    assert "mean τb over builds: **+0.50** (2 builds with a defined τb)" in text


def test_task_mean_weights_each_task_equally(tmp_path):
    """Three builds on one task must not outvote a single build on another."""
    _write(tmp_path, "task_a", {f"task_a_m_b{i}_l3_20260901_00000{i}": _series(1.0) for i in range(3)})
    _write(tmp_path, "task_b", {"task_b_m_b0_l3_20260902_000000": _series(-1.0)})

    text = MOD.render("m", *MOD.rows_for(tmp_path))

    assert "mean τb over builds: **+0.50**" in text
    assert "mean τb over task means: **+0.00** (2 tasks)" in text


def test_builds_sharing_an_index_stay_distinct(tmp_path):
    """banking_support has two separate `b0` launches; the summary must not collapse them."""
    _write(tmp_path, "banking_support", {
        "banking_support_m_b0_l3_20260920_174913": _series(-0.33),
        "banking_support_m_b0_l3_20260921_075749": _series(-0.5),
    })

    rows, _ = MOD.rows_for(tmp_path)

    assert [r["build"] for r in rows] == ["b0 (0920)", "b0 (0921)"]


def test_skipped_builds_are_reported_not_dropped(tmp_path):
    _write(tmp_path, "task_a", {"task_a_m_b0_l3_20260901_000000": _series(1.0)},
           skipped={"fewer_than_two_points": ["task_a_m_b1_l3_20260901_000001"]})

    text = MOD.render("m", *MOD.rows_for(tmp_path))

    assert "task_a: 1 build(s) skipped, `fewer_than_two_points`" in text


def test_loop_agreement_pools_decisions_across_builds(tmp_path):
    _write(tmp_path, "task_a", {"task_a_m_b0_l3_20260901_000000": _series(1.0, correct_keep=3, wrong_reject=1)})

    text = MOD.render("m", *MOD.rows_for(tmp_path))

    assert "external eval agreed with: **75%** of 4" in text


def test_missing_model_directory_is_an_error_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "RESULTS", tmp_path)

    assert MOD.main(["--model", "absent"]) == 1
