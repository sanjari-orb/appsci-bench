"""Tests for tool_specs: the confirmed BASE + retrieval + discovery ToolSpec catalog.

These pin the advertised tool set to exactly the interfaces confirmed by the channel operators' spec
(and the confirmed retrieval tools), so no guessed BASE/specialist tool can silently creep in, and the
spec's specific requirements are honored: seven required fields on log_verification, the 19 escalation
reasons, no-argument tools with empty schemas, and the JSON-encoded-STRING "arguments" on the give/call
discoverable-tool pair (optional, since it defaults to "{}").
"""
from __future__ import annotations

import tool_specs
from agent_core import ToolSpec

# The exact set the operators' spec confirmed as BASE, plus the two confirmed retrieval tools.
# KB_search_dense is deliberately excluded (unavailable in the build env).
EXPECTED_AGENT_TOOL_NAMES = {
    # retrieval
    "KB_search_bm25",
    "shell",
    # BASE read
    "get_current_time",
    "get_user_information_by_id",
    "get_user_information_by_name",
    "get_user_information_by_email",
    "get_referrals_by_user",
    "get_credit_card_transactions_by_user",
    "get_credit_card_accounts_by_user",
    "list_discoverable_agent_tools",
    # BASE action
    "change_user_email",
    "log_verification",
    "transfer_to_human_agents",
    "give_discoverable_user_tool",
    "unlock_discoverable_agent_tool",
    "call_discoverable_agent_tool",
}


def test_agent_tools_are_exactly_the_confirmed_set():
    # Guardrail: exactly the confirmed tools, nothing guessed, nothing missing.
    names = {t.name for t in tool_specs.AGENT_TOOLS}
    assert names == EXPECTED_AGENT_TOOL_NAMES


def test_no_specialist_action_names_are_hardcoded():
    # The discovery pair is present, but no specific specialist/customer action is enumerated as its
    # own ToolSpec -- those are unlocked/called by name at runtime.
    names = {t.name for t in tool_specs.AGENT_TOOLS}
    assert "unlock_discoverable_agent_tool" in names
    assert "call_discoverable_agent_tool" in names
    # A handful of plausible specialist actions must NOT appear as advertised tools.
    for forbidden in ("file_dispute", "open_savings_account", "dispute_transaction", "close_account"):
        assert forbidden not in names


def test_dense_search_excluded_from_agent_tools():
    names = {t.name for t in tool_specs.AGENT_TOOLS}
    assert "KB_search_dense" not in names
    # ...but it is still defined for re-enabling if confirmed available at eval time.
    assert tool_specs.KB_SEARCH_DENSE.name == "KB_search_dense"


def test_every_spec_has_a_valid_object_schema():
    for tool in tool_specs.AGENT_TOOLS:
        anth = tool.to_anthropic()
        assert set(anth) == {"name", "description", "input_schema"}
        assert anth["name"] and isinstance(anth["name"], str)
        assert anth["description"].strip()
        schema = anth["input_schema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        assert isinstance(schema["required"], list)
        # Every required field must actually be a declared property.
        assert set(schema["required"]).issubset(set(schema["properties"]))


def test_no_argument_tools_have_empty_schemas():
    for name in ("get_current_time", "list_discoverable_agent_tools"):
        spec = next(t for t in tool_specs.AGENT_TOOLS if t.name == name)
        assert spec.input_schema["properties"] == {}
        assert spec.input_schema["required"] == []


def test_log_verification_requires_all_seven_fields():
    spec = tool_specs.LOG_VERIFICATION
    expected = {"name", "user_id", "address", "email", "phone_number", "date_of_birth", "time_verified"}
    assert set(spec.input_schema["properties"]) == expected
    assert set(spec.input_schema["required"]) == expected


def test_transfer_reason_is_optional_enum_with_all_reasons():
    spec = tool_specs.TRANSFER_TO_HUMAN_AGENTS
    props = spec.input_schema["properties"]
    assert spec.input_schema["required"] == ["summary"]        # reason has a default -> optional
    assert "other" in props["reason"]["enum"]
    assert len(props["reason"]["enum"]) == 19
    # A few representative reasons the spec listed.
    for reason in ("fraud_or_security_concern", "complex_billing_dispute", "deceased_account_holder"):
        assert reason in props["reason"]["enum"]


def test_discovery_pair_arguments_are_optional_json_strings():
    for name in ("give_discoverable_user_tool", "call_discoverable_agent_tool"):
        spec = next(t for t in tool_specs.AGENT_TOOLS if t.name == name)
        props = spec.input_schema["properties"]
        # arguments is a STRING (JSON-encoded), not a nested object, and is optional.
        assert props["arguments"]["type"] == "string"
        assert "arguments" not in spec.input_schema["required"]
        assert "JSON-encoded string" in props["arguments"]["description"]


def test_give_and_call_require_the_tool_name():
    assert tool_specs.GIVE_DISCOVERABLE_USER_TOOL.input_schema["required"] == ["discoverable_tool_name"]
    assert tool_specs.CALL_DISCOVERABLE_AGENT_TOOL.input_schema["required"] == ["agent_tool_name"]
    assert tool_specs.UNLOCK_DISCOVERABLE_AGENT_TOOL.input_schema["required"] == ["agent_tool_name"]


def test_change_user_email_requires_user_id_and_new_email():
    spec = tool_specs.CHANGE_USER_EMAIL
    assert set(spec.input_schema["required"]) == {"user_id", "new_email"}


def test_all_specs_are_toolspec_instances():
    assert all(isinstance(t, ToolSpec) for t in tool_specs.AGENT_TOOLS)
