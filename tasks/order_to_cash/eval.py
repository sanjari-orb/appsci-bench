"""Order-to-cash eval functions.

Ported from the pre-overhaul `evaluators/deterministic/*.py` (batch `check(results, samples) ->
dict`) and `evaluators/ai_judge/reason_quality.jinja2` onto the per-data-point, keyword-only
eval-function contract (design: Task-Specific Evaluation Function). The port kept each function's
comparison logic and only changed the invocation shape, and with it how "not applicable to this
sample" is represented -- see each function's docstring. The one later departure is
`fault_classification`, which now reads the fault type the artifact declares instead of inferring
one from its prose; that function's docstring says why.
"""
from __future__ import annotations

import json
from typing import Any

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn, _expected
from appsci_bench.eval.helpers import make_transcript_judge, parse_bool, resolve_scalar

#: The closed set of fault types the artifact may declare. This is the authority the artifact's
#: `fault_type` is validated against, and it is the same six values the customer states during BUILD
#: (via the user-sim's shared output-contract document) -- so a value outside this set is the
#: artifact ignoring a requirement it was given, not an unforeseen answer.
FAULT_TYPES = {
    "PRICE_DELTA", "QTY_MISMATCH", "DUPLICATE", "OFF_CONTRACT_FEE", "OFF_CONTRACT_ITEM", "TOTAL_MATH",
}


#: The one name for the flag field, used by the artifact's contract, the oracle, the dataset and the
#: DB fixture alike. Named once here so the metrics and their failure messages can never drift apart.
FLAG_KEY = "flagged"

#: The contract failures every metric here reports, kept DISTINCT because they are different mistakes
#: and a reader of the report needs to tell them apart. The response contract -- a JSON object with
#: `flagged`/`fault_type`/`reason` -- is stated to the agent during BUILD, and the transport fills the
#: structured slot only when the whole reply IS that object
#: (`appsci_bench.eval.datatypes.extract_json_object`).
NO_PAYLOAD = "artifact returned no JSON object (response contract not met)"
NO_FLAG_FIELD = f"artifact's JSON object has no {FLAG_KEY!r} field (response contract not met)"

#: Reported when the oracle itself can't answer the question -- a dataset-authoring gap, not the
#: artifact's fault, so the sample is excluded rather than scored against it.
NO_ORACLE_FLAG = f"sample's expected_output has no {FLAG_KEY!r} field (dataset gap, not scoreable)"


def _contract_payload(conversation: list[ConversationTurn]) -> tuple[dict | None, str | None]:
    """The artifact's last structured response, or `(None, why-it-cannot-be-scored)`.

    Three ways a reply fails the contract, and NONE of them may be readable as a flag decision:

      - no JSON object at all (empty slot, unparseable, or a non-object) -> `NO_PAYLOAD`
      - a bare object that simply lacks the flag field                   -> `NO_FLAG_FIELD`
      - a flag field that isn't boolean-resolvable (`null`, "maybe", 7)  -> a reason naming the value

    The middle two are the subtle ones and were a live bug. Checking only whether the payload was
    ABSENT let a present-but-wrong object -- `{}`, a DB row, an object using some other key name --
    fall through to `.get(FLAG_KEY, False)` and read as a confident "not flagged". That scored 1.0 on
    every clean invoice in the set (19 of 60, a 0.32 floor for free) and recorded false negatives on
    the flagged ones, with `fault_classification` dropping out entirely. Same defect as the empty slot,
    one door over: silent coercion of something the artifact never said.

    A value the `parse_bool` alias table resolves is fine -- `true`, `false`, `"False"`, `"no"`, `0`.
    Only genuinely non-boolean values are rejected, because coercing those is the same mistake in
    miniature: `bool("maybe")` is True, which invents a flag decision out of a shrug."""
    if not conversation:
        return None, NO_PAYLOAD
    raw = conversation[-1].response.json
    if not raw:
        return None, NO_PAYLOAD
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None, NO_PAYLOAD
    if not isinstance(parsed, dict):
        return None, NO_PAYLOAD
    if FLAG_KEY not in parsed:
        return None, NO_FLAG_FIELD
    if parse_bool(parsed[FLAG_KEY]) is None:
        return None, (f"artifact's {FLAG_KEY!r} is {parsed[FLAG_KEY]!r}, not a boolean "
                      "(response contract not met)")
    return parsed, None


def _oracle(expected_output: ArtifactAgentOutput | None) -> tuple[dict | None, str | None]:
    """The sample's oracle, or `(None, why)` when it can't answer the flag question.

    Guards the mirror image of the payload hole on OUR side of the comparison: an absent or malformed
    `expected_output` used to collapse to `{}` and read as "this invoice is clean", quietly turning a
    dataset gap into a scoreable expectation. Every current sample carries the field; this exists so a
    newly-authored one that doesn't fails visibly instead of scoring the artifact against a default."""
    data = (_expected(expected_output) or {})
    if FLAG_KEY not in data:
        return None, NO_ORACLE_FLAG
    return data, None


