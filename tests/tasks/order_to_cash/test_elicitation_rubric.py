"""order_to_cash's elicitation rubric: the shared soundness checks plus this task's own trap.

The trap is granularity. The agent-facing KPI document lists the fault categories informally in prose --
"price issue, quantity mismatch, off-contract charge, duplicate, etc." -- while only the exact spellings
live with the simulator. An item phrased as "knows the categories exist" would therefore score every
agent that read a file it was handed. The item has to be the spellings, and that is what these pin.
"""
from __future__ import annotations

from tests.tasks.conftest import (
    agent_visible_text,
    assert_rubric_is_sound,
)

TASK = "order_to_cash"

FAULT_TYPES = ("PRICE_DELTA", "QTY_MISMATCH", "TOTAL_MATH", "OFF_CONTRACT_ITEM",
               "OFF_CONTRACT_FEE", "DUPLICATE")


def _rubric():
    from appsci_bench.core.spec import load_task_spec
    from appsci_bench.eval.elicitation_rubric import load_rubric

    return load_rubric(load_task_spec(TASK))


def test_the_rubric_is_sound():
    assert_rubric_is_sound(TASK)


def test_the_spelling_item_carries_all_six_values_verbatim():
    (item,) = [i for i in _rubric().items if i.item_id == "fault_taxonomy_spellings"] or \
              [i for i in _rubric().items if "spelling" in i.item_id]
    for value in FAULT_TYPES:
        assert value in item.fact, f"the spelling item omits {value}"
    # All-or-nothing: the judge has to know that a partial list is not this item.
    assert "six" in item.fact.lower()


def test_no_fault_type_spelling_is_readable_from_an_agent_facing_file():
    """The whole reason the spelling item is elicitable. If a future edit mounts a document naming these
    values, this fails and the item has to be rewritten or dropped -- not left silently measuring
    file-reading."""
    visible = agent_visible_text(TASK)
    leaked = [v for v in FAULT_TYPES if v in visible]
    assert leaked == [], f"these fault_type values are readable by the agent: {leaked}"


def test_the_informal_category_prose_the_agent_can_read_does_not_satisfy_the_item():
    # The distinction the item rests on: the categories as ideas are handed, the exact strings are not.
    visible = agent_visible_text(TASK).lower()
    for informal in ("price", "quantity", "duplicate", "contract"):
        assert informal in visible, f"expected the agent-facing prose to mention {informal!r}"
    assert "closed set" not in visible
    assert "capitaliz" not in visible


def test_the_dollar_thresholds_are_not_readable_by_the_agent():
    # Both the price and total checks turn on $500. Nothing the agent is given states it.
    assert "$500" not in agent_visible_text(TASK)


def test_the_response_contract_items_cover_the_whole_envelope():
    """A rubric that pinned the fault values but not the envelope would miss the failure that costs the
    most: a correct object wrapped in prose scores zero on every objective.

    `dropped` used to be required here too, pinning the CONSEQUENCE half of
    `json_only_reply_or_invoice_dropped` -- that a malformed reply is dropped unreviewed rather than
    degrading the answer. That clause was removed from the rubric as failing authoring rule 1 (it changes
    nothing an agent builds: emit a bare object either way), and this list follows it. What still has to
    be covered is the SHAPE, which is what an agent can get wrong -- so the four shape tokens stay and
    only the consequence word goes. Do not read this as the envelope mattering less; b5 of
    j-20260910-114523-000000 emitted `flag`/`fault_types`/`reasons` and scored 0.000 on all three
    deterministic objectives with otherwise-correct detection logic."""
    facts = " ".join(i.fact for i in _rubric().items)
    for required in ("single JSON object", "flagged", "fault_type", "reason"):
        assert required in facts, f"no item states {required!r}"
