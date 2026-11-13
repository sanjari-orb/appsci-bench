"""banking_support's elicitation rubric: the shared soundness checks, run against the surface this task
actually exposes, plus its own traps.

WHY THIS DOES NOT CALL `assert_rubric_is_sound`, and please do not "fix" it by doing so. That helper's
leak scan reads `agent_visible_text`, which treats the whole of `tasks/<id>/environment/` as readable on
the stated grounds that the tree is bind-mounted into the agent's container. For this task it is not: the
orchestrator mounts only `environment/data` (at /data) and `environment/sop` (at /sop), and banking has
NEITHER. Banking's task environment is a separate container reached over HTTP, so the agent's whole
readable surface is the redacted spec plus whatever the tool API's replies tell it.

The difference is not cosmetic -- it is ~1.5 MB of source, including `environment/tools.py`, which names
every tool and whose `log_verification` docstring spells out the two-of-four rule. Scanned as if visible,
it fails legitimate items for material the agent provably cannot reach. The scan errs safe (it can only
reject, never wave a real leak through), so this is a false-failure problem rather than a hole.

AIRLINE HAS THE SAME SHAPE (no data/, no sop/), so this is a flaw in the shared helper rather than a
banking quirk; it is reported separately rather than patched here, because the fix changes behaviour for
the six tasks that call it. Every check the helper makes is reproduced below, using its own token and
phrase comparators so only the SURFACE differs.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from tests.tasks.conftest import (
    REPO,
    _MIN_TOKENS_FOR_LEAK_VERDICT,
    _normalise,
    _tokens,
    assert_items_feed_scored_objectives,
    readable_tokens,
    shared_phrases,
)

TASK = "banking_support"

#: Items allowed to rest only on objectives the task prices at zero. EMPTY on purpose: every item names
#: `task_success`, and each one carries a written argument for why -- that missing the fact makes the
#: artifact miss a golden action or perform one the golden replay does not have, either of which diverges
#: the state hash. That also makes the rubric robust to the open weighting question: `task_success` is
#: priced whether it carries 1.0 or 0.5, so no item's justification depends on which way that lands.
_UNPRICED_OK: tuple[str, ...] = ()

BUNDLE_CHARS = 300
BIG_BUNDLE_CHARS = 420


def _rubric():
    from appsci_bench.core.spec import load_task_spec
    from appsci_bench.eval.elicitation_rubric import load_rubric

    return load_rubric(load_task_spec(TASK))


def _spec() -> dict:
    return yaml.safe_load((REPO / "tasks" / TASK / "spec.yaml").read_text())


def agent_visible_text_for_banking() -> str:
    """Everything this task's agent can actually read: the redacted spec, plus /data and /sop if they
    existed. They do not, so in practice this is the redacted spec alone."""
    from appsci_bench.core.spec import build_agent_spec

    parts = [yaml.safe_dump(build_agent_spec(_spec()), sort_keys=False, allow_unicode=True)]
    for mounted in ("data", "sop"):
        root = REPO / "tasks" / TASK / "environment" / mounted
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".sql"}:
                    parts.append(path.read_text(errors="ignore"))
    return _normalise("\n".join(parts))


# --- the shared soundness checks, on the right surface -----------------------------------------

def test_the_rubric_document_validates_and_the_pointer_is_wired():
    from appsci_bench.eval.elicitation_rubric import validate_rubric_document

    pointer = (_spec().get("evaluation_data") or {}).get("elicitation_rubric")
    assert pointer, "spec declares no elicitation_rubric"
    assert validate_rubric_document(yaml.safe_load((REPO / pointer).read_text())) == []
    rubric = _rubric()
    assert rubric is not None and rubric.task_id == TASK


def test_every_item_feeds_a_real_and_priced_objective_of_this_task():
    from appsci_bench.eval.elicitation_rubric import validate_rubric

    spec = _spec()
    rubric = _rubric()
    assert validate_rubric(rubric, (spec.get("evaluation_data") or {}).get("objectives") or []) == []
    assert_items_feed_scored_objectives(TASK, rubric, unpriced_ok=_UNPRICED_OK)


def test_within_the_cap_with_unique_ids():
    from appsci_bench.eval.elicitation_rubric import MAX_ITEMS

    ids = [i.item_id for i in _rubric().items]
    assert 0 < len(ids) <= MAX_ITEMS
    assert len(set(ids)) == len(ids)


def test_no_items_values_are_all_readable_from_what_the_agent_was_given():
    visible = agent_visible_text_for_banking()
    leaked = []
    for item in _rubric().items:
        tokens = _tokens(item.fact)
        if (len(tokens) >= _MIN_TOKENS_FOR_LEAK_VERDICT
                and readable_tokens(item.fact, visible) == tokens):
            leaked.append((item.item_id, sorted(tokens)))
    assert leaked == [], f"these items measure spec-reading rather than asking: {leaked}"


def test_no_item_was_written_by_copying_the_agents_own_material():
    visible = agent_visible_text_for_banking()
    copied = [(i.item_id, p) for i in _rubric().items for p in shared_phrases(i.fact, visible)]
    assert copied == [], f"items share a long phrase with agent-readable text: {copied}"


def test_a_fact_is_thicker_than_its_own_label():
    for item in _rubric().items:
        assert len(item.fact) > len(item.item_id) * 3, f"{item.item_id}: fact is too thin to judge"


# --- bundling: items are judged all-or-nothing ------------------------------------------------

def test_every_bundled_item_states_its_own_completeness():
    import re

    bundles = [i for i in _rubric().items if len(i.fact) > BUNDLE_CHARS]
    assert bundles, "all single-fact items; the bundling check has nothing to check"
    for item in bundles:
        assert re.search(
            r"\b(?:exactly|all|both|whole|every|four|three|two|five|only|never)\b"
            r"|\bnothing else\b|\bno other\b|\bin order\b",
            item.fact, re.IGNORECASE), (
            f"{item.item_id}: this bundle does not bound itself -- state a count, enumerate the members, "
            f"or say what is excluded")


def test_the_biggest_bundles_carry_a_judge_hint():
    for item in _rubric().items:
        if len(item.fact) > BIG_BUNDLE_CHARS:
            assert item.judge_hint, f"{item.item_id} is a large bundle with no completeness hint"


# --- this task's own traps ---------------------------------------------------------------------

def test_the_verification_item_pins_the_two_of_four_threshold():
    """The single most consequential number in the rubric. An artifact that assumes ALL FOUR identity
    fields must match refuses customers it should have served -- and 81 of the 97 cases have a golden
    verification record, so that mistake alone zeroes five sixths of the dataset."""
    (item,) = [i for i in _rubric().items if "verification" in i.item_id]
    fact = item.fact.lower()
    assert "two of" in fact, "the two-of-four threshold is the item; it must be stated"
    for field in ("date of birth", "email", "phone number", "postal address"):
        assert field in fact, f"the verification item omits {field!r}"
    # The four field NAMES are recoverable from the tool's own parameters, so the hint must say the
    # threshold is the elicitable part rather than the field list.
    assert item.judge_hint and "two of" in item.judge_hint.lower()
    assert "recorded" in fact, "that the check must be RECORDED is part of the rule"


def test_the_sequence_item_pins_unlock_before_call():
    (item,) = [i for i in _rubric().items if "sequence" in i.item_id]
    fact = item.fact.lower()
    assert "cannot be called before it is unlocked" in fact
    for step in ("search", "read the name", "unlock", "call"):
        assert step in fact, f"the sequence item omits {step!r}"
    assert "guess" in fact, "that names cannot be guessed is what makes retrieval load-bearing"


def test_every_tool_name_the_rubric_states_actually_exists():
    """A rubric that names a tool wrongly teaches the simulator to hand the agent a name that 400s. This
    is the check that keeps the tool-name items honest, and it is why they are worth having as items at
    all: the names are the literal strings the tool route takes."""
    import re

    from tasks.banking_support.environment import BankingEnvironment
    from tasks.banking_support.environment._runtime.retrieval import build_retrieval_bundle
    from tasks.banking_support.environment.corpus import DocumentCorpus
    from tasks.banking_support.environment.data_model import TransactionalDB

    corpus = DocumentCorpus({"d": {"id": "d", "title": "t", "content": "c"}})
    env = BankingEnvironment(TransactionalDB(), corpus,
                             bundle=build_retrieval_bundle(corpus, enable_shell=False))
    real = set(env.agent_tools.tools) | set(env.user_tools.tools)

    named = set()
    for item in _rubric().items:
        if "tool_names" in item.item_id:
            named |= set(re.findall(r"\b[a-z][a-z0-9_]{6,}\b", item.fact))
    # Only judge the tokens that look like tool names, i.e. those the env would recognise or that end in
    # a tool-ish shape. A prose word like "specialist" must not be mistaken for a claimed tool.
    claimed = {n for n in named if n in real or n.startswith(("get_", "log_", "submit_", "apply_",
                                                             "change_", "unlock_", "call_", "give_",
                                                             "request_", "transfer_", "list_"))}
    assert claimed, "the tool-name items name no tools"
    missing = sorted(claimed - real)
    assert missing == [], f"the rubric names tools that do not exist: {missing}"


def test_no_item_restates_something_the_spec_already_hands_the_agent():
    """The agent-facing spec says grounding matters, that not every action is available up front, that the
    tools do not enforce policy, and names the retrieval tools. An item covering any of those would score
    every artifact that read its own spec."""
    facts = " ".join(i.fact for i in _rubric().items).lower()
    for handed in ("never make things up", "tools do not enforce policy", "no raw-corpus endpoint",
                   "kb_search_bm25", "kb_search_dense"):
        assert handed not in facts, f"an item restates handed material: {handed!r}"


def test_every_section_of_the_operating_rules_is_represented():
    """The rules were moved into the simulator section by section. A rubric covering verification but not
    escalation would report a high elicitation rate for an artifact that never asked about half of them."""
    facts = " ".join(i.fact for i in _rubric().items).lower()
    for section, needle in (
        ("verifying who you are talking to", "two of"),
        ("specialist actions: the sequence", "unlock"),
        ("specialist actions: customer-side", "customer performs it themselves"),
        ("when to escalate", "no applicable action"),
        ("actions the bank does not permit", "never authorizes"),
    ):
        assert needle in facts, f"no item covers {section!r}"


def test_the_reference_clock_item_says_to_read_it_rather_than_assume_it():
    """All 81 golden verification records carry one fixed timestamp, and the record's identity derives
    from it -- so an artifact using the host clock diverges the hash even when its verification logic is
    perfect."""
    (item,) = [i for i in _rubric().items if "reference_time" in i.item_id]
    assert "get_current_time" in item.fact
    assert "fixed" in item.fact.lower()