def _as_bool(value: Any) -> bool:
    """Coerce an ORACLE-side flag value to bool via parse_bool's alias table (reused, not
    reimplemented) -- so a stringly-typed flag ("false"/"no"/"0") isn't misread as truthy by a naive
    bool(value). Falls back to plain bool() only for values parse_bool can't resolve; that fallback is
    tolerable here because the oracle is data we authored, unlike the artifact's reply, where a
    non-boolean is rejected outright (see `_contract_payload`)."""
    parsed = parse_bool(value)
    return parsed if parsed is not None else bool(value)


def _predicted_flag(payload: dict) -> bool:
    """The flag the artifact declared. Only valid on a payload `_contract_payload` returned, which
    guarantees the field is present and boolean-resolvable -- so there is no default to fall back to
    and no coercion left to hide a missing answer."""
    return bool(parse_bool(payload[FLAG_KEY]))


def _predicted_fault_type(payload: dict) -> str | None:
    """The fault type the artifact DECLARED, normalized for comparison (upper-cased, stripped).

    None when the field is absent, null, or blank -- distinct from a present-but-non-canonical value,
    which is returned as-is so the caller can report what the artifact actually said."""
    raw = payload.get("fault_type")
    if raw is None:
        return None
    text = str(raw).strip()
    return text.upper() if text else None




