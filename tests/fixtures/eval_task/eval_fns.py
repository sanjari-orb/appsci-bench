"""In-repo eval functions for evaluator unit/e2e tests. They conform to the eval-function contract
(see appsci_bench/eval/README.md's "Eval-function contract" section): keyword-only
(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs) -> (float, metadata_dict).
Keyword-only so task authors can't depend on positional order; the orchestrator calls by keyword."""
from __future__ import annotations

import time


def always_one(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    return 1.0, {}


def text_exact_match(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    got = conversation[-1].response.text if conversation else None
    exp = expected_output.text if expected_output is not None else None
    return (1.0 if got == exp else 0.0), {"got": got, "expected": exp}


def echo_final_state(*, data_point_id, conversation, expected_output=None, eval_rubric=None,
                     final_state=None, **kwargs):
    """Proves scoring.py forwards the per-sample `final_state` (from the read_final_state hook) into
    eval functions. Scores 1.0 iff a final_state was delivered; echoes it in metadata."""
    return (1.0 if final_state else 0.0), {"final_state": final_state}


def raises(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    raise RuntimeError("boom")


def returns_out_of_range(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    return 5.0, {}


def slow(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    time.sleep(2.0)
    return 1.0, {}


def very_slow(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    """Sleeps far longer than the timeout under test -- for asserting a timed-out call's abandoned
    worker doesn't block the caller's return (see test_timeout_does_not_block_on_the_abandoned_
    call). Kept short (not e.g. 30s): `concurrent.futures` joins abandoned worker threads at
    process exit, so a long sleep here would hang the whole test run after results are printed,
    even though the call under test itself returns immediately."""
    time.sleep(3.0)
    return 1.0, {}


NOT_CALLABLE = 123


def returns_bad_shape(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    return 1.0  # not a (score, metadata) pair


def returns_bad_metadata(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    return 1.0, "not a dict"


def returns_none(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    """Not a failure -- signals this metric doesn't apply to this data point (design:
    Task-Specific Evaluation Function). Excluded from aggregation, never scored 0."""
    return None, {"applicable": False}


def missing_kwargs_catchall(*, data_point_id, conversation, expected_output=None, eval_rubric=None):
    """Deliberately mis-shaped: no **kwargs, so a future additive contract field would TypeError.
    Used to test validate_eval_function_signature, never registered as a real objective."""
    return 1.0, {}


def requires_an_extra_positional_arg(extra, *, data_point_id, conversation, expected_output=None,
                                     eval_rubric=None, **kwargs):
    """Deliberately mis-shaped: `extra` has no default and scoring.py never supplies it, so the
    real call would TypeError. Used to test validate_eval_function_signature."""
    return 1.0, {}


def wrong_keyword_names(*, sample_id, turns, **kwargs):
    """Deliberately mis-shaped: none of the contract's keywords are accepted by name, and there's
    no default for either, so the real call would TypeError before **kwargs ever helps. Used to
    test validate_eval_function_signature."""
    return 1.0, {}
