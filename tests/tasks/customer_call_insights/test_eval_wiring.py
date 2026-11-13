"""Customer-call-insights EVAL-PIPELINE test: the real spec + the real dataset, end to end.

Everything in `test_eval.py` calls the four eval functions directly with hand-built ConversationTurns.
That is the right shape for testing scoring logic and the wrong shape for catching a WIRING defect --
and this task has already lost a full end-to-end run to one. The evaluator attaches each call as a file
in a top-level `files` array; the artifact read only `messages`, received a 129-character instruction as
if it were the transcript, and returned an empty reading for every call. It scored 0.375 on a completely
healthy environment. Not one of the ~88 unit tests noticed, because none of them ever put a sample on
the wire.

So this file drives the genuine article: `spec.yaml` off disk, `eval_data/` off disk, through
`run_evaluation()`, against in-process fake artifacts. No Docker and no credentials -- the item matcher
is the only model call the four metrics make, so patching it once covers a whole run.

What that buys, none of which a unit test can reach:
  * the transcript really arrives at the artifact, as bytes (the defect above);
  * the four `eval_function_path` strings in spec.yaml actually resolve (the unit tests import the
    functions directly, so a typo there would pass every one of them and fail only in a real run);
  * the response contract is enforced across the whole pipeline, not just inside a metric;
  * all four objectives reach the report -- see `test_the_report_carries_all_four_objectives`.

The baselines below are asserted as EXACT values because each one was measured, not guessed. A range
would let a real regression through, which is the entire point of the file.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import pytest
import yaml

from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, extract_json_object
from appsci_bench.eval.eval_system import run_evaluation

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TASK_DIR = REPO_ROOT / "tasks" / "customer_call_insights"
EVAL_DATA = TASK_DIR / "eval_data"

#: The blind artifact's reply, verbatim from the failed run's `artifact/server.py` SAFE_DEFAULT. Kept
#: exact so the 0.375 below stays traceable to a real observation rather than a plausible-looking stub.
BLIND_REPLY = {"next_steps": [], "risks": [], "commitments": [], "sentiment": "NEUTRAL"}


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load((TASK_DIR / "spec.yaml").read_text())


def _oracle_for(text: str) -> str:
    """The oracle payload for whichever sample this query is asking about.

    Dispatches on the call id in the query text, which is how a *reader* tells the samples apart --
    deliberately NOT on the attachment, so that a gold artifact built on this helper still has to
    receive the attachment for `test_the_transcript_reaches_the_artifact_as_bytes` to mean anything.
    """
    for sample_dir in sorted(EVAL_DATA.iterdir()):
        if not sample_dir.is_dir():
            continue
        sample = json.loads((sample_dir / "sample.json").read_text())
        call_id = sample["single_turn_input"]["text"].split("call ", 1)[1].split(" ", 1)[0]
        if call_id in text:
            return sample["expected_output"]["json"]
    raise AssertionError(f"no sample matches query text {text!r}")


def _matcher_reply(n: int = 60) -> str:
    """A matcher verdict pairing index i with index i, for lists up to `n` long.

    Generous on purpose: the gold artifact returns the oracle verbatim, so a matcher that pairs
    positionally is exactly right, and any real F1 below 1.0 then means the PIPELINE lost something --
    which is what this file is looking for. `_validate_pairs` drops out-of-range pairs, so overshooting
    `n` is harmless.
    """
    return json.dumps({"reasoning": "positionally aligned", "matched_expected_indices": list(range(n)),
                       "matched_predicted_indices": list(range(n)), "score": 0.0})


def _run(spec: dict, respond, tmp_path) -> object:
    """`run_evaluation` over the real spec/dataset with `respond` as the artifact. Matcher patched."""
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_matcher_reply()):
        return run_evaluation(spec, InProcessArtifactClient(respond), workdir=str(tmp_path))


# --- the defect this file exists for --------------------------------------------------------------

def test_the_transcript_reaches_the_artifact_as_bytes(spec: dict, tmp_path) -> None:
    """Every query must carry its call as a materialized file whose bytes match the transcript on disk.

    The one assertion that would have caught the 0.375 run. `File` holds a lazy `file_path` until
    something materializes it; if that ever regresses, `to_dict` puts a host path on the wire instead of
    content and the artifact silently has nothing to read (`datatypes.py` File.to_dict).
    """
    received: list[tuple[str, bytes]] = []

    def recorder(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
        assert q.files, f"query {q.text!r} arrived with no attachment"
        for f in q.files:
            assert f.file_data, f"{f.file_name} arrived unmaterialized -- the artifact cannot read it"
            received.append((f.file_name, f.file_data))
        payload = _oracle_for(q.text or "")
        return ArtifactAgentOutput(text=payload, json=payload)

    _run(spec, recorder, tmp_path)

    on_disk = {p.read_bytes() for p in EVAL_DATA.glob("*/transcript.txt")}
    # Both sides of the comparison below come off disk, so an empty dataset would compare 0 to 0 and
    # pass. Floor it: a partial checkout or a moved directory has to fail here, not score a silent 0.
    assert len(on_disk) >= 50, f"expected the full eval set on disk, found {len(on_disk)} transcripts"
    assert len(received) == len(on_disk), f"expected one attachment per sample, got {len(received)}"
    assert {data for _, data in received} == on_disk, "attachment bytes differ from the transcripts"


# --- the metrics are reachable through the real spec ----------------------------------------------

def test_a_gold_artifact_scores_a_perfect_one(spec: dict, tmp_path) -> None:
    """Answering with each sample's own oracle must score 1.0 across all four PRICED objectives.

    Two independent things at once: no metric has a ceiling below 1.0 on the shipped data, and the
    `eval_function_path` strings in spec.yaml resolve to these functions. The unit tests import the
    functions directly, so a typo in one of those paths passes all of them and fails only in a real run.

    Diagnostic (weight 0.0) safety axes are excluded from the perfect-1.0 check on purpose: this
    fixture's only mocked model reply is `_run`'s matcher-shaped JSON, whose `score` field is a fixed
    dummy `0.0` (matcher verdicts don't carry a meaningful score of their own -- the real F1 is
    computed in code from the index lists). `JudgeVerdict` parses that same reply by keeping only its
    own declared fields, so `eval_safety.py`'s functions deterministically get `score=0.0` under this
    shared mock -- not a real safety judgment, just what this particular mock happens to produce, and
    not the same as the `None` fail-safe path (which only fires when parsing raises outright).
    """
    def gold(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
        payload = _oracle_for(q.text or "")
        return ArtifactAgentOutput(text=payload, json=payload)

    report = _run(spec, gold, tmp_path)

    assert report.final_score == pytest.approx(1.0)
    priced = [m for m in report.views["per_metric"] if m["weight"] > 0]
    assert priced, "expected at least one priced objective in the report"
    assert all(m["score"] == pytest.approx(1.0) for m in priced)
    diagnostic = {m["objective"]: m for m in report.views["per_metric"] if m["weight"] == 0}
    assert set(diagnostic) == {"safety_no_fabrication", "safety_safe_by_construction"}
    assert all(m["score"] == pytest.approx(0.0) for m in diagnostic.values()), (
        "safety_* objectives are expected to parse the shared matcher-shaped mock and surface its "
        "dummy score=0.0 here -- a different value would mean the judge call or its parsing changed"
    )


def test_the_report_carries_all_four_objectives(spec: dict, tmp_path) -> None:
    """All four must be present AND actually scored -- a dropped one silently rescales the final score.

    `aggregate.py` EXCLUDES a `None`-scored objective rather than scoring it 0, and never pre-normalizes
    weights: the survivors re-normalize among themselves. So an objective that drops out (these metrics
    return `None` on a malformed oracle) changes the divisor, and the final score is no longer the one
    spec.yaml declares -- with only a log warning to say so. Measured, lists at 0.5 and sentiment at 1.0:
    0.625 with all four, 0.667 once `risks_f1` vanishes. The direction depends on whether the lost metric
    was above or below the mean, which is exactly why it cannot be spotted by eyeballing the headline.

    Both halves of the assertion are load-bearing: a dropped metric still appears in `per_metric` (with
    `score: None`), so checking the roster alone would not catch it.

    Scoped to the four PRICED (weight 0.25) objectives -- diagnostic (weight 0.0) safety axes are a
    separate contract with their own coverage in `test_a_gold_artifact_scores_a_perfect_one` above,
    and are correctly allowed a `None` score here (unlike these four), so folding them into this
    exact-set/all-not-None check would wrongly require them to always resolve too.
    """
    def gold(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
        payload = _oracle_for(q.text or "")
        return ArtifactAgentOutput(text=payload, json=payload)

    report = _run(spec, gold, tmp_path)

    priced_names = {"next_steps_f1", "risks_f1", "commitments_f1", "sentiment_accuracy"}
    scored = {m["objective"]: m for m in report.views["per_metric"] if m["objective"] in priced_names}
    assert set(scored) == priced_names
    assert all(m["weight"] == pytest.approx(0.25) for m in scored.values())
    assert all(m["score"] is not None for m in scored.values()), (
        "an objective scored None on every sample and was dropped from aggregation -- the remaining "
        "weights silently re-normalized, so the final score is not the one spec.yaml declares"
    )


# --- known baselines, pinned to exact measured values ---------------------------------------------

def test_the_blind_artifact_still_scores_exactly_the_run_that_exposed_it(spec: dict, tmp_path) -> None:
    """An artifact that never reads the attachment scores 0.455 -- the floor every real score sits above.

    Each list objective's score here is just that label's empty share, because under set-F1 empty-vs-empty
    is 1.0 and empty-vs-anything is 0.0: next steps 0.28, risks 0.64, commitments 0.42. Sentiment is 0.48,
    the majority class's share, since this artifact always answers the same value. So the number is a
    DATASET composition property, not a metric defect -- it moves whenever the label mix moves.

    Pinned so that when the dataset changes the number moves visibly and someone has to look at it.
    """
    reply = json.dumps(BLIND_REPLY)
    report = _run(spec, lambda q: ArtifactAgentOutput(text=reply, json=reply), tmp_path)

    assert report.final_score == pytest.approx(0.455)
    scored = {m["objective"]: m["score"] for m in report.views["per_metric"]}
    assert scored["sentiment_accuracy"] == pytest.approx(0.48)
    assert scored["next_steps_f1"] == pytest.approx(0.28)
    assert scored["risks_f1"] == pytest.approx(0.64)
    assert scored["commitments_f1"] == pytest.approx(0.42)


def test_padding_scores_worse_than_returning_nothing(spec: dict, tmp_path) -> None:
    """The load-bearing property of the symmetric design, measured rather than asserted in prose.

    An artifact that invents plausible items everywhere scores 0.436 -- below the 0.455 of one that returns
    nothing. Fabrication is punished by F1's precision half without any separate grounding objective, which
    is why this task has none. If a future change ever makes padding the cheaper strategy, this fails.

    Read the margin, not just the pass: it is 0.019, and it rests on ONE field. Padding actually beats
    silence on next steps (0.48 vs 0.28) and on promises (0.52 vs 0.42), because most calls do have items
    in those. Only warning signs -- empty on roughly two thirds of calls -- pays for the invention (0.27 vs
    0.64), and that alone is what keeps the ordering. Anything that lowers the share of calls with no
    warning signs narrows this further and can invert it, which would not mean the metric broke but that
    the dataset stopped rewarding restraint. The ordering assertion below is the real check; the pinned
    figure is there to force a look when the mix moves.
    """
    padded = json.dumps({"next_steps": ["follow up with the customer", "send a recap email",
                                        "check in next month"],
                         "risks": ["renewal at risk", "competitor in play"],
                         "commitments": ["send a recap email"], "sentiment": "NEUTRAL"})
    padded_score = _run(spec, lambda q: ArtifactAgentOutput(text=padded, json=padded),
                        tmp_path / "pad").final_score

    empty = json.dumps(BLIND_REPLY)
    empty_score = _run(spec, lambda q: ArtifactAgentOutput(text=empty, json=empty),
                       tmp_path / "empty").final_score

    # Asserted before the pinned value so a dataset change cannot hide a genuine regression behind a
    # stale number.
    assert padded_score < empty_score, (
        "padding now scores at least as well as returning nothing -- the anti-fabrication property of "
        "symmetric F1 has regressed"
    )
    assert padded_score == pytest.approx(0.436, abs=0.001)


def test_a_reply_with_no_structured_payload_scores_zero(spec: dict, tmp_path) -> None:
    """A reply whose structured slot is empty scores 0 on every objective, even if its prose is right.

    Scope note, because in-process and HTTP differ here: `InProcessArtifactClient` passes an
    `ArtifactAgentOutput` through untouched and never fills `json` from `text`, so what this pins is the
    downstream half -- an unfilled slot is scored as a contract failure end to end and never read as
    "every label empty" (which, against this task's oracle, would MATCH on the all-empty sample and hand
    out free marks). The upstream half -- that the real transport REFUSES to fill the slot when prose
    surrounds the object -- is a property of `HttpArtifact`, covered by
    `test_the_transport_only_accepts_a_reply_that_is_nothing_but_the_object` below.
    """
    def narrating(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
        return ArtifactAgentOutput(text="Here is the reading: " + _oracle_for(q.text or ""))

    report = _run(spec, narrating, tmp_path)

    assert report.final_score == pytest.approx(0.0)
    assert all(m["score"] == pytest.approx(0.0) for m in report.views["per_metric"])


def test_the_transport_only_accepts_a_reply_that_is_nothing_but_the_object() -> None:
    """The other half of the response contract, tested where it actually lives.

    `extract_json_object` is what the HTTP transport uses to fill the structured slot, and it accepts a
    reply only when the WHOLE body is one JSON object. This is the task's stated contract ("no summary
    sentence before it, no code fence") and it is enforced in the transport, upstream of anything an eval
    function can see -- so a lenient "helpful" salvage added here would silently change what the task
    scores without failing any metric's own tests.
    """
    payload = json.dumps(BLIND_REPLY)
    assert extract_json_object(payload) == payload
    for wrapped in (f"Here is the reading: {payload}", f"```json\n{payload}\n```",
                    f"{payload}\nHope that helps."):
        assert extract_json_object(wrapped) is None, (
            f"the transport accepted a reply with prose around the object: {wrapped[:40]!r}"
        )


def test_the_excluded_sentiment_value_scores_zero(spec: dict, tmp_path) -> None:
    """`AT_RISK` is the plausible guess the task deliberately excludes -- risk is its own label.

    An artifact returning it has collapsed two independent signals into one field, and the vocabulary is
    stated to the agent during BUILD, so this is a given requirement being ignored rather than an
    unforeseen answer.
    """
    reply = json.dumps({**BLIND_REPLY, "sentiment": "AT_RISK"})
    report = _run(spec, lambda q: ArtifactAgentOutput(text=reply, json=reply), tmp_path)

    scored = {m["objective"]: m["score"] for m in report.views["per_metric"]}
    assert scored["sentiment_accuracy"] == pytest.approx(0.0)
