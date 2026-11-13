"""Tool specifications advertised to the model.

CONFIRMED interfaces only. Every ToolSpec here has a name and argument schema established either from
the tool API's own documented validation feedback (the retrieval tools) or from the authoring-permitted
BASE-interface specification supplied by the channel operators (the BASE tools). Nothing here is
guessed.

Groups:
  * RETRIEVAL_TOOLS   -- KB search + read-only shell over the corpus (confirmed working).
  * BASE_READ_TOOLS   -- always-available lookups: customer records (by id / name / email), credit-card
                         accounts and transactions, referrals, the environment clock, and the
                         discoverable-tool listing.
  * BASE_ACTION_TOOLS -- always-available writes and the discovery mechanism: verification logging,
                         email change, human escalation, and the give / unlock / call discoverable-tool
                         trio.
  * AGENT_TOOLS       -- the full set advertised to the model (retrieval + BASE read + BASE action).

Specialist AGENT actions and customer-performed actions are DELIBERATELY not enumerated here. They are
discovered at runtime: search the KB (or list_discoverable_agent_tools) for the procedure -> read the
exact action name -> unlock_discoverable_agent_tool(name) -> read the parameter list the unlock returns
-> call_discoverable_agent_tool(name, arguments). We never hard-code specialist action names or schemas.

Transport (confirmed): POST {BANKING_TOOLS_URL}/<name> {"arguments": {...}} -> {"result": <string>}.
Record lookups return human-readable record text. Some tool failures arrive as a string beginning
"Error:" inside an HTTP 200 response; agent_core surfaces those to the model as errors, not success.

For give_discoverable_user_tool and call_discoverable_agent_tool, the inner "arguments" field is itself
a JSON-ENCODED STRING (e.g. "{\\"key\\": \\"value\\"}"), NOT a nested object. It defaults to "{}", so
the schemas below mark it optional and the descriptions state the string requirement explicitly.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agent_core import ToolSpec


def _string(description: str) -> Dict[str, Any]:
    return {"type": "string", "description": description}


def _object_schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


# ---------------------------------------------------------------------------------------------------
# Retrieval tools -- confirmed via the tool API's own empty-argument HTTP 400 validation feedback.
# ---------------------------------------------------------------------------------------------------

KB_SEARCH_BM25 = ToolSpec(
    name="KB_search_bm25",
    description=(
        "Full-text (BM25) keyword search over Rho-Bank's official knowledge base. Use this FIRST to "
        "find the policy, fee, eligibility rule, or documented procedure relevant to the customer's "
        "request before you answer or take any action. Returns matching knowledge-base passages."
    ),
    input_schema=_object_schema(
        {"query": _string("Natural-language or keyword search query.")},
        required=["query"],
    ),
)

SHELL = ToolSpec(
    name="shell",
    description=(
        "Run a READ-ONLY shell command (ls, cat, grep, find, head) over the knowledge-base corpus "
        "files to read a documented procedure verbatim -- for example to read the exact steps and "
        "required inputs of an action after a search locates the file. Read-only: it cannot modify "
        "anything or reach account data."
    ),
    input_schema=_object_schema(
        {"command": _string('A read-only shell command over the corpus, e.g. "grep -ril \'dispute\' kb".')},
        required=["command"],
    ),
)

# Confirmed to exist but reported UNAVAILABLE in the build env (its result says so); kept out of the
# default set so the model does not spend a turn on a dead tool. Re-add to AGENT_TOOLS if confirmed
# available at eval time.
KB_SEARCH_DENSE = ToolSpec(
    name="KB_search_dense",
    description=(
        "Semantic (dense-embedding) search over Rho-Bank's knowledge base. Complements BM25 for "
        "paraphrased queries. Returns matching knowledge-base passages."
    ),
    input_schema=_object_schema(
        {"query": _string("Natural-language search query.")},
        required=["query"],
    ),
)

# The confirmed, working retrieval tools advertised to the model by default.
RETRIEVAL_TOOLS: List[ToolSpec] = [KB_SEARCH_BM25, SHELL]


# ---------------------------------------------------------------------------------------------------
# BASE read tools -- always available; return human-readable record text. (Confirmed spec.)
# ---------------------------------------------------------------------------------------------------

GET_CURRENT_TIME = ToolSpec(
    name="get_current_time",
    description=(
        "Return the bank's current timestamp. Use it whenever you need the current time -- in "
        "particular, get the timestamp to record as 'time_verified' when you log an identity "
        "verification."
    ),
    input_schema=_object_schema({}, required=[]),
)

GET_USER_INFORMATION_BY_ID = ToolSpec(
    name="get_user_information_by_id",
    description=(
        "Look up a customer's full record by their user id. Returns the human-readable customer record "
        "(name, address, email, phone, date of birth, and account references) used to verify identity "
        "and to drive account actions."
    ),
    input_schema=_object_schema(
        {"user_id": _string("The customer's user id.")},
        required=["user_id"],
    ),
)

GET_USER_INFORMATION_BY_NAME = ToolSpec(
    name="get_user_information_by_name",
    description=(
        "Look up a customer's record by full name. Returns the human-readable customer record. Use "
        "when you have the customer's name but not their user id; a name may match more than one "
        "person, so confirm you have the right customer before acting."
    ),
    input_schema=_object_schema(
        {"customer_name": _string("The customer's full name.")},
        required=["customer_name"],
    ),
)

GET_USER_INFORMATION_BY_EMAIL = ToolSpec(
    name="get_user_information_by_email",
    description=(
        "Look up a customer's record by email address. Returns the human-readable customer record."
    ),
    input_schema=_object_schema(
        {"email": _string("The customer's email address.")},
        required=["email"],
    ),
)

GET_REFERRALS_BY_USER = ToolSpec(
    name="get_referrals_by_user",
    description=(
        "List the referrals associated with a customer, by user id. Returns the human-readable "
        "referral records."
    ),
    input_schema=_object_schema(
        {"user_id": _string("The customer's user id.")},
        required=["user_id"],
    ),
)

GET_CREDIT_CARD_TRANSACTIONS_BY_USER = ToolSpec(
    name="get_credit_card_transactions_by_user",
    description=(
        "List a customer's credit-card transactions by user id. Returns the human-readable transaction "
        "records. Use this to locate and read the AUTHORITATIVE details of a charge for a dispute: "
        "customer-supplied amount/date/card details only help you find the charge; the bank record is "
        "the source of truth."
    ),
    input_schema=_object_schema(
        {"user_id": _string("The customer's user id.")},
        required=["user_id"],
    ),
)

GET_CREDIT_CARD_ACCOUNTS_BY_USER = ToolSpec(
    name="get_credit_card_accounts_by_user",
    description=(
        "List a customer's credit-card accounts by user id. Returns the human-readable account records."
    ),
    input_schema=_object_schema(
        {"user_id": _string("The customer's user id.")},
        required=["user_id"],
    ),
)

LIST_DISCOVERABLE_AGENT_TOOLS = ToolSpec(
    name="list_discoverable_agent_tools",
    description=(
        "List the names of specialist AGENT actions that can be unlocked. These are NOT callable until "
        "you unlock them, and this list does not reveal their parameters -- unlock an action to see "
        "those. Which action applies to the customer's request should be driven by the knowledge-base "
        "procedure; this list only shows which action names exist."
    ),
    input_schema=_object_schema({}, required=[]),
)


# ---------------------------------------------------------------------------------------------------
# BASE action tools -- always available; writes + escalation + the discovery mechanism. (Confirmed.)
# ---------------------------------------------------------------------------------------------------

# Escalation reasons enumerated by the confirmed spec. Non-secret; encoded so the model picks a valid
# value. "reason" defaults to "other" server-side, so it is optional.
_TRANSFER_REASONS = [
    "fraud_or_security_concern",
    "account_closure_request",
    "deceased_account_holder",
    "legal_or_regulatory_matter",
    "account_ownership_dispute",
    "complex_billing_dispute",
    "abusive_customer_behavior",
    "third_party_inquiry",
    "technical_system_error",
    "unconfirmed_external_communication",
    "customer_demands_after_unavailable_offer_refusal",
    "kb_search_unsuccessful_customer_requests_transfer",
    "specialized_department_required",
    "accessibility_or_special_needs",
    "customer_frustrated_demands_human",
    "supervisor_request_service_complaint",
    "customer_requests_human_no_specific_reason",
    "request_completed_customer_wants_human_followup",
    "other",
]

CHANGE_USER_EMAIL = ToolSpec(
    name="change_user_email",
    description=(
        "Change a customer's email address on their record. This is an account action: perform it only "
        "when the customer has explicitly requested it AND you have verified their identity (at least "
        "two identity fields matched against the record and recorded via log_verification). Confirm the "
        "change is reflected in the record afterward before telling the customer it is done."
    ),
    input_schema=_object_schema(
        {
            "user_id": _string("The customer's user id."),
            "new_email": _string("The new email address to set on the record."),
        },
        required=["user_id", "new_email"],
    ),
)

LOG_VERIFICATION = ToolSpec(
    name="log_verification",
    description=(
        "Record that you verified a customer's identity, BEFORE taking any account action on their "
        "behalf. First look up the customer's record and confirm that at least TWO of {date of birth, "
        "email, phone number, postal address} the customer provides match the record. Then call this "
        "with all seven fields taken from the looked-up record, and 'time_verified' set to the current "
        "timestamp from get_current_time. Do not take an account action without a recorded "
        "verification; skip verification only for general product/informational questions that need no "
        "account action."
    ),
    input_schema=_object_schema(
        {
            "name": _string("The customer's full name, from the record."),
            "user_id": _string("The customer's user id."),
            "address": _string("The customer's postal address, from the record."),
            "email": _string("The customer's email, from the record."),
            "phone_number": _string("The customer's phone number, from the record."),
            "date_of_birth": _string("The customer's date of birth, from the record."),
            "time_verified": _string("The current timestamp, from get_current_time."),
        },
        required=["name", "user_id", "address", "email", "phone_number", "date_of_birth", "time_verified"],
    ),
)

TRANSFER_TO_HUMAN_AGENTS = ToolSpec(
    name="transfer_to_human_agents",
    description=(
        "Escalate the conversation to a human agent. Use ONLY when a genuine knowledge-base search "
        "finds nothing that applies to the customer's request, or when the situation matches one of "
        "the listed reasons. Provide a concise summary of the conversation and the single "
        "best-matching reason. Do not escalate merely because an informational answer has no action "
        "procedure -- if the KB answers the question, answer it."
    ),
    input_schema=_object_schema(
        {
            "summary": _string("A concise summary of the conversation and why a human is needed."),
            "reason": {
                "type": "string",
                "enum": list(_TRANSFER_REASONS),
                "description": "The single best-matching escalation reason. Defaults to 'other'.",
            },
        },
        required=["summary"],
    ),
)

GIVE_DISCOVERABLE_USER_TOOL = ToolSpec(
    name="give_discoverable_user_tool",
    description=(
        "Give the customer a discoverable USER tool so they can perform a customer-performed action "
        "themselves, when the knowledge-base procedure identifies the action as one the customer "
        "performs. Provide the tool name and its arguments as a JSON-ENCODED STRING (e.g. "
        '"{\\"key\\": \\"value\\"}"), NOT a nested object; omit arguments to send "{}". After giving '
        "the tool, guide the customer and confirm completion in the records before claiming success."
    ),
    input_schema=_object_schema(
        {
            "discoverable_tool_name": _string("The exact name of the discoverable user tool to give."),
            "arguments": _string(
                'The tool arguments as a JSON-encoded string (e.g. "{\\"key\\": \\"value\\"}"). '
                'Defaults to "{}".'
            ),
        },
        required=["discoverable_tool_name"],
    ),
)

UNLOCK_DISCOVERABLE_AGENT_TOOL = ToolSpec(
    name="unlock_discoverable_agent_tool",
    description=(
        "Unlock a specialist AGENT action by name so it becomes callable. Returns a text description "
        "that INCLUDES the action's parameters. First find the correct action name from the "
        "knowledge-base procedure (or list_discoverable_agent_tools), unlock it, then read the returned "
        "parameter list to construct the call. Being able to unlock a tool is not permission to use it: "
        "follow the procedure and verify identity where the procedure requires it."
    ),
    input_schema=_object_schema(
        {"agent_tool_name": _string("The exact name of the specialist agent action to unlock.")},
        required=["agent_tool_name"],
    ),
)

CALL_DISCOVERABLE_AGENT_TOOL = ToolSpec(
    name="call_discoverable_agent_tool",
    description=(
        "Call a specialist AGENT action you have already unlocked. Provide the action name and its "
        "arguments as a JSON-ENCODED STRING (e.g. "
        '"{\\"charge_id\\": \\"...\\"}"), NOT a nested object -- build the arguments from the parameter '
        'list returned by unlock_discoverable_agent_tool; omit arguments to send "{}". Returns the '
        "action's result; confirm the intended outcome is reflected in the records before telling the "
        "customer it is done."
    ),
    input_schema=_object_schema(
        {
            "agent_tool_name": _string("The exact name of the unlocked specialist agent action to call."),
            "arguments": _string(
                'The action arguments as a JSON-encoded string (e.g. "{\\"charge_id\\": \\"...\\"}"). '
                'Defaults to "{}".'
            ),
        },
        required=["agent_tool_name"],
    ),
)


BASE_READ_TOOLS: List[ToolSpec] = [
    GET_CURRENT_TIME,
    GET_USER_INFORMATION_BY_ID,
    GET_USER_INFORMATION_BY_NAME,
    GET_USER_INFORMATION_BY_EMAIL,
    GET_REFERRALS_BY_USER,
    GET_CREDIT_CARD_TRANSACTIONS_BY_USER,
    GET_CREDIT_CARD_ACCOUNTS_BY_USER,
    LIST_DISCOVERABLE_AGENT_TOOLS,
]

BASE_ACTION_TOOLS: List[ToolSpec] = [
    CHANGE_USER_EMAIL,
    LOG_VERIFICATION,
    TRANSFER_TO_HUMAN_AGENTS,
    GIVE_DISCOVERABLE_USER_TOOL,
    UNLOCK_DISCOVERABLE_AGENT_TOOL,
    CALL_DISCOVERABLE_AGENT_TOOL,
]

# The complete set advertised to the model: confirmed retrieval + BASE read + BASE action tools.
# (KB_search_dense is intentionally excluded -- unavailable in the build env; see above.)
AGENT_TOOLS: List[ToolSpec] = RETRIEVAL_TOOLS + BASE_READ_TOOLS + BASE_ACTION_TOOLS
