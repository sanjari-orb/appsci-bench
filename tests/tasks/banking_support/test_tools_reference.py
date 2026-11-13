"""The tools reference the simulator holds: it must describe the action space the environment has.

`user_sim_docs/common/` is loaded for EVERY persona with no declaration needed, so a file placed there is
what the stakeholder can answer from. That makes accuracy load-bearing: the tool names and argument lists
are sim-only material the agent has to ask for, and a wrong entry teaches the stakeholder to hand over a
name that 400s -- worse than withholding it, because the agent cannot tell a bad answer from its own
mistake.

Deliberately independent of the elicitation rubric: these hold whether or not the rubric scores the names.
"""
from __future__ import annotations

import json
import pathlib
import re

from tests.tasks.conftest import REPO

TASK = "banking_support"
_SIM_COPY = REPO / "tasks" / TASK / "user_sim_docs" / "common" / "tools_reference.json"
_CURATED = REPO / "tasks" / TASK / "seed_data" / "tools_reference.json"


def _reference(path: pathlib.Path = _SIM_COPY) -> list[dict]:
    """Parse the reference, tolerating leading `//` comment lines.

    The sim copy carries a comment header telling the stakeholder it DOES hold this catalogue and must
    not deflect tool questions as "not my area" -- the deflection observed on
    banking_support_b0_l3_20260913_050312, where the agent asked for the tool names outright and was
    refused by a persona that had this very file in its prompt. The header works because the simulator
    receives sim-only documents as RAW TEXT (`persona._load_doc_content` reads and never parses), so a
    JSON comment reaches it verbatim. This loader is the only consumer that parses the file, so it is
    the only place that has to know the header is not data.

    Comments are stripped rather than the file being made JSON-legal on purpose: an instruction to the
    role-player is not part of the action space, and a `_comment` key would show up in every consumer
    that iterates entries.
    """
    lines = path.read_text().splitlines()
    body = [ln for ln in lines if not ln.lstrip().startswith("//")]
    return json.loads("\n".join(body))


def _name(entry: dict) -> str | None:
    return (entry.get("function") or {}).get("name")


def _live_tools() -> dict[str, set[str]]:
    from tasks.banking_support.environment import BankingEnvironment
    from tasks.banking_support.environment._runtime.retrieval import build_retrieval_bundle
    from tasks.banking_support.environment.corpus import DocumentCorpus
    from tasks.banking_support.environment.data_model import TransactionalDB

    corpus = DocumentCorpus({"d": {"id": "d", "title": "t", "content": "c"}})
    env = BankingEnvironment(TransactionalDB(), corpus,
                             bundle=build_retrieval_bundle(corpus, enable_shell=False))
    return {"agent": set(env.agent_tools.tools), "user": set(env.user_tools.tools)}


def test_the_simulator_can_answer_about_the_base_action_space():
    named = {_name(e) for e in _reference()}
    assert named, "the tools reference is empty"
    for expected in ("log_verification", "unlock_discoverable_agent_tool",
                     "call_discoverable_agent_tool", "give_discoverable_user_tool",
                     "transfer_to_human_agents", "get_current_time", "apply_for_credit_card"):
        assert expected in named, f"the simulator cannot answer about {expected!r}"


def test_the_reference_documents_no_tool_the_environment_lacks():
    """This is the check that caught `grep` surviving in the reference after it was dropped from the tool
    surface for `all_tools` parity."""
    real = _live_tools()
    phantom = [(_name(e), e.get("role")) for e in _reference()
               if _name(e) not in real.get(e.get("role"), set())]
    assert phantom == [], f"the reference documents tools the environment does not have: {phantom}"


def test_the_reference_omits_the_specialist_tools():
    """The ~48 specialist actions must NOT be listed: the knowledge base is the only route to their names,
    and a stakeholder who could recite them would collapse the retrieval mechanic entirely."""
    leaked = sorted(n for n in (_name(e) for e in _reference()) if n and re.search(r"_\d{4}$", n))
    assert leaked == [], f"specialist tools leaked into the reference: {leaked}"


def test_every_documented_tool_declares_a_role_and_parameters():
    for entry in _reference():
        name = _name(entry)
        assert entry.get("role") in {"agent", "user"}, f"{name}: role must be agent or user"
        assert entry.get("tool_type"), f"{name}: no tool_type"
        fn = entry["function"]
        assert fn.get("description"), f"{name}: no description for the stakeholder to answer from"
        assert "parameters" in fn, f"{name}: no parameter list, which is half of what is elicited"


def test_the_two_copies_are_identical():
    """`seed_data/` keeps the curated provenance copy; `user_sim_docs/common/` is the one a run reads.
    Drift means the audit record and the simulator disagree about the action space."""
    assert _reference(_CURATED) == _reference(_SIM_COPY)
