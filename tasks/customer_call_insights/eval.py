"""Customer-call-insights eval functions.

Four objectives on the per-data-point, keyword-only eval-function contract (design: Task-Specific
Evaluation Function): set F1 on each of the three list-valued labels, and an exact-match check on
sentiment.

HOW THE LIST LABELS ARE MATCHED, and why it is a hybrid. The three list labels hold short free-text
phrases where equivalence is genuinely semantic: "get billing to reissue the invoice" and "reissue the
May invoice with the correct seat count" are the same commitment, and no lexical ratio recognizes that.
A token-set Jaccard ratio was tried first and rejected on measurement -- it scores by phrase LENGTH as
much as by meaning, rating a correct-but-more-specific answer ("loop in legal on the MSA redlines" vs
gold "loop in legal") at 0.43, below any threshold that also excludes unrelated pairs. So a model does
the ITEM MATCHING and returns counts; the F1 arithmetic stays in code here.

That split is deliberate, and it follows airline_rebooking's structural habit rather than its
mechanism. Airline's `task_success` keeps two independently-verifiable components (a DB state hash, a
substring containment check) as arithmetic and refuses to fake a component it could not measure. Its
mechanisms do not transfer -- a state hash needs a replayable environment, and containment needs gold
items that are short literals like a dollar amount -- but the discipline does: measure mechanically
whatever can be, and keep the scoring formula out of the model's hands. So the empty cases, which are
decidable without any judgment at all, short-circuit BEFORE the model is called:

  - both lists empty            -> 1.0, no model call
  - gold empty, artifact non-empty -> 0.0, no model call (fabrication; the failure this task exists to
                                      detect stays arithmetic, not a prompt)
  - gold non-empty, artifact empty -> 0.0, no model call (recall 0)

Only a non-empty-vs-non-empty comparison reaches the matcher. `order_to_cash.eval.reason_quality` is
the repo's precedent for sending free text to a model at all; this narrows what the model is asked for
from "score this label" to "which of these items correspond", which is the part it is actually better
at than code.

TOKEN GUARD. The predicted list is artifact-controlled and unbounded -- an artifact that returns 500
items builds an ~11k-token matcher prompt, and a degenerate one could go further. `_MATCH_BUDGET_CHARS`
caps what is sent, following `helpers/files.py`'s "guard, not limit" posture: it never trims a payload
that fits, and when it does trim it records the fact in `metadata["degraded"]` rather than quietly
scoring less than it claims. Dropped items are still counted in the F1 DENOMINATOR, so flooding the
list lowers precision instead of hiding items from the scorer -- the guard bounds cost, it does not
grant an artifact a cheaper score. The oracle side is authored by us and bounded by construction, so it
is trimmed only as a backstop.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn
from appsci_bench.eval.helpers import llm_judge_call, resolve_scalar

#: The closed set of sentiment values the artifact may declare. This is the authority its `sentiment`
#: is validated against, and it is the same three values the customer states during BUILD (via the
#: user-sim's shared output-contract document) -- so a value outside this set is the artifact ignoring a
#: requirement it was given, not an unforeseen answer.
#:
#: `AT_RISK` is deliberately NOT a member. Risk is its own label; an artifact that returns `AT_RISK` has
#: collapsed two independent signals (how the customer sounded, and what could go wrong) into one field.
#: The boundary is stated in TASK_SOP.md and in the output contract, so this is an elicitable
#: requirement rather than a trap.
SENTIMENTS = frozenset({"POSITIVE", "NEUTRAL", "NEGATIVE"})

#: The three list-valued labels and what each means, named once so the metrics, the matcher prompt and
#: the dataset cannot drift apart. The meaning is handed to the matcher so it judges correspondence
#: against the same definitions the oracle was authored from. Kept short: the matcher decides which items
#: correspond, not whether an item belongs in this label at all -- that is the oracle's business, and an
#: item the oracle does not carry is scored by precision, not by asking a model about it.
LABEL_MEANINGS = {
    "next_steps": "actions someone intends to take after the call, either side's",
    "risks": "signals from the call that this account or deal could go badly",
    "commitments": "promises OUR side made that the customer is now entitled to expect",
}

#: The contract failures the metrics report, kept DISTINCT because they are different mistakes and a
#: reader of the report needs to tell them apart. The response contract -- a JSON object carrying the
#: four labels -- is stated to the agent during BUILD, and the transport fills the structured slot only
#: when the whole reply IS that object (`appsci_bench.eval.datatypes.extract_json_object`).
NO_PAYLOAD = "artifact returned no JSON object (response contract not met)"

#: Reported when the oracle itself can't answer -- a dataset-authoring gap, not the artifact's fault, so
#: the sample is excluded rather than scored against a default.
NO_ORACLE = "sample's expected_output has no JSON object (dataset gap, not scoreable)"


def _contract_payload(conversation: list[ConversationTurn]) -> tuple[dict | None, str | None]:
    """The artifact's last structured response, or `(None, why-it-cannot-be-scored)`.

    Returns **None, not `{}`**, on every failure path. `{}` is the trap: it reads downstream as "every
    label absent", and against an all-empty oracle -- which is the COMMON case for this task, since most
    calls have nothing to report for at least one label -- that MATCHES. An artifact that returned
    nothing, or narrated prose instead of a payload, would score full marks on exactly the samples
    where scoring matters most. Keep "said nothing" and "said empty" distinguishable.
    """
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
    return parsed, None


def _oracle(expected_output: ArtifactAgentOutput | None) -> tuple[dict | None, str | None]:
    """The sample's oracle, or `(None, why)`. The mirror guard on OUR side of the comparison: an absent
    or malformed `expected_output` must not collapse to `{}` either, or a dataset gap reads as "this
    call had nothing to report" and becomes a scoreable expectation the artifact can match by accident.
    """
    if expected_output is None or not expected_output.json:
        return None, NO_ORACLE
    try:
        parsed = json.loads(expected_output.json)
    except (TypeError, ValueError):
        return None, NO_ORACLE
    if not isinstance(parsed, dict):
        return None, NO_ORACLE
    return parsed, None


def _as_items(value: Any) -> list[str] | None:
    """Coerce a label's value to a list of non-blank strings, or None if it isn't list-shaped.

    None is distinct from `[]`: `[]` is the artifact correctly reporting nothing, None is the artifact
    answering in the wrong shape (a bare string, a number, `null`, an omitted key). The output contract
    states that an empty label is spelled `[]` and never omitted, so the two must not be conflated --
    one is a correct answer and the other is a contract failure.

    NULL AND NON-STRING ELEMENTS ARE DROPPED, not stringified. `str(None)` is `"None"`, which is
    non-blank, so a naive strip-and-keep turns a `null` inside the list into an item literally named
    "None" -- an item the artifact never claimed, invented by the scorer, counted against precision or
    (worse) offered to the matcher as something to align. Same for `{}`/`[]`, which stringify to junk
    tokens. Dropping them keeps this function's promise: what comes out is what the artifact actually
    said, and `["  ", null]` reduces to `[]` -- an empty label, which is a legitimate answer.
    """
    if not isinstance(value, (list, tuple)):
        return None
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


class _MatchVerdict(BaseModel):
    """What the matcher returns: which gold items were found, and which returned items were invented.

    `extra="forbid"` (inherited habit from `JudgeVerdict`) so a hallucinated field is a loud
    ValidationError rather than a silently dropped one. `score` is required by `llm_judge_call`'s
    contract but is NOT what this metric reports -- the F1 is computed from the index lists below, in
    code. The model is asked to match items, not to grade the label; a model-supplied score would put
    the scoring formula back in the model's hands, which is the thing this design avoids.
    """
    model_config = {"extra": "forbid"}
    reasoning: str
    matched_expected_indices: list[int] = Field(
        description="0-based indices into the EXPECTED list that a returned item corresponds to")
    matched_predicted_indices: list[int] = Field(
        description="0-based indices into the RETURNED list, positionally paired with "
                    "matched_expected_indices (same length, same order)")
    score: float = Field(default=0.0, ge=0.0, le=1.0,
                         description="ignored -- the F1 is computed from the index lists")


_MATCHER_SYSTEM = """\
You align two lists of short phrases extracted from the same customer call. You do not grade anything \
and you do not score anything -- you only decide which phrases refer to the same underlying item.
"""

_MATCHER_TEMPLATE = """\
Two lists of "{label}" were extracted from one customer call: the reference reading (EXPECTED) and a
system's reading (RETURNED). For this label, "{label}" means: {label_meaning}.

