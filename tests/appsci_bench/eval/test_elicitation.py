"""Scoring a whole rubric against a build conversation. The judge's model call is mocked throughout.

Three properties get the most attention here.

**Unmeasurable is not zero.** A run where the agent never contacted the stakeholder scores zero,
correctly and informatively. A run whose conversation log is missing has no score at all. Both look
like "0" in a bare number and mean opposite things, so each status is pinned separately, including
that neither spends a model call to discover something already visible on disk.

**Warm, then fan out.** The shared prefix does not become readable until a first response has begun,
so items launched together all miss it. This is asserted with a barrier -- the fake judge blocks on the
first item while the test checks that nothing has been submitted yet -- rather than with a sleep or an
inspection of the code.

**Never raises.** A diagnostic that kills an otherwise-good eval run is worse than a missing one.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from appsci_bench.eval.build_transcript import (
    STATUS_EMPTY,
    STATUS_OK,
    STATUS_UNREADABLE,
    BuildTranscript,
    Exchange,
)
from appsci_bench.eval.elicitation import (
    STATUS_NO_EXCHANGES,
    STATUS_NO_RUBRIC,
    STATUS_SCORED,
    STATUS_UNMEASURABLE,
    VERDICT_ASKED_NOT_ELICITED,
    VERDICT_ELICITED,
    VERDICT_ERROR,
    VERDICT_MISSED,
    ElicitationVerdict,
    score_elicitation,
)
from appsci_bench.eval.elicitation_rubric import ElicitationRubric, RubricItem
from appsci_bench.eval.helpers.llm import JudgeParseError

_FACT = "The six fault_type values, spelled exactly, and the tolerance below which nothing is flagged."


def _rubric(*item_ids: str) -> ElicitationRubric:
    return ElicitationRubric(
        task_id="demo_task",
        items=tuple(RubricItem(item_id=i, fact=_FACT, objectives=("coverage",)) for i in item_ids),
        source="tasks/demo_task/elicitation_rubric.yaml")


def _transcript(n: int = 2, status: str = STATUS_OK) -> BuildTranscript:
    return BuildTranscript(
        exchanges=tuple(Exchange(turn=t, question=f"q{t}", answer=f"a{t}") for t in range(1, n + 1)),
        source="runs/demo_20260823", status=status)


def _verdict(asked: bool, elicited: bool) -> ElicitationVerdict:
    return ElicitationVerdict(reasoning="because", asked=asked, elicited=elicited,
                              evidence="sim > a1" if asked else "")


def _fixed(asked: bool, elicited: bool):
    """A judge that returns the same verdict for every item."""
    return lambda item, text, config=None: _verdict(asked, elicited)


def _by_item(mapping: dict[str, tuple[bool, bool]]):
    """A judge whose verdict depends on the item id."""
    return lambda item, text, config=None: _verdict(*mapping[item.item_id])


# --- the three rates -------------------------------------------------------------------------

def test_all_elicited():
    with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, True)):
        r = score_elicitation(_rubric("a", "b", "c"), _transcript())
    assert r.status == STATUS_SCORED
    assert (r.elicited_rate, r.asked_not_elicited_rate, r.missed_rate) == (1.0, 0.0, 0.0)
    assert r.total_items == 3 and r.error_items == 0
    assert {i.verdict for i in r.items} == {VERDICT_ELICITED}


def test_all_missed():
    with patch("appsci_bench.eval.elicitation.judge_item", _fixed(False, False)):
        r = score_elicitation(_rubric("a", "b"), _transcript())
    assert (r.elicited_rate, r.asked_not_elicited_rate, r.missed_rate) == (0.0, 0.0, 1.0)
    assert {i.verdict for i in r.items} == {VERDICT_MISSED}


def test_all_asked_but_not_elicited():
    with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, False)):
        r = score_elicitation(_rubric("a", "b"), _transcript())
    assert (r.elicited_rate, r.asked_not_elicited_rate, r.missed_rate) == (0.0, 1.0, 0.0)


def test_a_mixed_rubric_matches_hand_computed_rates():
    judge = _by_item({"a": (True, True), "b": (True, True),
                      "c": (True, False), "d": (False, False)})
    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a", "b", "c", "d"), _transcript())
    assert (r.elicited_rate, r.asked_not_elicited_rate, r.missed_rate) == (0.5, 0.25, 0.25)
    assert r.elicited_rate + r.asked_not_elicited_rate + r.missed_rate == 1.0


def test_the_three_verdicts_partition_the_rubric():
    # The decomposition is what makes the headline readable: 0.3 means something very different when
    # the rest is "missed" than when it is "asked but not elicited".
    judge = _by_item({"a": (True, True), "b": (True, False), "c": (False, False)})
    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a", "b", "c"), _transcript())
    assert [i.verdict for i in r.items] == [
        VERDICT_ELICITED, VERDICT_ASKED_NOT_ELICITED, VERDICT_MISSED]


def test_the_evidence_and_reasoning_ride_along_for_the_audit_table():
    with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, True)):
        r = score_elicitation(_rubric("a"), _transcript())
    assert r.items[0].evidence == "sim > a1"
    assert r.items[0].reasoning == "because"
    assert r.items[0].error is None


# --- statuses that spend no model calls ------------------------------------------------------

def test_no_rubric_returns_before_any_model_call():
    with patch("appsci_bench.eval.elicitation.judge_item") as judge:
        r = score_elicitation(None, _transcript())
    judge.assert_not_called()
    assert r.status == STATUS_NO_RUBRIC
    assert (r.elicited_rate, r.missed_rate) == (None, None)
    assert r.total_items == 0


def test_an_empty_rubric_is_no_rubric_with_a_warning():
    with patch("appsci_bench.eval.elicitation.judge_item") as judge:
        r = score_elicitation(_rubric(), _transcript())
    judge.assert_not_called()
    assert r.status == STATUS_NO_RUBRIC
    assert any("no items" in w for w in r.warnings)


def test_an_unreadable_log_is_unmeasurable_not_zero():
    with patch("appsci_bench.eval.elicitation.judge_item") as judge:
        r = score_elicitation(_rubric("a", "b"), _transcript(0, STATUS_UNREADABLE))
    judge.assert_not_called()
    assert r.status == STATUS_UNMEASURABLE
    # No rates at all -- a 0.0 here would read as "the agent asked nothing", which is a different run.
    assert (r.elicited_rate, r.asked_not_elicited_rate, r.missed_rate) == (None, None, None)
    assert r.total_items == 2  # the rubric is still known, just unscored
    assert any("NOT a zero" in w for w in r.warnings)


def test_a_conversation_that_never_happened_is_an_informative_zero():
    with patch("appsci_bench.eval.elicitation.judge_item") as judge:
        r = score_elicitation(_rubric("a", "b"), _transcript(0, STATUS_EMPTY))
    judge.assert_not_called()  # knowable from disk; spending calls to rediscover it is waste
    assert r.status == STATUS_NO_EXCHANGES
    assert (r.elicited_rate, r.asked_not_elicited_rate, r.missed_rate) == (0.0, 0.0, 1.0)
    assert [i.verdict for i in r.items] == [VERDICT_MISSED, VERDICT_MISSED]
    assert "never contacted" in r.items[0].reasoning


def test_the_transcript_source_and_exchange_count_are_recorded():
    with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, True)):
        r = score_elicitation(_rubric("a"), _transcript(3))
    assert r.transcript_source == "runs/demo_20260823"
    assert r.num_exchanges == 3
    assert r.task_id == "demo_task"


# --- warm, then fan out ----------------------------------------------------------------------

class _RecordingPool:
    """A real pool that counts submissions, so "was anything submitted yet" is answerable."""

    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self.submissions = 0
        self._lock = threading.Lock()

    def submit(self, fn, *a, **kw):
        with self._lock:
            self.submissions += 1
        return self._pool.submit(fn, *a, **kw)

    def shutdown(self, **kw):
        raise AssertionError("an injected pool must not be shut down by its user")

    def close(self) -> None:
        self._pool.shutdown(wait=True)


def test_the_first_item_finishes_before_anything_is_submitted():
    """The barrier test. The fake judge blocks on the first item; while it is blocked the test asserts
    the pool has received nothing. If the warm call were submitted alongside the others -- or into the
    pool at all -- the count would already be non-zero and this fails.

    Not a sleep and not a code inspection: the assertion happens while the warm call is provably still
    in flight."""
    warm_started, release = threading.Event(), threading.Event()
    seen: list[str] = []

    def judge(item, text, config=None):
        seen.append(item.item_id)
        if item.item_id == "a_first":
            warm_started.set()
            assert release.wait(timeout=10), "test did not release the warm call"
        return _verdict(True, True)

    pool = _RecordingPool()
    result: list = []
    try:
        with patch("appsci_bench.eval.elicitation.judge_item", judge):
            worker = threading.Thread(
                target=lambda: result.append(
                    score_elicitation(_rubric("c_third", "a_first", "b_second"),
                                      _transcript(), pool=pool)))
            worker.start()
            assert warm_started.wait(timeout=10), "the warm call never started"
            assert pool.submissions == 0, "items were submitted before the cache was warm"
            assert seen == ["a_first"], f"more than the warm item ran first: {seen}"
            release.set()
            worker.join(timeout=20)
    finally:
        release.set()
        pool.close()

    assert result[0].elicited_rate == 1.0
    assert pool.submissions == 2, "the two remaining items should be the only submissions"


def test_the_warm_item_is_the_first_by_sorted_item_id():
    # Sorted, not file order: the warm item has to be the same one on every run of the same rubric, or
    # a re-run writes a different cache entry for no reason.
    order: list[str] = []

    def judge(item, text, config=None):
        order.append(item.item_id)
        return _verdict(True, True)

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        score_elicitation(_rubric("zulu", "alpha", "mike"), _transcript())
    assert order[0] == "alpha"


def test_a_single_item_rubric_needs_no_pool_at_all():
    pool = _RecordingPool()
    try:
        with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, True)):
            r = score_elicitation(_rubric("only"), _transcript(), pool=pool)
    finally:
        pool.close()
    assert pool.submissions == 0
    assert r.elicited_rate == 1.0


def test_an_injected_pool_is_used_and_left_running():
    pool = _RecordingPool()
    try:
        with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, True)):
            score_elicitation(_rubric("a", "b", "c", "d"), _transcript(), pool=pool)
        assert pool.submissions == 3          # four items, one judged warm on the calling thread
        assert pool._pool.submit(lambda: 1).result() == 1   # _RecordingPool.shutdown would have raised
    finally:
        pool.close()


def test_without_a_pool_it_opens_and_closes_its_own():
    with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, True)):
        r = score_elicitation(_rubric("a", "b", "c"), _transcript())
    assert r.elicited_rate == 1.0


def test_the_same_transcript_text_reaches_every_item():
    # One render, shared byte for byte -- the property the whole caching design rests on.
    texts: list[str] = []

    def judge(item, text, config=None):
        texts.append(text)
        return _verdict(True, True)

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        score_elicitation(_rubric("a", "b", "c"), _transcript())
    assert len(set(texts)) == 1


# --- an instrument failure must never read as an agent failure --------------------------------

def test_a_judge_that_fails_once_then_succeeds_yields_the_verdict():
    calls = {"n": 0}

    def judge(item, text, config=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise JudgeParseError("garbled")
        return _verdict(True, True)

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a"), _transcript())
    assert r.items[0].verdict == VERDICT_ELICITED
    assert r.error_items == 0
    assert calls["n"] == 2


def test_a_judge_that_never_succeeds_becomes_an_error_not_a_miss():
    """An unjudged item is not evidence the agent did not ask. Folding it into "missed" would make an
    instrument failure indistinguishable from an agent failure."""
    def judge(item, text, config=None):
        if item.item_id == "broken":
            raise JudgeParseError("garbled again")
        return _verdict(True, True)

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("broken", "fine"), _transcript())
    by_id = {i.item_id: i for i in r.items}
    assert by_id["broken"].verdict == VERDICT_ERROR
    assert "garbled again" in by_id["broken"].error
    assert by_id["fine"].verdict == VERDICT_ELICITED     # the others are unaffected
    assert r.error_items == 1
    assert r.missed_rate == 0.0                          # NOT counted as a miss


def test_unjudged_items_are_excluded_from_the_denominator():
    """An item the judge could not read says nothing about whether the agent asked, so counting it in
    the denominator would charge the agent for our own instrument failing. Excluded instead -- the same
    treatment an eval function's "not applicable" already gets from the aggregator."""
    def judge(item, text, config=None):
        if item.item_id in ("c", "d"):
            raise RuntimeError("transport died")
        return _verdict(True, True)

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a", "b", "c", "d"), _transcript())
    assert r.elicited_rate == 1.0            # 2 of 2 JUDGED, not 2 of 4
    assert (r.judged_items, r.error_items, r.total_items) == (2, 2, 4)
    assert any("EXCLUDED" in w and "2 of 4" in w and "not the agent's" in w for w in r.warnings)


