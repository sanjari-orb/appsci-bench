"""Tests for scripts/score_elicitation_batch.py -- the mirror-wide elicitation scorer.

Loaded by path like its single-run sibling, since scripts/ is a directory of runnable tools rather
than an importable package. Nothing here reaches S3 or a model.

The three behaviours under test all come from the same review, and all three share a shape: a failure
that reported itself as success.

* A batch that scored 61 runs and lost one exited 0, because the guard was `failures and not results`.
  An operator scripting around this saw green over a real failure, in a tool that writes to shared S3.
* `_get_json` swallowed every exception and returned `{}`, which the selection pass reads as "this run
  has no manifest". A credentials lapse therefore dropped runs from the work set silently, shrinking
  the scope of the batch under a summary that still claimed success.
* A read that fails is not a run that does not qualify, so it must not land in the skip census.

Exit status still follows the rule the single-run script sets: it is about whether the tool could RUN,
never about the score. An unmeasurable run, or a legitimately low rate, is not a failure.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "score_elicitation_batch.py"


def _load():
    spec = importlib.util.spec_from_file_location("_score_elicitation_batch_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Boom(Exception):
    """A read failure that is NOT a missing object."""


class _Missing(Exception):
    def __init__(self):
        super().__init__("nope")
        self.response = {"Error": {"Code": "NoSuchKey"}}


# --- the absent/unreadable distinction ----------------------------------------------------------

def test_a_missing_object_is_absent_and_anything_else_is_a_failure():
    mod = _load()
    assert mod._is_missing_key(_Missing()) is True
    assert mod._is_missing_key(_Boom("connection reset")) is False


def test_a_missing_key_is_reported_as_absent_but_a_read_failure_raises(monkeypatch):
    """The swallow this replaces is why a credentials lapse could shrink a batch in silence."""
    mod = _load()
    cfg = type("C", (), {"bucket": "b", "prefix": "runs"})()

    monkeypatch.setattr(mod, "_with_retry", lambda _cfg, _op: (_ for _ in ()).throw(_Missing()))
    assert mod._get_json(cfg, "r", "run_manifest.json") == {}

    monkeypatch.setattr(mod, "_with_retry", lambda _cfg, _op: (_ for _ in ()).throw(_Boom("reset")))
    with pytest.raises(_Boom):
        mod._get_json(cfg, "r", "run_manifest.json")


def test_an_unreadable_run_is_an_error_not_a_skip(monkeypatch):
    """A skip means "does not qualify". A run we could not look at is a different statement, and
    burying it in the skip census is how the batch came to under-report its own scope."""
    mod = _load()
    monkeypatch.setattr(mod, "_get_json", lambda *a, **k: (_ for _ in ()).throw(_Boom("reset")))
    runs = {"order_to_cash_20260101_000000": {mod.USERSIM_EVENTS_FILENAME: 10}}

    selected, skipped, read_errors = mod._select(object(), runs, False)

    assert selected == []
    assert sum(skipped.values()) == 0, "an unreadable run was miscounted as a skip"
    assert len(read_errors) == 1
    assert "order_to_cash_20260101_000000" == read_errors[0]["run_id"]


def test_one_unreadable_run_does_not_end_the_sweep(monkeypatch):
    """The other 300 runs are still scoreable; a single bad read must not take them down."""
    mod = _load()
    calls = {"n": 0}

    def flaky(_cfg, run_id, _name):
        calls["n"] += 1
        if run_id.startswith("bad"):
            raise _Boom("reset")
        return {"task_id": "order_to_cash"}

    monkeypatch.setattr(mod, "_get_json", flaky)
    monkeypatch.setattr(mod, "load_rubric", lambda _spec: object())
    monkeypatch.setattr(mod, "load_task_spec", lambda _t: object())
    runs = {f"{p}_20260101_00000{i}": {mod.USERSIM_EVENTS_FILENAME: 10}
            for i, p in enumerate(("bad", "good"))}

    selected, _skipped, read_errors = mod._select(object(), runs, False)

    assert calls["n"] == 2, "the sweep stopped at the failure instead of continuing"
    assert len(selected) == 1 and len(read_errors) == 1


# --- an empty conversation is a real zero -------------------------------------------------------

def test_an_empty_conversation_log_is_selected_rather_than_skipped():
    """A log that exists but is empty means the agent never asked: a real zero, scored without a model
    call. Only a MISSING log is unmeasurable. Filtering on size > 0 conflated the two and dropped 47
    legitimate zeros."""
    mod = _load()
    runs = {
        "order_to_cash_20260101_000000": {mod.USERSIM_EVENTS_FILENAME: 0},   # empty -> a real zero
        "order_to_cash_20260101_000001": {"eval/report.json": 5},            # absent -> unmeasurable
    }
    import unittest.mock as m
    with m.patch.object(mod, "_get_json", lambda *a, **k: {"task_id": "order_to_cash"}), \
         m.patch.object(mod, "load_rubric", lambda _s: object()), \
         m.patch.object(mod, "load_task_spec", lambda _t: object()):
        selected, skipped, _errors = mod._select(object(), runs, False)

    assert [r for r, _t, _rub in selected] == ["order_to_cash_20260101_000000"]
    assert skipped["no conversation log (eval trial or pre-instrumentation)"] == 1


# --- both storage conventions count as scored ---------------------------------------------------

def test_a_score_at_the_run_root_counts_as_scored_like_one_under_eval():
    """Two conventions exist: the orchestrator writes into the run's eval/ dir, the single-run script
    writes to the run root. Checking only the first re-scores every hand-scored run, leaving it with
    two numbers in two places that can disagree."""
    mod = _load()
    runs = {
        "order_to_cash_20260101_000000": {mod.USERSIM_EVENTS_FILENAME: 9,
                                          mod.ELICITATION_KEY: 5},
        "order_to_cash_20260101_000001": {mod.USERSIM_EVENTS_FILENAME: 9,
                                          mod.ROOT_ELICITATION_KEY: 5},
    }
    import unittest.mock as m
    with m.patch.object(mod, "_get_json", lambda *a, **k: {"task_id": "order_to_cash"}), \
         m.patch.object(mod, "load_rubric", lambda _s: object()), \
         m.patch.object(mod, "load_task_spec", lambda _t: object()):
        selected, skipped, _errors = mod._select(object(), runs, False)

    assert selected == []
    assert skipped["already scored (eval/)"] == 1
    assert skipped["already scored (run root)"] == 1


# --- a measurement is never overwritten, a placeholder always is --------------------------------

@pytest.mark.parametrize("status,expected_keep", [
    ("scored", True),           # somebody's measurement
    ("no_exchanges", True),     # a real zero IS a measurement
    ("unmeasurable", False),    # a placeholder saying nothing was measured
    ("no_rubric", False),
    (None, False),
])
def test_only_a_real_measurement_blocks_the_fold(status, expected_keep):
    mod = _load()
    assert (status in mod.MEASURED_STATUSES) is expected_keep


# --- the exit status, which is what the review was actually about --------------------------------

def _staged(tmp_path, run_id, *, rate=0.5):
    d = tmp_path / run_id / "eval"
    d.mkdir(parents=True)
    (d / "elicitation.json").write_text(json.dumps({
        "task_id": "order_to_cash", "status": "scored", "elicited_rate": rate,
        "asked_not_elicited_rate": 0.2, "missed_rate": 0.3, "error_items": 0,
        "num_exchanges": 2, "total_items": 12, "judged_items": 12, "items": [], "warnings": [],
    }))
    return d


def _run_fold_only(mod, monkeypatch, tmp_path, out, failing: set[str]):
    """Drive main() in --fold-only mode with no S3 and no judge. Runs in `failing` raise on upload."""
    monkeypatch.setattr(mod.S3Config, "from_env", classmethod(lambda cls: cls(bucket="b", prefix="runs")))
    monkeypatch.setattr(mod, "_list_mirror", lambda _cfg: {})
    monkeypatch.setattr(mod, "_fold_into_report", lambda *a, **k: "no-report")

    def upload(stage, _sub, _cfg):
        if stage.name in failing:
            raise RuntimeError("upload exploded")
        return type("U", (), {"uploaded": 1})()

    monkeypatch.setattr(mod, "upload_subtree", upload)
    return mod.main(["--fold-only", "--upload",
                     "--stage-dir", str(tmp_path), "--out", str(out)])


def test_a_batch_that_loses_one_run_of_many_exits_nonzero(monkeypatch, tmp_path, capsys):
    """THE reviewed bug: `failures and not results` meant 61 successes and 1 failure exited 0, so a
    caller or CI check saw green over a real failure in a tool that writes to shared state."""
    mod = _load()
    _staged(tmp_path, "order_to_cash_20260101_000000")
    _staged(tmp_path, "order_to_cash_20260101_000001")
    out = tmp_path / "summary.json"

    code = _run_fold_only(mod, monkeypatch, tmp_path, out,
                          failing={"order_to_cash_20260101_000001"})

    assert code == 1, "a batch with one failure and one success reported success"
    summary = json.loads(out.read_text())
    assert len(summary["results"]) == 1 and len(summary["failures"]) == 1


def test_a_wholly_clean_batch_exits_zero(monkeypatch, tmp_path):
    mod = _load()
    _staged(tmp_path, "order_to_cash_20260101_000000")
    out = tmp_path / "summary.json"
    assert _run_fold_only(mod, monkeypatch, tmp_path, out, failing=set()) == 0


def test_an_unreadable_run_reaches_the_exit_status_too(monkeypatch, tmp_path):
    """A run we could not read is a failure of the batch, not a silent omission from it."""
    mod = _load()
    monkeypatch.setattr(mod.S3Config, "from_env",
                        classmethod(lambda cls: cls(bucket="b", prefix="runs")))
    monkeypatch.setattr(mod, "_list_mirror",
                        lambda _cfg: {"order_to_cash_20260101_000000":
                                      {mod.USERSIM_EVENTS_FILENAME: 10}})
    monkeypatch.setattr(mod, "_get_json", lambda *a, **k: (_ for _ in ()).throw(_Boom("reset")))
    out = tmp_path / "summary.json"
    code = mod.main(["--stage-dir", str(tmp_path), "--out", str(out)])
    assert code == 1, "an unreadable run left the batch reporting success"