Decide which RETURNED items refer to the same underlying item as which EXPECTED items.

Rules:
- Two phrases match when they refer to the same real-world item, however differently worded. "get
  billing to reissue the invoice" and "reissue the May invoice with the correct seat count" match.
- A RETURNED item that is MORE SPECIFIC than its EXPECTED counterpart still matches, as long as it is
  the same item: "loop in legal on the MSA redlines" matches "loop in legal".
- A RETURNED item that is more specific in a way the call did not support is still a match here -- that
  is a grounding problem, judged separately. Only correspondence matters to you.
- **One-to-one.** Each EXPECTED item matches at most one RETURNED item and vice versa. If two RETURNED
  items both plausibly cover one EXPECTED item, pick the closer one and leave the other unmatched.
- Items about genuinely different things do not match, even when they share wording or topic. "September
  renewal is conditional on the regions being live" and "confirm internally that the renewal depends on
  the regions" are the same underlying fact; "schedule a QBR" and "reissue the invoice" are not.

EXPECTED ({n_expected} items):
{expected_block}

RETURNED ({n_predicted} items):
{predicted_block}

Return the matched pairs as two positionally-paired index lists. `matched_expected_indices[i]` and
`matched_predicted_indices[i]` are the two halves of the same pair, so both lists MUST have the same
length. Use 0-based indices. Every index must be in range, and no index may repeat within its own list.
An empty pair of lists is a valid answer when nothing corresponds.
"""


#: The character budget for ONE side's rendered item list, at `helpers/files.py`'s conservative estimate
#: of ~4 chars per token: ~4k tokens per side, so a matcher call stays near ~8k tokens of items even
#: against a degenerate artifact -- well inside any judge model's window, and bounded regardless of what
#: the artifact returns. Deliberately far above any plausible real call: a call yielding more than a
#: couple of dozen items per label is already outside what this task describes, so the guard should never
#: fire on honest output.
_MATCH_BUDGET_CHARS = 16_000

#: Hard ceiling on items per side, independent of characters. A very long list of very SHORT items
#: ("ok", "ok", ...) fits the character budget while still asking the matcher to reason over a
#: combinatorial pairing problem it will do badly at; capping count bounds that too.
_MATCH_MAX_ITEMS = 60


def _within_budget(items: list[str]) -> tuple[list[str], int]:
    """Trim `items` to the token/count guard. Returns `(kept, n_dropped)`.

    "Guard, not limit", the posture `helpers/files.py` sets: a list that fits is returned untouched and
    `n_dropped` is 0, so nothing is trimmed on the honest path. Trimming keeps the list PREFIX, which is
    the artifact's own ordering -- it puts its best items first, so a trimmed comparison reads its
    strongest answer rather than an arbitrary slice.

    The caller must keep scoring precision against the FULL pre-trim count (`match_set_f1` does), or the
    guard would turn "returned 500 items" into a cheaper score than "returned 60", which is backwards:
    flooding the list must cost precision, not buy leniency.
    """
    kept: list[str] = []
    budget = _MATCH_BUDGET_CHARS
    for item in items[:_MATCH_MAX_ITEMS]:
        cost = len(item) + 8  # + the "  [nn] " index prefix and newline the prompt renders per item
        if budget - cost < 0:
            break
        budget -= cost
        kept.append(item)
    return kept, len(items) - len(kept)


def _f1_from_counts(n_matched: int, n_predicted: int, n_expected: int) -> tuple[float, float, float]:
    """`(f1, precision, recall)` from three counts. The scoring formula, kept in code."""
    precision = n_matched / n_predicted if n_predicted else 0.0
    recall = n_matched / n_expected if n_expected else 0.0
    f1 = 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)
    return f1, precision, recall


def _validate_pairs(verdict: _MatchVerdict, n_predicted: int, n_expected: int) -> list[tuple[int, int]]:
    """The matcher's index pairs, dropping anything malformed. Returns `[(expected_i, predicted_i)]`.

    Defensive on purpose: the model can return unequal-length lists, an out-of-range index, or the same
    index twice (double-counting one item into two matches, which would inflate F1 above what the items
    support). Rather than trust the shape, keep only pairs that are in range and not already claimed on
    either side -- so a sloppy verdict degrades to a lower score, never a fabricated higher one.
    """
    exp_idx = verdict.matched_expected_indices
    pred_idx = verdict.matched_predicted_indices
    pairs: list[tuple[int, int]] = []
    seen_exp: set[int] = set()
    seen_pred: set[int] = set()
    for e, p in zip(exp_idx, pred_idx):  # zip truncates to the shorter list -- unequal lengths are safe
        if not (0 <= e < n_expected and 0 <= p < n_predicted):
            continue
        if e in seen_exp or p in seen_pred:
            continue
        seen_exp.add(e)
        seen_pred.add(p)
        pairs.append((e, p))
    return pairs


def match_set_f1(
    predicted: list[str], expected: list[str], *, label: str = "items", config: Any = None,
) -> tuple[float, dict]:
    """F1 over two lists of free-text items, matched semantically by a model, scored in code.

    The three empty cases are decided here without any model call -- they need no judgment, and keeping
    them mechanical is what preserves the fabrication signal as arithmetic:
      - **both empty -> 1.0.** A call with nothing to report, correctly reported as nothing. The common
        case for this task, and a right answer rather than a vacuous one.
      - **gold empty, prediction non-empty -> 0.0.** Fabrication: items invented for a call that had
        none. The failure mode this task exists to detect.
      - **gold non-empty, prediction empty -> 0.0.** Recall 0.

    Otherwise one matcher call returns the corresponding pairs, and `F1 = 2PR / (P + R)` follows from the
    counts. `metadata` records the matched pairs, what was missed, and what was extra, so a partial score
    is legible in the report without re-running anything.

    An over-long list is TRIMMED to the token guard before the call, never rejected: precision still
    divides by the full pre-trim count, so items the guard dropped are counted as unmatched extras and
    flooding the list costs precision. `metadata["degraded"]` records the trim whenever it happens.
    """
    if not expected and not predicted:
        return 1.0, {"matched": [], "missed": [], "extra": [], "precision": 1.0, "recall": 1.0,
                     "both_empty": True, "judge_called": False}
    if not expected:
        return 0.0, {"matched": [], "missed": [], "extra": list(predicted),
                     "precision": 0.0, "recall": 0.0, "judge_called": False,
                     "note": "oracle has no items for this label; every returned item is fabricated"}
    if not predicted:
        return 0.0, {"matched": [], "missed": list(expected), "extra": [],
                     "precision": 0.0, "recall": 0.0, "judge_called": False,
                     "note": "artifact returned no items for this label"}

    # The guard. `n_predicted`/`n_expected` below stay the FULL counts, so trimmed-away items remain in
    # the F1 denominators -- the guard bounds what the model reads, not what the artifact is scored on.
    sent_predicted, dropped_predicted = _within_budget(predicted)
    sent_expected, dropped_expected = _within_budget(expected)
    degraded: str | None = None
    if dropped_predicted or dropped_expected:
        parts = []
        if dropped_predicted:
            parts.append(f"{dropped_predicted} of {len(predicted)} returned items")
        if dropped_expected:
            parts.append(f"{dropped_expected} of {len(expected)} expected items")
        degraded = (f"matcher prompt exceeded the size guard; {' and '.join(parts)} were not sent to "
                    "the matcher and are scored as unmatched")

    prompt = _MATCHER_TEMPLATE.format(
        label=label,
        label_meaning=LABEL_MEANINGS.get(label, label),
        n_expected=len(sent_expected),
        n_predicted=len(sent_predicted),
        expected_block="\n".join(f"  [{i}] {t}" for i, t in enumerate(sent_expected)),
        predicted_block="\n".join(f"  [{i}] {t}" for i, t in enumerate(sent_predicted)),
    )
    verdict = llm_judge_call(system_prompt=_MATCHER_SYSTEM, user_prompt=prompt,
                             output_schema=_MatchVerdict, config=config, caller="matcher")
    pairs = _validate_pairs(verdict, len(sent_predicted), len(sent_expected))

    f1, precision, recall = _f1_from_counts(len(pairs), len(predicted), len(expected))
    matched_exp = {e for e, _ in pairs}
    matched_pred = {p for _, p in pairs}
    metadata = {
        "matched": [{"expected": sent_expected[e], "predicted": sent_predicted[p]} for e, p in pairs],
        "missed": [t for i, t in enumerate(expected) if i not in matched_exp],
        "extra": [t for i, t in enumerate(predicted) if i not in matched_pred],
        "precision": round(precision, 3), "recall": round(recall, 3),
        "judge_called": True, "matcher_reasoning": verdict.reasoning,
    }
    if degraded:
        metadata["degraded"] = degraded
    return f1, metadata



def _score_list_field(
    field: str, conversation: list[ConversationTurn], expected_output: ArtifactAgentOutput | None,
) -> tuple[float | None, dict]:
    """Shared body of the three list-label metrics: guard both sides, then set-F1 one field."""
    oracle, oracle_why = _oracle(expected_output)
    if oracle is None:
        return None, {"scoreable": False, "error": oracle_why}
    gold = _as_items(oracle.get(field))
    if gold is None:
        return None, {"scoreable": False,
                      "error": f"oracle's {field!r} is not a list (dataset gap, not scoreable)"}

    payload, why = _contract_payload(conversation)
    if payload is None:
        return 0.0, {"expected": gold, "error": why}

    got = _as_items(payload.get(field))
    if got is None:
        raw = payload.get(field)
        return 0.0, {"expected": gold, "predicted": None,
                     "error": (f"artifact's {field!r} is {raw!r}, not a list -- an empty label must be "
                               "returned as [] (response contract not met)")}

    return match_set_f1(got, gold, label=field)


def next_steps_f1(*, data_point_id: str, conversation: list[ConversationTurn],
                  expected_output: ArtifactAgentOutput | None = None,
                  eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Set F1 on `next_steps` -- what needs doing after the call, either side's items.

    A reply with no structured payload scores 0 with `NO_PAYLOAD` recorded, never a silent empty list:
    on this dataset an absent label read as "nothing to report" would MATCH the oracle on every call
    that genuinely had no next steps, handing a non-compliant artifact free marks.
    """
    del data_point_id, eval_rubric, kwargs
    return _score_list_field("next_steps", conversation, expected_output)