def flag_accuracy(*, data_point_id: str, conversation: list[ConversationTurn],
                  expected_output: ArtifactAgentOutput | None = None,
                  eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """Per-sample flag decision accuracy: 1.0 if the predicted flag matches the oracle, else 0.0.
    Averaged across the dataset by the aggregator, this reproduces the old batch check's overall
    accuracy (design: Evaluation System step 3, "per metric score list by averaging over data
    points").

    A reply with no structured payload scores 0 with `NO_PAYLOAD` recorded -- never a silent `false`.
    Reading an absent flag as "not flagged" would hand a non-compliant artifact a free 1.0 on every
    clean invoice in the set (19 of 60 here, so a floor of 0.32 for answering in the wrong shape)."""
    del data_point_id, eval_rubric, kwargs
    oracle, oracle_why = _oracle(expected_output)
    if oracle is None:
        return None, {"scoreable": False, "error": oracle_why}
    expected = _as_bool(oracle[FLAG_KEY])

    payload, why = _contract_payload(conversation)
    if payload is None:
        return 0.0, {"expected": expected, "error": why}

    predicted = _predicted_flag(payload)
    ok, how = resolve_scalar(predicted, expected)
    return (1.0 if ok else 0.0), {"predicted": predicted, "expected": expected, "how": how}


def flag_recall(*, data_point_id: str, conversation: list[ConversationTurn],
             expected_output: ArtifactAgentOutput | None = None,
             eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Per-sample false-negative check: for a sample the oracle says SHOULD be flagged, 1.0 if the
    artifact caught it, 0.0 if it missed it -- a false negative, the failure mode this metric
    exists to catch. For a sample the oracle says should NOT be flagged, this metric doesn't apply
    -- returns `None` (excluded from aggregation, not scored 0 or 1) rather than a fake pass, since
    `flag_accuracy` already penalizes a wrong decision on these samples (this avoids
    double-penalizing the same mistake under two objectives).

    Applicability is decided by the ORACLE alone, never by what the artifact returned: a missing
    payload on a flagged invoice is a miss (0.0 with `NO_PAYLOAD`), because an answer AP cannot parse
    catches nothing. Gating on the artifact's own flag would let a non-compliant reply present as
    "not applicable" and vanish from the average.

    WHAT AGGREGATES IS RECALL -- the share of real faults caught -- because negatives are excluded
    rather than averaged in as vacuous passes (score=None semantics), so the mean is taken over exactly
    the oracle-positive samples: TP / (TP + FN).

    It was called `flag_fnr` until the name was fixed, carried over from the batch false-negative-rate
    check it replaced. The value was never an FNR: an FNR returns 1.0 for a MISS and reads
    lower-is-better, while this returns 1.0 for a CATCH and is declared `positive: true`. The docstring
    used to carry a paragraph explaining that the metric did not do what its name said, which is a sign
    to rename rather than to keep explaining. Reports written before the rename key this objective as
    `flag_fnr`; the number means the same thing, so cross-run analysis over both needs a name mapping
    (noted in README.md).
    """
    del data_point_id, eval_rubric, kwargs
    oracle, oracle_why = _oracle(expected_output)
    if oracle is None:
        return None, {"applicable": False, "error": oracle_why}
    if not _as_bool(oracle[FLAG_KEY]):
        return None, {"applicable": False}

    payload, why = _contract_payload(conversation)
    if payload is None:
        return 0.0, {"applicable": True, "error": why}

    predicted = _predicted_flag(payload)
    return (1.0 if predicted else 0.0), {"applicable": True, "predicted": predicted}


def fault_classification(*, data_point_id: str, conversation: list[ConversationTurn],
                         expected_output: ArtifactAgentOutput | None = None,
                         eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Per-sample fault-type classification: compare the fault type the artifact DECLARED against
    the oracle's, case- and whitespace-insensitively (`resolve_scalar`). No LLM, no cost, no judge
    variance -- the artifact's answer is a value from a closed set, so this is a mechanical
    comparison (design: "comparable ground truth exists -> deterministic").

    A missing, blank, or non-canonical `fault_type` scores 0. The response contract -- a JSON object
    carrying `flagged`, `fault_type`, `reason`, with `fault_type` one of `FAULT_TYPES` -- is stated to
    the agent during BUILD, so an artifact that omits the field or invents a value has failed a
    requirement it was given; the metric says so instead of rescuing it.

    (This function previously ran an LLM judge that inferred a fault type from the artifact's
    free-text `reason`, which left the declared field unread: the metric measured how the artifact
    EXPLAINED the discrepancy, not how it CLASSIFIED it -- and `reason_quality` already scores the
    explanation. The judge also let an artifact that never emitted a fault type score full marks.)

    Applicability, in order, and the ORDER IS THE POINT:
      - oracle says clean            -> `None`. A data-side fact: there is no fault type to classify.
      - no structured payload        -> 0.0 with `NO_PAYLOAD`. A contract failure must be scored, not
                                       excluded. Checking the artifact's flag first (as this did) let
                                       an unparseable reply read as "not flagged" and return `None`,
                                       so the failure dropped out of aggregation entirely and the
                                       objective scored as if the data point did not exist.
      - artifact said "not flagged"  -> `None`. A genuine missed positive, already penalized by
                                       `flag_recall`; not double-counted here.
      - otherwise                    -> compare the declared type against the oracle's."""
    del data_point_id, eval_rubric, kwargs
    oracle, oracle_why = _oracle(expected_output)
    if oracle is None:
        return None, {"applicable": False, "error": oracle_why}
    if not _as_bool(oracle[FLAG_KEY]):
        return None, {"applicable": False}

    expected_fault_type = oracle.get("fault_type")
    payload, why = _contract_payload(conversation)
    if payload is None:
        return 0.0, {"applicable": True, "predicted_fault_type": None,
                     "expected_fault_type": expected_fault_type, "error": why}
    if not _predicted_flag(payload):
        return None, {"applicable": False}

    predicted_fault_type = _predicted_fault_type(payload)
    meta = {"applicable": True, "predicted_fault_type": predicted_fault_type,
            "expected_fault_type": expected_fault_type}

    if predicted_fault_type is None:
        return 0.0, {**meta, "error": "artifact declared no fault_type on a flagged invoice"}
    if predicted_fault_type not in FAULT_TYPES:
        return 0.0, {**meta, "error": f"fault_type {predicted_fault_type!r} is not one of "
                                      f"{sorted(FAULT_TYPES)}"}

    correct, how = resolve_scalar(predicted_fault_type, expected_fault_type)
    return (1.0 if correct else 0.0), {**meta, "how": how}


_REASON_QUALITY_TEMPLATE = """\
You are an expert accounts-payable reviewer evaluating the quality of a discrepancy reason
provided by an invoice review system. When the system flags an invoice, the reason should
correctly identify the discrepancy type and provide enough detail for an AP reviewer to act on it.

## Scoring Criteria (0-1 Scale)

**0.9-1.0 (Excellent)**: Reason names the exact discrepancy with specific amounts (e.g., "unit
price 148.14 != PO 137.08, total delta $3174.22"). An AP reviewer could immediately understand and
act on this.

**0.6-0.8 (Good)**: Reason names the discrepancy type correctly but lacks specific amounts (e.g.,
"price mismatch detected" or "quantity doesn't match PO"). Reviewer understands the issue but may
need to look up details.

**0.3-0.5 (Fair)**: Reason is vague or partially correct (e.g., "invoice doesn't match PO" or
"discrepancy found"). The type of issue is not clearly identified.

**0.1-0.2 (Poor)**: Reason is present but wrong or misleading (e.g., identifies wrong discrepancy
type, or reason contradicts the actual issue).

**0.0 (Critical)**: Reason is missing, empty, or completely nonsensical.

## Special Cases

- If the system correctly did NOT flag a clean invoice, score 1.0 (no reason needed for
  non-flagged invoices).
- If the system incorrectly flagged a clean invoice, score based on whether the false reason is
  plausible (0.3) or nonsensical (0.0).

## Ground truth (oracle label for this invoice)
{{ expected_json }}

## Conversation to judge (the review task given, and the system's actual response)
{{ transcript }}

Evaluate step by step: (1) check if the flag decision matches the oracle; (2) if flagged, assess
whether the reason identifies the correct discrepancy type; (3) evaluate the specificity and
actionability of the reason; (4) give a final score and explanation.
"""

reason_quality = make_transcript_judge(_REASON_QUALITY_TEMPLATE)
