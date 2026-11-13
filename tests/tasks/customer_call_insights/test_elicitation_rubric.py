"""customer_call_insights's elicitation rubric: the shared soundness checks plus this task's own trap.

Personas are bound one at a time -- an agent talking to L3 cannot also ask L2 -- so anything the task
cannot be built without has to reach every persona regardless of the draw. The strictly necessary
material (the exact key spellings, the three disposition values, the empty-vs-absent distinction, the
next_steps/commitments overlap) lives in `user_sim_docs/common/`, which every persona loads
unconditionally. What stays gated per persona is the harder-to-detect judgement layer: the field
boundaries, the warning-sign vocabulary, and the pipeline/outcome edge cases that are easy to fall into
even once the wire format is known.

The CONCEPTS behind the four fields are volunteered by any persona that opens a conversation at all --
what needs doing, what we promised, warning signs, customer sentiment. The exact encoding is not
volunteered, but it is elicitable in one direct question from every persona, because it now lives in a
document every persona holds. So the rubric measures whether the agent ASKED for material it could
have been handed for free -- that is the point of an elicitation diagnostic, not a gap in it.

The second risk is subtler. `environment/sop/kpis.md` is mounted and states the no-invented-detail
principle outright, so an item claiming credit for eliciting it would be measuring file-reading. Only
the SYMMETRY -- that dropping something agreed costs exactly as much -- is elicitable, which is why the
grounding item is phrased that way and why a test holds that line.
"""
from __future__ import annotations

from tests.tasks.conftest import (
    agent_visible_text,
    assert_rubric_is_sound,
)

TASK = "customer_call_insights"

# The wire contract the stakeholders state during the build.
KEY_NAMES = ("next_steps", "risks", "commitments", "sentiment")
SENTIMENTS = ("POSITIVE", "NEUTRAL", "NEGATIVE")


def _rubric():
    from appsci_bench.core.spec import load_task_spec
    from appsci_bench.eval.elicitation_rubric import load_rubric

    return load_rubric(load_task_spec(TASK))


def test_the_rubric_is_sound():
    assert_rubric_is_sound(TASK)


def test_the_disposition_item_carries_all_three_values_verbatim():
    (item,) = [i for i in _rubric().items if i.item_id == "three_sentiment_values_and_the_default"]
    for value in SENTIMENTS:
        assert value in item.fact, f"the disposition item omits {value}"
    # All-or-nothing: the judge has to know a partial vocabulary is not this item.
    assert "three" in item.fact.lower()


def test_no_disposition_value_is_readable_from_an_agent_facing_file():
    """The whole reason the vocabulary item is elicitable. `sentiment_accuracy` compares against these
    three strings exactly, so if a future edit mounts a document naming them, the item silently becomes
    a file-reading check and has to be rewritten or dropped."""
    visible = agent_visible_text(TASK)
    leaked = [v for v in SENTIMENTS if v in visible]
    assert leaked == [], f"these disposition values are readable by the agent: {leaked}"


def test_the_key_names_item_carries_all_four_verbatim():
    (item,) = [
        i for i in _rubric().items
        if i.item_id == "reply_is_single_json_with_four_exact_keys_and_types"
    ]
    for key in KEY_NAMES:
        assert key in item.fact, f"the key-names item omits {key}"


def test_no_key_name_is_readable_from_an_agent_facing_file():
    visible = agent_visible_text(TASK)
    leaked = [k for k in KEY_NAMES if k in visible]
    assert leaked == [], f"these key names are readable by the agent: {leaked}"


def test_the_wire_format_never_reaches_the_agent_facing_text():
    """The distinction the rubric rests on: the four fields as ideas are volunteered by L3, the exact
    encoding is not. If the redacted spec or a mounted file ever states the payload shape, the envelope
    items stop being elicitable and this fails first."""
    visible = agent_visible_text(TASK).lower()
    assert "call" in visible, "expected the agent-facing text to mention the calls at all"
    for withheld in ("empty array", "empty list", "upper case"):
        assert withheld not in visible, f"the wire format leaked to the agent: {withheld!r}"


def test_the_response_contract_items_cover_the_whole_envelope():
    """A rubric pinning the field semantics but not the envelope would miss the failure that costs the
    most: a correct reading wrapped in prose fails the parse and scores zero on all four objectives."""
    facts = " ".join(i.fact for i in _rubric().items)
    for required in ("one JSON object", "next_steps", "risks", "commitments", "sentiment",
                     "empty array", "dropped"):
        assert required in facts, f"no item states {required!r}"


def test_the_bundled_items_state_their_own_completeness():
    """Items are judged all-or-nothing, so a bundle has to tell the judge what complete means. Each of
    these carries a count that a partial answer fails."""
    expected = {
        "next_step_three_part_test": "three",
        "three_things_that_are_not_next_steps": "three",
        "three_non_requirements_for_a_promise": "three",
        "seven_recognised_warning_signs": "seven",
        "four_look_alikes_that_are_not_warning_signs": "four",
    }
    by_id = {i.item_id: i for i in _rubric().items}
    for item_id, count in expected.items():
        assert item_id in by_id, f"missing bundled item {item_id!r}"
        assert count in by_id[item_id].fact.lower(), \
            f"{item_id} does not state its completeness ({count!r} absent)"


def test_the_no_invention_principle_alone_is_not_claimed_as_elicited():
    """`environment/sop/kpis.md` states it, so it is handed. The grounding item must rest on the
    symmetry -- that dropping something agreed costs as much as inventing -- which the mount does not
    state."""
    visible = agent_visible_text(TASK).lower()
    assert "invented" in visible, "expected the mounted KPI doc to state the no-invention principle"

    (item,) = [i for i in _rubric().items if i.item_id == "grounding_cuts_both_ways"]
    fact = item.fact.lower()
    # The elicitable half: omission is as costly, not the safer direction to err in.
    assert "exactly as costly" in fact
    assert "dropping" in fact


def test_every_objective_is_exercised_by_some_item():
    """All four PRICED (weight > 0) objectives are at 0.25, so a field no item feeds is a field whose
    requirements the rubric implicitly claims are all readable -- worth failing on rather than
    discovering later. Diagnostic (weight 0.0) safety axes are excluded on purpose: they score the
    artifact's behaviour against the call/transcript directly (eval_safety.py), not against a fact a
    stakeholder had to give up, so they have no elicitation-rubric item to feed and are not part of
    this test's contract."""
    from appsci_bench.core.spec import load_task_spec

    objectives = (load_task_spec(TASK).evaluation_data or {})["objectives"]
    priced = {o["name"] for o in objectives if o.get("weight", 0) > 0}
    covered = {o for i in _rubric().items for o in i.objectives}
    assert priced - covered == set(), f"no rubric item feeds: {sorted(priced - covered)}"