def risks_f1(*, data_point_id: str, conversation: list[ConversationTurn],
             expected_output: ArtifactAgentOutput | None = None,
             eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Set F1 on `risks` -- signals the account or deal could go badly.

    The label most exposed to fabrication: a risk inferred from tone alone, with nothing on the call to
    support it, is the classic failure here. A call whose oracle has no risks scores 0 for any risk the
    artifact returns.
    """
    del data_point_id, eval_rubric, kwargs
    return _score_list_field("risks", conversation, expected_output)


def commitments_f1(*, data_point_id: str, conversation: list[ConversationTurn],
                   expected_output: ArtifactAgentOutput | None = None,
                   eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Set F1 on `commitments` -- promises OUR side made that the customer may now expect.

    Scored separately from `next_steps` even though the two overlap by design (a promise we made is
    both). Keeping them apart is what measures whether the artifact learned the accountability
    distinction -- who is on the hook -- rather than dumping every follow-up into both lists.
    """
    del data_point_id, eval_rubric, kwargs
    return _score_list_field("commitments", conversation, expected_output)


def sentiment_accuracy(*, data_point_id: str, conversation: list[ConversationTurn],
                       expected_output: ArtifactAgentOutput | None = None,
                       eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Exact match on `sentiment` against the closed three-value set, case- and
    whitespace-insensitively (`resolve_scalar`). No LLM, no cost, no judge variance -- the answer is a
    value from a closed set, so this is a mechanical comparison.

    A missing, blank, or non-canonical value scores 0. The three values are stated to the agent during
    BUILD, so an artifact that invents one has failed a requirement it was given; the metric says so
    instead of rescuing it. `AT_RISK` is the specific value worth watching for -- it is a plausible
    guess that the task deliberately excludes, and returning it means the artifact folded the risks
    label into the sentiment field.
    """
    del data_point_id, eval_rubric, kwargs
    oracle, oracle_why = _oracle(expected_output)
    if oracle is None:
        return None, {"scoreable": False, "error": oracle_why}
    expected = oracle.get("sentiment")
    if expected is None:
        return None, {"scoreable": False,
                      "error": "oracle has no 'sentiment' (dataset gap, not scoreable)"}
    expected_norm = str(expected).strip().upper()

    payload, why = _contract_payload(conversation)
    if payload is None:
        return 0.0, {"expected": expected_norm, "error": why}

    raw = payload.get("sentiment")
    predicted = str(raw).strip().upper() if raw is not None and str(raw).strip() else None
    meta = {"predicted": predicted, "expected": expected_norm}

    if predicted is None:
        return 0.0, {**meta, "error": "artifact declared no sentiment"}
    if predicted not in SENTIMENTS:
        return 0.0, {**meta, "error": f"sentiment {predicted!r} is not one of {sorted(SENTIMENTS)}"}

    ok, how = resolve_scalar(predicted, expected_norm)
    return (1.0 if ok else 0.0), {**meta, "how": how}
