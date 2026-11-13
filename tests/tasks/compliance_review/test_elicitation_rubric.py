"""compliance_review's elicitation rubric: the shared soundness checks, plus a pin on the 4
per-policy ruleset items against scripts/build_seed_data.py's POLICIES table.

Without the pin, the rubric is a third hand-authored copy of the same 22 (category, rule type) pairs
that POLICIES and the *_rules.md docs already carry (see test_seed_data_rules_pinned.py) -- nothing
catches a category renamed in the generator and the rules doc but left stale here, and the suite
would stay green while the rubric quietly measured the wrong ruleset.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

from tests.tasks.conftest import assert_rubric_is_sound

_REPO = pathlib.Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "tasks" / "compliance_review" / "scripts" / "build_seed_data.py"

TASK = "compliance_review"

_RULESET_ITEM_IDS = {
    "vendor_standard": "vendor_standard_full_ruleset",
    "ip_protective": "ip_protective_full_ruleset",
    "risk_averse": "risk_averse_full_ruleset",
    "procurement_clean": "procurement_clean_full_ruleset",
}
_FACT_PAIR = re.compile(r"([A-Z][A-Za-z \-]+?)\s*\((must_have|must_not_have|needs_review)\)")


def _rubric():
    from appsci_bench.core.spec import load_task_spec
    from appsci_bench.eval.elicitation_rubric import load_rubric

    return load_rubric(load_task_spec(TASK))


def _load_policies() -> list[dict]:
    spec = importlib.util.spec_from_file_location("_build_seed_data_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.POLICIES


def _pairs_from_fact(fact: str) -> set[tuple[str, str]]:
    return {(category.strip(), rule) for category, rule in _FACT_PAIR.findall(fact)}


def test_the_rubric_is_sound():
    assert_rubric_is_sound(TASK)


def test_every_policy_has_a_ruleset_item():
    items_by_id = {i.item_id: i for i in _rubric().items}
    for policy_id, item_id in _RULESET_ITEM_IDS.items():
        assert item_id in items_by_id, f"missing elicitation rubric item {item_id!r} for {policy_id!r}"


def test_ruleset_items_match_the_policies_table():
    """The (category, rule type) pairs each ruleset item states must be exactly the pairs
    build_seed_data.py's POLICIES table encodes -- a rename in the generator and the *_rules.md docs
    that isn't mirrored here would otherwise leave this rubric silently stale."""
    policies = _load_policies()
    items_by_id = {i.item_id: i for i in _rubric().items}
    mismatches = []
    for policy in policies:
        item_id = _RULESET_ITEM_IDS.get(policy["id"])
        item = items_by_id.get(item_id)
        if item is None:
            continue  # reported by test_every_policy_has_a_ruleset_item
        expected = {(rule["category"], rule["type"]) for rule in policy["rules"]}
        actual = _pairs_from_fact(item.fact)
        if actual != expected:
            mismatches.append(
                f"{policy['id']}: item has {actual - expected or '{}'} not in POLICIES, "
                f"POLICIES has {expected - actual or '{}'} not in the item"
            )
    assert not mismatches, "\n".join(mismatches)
