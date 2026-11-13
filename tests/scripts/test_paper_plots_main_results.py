"""Tests for scripts/paper_plots/make_main_results_table.py: the native-harness rows, now one per
optimization setting.

Loaded by path, since `scripts/` is not a package. Nothing it imports needs plotly.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "paper_plots"
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location("paper_plots_main_results", _DIR / "make_main_results_table.py")
assert _spec is not None and _spec.loader is not None
main_results = importlib.util.module_from_spec(_spec)
sys.modules["paper_plots_main_results"] = main_results
_spec.loader.exec_module(main_results)

HEADER = "model,harness,opt_enabled,task,k_builds,score_pow_k_mean,score_pow_k_std,note\n"


def _write(tmp_path: pathlib.Path, rows: list[str]) -> pathlib.Path:
    path = tmp_path / "harness.csv"
    path.write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return path


def _full_run(model: str, harness: str, opt: str, score: str = "0.5") -> list[str]:
    return [f"{model},{harness},{opt},{t},3,{score},0.01," for t in main_results.TASKS]


def test_committed_table_has_both_settings_for_every_harness_run():
    runs = main_results.load_harness()
    pairs = {(m, h) for m, h, _ in runs}
    assert pairs == {("claude-opus-4-8", "claude_code"), ("claude-opus-5", "claude_code"), ("gpt-5.5", "codex")}
    assert {(m, h, opt) for m, h, opt in runs} == {(m, h, opt) for m, h in pairs for opt in (True, False)}
    # Optimization on comes first, as for the AppSci rows.
    keys = list(runs)
    assert all(k[2] for k in keys[:3]) and not any(k[2] for k in keys[3:])


def test_on_and_off_are_separate_runs(tmp_path):
    path = _write(tmp_path, _full_run("gpt-5.5", "codex", "false", "0.2") + _full_run("gpt-5.5", "codex", "true", "0.4"))
    runs = main_results.load_harness(path)
    assert list(runs) == [("gpt-5.5", "codex", True), ("gpt-5.5", "codex", False)]
    assert runs[("gpt-5.5", "codex", True)]["slides_generator"].score == 0.4
    rows = main_results.native_rows(runs, "gpt-5.5")
    assert [r.harness for r in rows] == [r"Codex\optOn", r"Codex\optOff"]
    assert {r.native for r in rows} == {"Codex"}


@pytest.mark.parametrize("opt", ["", "yes", "1"])
def test_opt_enabled_must_be_true_or_false(tmp_path, opt):
    path = _write(tmp_path, _full_run("gpt-5.5", "codex", opt))
    with pytest.raises(ValueError, match="opt_enabled"):
        main_results.load_harness(path)


def test_a_duplicate_setting_and_task_is_refused(tmp_path):
    rows = _full_run("gpt-5.5", "codex", "true")
    path = _write(tmp_path, rows + rows[:1])
    with pytest.raises(ValueError, match="duplicate"):
        main_results.load_harness(path)


def test_a_setting_missing_a_task_is_refused(tmp_path):
    path = _write(tmp_path, _full_run("gpt-5.5", "codex", "true") + _full_run("gpt-5.5", "codex", "false")[:-1])
    with pytest.raises(ValueError, match="has no row for"):
        main_results.load_harness(path)


def test_the_tables_name_each_harness_once_and_carry_both_rows():
    best, full, tasks = main_results.build_sections()
    tex = main_results.render(best, full, tasks)
    assert r"Claude Code\optOn" in tex and r"Claude Code\optOff" in tex
    assert r"Codex\optOn" in tex and r"Codex\optOff" in tex
    rows, _ = main_results.build_detailed()
    detailed = main_results.render([], rows, tasks, detailed=True)
    assert "Opus 5 and Opus 4.8 in Claude Code, and GPT-5.5 in Codex." in detailed
