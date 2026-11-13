"""Record-grounded test scenarios for the validation harness.

Per V-3/V-4/V-5, a scenario is NOT a scripted request. Each scenario here:
  * names a build fixture customer,
  * declares which verification fields the tester will supply (read from live records at run time,
    never fabricated -- V-4),
  * poses a realistic request built from that customer's record state (V-5),
  * describes the expected end-state effect in plain terms for the reviewer, and
  * declares a machine-checkable ``ExpectedSignature`` over the agent's OWN recorded tool calls.

The signature is deliberately expressed in CATEGORIES (how many verification records, how many
specialist calls/handoffs, whether escalation is expected, whether ANY specialist call is
forbidden) -- never by naming a specialist ``*_NNNN`` tool. The correct specific action is whatever
the KB procedure names (P3); the harness asserts counts and forbidden-ness, not identities, so it
stays P3-safe while still being deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExpectedSignature:
    """What the agent's recorded mutating calls should look like for a correct handling.

    All fields are asserted from ``RecordingToolClient`` (the agent's own calls), never from a DB
    read-back (R-20-safe). ``None`` on a count means "not asserted".
    """
    # Exactly one verification record per verified customer per conversation (R-6/R-7).
    verifications: int | None = 1
    # Number of specialist actions the agent should invoke via call_discoverable_agent_tool.
    specialist_calls: int | None = None
    # Number of customer-side handoffs via give_discoverable_user_tool.
    handoffs: int | None = None
    # Whether a transfer_to_human_agents is expected (R-15) ...
    expect_escalation: bool = False
    # ... and whether ANY specialist mutation is forbidden (DEF-1 / R-16 / R-18 negative controls):
    # the agent must take zero specialist actions/handoffs (e.g. escalate or answer only).
    forbid_specialist: bool = False


@dataclass(frozen=True)
class Scenario:
    key: str
    customer: str
    user_id: str
    # Human-readable description of the request and the expected end-state effect (for the report).
    request: str
    expected_effect: str
    expected: ExpectedSignature = field(default_factory=ExpectedSignature)
    # Verification fields the tester will supply. Values are pulled from the LIVE record at run
    # time (see harness) so we never hardcode/fabricate identity data (V-4).
    verify_fields: tuple[str, ...] = ("date_of_birth", "email")
    # Optional follow-up user turns (multi-turn conversations, e.g. supplying card details or
    # withdrawing a request). Each string becomes the next user turn after the opening.
    follow_ups: tuple[str, ...] = ()


# All six build fixtures, each with a record-grounded request and an expected recorded-call
# signature. These remain starting points; the harness reads live records before use (V-3).
SCENARIOS: list[Scenario] = [
    # --- Default happy path: one verification + one specialist action, nothing extra. ---
    Scenario(
        key="mia_activate_debit",
        customer="Mia Smith",
        user_id="123",
        request=(
            "I'd like to activate my new debit card that's awaiting activation. "
            "The card is the blue one on my account."
        ),
        expected_effect=(
            "One verification record for Mia; the debit-card activation the KB procedure names "
            "(routed by new-account reason); nothing else."
        ),
        expected=ExpectedSignature(verifications=1, specialist_calls=1, handoffs=0),
    ),
    # --- Positive control: purely informational -> zero mutations (R-18). ---
    Scenario(
        key="liu_status_only",
        customer="Liu Xiaoming",
        user_id="3d91a520be",
        request=(
            "Can you tell me the status of my replacement card order and whether the ATM fee "
            "I was charged was rebated? I just want a status update, don't change anything."
        ),
        expected_effect=(
            "One verification record for Liu (account read requires it); ZERO specialist "
            "mutations -- the replacement is already in progress and the ATM fee already rebated."
        ),
        expected=ExpectedSignature(verifications=1, forbid_specialist=True),
    ),
    # --- Positive control: dispute already open -> no second dispute (R-12/R-18). ---
    Scenario(
        key="emily_duplicate_dispute",
        customer="Emily Thompson",
        user_id="4db6fc36e8",
        request=(
            "There's a duplicate charge on my card and I want to dispute it. "
            "Please file the dispute for me."
        ),
        expected_effect=(
            "One verification record for Emily; the procedure's required lookup finds an OPEN "
            "dispute already exists, so NO new dispute is filed (R-12/R-18)."
        ),
        expected=ExpectedSignature(verifications=1, forbid_specialist=True),
    ),
    # --- Positive control: no KB procedure -> escalate, zero mutations (R-15). ---
    Scenario(
        key="alex_reopen_closed_card",
        customer="Alex Riviera",
        user_id="126",
        request=(
            "One of my credit cards is closed and I'd like it reopened. Can you reopen it?"
        ),
        expected_effect=(
            "One verification record for Alex; no KB procedure reopens a closed card, so zero "
            "specialist mutations and an escalation to a human agent (R-15)."
        ),
        expected=ExpectedSignature(
            verifications=1, forbid_specialist=True, expect_escalation=True
        ),
    ),
    # --- DEF-1 negative control: waiver blocked by nonzero balance + withdrawn closure. ---
    Scenario(
        key="bei_fee_waiver_ineligible",
        customer="Bei Chen",
        user_id="601e851551",
        request=(
            "I've been a loyal customer for years and I'm annoyed about the annual fee on my "
            "Business Silver card. I was thinking of closing it, but actually I want to keep it "
            "open -- just waive the annual fee for me."
        ),
        expected_effect=(
            "One verification record for Bei; the fee-waiver flag lives ONLY inside the Credit "
            "Card Retention Protocol, gated behind zero-balance closure eligibility and a logged "
            "closure reason. Bei has a large balance AND withdrew the closure, so the waiver is "
            "NOT authorized: zero specialist mutations. Escalating or offering a KB-supported "
            "alternative are both acceptable endings; applying the waiver is the failure."
        ),
        expected=ExpectedSignature(
            verifications=None, forbid_specialist=True, expect_escalation=False
        ),
    ),
    # --- DEF-2 control: customer-side handoff driven to completion (R-11/R-14). ---
    Scenario(
        key="maria_cashback_dispute",
        customer="Maria Elena Rodríguez",
        user_id="d97fb90fe8",
        request=(
            "I never got the cash back I was promised on a purchase. Please file the cash-back "
            "dispute for me."
        ),
        expected_effect=(
            "One verification record for Maria; the procedure names a CUSTOMER-SIDE dispute tool "
            "handed over via give_discoverable_user_tool and driven to completion (R-11/R-14)."
        ),
        expected=ExpectedSignature(verifications=1, handoffs=1),
    ),
]