def test_the_three_rates_always_sum_to_one_even_with_errors_present():
    # The invariant excluding errors restores. It is what lets a reader treat the decomposition as a
    # decomposition rather than as three numbers that happen to be related.
    def judge(item, text, config=None):
        if item.item_id == "err":
            raise RuntimeError("down")
        return {"win": _verdict(True, True), "part": _verdict(True, False),
                "none": _verdict(False, False)}[item.item_id]

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("win", "part", "none", "err"), _transcript())
    assert r.elicited_rate + r.asked_not_elicited_rate + r.missed_rate == pytest.approx(1.0)
    assert r.judged_items == 3
    assert all(v == pytest.approx(1 / 3) for v in
               (r.elicited_rate, r.asked_not_elicited_rate, r.missed_rate))


def test_the_judged_count_is_the_rubric_size_when_nothing_failed():
    with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, True)):
        r = score_elicitation(_rubric("a", "b", "c"), _transcript())
    assert (r.judged_items, r.error_items, r.total_items) == (3, 0, 3)
    assert not r.warnings                    # nothing to caveat when everything was judged


def test_a_timeout_is_recorded_as_an_error_rather_than_hanging():
    def judge(item, text, config=None):
        raise TimeoutError("stuck")

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a"), _transcript())
    assert r.items[0].verdict == VERDICT_ERROR and "stuck" in r.items[0].error


