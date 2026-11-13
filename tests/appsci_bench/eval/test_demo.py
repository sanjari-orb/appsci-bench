"""The end-to-end demo SCRIPT (scripts/evaluator_system_e2e.py) runs clean with no creds.

The demo lives in scripts/ (a manual check, not an importable module), so we load it by path.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "evaluator_system_e2e.py"


def _load_demo():
    spec = importlib.util.spec_from_file_location("_eval_demo_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_main_runs_clean(tmp_path, capsys):
    demo = _load_demo()
    rc = demo.main(["--out-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo_single_turn" in out and "demo_multi_turn" in out
    assert "FINAL SCORE:" in out
    assert "--- JSON ---" in out                       # prints JSON too


def test_demo_writes_reports_to_out_dir(tmp_path):
    demo = _load_demo()
    assert demo.main(["--out-dir", str(tmp_path)]) == 0
    for sub in ("single_turn", "multi_turn"):
        assert (tmp_path / sub / "report.txt").exists()
        doc = json.loads((tmp_path / sub / "report.json").read_text())
        assert "final_score" in doc and doc["results"]
        assert list((tmp_path / sub / "conversations").glob("*.json"))  # transcripts written
