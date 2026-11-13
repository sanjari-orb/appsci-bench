"""Tests for the LLM tool definitions built from the reference schema."""
from __future__ import annotations

import json
import pathlib

import policy
import tools

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def test_all_fourteen_tools_are_exposed():
    assert len(tools.llm_tools()) == 14


def test_every_tool_has_a_name_description_and_schema():
    for tool in tools.llm_tools():
        assert tool["name"]
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"


def test_write_tools_match_the_policy_guard():
    """The reference's own tool_type is the single source of truth for what must be guarded."""
    assert tools.write_tool_names() == policy.WRITE_TOOLS


def test_clarifying_notes_are_applied():
    """The pricing note matters: without it the model quotes a total that will not be charged."""
    by_name = {t["name"]: t for t in tools.llm_tools()}
    assert "COMPLETE new list" in by_name["update_reservation_flights"]["description"]
    assert "re-prices EVERY" in by_name["update_reservation_flights"]["description"]
    assert "no eligibility checking" in by_name["cancel_reservation"]["description"]


def test_artifact_copy_matches_the_sop_reference():
    """Guard against the artifact's copy of the schema drifting from the authoritative one."""
    sop = REPO_ROOT / "sop" / "tools_reference.json"
    if not sop.exists():
        return  # not available inside the deployed container; nothing to compare
    assert json.loads(tools.REFERENCE_PATH.read_text()) == json.loads(sop.read_text())
