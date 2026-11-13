"""Shared fixtures for the elicitation-score tests.

The pieces here are used by more than one test module (the transcript reader, the judge, the scorer),
so they live in one place rather than being re-hand-rolled per file.
"""
from __future__ import annotations

import json
import pathlib

import pytest


@pytest.fixture
def write_usersim_events():
    """Write a user-sim event log from a list of raw event dicts; returns the run directory."""
    def _write(run_dir: pathlib.Path, events: list[dict]) -> pathlib.Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "events-usersim.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return run_dir
    return _write


@pytest.fixture
def usersim_exchange():
    """One request/response event pair for a turn, in the shape the sim service actually emits."""
    def _pair(turn: int, question: str, answer: str) -> list[dict]:
        return [
            {"event_type": "usersim.request", "timestamp": f"2026-08-20T10:0{turn}:00+00:00",
             "payload": {"question": question, "turn": turn}},
            {"event_type": "usersim.response", "timestamp": f"2026-08-20T10:0{turn}:09+00:00",
             "payload": {"answer": answer, "turn": turn, "duration_ms": 1234.5678}},
        ]
    return _pair


@pytest.fixture
def rubric_task(tmp_path):
    """Build a synthetic task directory: a spec with two scored objectives, plus an optional rubric.

    Returns ``(spec_dict, repo_root)`` -- the pair every rubric loader call needs. The spec's rubric
    pointer is repo-root-relative, exactly as a real task's is, so path resolution is exercised rather
    than bypassed with an absolute path.
    """
    import yaml

    def _build(items: list[dict] | None = None, *, task_id: str = "demo_task",
               rubric_task_id: str | None = None, objectives: list[str] | None = None,
               rubric_filename: str = "elicitation_rubric.yaml",
               pointer: str | None = None, raw_rubric_text: str | None = None) -> tuple[dict, pathlib.Path]:
        repo_root = tmp_path / "repo"
        task_dir = repo_root / "tasks" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        names = objectives if objectives is not None else ["classification_correctness", "coverage"]
        spec = {
            "task_id": task_id,
            "evaluation_data": {
                "dataset": f"tasks/{task_id}/eval_data",
                "objectives": [{"name": n, "eval_function_path": f"{task_id}.eval.{n}",
                                "positive": True, "weight": 1.0} for n in names],
            },
        }
        if raw_rubric_text is not None:
            (task_dir / rubric_filename).write_text(raw_rubric_text)
        elif items is not None:
            (task_dir / rubric_filename).write_text(yaml.safe_dump(
                {"task_id": rubric_task_id or task_id, "items": items}, sort_keys=False))
        if items is not None or raw_rubric_text is not None or pointer is not None:
            spec["evaluation_data"]["elicitation_rubric"] = (
                pointer if pointer is not None else f"tasks/{task_id}/{rubric_filename}")
        return spec, repo_root

    return _build


@pytest.fixture(autouse=True)
def no_elicitation_retry_delay(monkeypatch):
    """Zero the judge's between-attempt waits for every test in this directory.

    The waits are real and deliberate in production -- what reaches that retry is usually a rate-limit
    burst, and re-firing into one immediately buys nothing. But a suite that actually slept through them
    would spend minutes proving the retry counter increments, so the delays are patched out and their
    LENGTH is asserted separately instead.
    """
    monkeypatch.setattr("appsci_bench.eval.elicitation._RETRY_DELAYS_S", (0.0, 0.0))