def test_a_judge_call_slower_than_the_item_timeout_is_abandoned():
    # The bound is real, not decorative: a hung call must not hold the phase open.
    def judge(item, text, config=None):
        assert threading.Event().wait(timeout=30) is False  # never returns in time
        return _verdict(True, True)

    with patch("appsci_bench.eval.elicitation.judge_item", judge), \
         patch("appsci_bench.eval.elicitation.ITEM_TIMEOUT_S", 0.2):
        r = score_elicitation(_rubric("a"), _transcript())
    assert r.items[0].verdict == VERDICT_ERROR
    assert "timed out" in r.items[0].error


def test_every_item_failing_is_unmeasurable_not_a_zero():
    """Nothing judged is no information, and rates of 0.0 would render as a real zero -- which says the
    agent asked nothing. Rendering an instrument failure as the worst possible agent result is exactly
    the conflation the four statuses exist to prevent, so this reports no score rather than a wrong one."""
    def judge(item, text, config=None):
        raise RuntimeError("everything is broken")

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a", "b"), _transcript())
    assert r.status == STATUS_UNMEASURABLE
    assert r.error_items == 2
    assert (r.elicited_rate, r.asked_not_elicited_rate, r.missed_rate) == (None, None, None)
    assert any("NO elicitation score" in w and "not a score of zero" in w for w in r.warnings)
    assert any("everything is broken" in w for w in r.warnings)   # the cause, not just the fact
    assert r.items and all(i.verdict == VERDICT_ERROR for i in r.items)   # the table still explains why


