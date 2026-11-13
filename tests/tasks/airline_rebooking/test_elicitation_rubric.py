"""airline_rebooking's elicitation rubric: the shared soundness checks plus this task's own two traps.

The first trap is size. Almost the whole policy is load-bearing for task success, so criticality alone
would overflow the item cap. Items are bundled by topic, which only works if every bundle states its own
completeness -- items are judged all-or-nothing, and "cancellation rules" gives a judge nothing to decide
against. These tests hold that line.

The second is the read tool. The agent can call the tool API, and one read returns most of the booking
argument list, so anything recoverable that way is handed rather than elicited.

There is also one item whose truth lives outside this file -- the test-record ids the personas hand out.
It goes stale the moment someone regenerates the build seed, so it is checked against the store and
against the personas rather than reviewed by eye.
"""
from __future__ import annotations

import re

import pytest

from tests.tasks.conftest import agent_visible_text, assert_rubric_is_sound

TASK = "airline_rebooking"

#: The items allowed to rest only on `policy_compliance`, which this task prices at zero.
#:
#: Naming `task_success` is a strong claim and the reason belongs on the item itself: that not knowing
#: the fact changes the post-run database or a required communication. The test for it is whether a
#: WRONG WRITE is reachable -- NOT whether the tool shows up among some case's golden actions, which is
#: the shallower question three re-mapped items were first justified by and had to be re-argued.
#:
#: These two survive it. Transferring is in the environment's `NON_WRITE` set and the exact wording
#: appears in no case's `communicate_info`, so neither following the protocol nor botching it can move
#: the database or a required string. The no-outside-knowledge prohibition is behavioural throughout and
#: writes nothing at all. Both remain real ask-only policy: the task zeroed the weight so its final
#: score would be the one figure comparable to a published tau2 number, not because policy stopped
#: mattering, and it still computes and reports the objective.
_UNPRICED_OK = (
    "transfer_protocol_and_exact_message",
    "no_outside_knowledge_or_recommendations",
)


def _rubric():
    from appsci_bench.core.spec import load_task_spec
    from appsci_bench.eval.elicitation_rubric import load_rubric

    return load_rubric(load_task_spec(TASK))


def test_the_rubric_is_sound():
    assert_rubric_is_sound(TASK, unpriced_ok=_UNPRICED_OK)


# A fact past this length is carrying more than one fact, which makes it a bundle -- and design rule 5
# applies to bundles specifically. A single-fact item ("the passenger count cannot change") has no
# boundary to state, and demanding one of it would just be a regex to satisfy.
BUNDLE_CHARS = 300
BIG_BUNDLE_CHARS = 420


def test_every_bundled_item_states_its_own_completeness():
    """A bundle whose boundary is unstated cannot be judged all-or-nothing. Each one has to say how many
    parts it has, enumerate them, or say what is excluded -- so "most of the allowance table" is decidably
    not the item."""
    import re

    bundles = [i for i in _rubric().items if len(i.fact) > BUNDLE_CHARS]
    assert bundles, "this rubric is all single-fact items; the bundling check has nothing to check"
    for item in bundles:
        bounded = re.search(
            r"\b(?:exactly|all|both|whole|every|nine|four|three|two)\b"
            r"|\b\d+\s*/\s*\d+\b|\bat most\b|\bnothing else\b|\bnever\b|\bonly\b"
            r"|\bno other\b|\bnot at all\b",
            item.fact, re.IGNORECASE)
        assert bounded, (
            f"{item.item_id}: this bundle does not bound itself -- state a count, enumerate the members, "
            f"or say what is excluded, or the judge cannot decide whether everything came back")


def test_the_biggest_bundles_carry_a_judge_hint():
    """Where a bundle has many parts, the author owes the judge a note on what complete means -- that is
    where an all-or-nothing verdict is easiest to get wrong."""
    for item in _rubric().items:
        if len(item.fact) > BIG_BUNDLE_CHARS:
            assert item.judge_hint, f"{item.item_id} is a large bundle with no completeness hint"


def test_the_baggage_table_carries_every_cell():
    (item,) = [i for i in _rubric().items if "baggage_allowance" in i.item_id]
    for level in ("regular", "silver", "gold"):
        assert level in item.fact
    for cabin in ("basic economy", "economy", "business"):
        assert cabin in item.fact
    # Nine allowances plus the extra-bag fee. Missing a cell means telling a customer a wrong allowance.
    for value in ("0", "1", "2", "3", "4", "50"):
        assert value in item.fact
    assert item.judge_hint and "nine" in item.judge_hint


def test_the_compensation_items_carry_both_amounts_and_the_gate():
    facts = " ".join(i.fact for i in _rubric().items)
    assert "100 dollars" in facts and "50 dollars" in facts
    for gate in ("silver", "business", "travel insurance", "explicitly asks"):
        assert gate in facts, f"the compensation gate omits {gate!r}"


def test_no_item_restates_something_the_spec_already_tells_the_agent():
    """The agent-facing `interface_design` states the confirmation gate, the one-tool-call rule, and that
    the tools do not enforce policy. An item covering any of those would score every agent that read its
    own spec."""
    facts = " ".join(i.fact for i in _rubric().items).lower()
    for handed in ("explicit user confirmation", "one tool call at a time",
                   "tools do not enforce", "argument schema"):
        assert handed not in facts, f"an item restates handed material: {handed!r}"


