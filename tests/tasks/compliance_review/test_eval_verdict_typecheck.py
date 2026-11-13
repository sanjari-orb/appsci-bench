"""The verdict a metric scores must be a real boolean, not merely a present key.

Both cases below were measured against this task's own 8-case eval set before the guard checked type,
and both let a reply that never gave a verdict collect real credit:

  * `{"compliant": null}` passed a key-presence check, and `bool(None)` is False, so it read as
    "not compliant" -- taking full marks on one of the two deterministic objectives for EVERY case
    (1.00 violation_set_f1 on the clean contracts, since an empty predicted violation set against an
    empty gold set is a perfect F1; 1.00 verdict_accuracy on the rest).
  * `{"compliant": "false"}` passed too, and every non-empty string is truthy in Python, so the string
    "false" scored as True -- the exact opposite of what the artifact said, with no error raised.

Neither needs an adversary: a model emitting null for a field it is unsure about, or a serialization
layer stringifying a boolean, are ordinary bugs. The point of these tests is that such a reply is
REJECTED as no-payload rather than silently scored.
"""
from __future__ import annotations

import json

import pytest

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn
from tasks.compliance_review.eval import (
    NO_PAYLOAD,
    verdict_accuracy,
    violation_set_f1,
)

#: A non-compliant oracle: the reply shapes below must not be able to match it by coercion.
_ORACLE = ArtifactAgentOutput(json=json.dumps({
    "compliant": False,
    "violations": [{"category": "Cap On Liability", "rule": "must_have",
                    "present": False, "evidence_spans": []}],
    "needs_review": [],
}))

#: A compliant oracle with no violations -- the shape that used to hand out a vacuous perfect F1.
_ORACLE_CLEAN = ArtifactAgentOutput(json=json.dumps({
    "compliant": True, "violations": [], "needs_review": [],
}))


def _conversation(reply: dict) -> list[ConversationTurn]:
    return [ConversationTurn(query=ArtifactAgentQuery(text="review this"),
                             response=ArtifactAgentOutput(json=json.dumps(reply)))]


#: Every way a verdict can be present but not a boolean. `0`/`1` are here because
#: `isinstance(True, int)` is True in Python, so a type check written as `int` would wave them through.
_NOT_A_VERDICT = [
    pytest.param(None, id="null"),
    pytest.param("false", id="string-false"),
    pytest.param("true", id="string-true"),
    pytest.param("", id="empty-string"),
    pytest.param(0, id="int-0"),
    pytest.param(1, id="int-1"),
    pytest.param([], id="empty-list"),
    pytest.param({}, id="empty-dict"),
]


@pytest.mark.parametrize("verdict", _NOT_A_VERDICT)
def test_a_non_boolean_verdict_is_rejected_not_scored(verdict) -> None:
    conv = _conversation({"compliant": verdict, "violations": []})
    for fn in (violation_set_f1, verdict_accuracy):
        score, meta = fn(data_point_id="s1", conversation=conv, expected_output=_ORACLE)
        assert score == 0.0, f"{fn.__name__} scored {score} for verdict {verdict!r}"
        assert meta.get("error") == NO_PAYLOAD, (
            f"{fn.__name__} scored verdict {verdict!r} instead of rejecting it as no-payload")


@pytest.mark.parametrize("verdict", _NOT_A_VERDICT)
def test_a_non_boolean_verdict_cannot_earn_the_vacuous_perfect_f1(verdict) -> None:
    """The specific regression: against a CLEAN oracle, an empty violations list is a perfect set
    match, so a reply with no real verdict used to score 1.0 on the heaviest objective."""
    conv = _conversation({"compliant": verdict, "violations": []})
    score, meta = violation_set_f1(data_point_id="s1", conversation=conv,
                                   expected_output=_ORACLE_CLEAN)
    assert score == 0.0, f"verdict {verdict!r} earned violation_set_f1 {score} against a clean oracle"
    assert meta.get("error") == NO_PAYLOAD


def test_a_missing_verdict_key_is_still_rejected() -> None:
    """The case the original guard already covered -- kept so tightening the check cannot lose it."""
    conv = _conversation({"violations": []})
    score, meta = verdict_accuracy(data_point_id="s1", conversation=conv, expected_output=_ORACLE)
    assert score == 0.0
    assert meta.get("error") == NO_PAYLOAD


@pytest.mark.parametrize("verdict", [True, False])
def test_a_real_boolean_verdict_is_scored_normally(verdict) -> None:
    """The guard must not reject genuine answers -- including `False`, which is falsy."""
    conv = _conversation({"compliant": verdict, "violations": [], "needs_review": []})
    score, meta = verdict_accuracy(data_point_id="s1", conversation=conv, expected_output=_ORACLE)
    assert "error" not in meta, f"a real boolean verdict {verdict!r} was rejected: {meta}"
    # _ORACLE is non-compliant, so False matches and True does not.
    assert score == (1.0 if verdict is False else 0.0)


def test_a_stringified_verdict_can_no_longer_invert_the_answer() -> None:
    """Before the fix, the string "false" against a non-compliant oracle scored 1.0 the wrong way
    round via `bool("false") is True`. It must now be rejected, never scored as either answer."""
    conv = _conversation({"compliant": "false", "violations": []})
    score, meta = verdict_accuracy(data_point_id="s1", conversation=conv, expected_output=_ORACLE)
    assert meta.get("error") == NO_PAYLOAD
    assert score == 0.0
