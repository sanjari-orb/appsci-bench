"""Pins tasks/compliance_review/user_sim_docs/common/*_rules.md to the POLICIES table in
scripts/build_seed_data.py -- the single source of truth for which (category, rule type) pairs each
of the 4 policies carries.

Without this, the two can drift silently: build_seed_data.py's POLICIES table is what the seeded DB
and the labeled calibration examples are generated from, but the *_rules.md files a persona actually
cites are hand-authored prose, edited independently. A category added to one and not the other would
ship a persona that states a rule the DB/eval never enforces, or vice versa, with nothing to catch it.

The script lives in scripts/ (a runnable tool, not an importable package), so we load it by path,
mirroring tests/scripts/test_detect_task_leakage.py.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parents[3]
_TASK_DIR = _REPO / "tasks" / "compliance_review"
_SCRIPT = _TASK_DIR / "scripts" / "build_seed_data.py"
_RULES_DIR = _TASK_DIR / "user_sim_docs" / "common"

_SECTION_TO_RULE_TYPE = {
    "Required clauses": "must_have",
    "Prohibited clauses": "must_not_have",
    "Flag for human review": "needs_review",
}
_BULLET_CATEGORY = re.compile(r"^-\s+\*\*(.+?)\*\*")
_SECTION_HEADING = re.compile(r"^##\s+(.+)$")


def _load_policies() -> list[dict]:
    spec = importlib.util.spec_from_file_location("_build_seed_data_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.POLICIES


def _rules_from_markdown(path: pathlib.Path) -> set[tuple[str, str]]:
    """Parse a *_rules.md file's bullets into {(category, rule_type)}, by section heading."""
    rules: set[tuple[str, str]] = set()
    current_rule_type: str | None = None
    for line in path.read_text().splitlines():
        heading = _SECTION_HEADING.match(line)
        if heading:
            current_rule_type = _SECTION_TO_RULE_TYPE.get(heading.group(1).strip())
            continue
        bullet = _BULLET_CATEGORY.match(line)
        if bullet and current_rule_type:
            rules.add((bullet.group(1), current_rule_type))
    return rules


def test_every_policy_has_a_rules_file() -> None:
    policies = _load_policies()
    for policy in policies:
        rules_path = _RULES_DIR / f"{policy['id']}_rules.md"
        assert rules_path.is_file(), f"missing {rules_path} for policy {policy['id']!r}"


def test_rules_md_matches_the_policies_table() -> None:
    """The (category, rule type) pairs a persona can cite must be exactly the pairs
    build_seed_data.py's POLICIES table encodes -- neither a superset (a rule the DB/eval never
    enforces) nor a subset (an enforced rule no persona can state)."""
    policies = _load_policies()
    mismatches = []
    for policy in policies:
        expected = {(rule["category"], rule["type"]) for rule in policy["rules"]}
        rules_path = _RULES_DIR / f"{policy['id']}_rules.md"
        if not rules_path.is_file():
            continue  # reported by test_every_policy_has_a_rules_file
        actual = _rules_from_markdown(rules_path)
        if actual != expected:
            mismatches.append(
                f"{policy['id']}: doc has {actual - expected or '{}'} not in POLICIES, "
                f"POLICIES has {expected - actual or '{}'} not in the doc"
            )
    assert not mismatches, "\n".join(mismatches)
