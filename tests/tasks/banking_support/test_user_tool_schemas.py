"""The signatures the eval-time customer simulator is given must match the tools it will actually call.

Each eval sample's `multi_turn_input.user_tools` carries the signature of every customer-held tool that
case grants, so the simulator knows what arguments to pass. Those signatures are a SECOND copy of what
`environment/tools.py` defines, and this module is the guard that keeps the two in step -- §4 of
KNOWN_ISSUES.md records what unguarded duplication has already cost this task once. The duplication is
now spread across 97 files rather than concentrated in one, which makes the guard more load-bearing, not
less: a single wrong signature is a single quietly unwinnable case.

Why it matters more than a normal drift check: a wrong or missing signature is SILENT. The simulator is
instructed not to invent values, so it either declines to act -- and the case fails as though the
artifact gave bad advice -- or it guesses a parameter name and the environment rejects the call with a
400. Neither reaches the score as "the harness could not measure this". Measured on
banking_support_b1.t0_l3_20260910_031639, before signatures were supplied: 3 of 3 on the tool taking no
arguments, 1 of 6 on the two-argument one whose values are spoken aloud, 0 of 19 on the three-argument
one.
"""
from __future__ import annotations

import inspect
import json

import pytest

from tests.tasks.conftest import REPO

TASK = REPO / "tasks" / "banking_support"


@pytest.fixture(scope="module")
def samples() -> list[tuple[str, dict]]:
    """(case id, sample document) for every eval case."""
    out = []
    for path in sorted((TASK / "eval_data").glob("*/sample.json")):
        out.append((path.parent.name, json.loads(path.read_text())))
    assert out, "no eval samples found"
    return out


@pytest.fixture(scope="module")
def declared_by_case(samples) -> dict[str, dict[str, dict]]:
    """Per case, the customer tools it grants keyed by name -> the entry as declared in the sample."""
    return {
        case: {t["name"]: t for t in (doc.get("multi_turn_input") or {}).get("user_tools") or []}
        for case, doc in samples
    }


@pytest.fixture(scope="module")
def declared(declared_by_case) -> dict[str, dict]:
    """Every distinct signature declared anywhere in the dataset, keyed by tool name."""
    out: dict[str, dict] = {}
    for tools in declared_by_case.values():
        out.update(tools)
    return out


def _user_toolkit_methods() -> dict[str, list[str]]:
    """Every public method on the environment's USER toolkit, with its argument names."""
    from tasks.banking_support.environment.tools import KnowledgeUserTools  # noqa: PLC0415

    out = {}
    for name, fn in inspect.getmembers(KnowledgeUserTools, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        args = [p for p in inspect.signature(fn).parameters if p != "self"]
        out[name] = args
    return out


def test_the_samples_declare_the_customer_tools(declared):
    assert declared, "no sample declares a customer-held tool"


def test_the_declared_set_covers_the_tool_the_discoverable_cases_need(declared):
    """Scoped to what a case can GRANT, deliberately.

    The obvious check -- every method on the user toolkit has a declared signature -- is the wrong
    property here, and asserting it fails on twelve names that are all correct: eight are ToolKitBase
    plumbing (`use_tool`, `has_tool`, `query_database`, ...) and four are DISCOVERABLE customer tools
    (`deposit_check_3847`, `submit_cash_back_dispute_0589`, `get_card_last_4_digits`,
    `get_referral_link`). Discoverable tools are undocumented on purpose: the agent hands one over at
    run time with `give_discoverable_user_tool`, and the customer then reaches it through
    `call_discoverable_user_tool` -- whose OWN signature is declared. Documenting them up front would
    publish the specialist surface the task exists to make the agent discover.
    """
    assert "call_discoverable_user_tool" in declared, (
        "call_discoverable_user_tool has no declared signature, so the cases whose golden action goes "
        "through it cannot be acted on even after the agent hands the tool over")


def test_no_declared_signature_names_a_tool_that_does_not_exist(declared):
    """The opposite drift: a signature for a tool the environment removed. The simulator would fill its
    arguments confidently and the call would 400."""
    unknown = sorted(set(declared) - set(_user_toolkit_methods()))
    assert not unknown, f"samples declare customer tools that do not exist: {unknown}"


def test_the_declared_arguments_match_the_real_ones(declared):
    """Names, not just counts. A parameter the simulator is told to send under the wrong name is
    rejected exactly like an invented one, and this is the check that would have caught the original
    defect had the signatures been supplied but wrong."""
    real = _user_toolkit_methods()
    wrong = {}
    for name, entry in declared.items():
        got = set(((entry.get("parameters") or {}).get("properties") or {}))
        actual = set(real[name])
        if got != actual:
            wrong[name] = {"declared": sorted(got), "actual": sorted(actual)}
    assert not wrong, f"declared arguments disagree with the toolkit: {wrong}"


def test_every_required_argument_is_a_real_parameter(declared):
    real = _user_toolkit_methods()
    bad = {}
    for name, entry in declared.items():
        required = set(((entry.get("parameters") or {}).get("required") or []))
        extra = required - set(real[name])
        if extra:
            bad[name] = sorted(extra)
    assert not bad, f"required arguments that the tool does not accept: {bad}"


def test_the_same_tool_is_declared_identically_in_every_case(declared_by_case):
    """The cost of moving the signatures into the samples: 97 copies can disagree with each other, not
    just with the toolkit. One case describing `apply_for_credit_card` differently from the rest is a
    single silently unwinnable case, which is the hardest kind of defect to see in an aggregate score.
    """
    seen: dict[str, tuple[str, str]] = {}
    conflicts = []
    for case, tools in sorted(declared_by_case.items()):
        for name, entry in tools.items():
            fingerprint = json.dumps(entry, sort_keys=True)
            first = seen.setdefault(name, (case, fingerprint))
            if first[1] != fingerprint:
                conflicts.append({"tool": name, "cases": [first[0], case]})
    assert not conflicts, f"the same customer tool is declared differently across cases: {conflicts}"


def test_every_user_tool_a_case_grants_has_a_signature(declared_by_case, samples):
    """The end-to-end property, per case rather than per dataset. A case granting a tool with no
    signature is a case the simulator cannot act on unless that tool takes no arguments -- which is
    exactly how 49 cases came to be described as 'unwinnable'.

    Read from the ORACLE's own `user_tools`, so this catches a sample whose granted list and whose
    signatures fell out of step, not merely one that declares nothing.
    """
    missing = {}
    for case, doc in samples:
        oracle = json.loads(doc["expected_output"]["json"]).get("user_tools") or []
        gap = sorted(set(oracle) - set(declared_by_case[case]))
        if gap:
            missing[case] = gap
    assert not missing, f"cases grant customer tools their sample declares no signature for: {missing}"


def test_the_spec_no_longer_points_at_a_task_level_schema_file():
    """The signatures used to live behind `task_runner.user_tool_schemas`. task_runner also starts the
    BUILD phase's environment, which has no customer simulator, so the field was carried through a phase
    that could never read it. Pinned so it does not come back by habit.
    """
    import yaml  # noqa: PLC0415

    runner = yaml.safe_load((TASK / "spec.yaml").read_text()).get("task_runner") or {}
    assert "user_tool_schemas" not in runner, (
        "user_tool_schemas is back in task_runner -- the signatures belong in each sample's "
        "multi_turn_input.user_tools, beside every other per-case fact about that simulator")