def test_a_partial_failure_still_scores():
    """The other side of the all-errors case: some items judged IS information, so it is reported rather
    than thrown away. Only a total failure yields no score."""
    def judge(item, text, config=None):
        if item.item_id in ("c", "d"):
            raise RuntimeError("gateway hiccup")
        return _verdict(True, False)

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a", "b", "c", "d"), _transcript())
    assert r.status == STATUS_SCORED
    assert r.asked_not_elicited_rate == 1.0
    assert r.judged_items == 2


# --- the retry budget and its waits ------------------------------------------------------------

def test_a_judge_that_fails_twice_then_succeeds_still_yields_the_verdict():
    # Three attempts, not two: what reaches this retry has already survived the transport's own backoff,
    # so it is usually a rate-limit burst -- and a burst routinely outlasts one more immediate attempt.
    calls = {"n": 0}

    def judge(item, text, config=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 rate limited")
        return _verdict(True, True)

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a"), _transcript())
    assert r.items[0].verdict == VERDICT_ELICITED
    assert calls["n"] == 3


def test_the_attempt_count_is_exhausted_before_an_error_is_recorded():
    calls = {"n": 0}

    def judge(item, text, config=None):
        calls["n"] += 1
        raise RuntimeError("still down")

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        score_elicitation(_rubric("a"), _transcript())
    from appsci_bench.eval.elicitation import _RETRY_DELAYS_S
    assert calls["n"] == len(_RETRY_DELAYS_S) + 1


def test_the_configured_delays_are_real_waits_not_zero():
    """The suite patches the delays to zero so it does not sleep through them. That makes it possible to
    ship a zeroed default by accident, so the shipped values are asserted from the source of truth."""
    import inspect

    import appsci_bench.eval.elicitation as mod

    src = inspect.getsource(mod)
    line = next(x for x in src.splitlines() if x.startswith("_RETRY_DELAYS_S"))
    delays = eval(line.split("=", 1)[1].strip())          # noqa: S307 -- a literal tuple in our own source
    assert len(delays) >= 2, "at least two retries after the first attempt"
    assert all(d > 0 for d in delays), "an immediate retry into a rate-limit burst buys nothing"
    assert delays == tuple(sorted(delays)), "later waits should not be shorter than earlier ones"


def test_the_waits_are_actually_taken_between_attempts():
    slept: list[float] = []

    def judge(item, text, config=None):
        raise RuntimeError("down")

    with patch("appsci_bench.eval.elicitation.judge_item", judge),          patch("appsci_bench.eval.elicitation._RETRY_DELAYS_S", (1.5, 4.5)),          patch("appsci_bench.eval.elicitation.time.sleep", slept.append):
        score_elicitation(_rubric("a"), _transcript())
    assert slept == [1.5, 4.5]                  # one wait BEFORE each retry, none before the first try


def test_no_wait_happens_when_the_first_attempt_succeeds():
    slept: list[float] = []
    with patch("appsci_bench.eval.elicitation.judge_item", _fixed(True, True)),          patch("appsci_bench.eval.elicitation._RETRY_DELAYS_S", (1.5, 4.5)),          patch("appsci_bench.eval.elicitation.time.sleep", slept.append):
        score_elicitation(_rubric("a", "b", "c"), _transcript())
    assert slept == []


def test_an_unexpected_internal_failure_never_propagates():
    """Whatever breaks -- including something this module did not anticipate -- the eval run survives
    and the diagnostic reports itself as unmeasured."""
    with patch("appsci_bench.eval.elicitation.render_for_judge",
               side_effect=MemoryError("out of memory")):
        r = score_elicitation(_rubric("a"), _transcript())
    assert r.status == STATUS_UNMEASURABLE
    assert r.task_id == "demo_task"
    assert any("produced no score" in w for w in r.warnings)


def test_a_contradictory_verdict_from_the_judge_becomes_an_error():
    # judge_item rejects these itself; this pins that the rejection lands in the error bucket rather
    # than crashing the fan-out.
    def judge(item, text, config=None):
        raise JudgeParseError("contradictory verdict")

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("a"), _transcript())
    assert r.items[0].verdict == VERDICT_ERROR