def test_no_item_is_reproducible_from_tool_argument_names_alone():
    """The read-tool escape route. An item whose whole content is argument names would be recoverable from
    one tool call, so the token scan is run against the tool surface specifically."""
    from tests.tasks.conftest import readable_tokens

    visible = agent_visible_text(TASK)
    for item in _rubric().items:
        from tests.tasks.conftest import _tokens

        tokens = _tokens(item.fact)
        if len(tokens) >= 2:
            assert readable_tokens(item.fact, visible) != tokens, (
                f"{item.item_id}: every value it pins is reachable from the tool surface")


def test_the_policy_areas_the_task_scores_are_all_represented():
    """A rubric that covered booking but not cancellation would report a high rate for an agent that never
    asked about half the policy. Each area the task exercises needs at least one item."""
    facts = " ".join(i.fact for i in _rubric().items).lower()
    for area, needle in (("booking payment", "gift card"), ("baggage", "checked bag"),
                         ("insurance", "travel insurance"), ("modification", "basic economy"),
                         ("cabin change", "already been flown"),
                         ("cancellation", "within the last 24 hours"),
                         ("refund", "business days"), ("compensation", "certificate"),
                         ("transfer", "transferred to a human")):
        assert needle in facts, f"no item covers {area}"


# --- the test-record item ------------------------------------------------------------------------
#
# Its content is a list of ids, which is the one kind of fact that rots on its own: regenerate the build
# seed and the rubric goes on grading the agent against records nobody can be told about any more. The
# store and the personas are the two things it has to agree with, and both are read here rather than
# copied.

RECORD_ITEM = "build_store_test_record_handles"

#: An id-shaped token in the item's fact. Deliberately wider than the ids themselves so a stale one is
#: caught rather than skipped -- membership in the store is what the assertions below decide on.
_ID_SHAPED = re.compile(r"\b(?:[a-z]+_[a-z]+_\d{3,}|[A-Z]{2,}\d{3,})\b")


def _record_item():
    (item,) = [i for i in _rubric().items if i.item_id == RECORD_ITEM]
    return item


def _persona_ids(level: str) -> list[str]:
    """What one persona actually hands out, read through the repo guard's own parser rather than a
    second copy of it -- a private list here would agree with itself while the personas moved on."""
    from tests.appsci_bench.core.test_usersim_docs_repo_guards import _declared_test_record_ids
    from tests.tasks.conftest import REPO

    return _declared_test_record_ids(
        REPO / "tasks" / TASK / "user_personas" / f"{level}.yaml")


@pytest.mark.parametrize("level", ["l1", "l2", "l3"])
def test_the_record_item_names_exactly_what_the_personas_hand_out(level):
    """The judge grades an answer against this fact, so an id the simulator can never say is an item no
    agent can elicit, and an id the simulator says that the fact omits is one the judge marks wrong."""
    declared = _persona_ids(level)
    assert declared, f"{level} declares no test records -- the rubric item has nothing to measure"
    fact_ids = set(_ID_SHAPED.findall(_record_item().fact))
    assert fact_ids == set(declared), (
        f"{TASK}: the rubric's test-record item and {level} disagree.\n"
        f"  rubric only: {sorted(fact_ids - set(declared))}\n"
        f"  {level} only:    {sorted(set(declared) - fact_ids)}\n"
        "Regenerate the item's id list from the personas' test_record_ids."
    )


def test_every_id_the_record_item_names_is_a_real_build_record():
    """The same guard the personas get. Held separately because the two files can drift together: a
    regenerated seed updated in the persona and copied into the rubric passes the check above and is
    still wrong if the copy went stale in both."""
    from tests.appsci_bench.core.test_usersim_docs_repo_guards import (
        _missing_from_store,
        _phase_ids,
    )

    build_ids = _phase_ids(TASK, "build")
    assert build_ids, f"{TASK} bundles no build store to check the rubric's ids against"
    fact_ids = sorted(_ID_SHAPED.findall(_record_item().fact))
    assert fact_ids, "the record item pins no ids at all"
    missing = _missing_from_store(fact_ids, build_ids)
    assert not missing, (
        f"{TASK}: the rubric's item names {missing}, which are not records of the build store -- "
        f"regenerate it from environment/build/data/db.json"
    )


def test_no_item_names_an_eval_store_record():
    """The rubric is answer-key material and is never mounted, but it is quoted to a judge alongside the
    build conversation, and an eval-store id in it would make the judge credit an agent for naming a row
    the artifact is graded on. Exact rather than shape-based: candidates are filtered by membership in
    the eval key set, so a word that merely looks like an id cannot fire it."""
    from tests.appsci_bench.core.test_usersim_docs_repo_guards import _phase_ids

    eval_ids = _phase_ids(TASK, "eval")
    assert eval_ids, f"{TASK} bundles no eval store"
    text = " ".join(f"{i.fact} {i.judge_hint or ''}" for i in _rubric().items)
    leaked = sorted(set(re.findall(r"[A-Za-z0-9_]{4,}", text)) & eval_ids)
    assert not leaked, (
        f"{TASK}: the rubric names eval-store records {leaked} -- those are rows the held-out cases "
        f"are scored against"
    )
