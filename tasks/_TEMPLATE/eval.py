"""Template task eval functions -- copy this file, delete these two examples, write your own.

Demonstrates the two eval-data patterns documented in `eval_data/README.md` (and worked in
`eval_data/s1`/`s2`): a deterministic, ground-truth-compared metric and an LLM-judge rubric metric.
Both follow the eval-function contract exactly (design: Task-Specific Evaluation Function --
docs/evaluator_system_design.md): keyword-only `(*, data_point_id, conversation,
expected_output=None, eval_rubric=None, **kwargs) -> (score: float in [0,1] | None, metadata: dict)`.
"""
from __future__ import annotations

import json
from typing import Any

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn, _expected
from appsci_bench.eval.helpers import json_field_match, make_transcript_judge


def _last_response_json(conversation: list[ConversationTurn]) -> dict | None:
    """The artifact's most recent structured response as a dict, or None if it produced none.

    None, NOT `{}`. Returning an empty dict here is a trap worth spelling out, because it is a real
    bug this repo has already paid for twice: `{}` reads downstream as "every field absent", and a
    metric that compares fields then treats a reply that said NOTHING as a reply that said "false",
    "empty list", "no violations". Against the right oracle that MATCHES, so an artifact returning
    nothing scores full marks. Keep the two distinguishable and fail loudly (see `field_match`)."""
    if not conversation:
        return None
    raw = conversation[-1].response.json
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None




# --- pattern 1: deterministic, compared against structured ground truth (see eval_data/s1) -------

def field_match(*, data_point_id: str, conversation: list[ConversationTurn],
                expected_output: ArtifactAgentOutput | None = None,
                eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """Fraction of expected_output.json's fields the artifact's response.json matches.

    `json_field_match` (appsci_bench.eval.helpers) does the actual comparison: bool/int/float/str
    fields use `resolve_scalar` (numeric-with-units, boolean aliases, normalized-string), falling
    back to fuzzy text_similarity for a near-miss string; list/set/tuple fields compare
    order-insensitively. A field missing from the response scores that field 0, not an error.

    THE TWO GUARDS BELOW ARE THE PATTERN TO COPY, not boilerplate to trim. A reply with no structured
    payload has failed the response contract your task states during BUILD: score it 0 with the reason
    recorded. Never let it reach the comparison, where an absent field reads as a confident wrong
    answer and can MATCH the oracle. And an absent oracle is a dataset gap, not the artifact's fault,
    so exclude the sample (`None`) with the reason recorded rather than scoring against a default --
    `json_field_match` with nothing to compare returns a vacuous 1.0.
    """
    del data_point_id, eval_rubric, kwargs
    expected = _expected(expected_output)
    if expected is None:
        return None, {"scoreable": False, "error": "sample has no expected_output.json (dataset gap)"}
    predicted = _last_response_json(conversation)
    if predicted is None:
        return 0.0, {"error": "artifact returned no JSON object (response contract not met)"}
    return json_field_match(predicted, expected)


# --- pattern 2: LLM judge against a natural-language rubric (see eval_data/s2) --------------------

_RESPONSE_QUALITY_TEMPLATE = """\
You are an expert evaluator scoring an AI agent's response to a user request.

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: The response fully addresses the request and matches the rubric below.

**0.4-0.7 (Partial)**: The response addresses the request but misses or gets wrong something the
rubric calls for.

**0.0-0.3 (Poor)**: The response fails to address the request, or contradicts the rubric.

## Rubric for this case
{{ eval_rubric }}

## Conversation to judge
{{ transcript }}

Evaluate step by step: (1) note what the rubric requires; (2) check the response against it;
(3) give a final score and explanation.
"""
# Note the split above -- copy it exactly, don't collapse it: "Scoring Criteria" is THIS METRIC's
# own definition, written once, right here in the template string, by you (the task designer). It
# never varies per sample. `{{ eval_rubric }}` ("Rubric for this case") is the OPPOSITE: it's
# `EvalSample.eval_rubric`, an OPTIONAL per-DATA-POINT NL note that varies sample to sample (or is
# None for most samples). Don't try to make `eval_rubric` carry your metric's scoring criteria --
# that's a real bug that has bitten this codebase before (see appsci_bench/eval/helpers/files.py's
# `make_source_grounding_judge` for the fixed version and its `metric_rubric` vs. `eval_rubric` split
# for a judge factory that ALSO needs a metric-level definition alongside the per-sample note).

response_quality = make_transcript_judge(_RESPONSE_QUALITY_TEMPLATE)