# --- deterministic output --------------------------------------------------------------------

def test_output_is_sorted_by_item_id_regardless_of_completion_order():
    finish_order = {"slow_one": 0.15, "quick_one": 0.0, "middle_one": 0.05}

    def judge(item, text, config=None):
        threading.Event().wait(timeout=finish_order[item.item_id])
        return _verdict(True, True)

    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric("slow_one", "quick_one", "middle_one"), _transcript())
    assert [i.item_id for i in r.items] == ["middle_one", "quick_one", "slow_one"]


def test_scoring_the_same_rubric_twice_gives_the_same_table():
    judge = _by_item({"a": (True, True), "b": (True, False), "c": (False, False)})
    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        first = score_elicitation(_rubric("a", "b", "c"), _transcript())
        second = score_elicitation(_rubric("c", "b", "a"), _transcript())   # file order shuffled
    assert first.items == second.items
    assert first.elicited_rate == second.elicited_rate


@pytest.mark.parametrize("n", [1, 2, 5, 17])
def test_rates_are_over_the_whole_rubric_for_any_size(n):
    ids = [f"item_{i:02d}" for i in range(n)]
    judge = _by_item({i: (True, i == ids[0]) for i in ids})
    with patch("appsci_bench.eval.elicitation.judge_item", judge):
        r = score_elicitation(_rubric(*ids), _transcript())
    assert r.elicited_rate == pytest.approx(1 / n)
    assert len(r.items) == n == r.total_items


