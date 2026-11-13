"""crm_insights eval functions.

Both objectives are LLM-judge eval functions built with `make_transcript_judge`, mirroring
tasks/order_to_cash/eval.py's `reason_quality` and tasks/airline_rebooking/eval.py's
`policy_compliance`. There is no regex/keyword parsing of the artifact's reply anywhere in this
file -- a judge reads the reply for meaning instead of matching literal labels/markers, so wording
variation that a positional text parser would mishandle is not a special case here.

`expected_output.json` (per seed_data/PROVENANCE.md) carries the real CRMArena answer plus a
best-effort supporting-record set: {"answer": ..., "evidence_case_ids": [...], "task_category": ...,
"reward_metric": "exact_match" | "fuzzy_match"}.
"""
from __future__ import annotations

import json
from typing import Any

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn
from appsci_bench.eval.helpers import make_transcript_judge


def _expected(expected_output: ArtifactAgentOutput | None) -> dict:
    if expected_output is None or not expected_output.json:
        return {}
    try:
        parsed = json.loads(expected_output.json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_ANSWER_CORRECTNESS_TEMPLATE = """\
You are grading whether an AI agent's answer to a CRM analytics question matches the ground truth.

The ground truth below gives the real answer and the matching convention (`reward_metric`) to judge
it under.

## Ground truth
{{ expected_json }}

## Conversation to judge (the question asked, and the agent's actual reply)
{{ transcript }}

## Scoring (binary -- no partial credit)

Score exactly **1.0** if the reply commits to a single answer and that answer is the same real-world
value as the gold `answer`, judged under the gold's own `reward_metric`:
  - `exact_match`: the stated answer must be the same value as gold (e.g. the same 2-letter state
    code, month, or record id) -- case, punctuation, and surrounding wording are irrelevant, but the
    VALUE itself must be the one the reply commits to, not merely mentioned in passing.
  - `fuzzy_match`: the stated answer must convey the same substantive meaning as gold, allowing
    paraphrase and formatting differences.

Score exactly **0.0** if:
  - the reply's answer is wrong, missing, or contradicts gold, OR
  - the reply hedges between multiple candidate answers without committing to one (e.g. "either OR or
    WA", "March, or possibly April"), OR
  - the reply lists many candidate values without stating which one is the answer (a value dump).

A reply that states the correct answer and ALSO cites supporting records, compares against runner-up
values, or reasons at length beforehand is still correct -- judge only the final stated answer, never
the reply's style, length, or whether it included citations.

Evaluate step by step: (1) identify what single value, if any, the reply commits to as its answer;
(2) compare that value to the gold answer under the stated reward_metric; (3) give a final score of
exactly 0.0 or 1.0 and a one-paragraph explanation.
"""

_GROUNDING_TEMPLATE = """\
You are grading how well an AI agent's cited supporting records back up its answer to a CRM analytics
question.

The ground truth below gives the real answer and a best-effort (approximate, not exhaustive -- see
seed_data/PROVENANCE.md) set of supporting Case record ids that justify it, `evidence_case_ids`.

## Ground truth
{{ expected_json }}

## Conversation to judge (the question asked, and the agent's actual reply)
{{ transcript }}

## Scoring (0.0-1.0, an F1-style overlap)

Read the reply for every Case record id it cites as supporting evidence for its answer -- structured
or plain text, labeled or not, wherever it appears. Score the overlap between that cited set and the
gold `evidence_case_ids` set:
  - **1.0**: the cited set matches the gold set closely -- covers the gold ids (recall) without being
    padded with a large number of unrelated or irrelevant ids (precision).
  - **~0.5**: partial overlap -- cites some but not all of the gold ids, or the right ids mixed with
    several wrong ones.
  - **0.0**: cites none of the gold ids, or cites no records at all.

If the answer itself is a record id, do not count the reply stating its own answer as a citation of
supporting evidence for itself -- judge only the CITED EVIDENCE set, separate from the answer.

Evaluate step by step: (1) list every record id the reply cites as evidence; (2) compare that set
against the gold evidence set; (3) give a final overlap score between 0.0 and 1.0 and a one-paragraph
explanation.
"""

answer_correctness = make_transcript_judge(_ANSWER_CORRECTNESS_TEMPLATE)
_grounding_judge = make_transcript_judge(_GROUNDING_TEMPLATE)


def grounding(*, data_point_id: str, conversation: list[ConversationTurn],
              expected_output: ArtifactAgentOutput | None = None,
              eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """LLM-judged overlap of cited record ids vs. the gold supporting Case ids. Not applicable
    (-> None, excluded from aggregation) when gold has no derivable evidence."""
    gold = _expected(expected_output)
    if not gold.get("evidence_case_ids"):
        return None, {"reason": "no derivable gold evidence for this sample"}
    return _grounding_judge(data_point_id=data_point_id, conversation=conversation,
                            expected_output=expected_output, eval_rubric=eval_rubric, **kwargs)
