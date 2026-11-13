"""compliance_review eval functions.

TEMPORARY placeholder pending a contributor's PR #34 (a contributor/oracle-data-tooling) merging. Once his
ground-truth/compliance_v1/eval.py publishes here (owner-gated, per his spec_snippet.yaml), this
file should be REPLACED wholesale -- same module path (tasks/compliance_review/eval.py), same
objective names/weights (already wired in spec.yaml), so no further spec changes are needed then.

Until that merge, this reimplements the same scoring logic independently (does not import his
unmerged ground-truth/ code) so the build-time e2e pipeline can exercise a real eval phase instead
of failing on a missing eval_data/ dir. Gold for tasks/compliance_review/eval_data/ comes from
scripts/build_temp_eval_data.py -- contracts disjoint from BOTH a contributor's frozen eval holdout
(seed_data/eval_holdout_contracts.txt) AND our own build-time corpus (seed_data/fixture/), i.e.
genuinely unseen by the agent during build. See seed_data/PROVENANCE.md.

Each objective is a keyword-only eval function
    fn(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs)
        -> (float | None, dict)
mirroring tasks/order_to_cash/eval.py.
"""
from __future__ import annotations

import json
import re
from typing import Any

from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn, _expected
from appsci_bench.eval.helpers import make_transcript_judge

_WORD = re.compile(r"\w+")


def _tokens(s: str) -> set:
    return set(_WORD.findall(s.lower()))


def _span_jaccard(pred: str, golds: list[str]) -> float:
    """Max token-Jaccard of a predicted span against any of the gold spans."""
    pt = _tokens(pred)
    best = 0.0
    for g in golds:
        gt = _tokens(g)
        union = pt | gt
        if union:
            best = max(best, len(pt & gt) / len(union))
    return best


def _set_prf1(pred_keys: set, gold_keys: set) -> dict:
    """Precision/recall/F1 over two sets of hashable keys (e.g. (category, rule))."""
    tp, fp, fn = pred_keys & gold_keys, pred_keys - gold_keys, gold_keys - pred_keys
    prec = len(tp) / (len(tp) + len(fp)) if (tp or fp) else 1.0
    rec = len(tp) / (len(tp) + len(fn)) if (tp or fn) else 1.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
            "tp": len(tp), "fp": len(fp), "fn": len(fn)}


def _last_response_json(conversation: list[ConversationTurn]) -> dict:
    """Best-effort parse of the artifact's most recent response into a dict.

    Checks `.json` first (the convention tasks/order_to_cash/eval.py assumes, and what
    ArtifactAgentOutput(json=...) sets), then falls back to `.text` -- a real deployed
    compliance_review artifact was observed returning its structured payload as a JSON-encoded
    string in `.text` with `.json` left null. Reading `.json` only silently scored every sample
    as an empty {} (vacuous precision=1.0, recall=0.0), which looked like a correctness bug in the
    agent's build but was actually this parser only checking one of the two fields a solution might
    reasonably populate.
    """
    if not conversation:
        return {}
    resp = conversation[-1].response
    for raw in (resp.json, resp.text):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


#: The verdict field every sample's oracle carries and `verdict_accuracy` reads. A payload without it
#: is not a compliance report, whatever else it contains.
VERDICT_KEY = "compliant"

NO_PAYLOAD = f"artifact returned no JSON object with a boolean {VERDICT_KEY!r} verdict (contract not met)"


def _answered(output: dict) -> bool:
    """Did the artifact actually return a compliance report?

    `_last_response_json` yields `{}` for a reply it could not parse, and `{}` silently reads as
    "compliant, no violations": `bool({}.get("compliant"))` is False and `{}.get("violations", [])` is
    empty. Against the wrong oracle both MATCH -- an artifact that returned nothing scored
    verdict_accuracy 1.0 on every non-compliant contract, and violation_set_f1 1.0 on every compliant
    one (empty predicted set vs empty gold set is a perfect F1). So a non-answer outscored many real
    attempts. The metrics below check this first and fail loudly instead.

    The verdict must be a real BOOLEAN, not merely present. Key-presence alone left two ways for a
    non-answer to be scored as a real one, both measured against this task's own eval set:

      * `{"compliant": null, ...}` passed, and `bool(None)` is False, so it read as "not compliant".
        That took full marks on one of the two deterministic objectives for EVERY case -- 1.00
        violation_set_f1 on the clean contracts (empty vs empty is a perfect F1, the exact trap the
        paragraph above describes) and 1.00 verdict_accuracy on the rest.
      * `{"compliant": "false", ...}` passed, and every non-empty string is truthy in Python, so the
        string "false" read as True -- the artifact scored as the OPPOSITE of what it said, with no
        error anywhere. A model emitting a stringified boolean is an ordinary bug, not an attack.

    `bool` is checked before `int` on purpose: `isinstance(True, int)` is True in Python, so testing
    for int first would silently accept 0/1 as a verdict.
    """
    return isinstance(output.get(VERDICT_KEY), bool)




def _pred_spans(v: dict) -> list[str]:
    if v.get("evidence_spans"):
        return v["evidence_spans"]
    return [v["evidence"]] if v.get("evidence") else []


def _norm_key(category: str, rule: str) -> tuple:
    """Case-insensitive (category, rule) key. Policy docs/personas use CUAD-exact category casing
    (e.g. "Ip Ownership Assignment") to match gold, but an artifact quoting a clause name with a
    different but equivalent casing (e.g. "IP Ownership Assignment") shouldn't be scored as a miss --
    the categories are conceptually identical."""
    return (category.strip().lower(), rule.strip().lower())