# --- the real judge, not a stand-in ------------------------------------------------------------

def test_the_scorer_calls_the_real_judge_correctly():
    """Everything above replaces `judge_item` with a stub, and a stub written by hand is more forgiving
    than the real function -- which is how the scorer shipped calling it with `config` positionally when
    that parameter is keyword-only. Every item failed against a live gateway while the whole suite passed.

    So this one patches only the TRANSPORT and lets the real judge run: prompt assembly, the block list,
    the schema parse, all of it. Any future signature drift fails here instead of in production."""
    raw = ('{"reasoning": "the stakeholder listed them", "asked": true, "elicited": true, '
           '"evidence": "sim > a1"}')
    from appsci_bench.common.model_config import ModelConfig

    cfg = ModelConfig(model="anthropic/claude-opus-4-8", api_key="no-token")
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw) as transport:
        r = score_elicitation(_rubric("a", "b", "c"), _transcript(), config=cfg)

    assert r.status == STATUS_SCORED, f"the real judge failed: {[i.error for i in r.items]}"
    assert r.elicited_rate == 1.0 and r.error_items == 0
    assert transport.call_count == 3          # one model call per item, no retries needed


def test_the_real_judge_is_reached_without_an_explicit_config_too():
    """The path a run actually takes: no config passed, so the judge resolves its own from the
    environment. Pins that the keyword threading works when the value is None."""
    raw = '{"reasoning": "r", "asked": false, "elicited": false, "evidence": ""}'
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw):
        r = score_elicitation(_rubric("a"), _transcript())
    assert r.status == STATUS_SCORED and r.missed_rate == 1.0
