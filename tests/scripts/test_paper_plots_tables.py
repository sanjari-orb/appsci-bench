"""Tests for scripts/paper_plots/make_tables.py, the LaTeX tables beside the paper figures.

Loaded by path, since `scripts/` is not a package. Nothing it imports needs plotly.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "paper_plots" / "make_tables.py"
_spec = importlib.util.spec_from_file_location("paper_plots_make_tables", _PATH)
assert _spec is not None and _spec.loader is not None
tables = importlib.util.module_from_spec(_spec)
sys.modules["paper_plots_make_tables"] = tables
_spec.loader.exec_module(tables)


def _row(text: str, model_name: str) -> list[str]:
    line = next(line for line in text.splitlines() if model_name in line and "&" in line)
    return [cell.strip().rstrip("\\").strip() for cell in line.split("&")]


def test_headline_table_has_every_model_and_balanced_rows():
    text = tables.headline_table(tables.load_results())
    for name in tables.MODELS.values():
        assert _row(text, name)
    assert text.count(r"\begin{table}") == text.count(r"\end{table}") == 1
    body = [line for line in text.splitlines() if line.endswith(r"\\") and "&" in line]
    assert {line.count("&") for line in body} == {3}


def test_headline_row_matches_the_committed_cells():
    results = tables.load_results()
    cells = tables.grid(results, opt_enabled=True)
    tasks = tables.shared_tasks(cells)
    model = "claude-opus-4-8"
    mean = sum(cells[(model, t)].score for t in tasks) / len(tasks)
    builds = sum(cells[(model, t)].k_builds for t in tasks)
    row = _row(tables.headline_table(results), tables.MODELS[model])
    assert row[1].startswith(f"{mean * 100:.1f} ")
    assert row[3] == f"{builds} / {tables.FULL_BUILDS * len(tasks)}"


def test_ablation_table_leaves_the_no_user_arms_blank_and_escapes_tau():
    text = tables.ablation_table(tables.load_results())
    rows = [line for line in text.splitlines() if line.endswith(r"\\") and "&" in line][2:]
    assert len(rows) == (len(tables.TASKS) + 1) * len(tables.MODELS)
    assert all(line.split("&")[4].strip() == "--" for line in rows)
    assert "τ" not in text and r"$\tau$" in text


def test_mean_std_is_the_population_std_and_rejects_empty():
    assert tables._mean_std([0.2, 0.4]) == pytest.approx((0.3, 0.1))
    assert tables._mean_std([0.5]) == (0.5, 0.0)
    with pytest.raises(ValueError):
        tables._mean_std([])


def _steps(total, co, internal, external):
    """`co` in the order internal changed, both changed, both same direction, held-out changed,
    held-out changed with the internal score flat."""
    keys = ("internal_changed", "both_changed", "both_changed_same_direction",
            "held_out_changed", "held_out_changed_internal_flat")
    counts = {"agreed": total, "disagreed": 0, "missed": 0, "false_improvement": 0,
              "no_change": 0}  # only the total matters here
    return tables.Steps(counts, builds=len(internal), internal_changes=internal,
                        external_changes=external, co=dict(zip(keys, co, strict=True)))


def test_internal_external_table_rows_order_and_totals():
    steps = {
        "crm_insights": _steps(2, (0, 0, 0, 2, 2), [0.0], [-0.02]),
        "slides_generator": _steps(3, (3, 2, 2, 2, 0), [0.07], [0.01]),
        "order_to_cash": _steps(1, (1, 0, 0, 0, 0), [0.55], [0.0]),
    }
    text = tables.internal_external_table(steps)
    # Two header rows (groups, columns), then the body.
    body = [line for line in text.splitlines() if line.endswith(r"\\") and "&" in line][2:]
    assert [line.split("&")[0].strip() for line in body] == [
        "Slides", "O2C", "CRM", r"\textit{All tasks}"]
    assert {line.count("&") for line in body} == {9}
    slides, o2c, crm, total = ([c.strip().rstrip("\\").strip() for c in line.split("&")]
                               for line in body)
    assert slides[3:] == ["3", r"2 (67\%)", r"2 (67\%)", "2", r"0 (0\%)", "+7.0", "+1.0"]
    assert o2c[3:8] == ["1", r"0 (0\%)", r"0 (0\%)", "0", "--"]  # held-out never changed
    assert crm[3:8] == ["0", "--", "--", "2", r"2 (100\%)"]  # self-grade never changed
    assert total[1:8] == ["3", "6", "4", r"2 (50\%)", r"2 (50\%)", "4", r"2 (50\%)"]


def test_internal_external_table_needs_a_task():
    with pytest.raises(ValueError):
        tables.internal_external_table({})


def test_change_formatting():
    assert tables._change(None) == "--"
    assert tables._change(0.0004) == "0.0" and tables._change(-0.0004) == "0.0"
    assert tables._change(0.169) == "+16.9" and tables._change(-0.086) == "$-$8.6"