def _score(output: dict, oracle: dict, span_threshold: float = 0.5) -> dict:
    gold_v = oracle.get("violations", [])
    pred_v = output.get("violations", [])

    gold_keys = {_norm_key(v["category"], v["rule"]) for v in gold_v}
    pred_keys = {_norm_key(v["category"], v["rule"]) for v in pred_v}
    prf = _set_prf1(pred_keys, gold_keys)

    gold_by_key = {_norm_key(v["category"], v["rule"]): v for v in gold_v}
    pred_by_key = {_norm_key(v["category"], v["rule"]): v for v in pred_v}
    ev_scores, ev_hits = [], 0
    for k in pred_keys & gold_keys:
        gv = gold_by_key[k]
        if gv.get("present") and gv.get("evidence_spans"):
            ov = max((_span_jaccard(ps, gv["evidence_spans"]) for ps in _pred_spans(pred_by_key[k])),
                     default=0.0)
            ev_scores.append(ov)
            ev_hits += ov >= span_threshold

    return {
        "violation_precision": prf["precision"], "violation_recall": prf["recall"],
        "violation_f1": prf["f1"],
        "evidence_overlap_mean": round(sum(ev_scores) / len(ev_scores), 4) if ev_scores else None,
        "evidence_hit_rate": round(ev_hits / len(ev_scores), 4) if ev_scores else None,
        # `_answered` has already guaranteed the artifact's verdict is a real bool, so this compares
        # bool to bool. Deliberately NOT `bool(output.get(...))`: coercing here is what let a
        # stringified verdict ("false") read as its own opposite. If a non-bool ever reaches this line
        # it now simply fails to match rather than silently inverting.
        "verdict_correct": output.get(VERDICT_KEY) == bool(oracle.get(VERDICT_KEY)),
        "tp": prf["tp"], "fp": prf["fp"], "fn": prf["fn"],
    }


def violation_set_f1(*, data_point_id: str, conversation: list[ConversationTurn],
                     expected_output: ArtifactAgentOutput | None = None,
                     eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """Violation-set F1 over (category, rule) keys. Deterministic."""
    del data_point_id, eval_rubric, kwargs
    output = _last_response_json(conversation)
    if not _answered(output):
        return 0.0, {"error": NO_PAYLOAD}
    sc = _score(output, (_expected(expected_output) or {}))
    return sc["violation_f1"], {"precision": sc["violation_precision"], "recall": sc["violation_recall"],
                               "tp": sc["tp"], "fp": sc["fp"], "fn": sc["fn"]}


def evidence_span_overlap(*, data_point_id: str, conversation: list[ConversationTurn],
                          expected_output: ArtifactAgentOutput | None = None,
                          eval_rubric: str | None = None, **kwargs: Any) -> tuple[float | None, dict]:
    """Mean token-Jaccard of cited spans vs gold, over matched present-clause violations.
    Not applicable (-> None, excluded from aggregation) when the gold has no present-clause spans.
    Deterministic."""
    del data_point_id, eval_rubric, kwargs
    output = _last_response_json(conversation)
    if not _answered(output):
        # None, not 0.0, and NOT the silent-exclusion trap: this metric is a mean over violations the
        # artifact and the gold BOTH name, so a reply naming none has nothing to measure -- exactly as
        # for a real answer that found no violations. Nothing is hidden: violation_set_f1 and
        # verdict_accuracy both score this reply 0 with the same reason.
        return None, {"error": NO_PAYLOAD}
    sc = _score(output, (_expected(expected_output) or {}))
    return sc["evidence_overlap_mean"], {"hit_rate": sc["evidence_hit_rate"]}


def verdict_accuracy(*, data_point_id: str, conversation: list[ConversationTurn],
                     expected_output: ArtifactAgentOutput | None = None,
                     eval_rubric: str | None = None, **kwargs: Any) -> tuple[float, dict]:
    """1.0 if the artifact's overall `compliant` verdict matches the gold, else 0.0. Deterministic."""
    del data_point_id, eval_rubric, kwargs
    output = _last_response_json(conversation)
    if not _answered(output):
        return 0.0, {"verdict_correct": False, "error": NO_PAYLOAD}
    sc = _score(output, (_expected(expected_output) or {}))
    return (1.0 if sc["verdict_correct"] else 0.0), {"verdict_correct": sc["verdict_correct"]}


_RATIONALE_QUALITY_TEMPLATE = """\
You are an expert contracts reviewer evaluating the quality of a compliance report's rationales.
When the system reports a violation, its rationale should correctly identify the broken rule and
ground it in the contract/policy (naming the offending clause when the clause is present).

## Scoring Criteria (0-1 Scale)

**0.9-1.0 (Excellent)**: Each reported violation's rationale names the correct rule and cites the
specific offending clause / missing-clause requirement. A reviewer could act on it immediately.

**0.6-0.8 (Good)**: Rationales identify the right rule but are light on grounding (no specific clause
reference).

**0.3-0.5 (Fair)**: Rationales are vague or only partially correct.

**0.1-0.2 (Poor)**: Rationales are present but wrong or misleading (wrong rule, ungrounded).

**0.0 (Critical)**: Rationales are missing or nonsensical.

## Special Cases
- If the system correctly reports a fully compliant contract (no violations), score 1.0.
- If the system wrongly reports violations on a compliant contract, score by whether the false
rationale is plausible (0.3) or nonsensical (0.0).

## Ground truth (oracle label for this contract+policy)
{{ expected_json }}

## Conversation to judge (the review task given, and the system's actual response)
{{ transcript }}

Evaluate step by step: (1) does the reported violation set match the oracle; (2) for each reported
violation, is the rationale correct and grounded in the contract/policy; (3) give a final score and
explanation.
"""

rationale_quality = make_transcript_judge(_RATIONALE_QUALITY_TEMPLATE)
