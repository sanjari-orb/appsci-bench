#!/usr/bin/env python
"""Tests for the compliance review artifact.

Organised around the 22 policy-rule assignments (8 must_have + 10 must_not_have + 4 needs_review).
Each assignment is a (policy, category) pair, and each is exercised through the HTTP contract rather
than the detector directly, so the tests measure what the review queue would actually receive.

The false-clean probes cover the 18 violation assignments only: a document whose single hit is a
needs_review routing flag correctly returns compliant: true, so probing it would assert the wrong
thing. The probes are a finite corpus -- they establish no false clean *on the wordings tested* and
report measured coverage, not a general guarantee (see test_zz_report_probe_coverage).
"""
from __future__ import annotations

import base64
import json
import logging
import statistics
import time

import pytest
from fastapi.testclient import TestClient

import detect
import escalate
import server
from policies import MUST_HAVE, MUST_NOT_HAVE, NEEDS_REVIEW, POLICIES

client = TestClient(server.app, raise_server_exceptions=False)

VERDICT_KEYS = {"compliant", "violations", "needs_review"}


@pytest.fixture
def anyio_backend():
    """asyncio only: the artifact has no trio dependency."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Corpus. Deliberately plain boilerplate: no sentence here may carry a signal for any category,
# or the "clean document must not 422" tests would be testing the filler instead of the logic.
# ---------------------------------------------------------------------------

HEADER = "MASTER SERVICES AGREEMENT\n"
FILLER = (
    "This Agreement is entered into by Buyer and Vendor as of the Effective Date.\n"
    "Vendor shall provide the Services described in Exhibit A.\n"
    "Vendor shall invoice Buyer monthly and Buyer shall pay each invoice within thirty days.\n"
    "Notices shall be delivered to the addresses stated on the signature page.\n"
)

# must_have: canonical wording, a paraphrase, a clean negation, and an ambiguous-only signal.
MUST_HAVE_CORPUS: dict[str, dict[str, str]] = {
    "Cap On Liability": {
        "canonical": "Vendor's aggregate liability under this Agreement shall not exceed the total "
                     "fees paid in the twelve months preceding the claim.",
        "paraphrase": "In no event shall either party's total liability for any claim exceed the "
                      "amounts actually paid under this Agreement.",
        "negated": "The limitation of liability in this Section shall not apply.",
        "ambiguous": "12. LIMITATION OF LIABILITY\n",
    },
    "Audit Rights": {
        "canonical": "Buyer may audit Vendor's books and records upon thirty days' written notice.",
        "paraphrase": "Vendor shall permit Buyer to inspect the invoices supporting each charge.",
        "negated": "Buyer shall have no right to audit or inspect Vendor's records.",
        "ambiguous": "9. AUDIT\n",
    },
    "Insurance": {
        "canonical": "Vendor shall maintain commercial general liability insurance with limits of "
                     "not less than $5,000,000 per occurrence.",
        "paraphrase": "Certificates of insurance naming Buyer as an additional insured shall be "
                      "delivered before the Effective Date.",
        "negated": "Vendor shall not be required to maintain any insurance for the Services.",
        "ambiguous": "14. INSURANCE\n",
    },
    "Governing Law": {
        "canonical": "This Agreement shall be governed by the laws of the State of New York.",
        "paraphrase": "The rights of the parties shall be construed in accordance with the laws of "
                      "Delaware.",
        "negated": "This Agreement is not governed by the laws of any particular jurisdiction.",
        "ambiguous": "18. GOVERNING LAW\n",
    },
    "License Grant": {
        "canonical": "Licensor hereby grants Licensee a worldwide, royalty-free licence to use the "
                     "Software.",
        "paraphrase": "Licensee is granted a licence to reproduce and distribute the Documentation.",
        "negated": "No licence is hereby granted to Licensee under this Agreement.",
        "ambiguous": "3. LICENSE\n",
    },
    "Ip Ownership Assignment": {
        "canonical": "Vendor hereby assigns to Buyer all intellectual property in the Deliverables.",
        "paraphrase": "The Deliverables are works made for hire prepared at Buyer's direction.",
        "negated": "Vendor assigns nothing and retains all right, title, and interest in the Works.",
        "ambiguous": "7. INTELLECTUAL PROPERTY\n",
    },
}

# must_not_have: a present clause, a paraphrase of it, a clean negation, an ambiguous-only signal.
MUST_NOT_HAVE_CORPUS: dict[str, dict[str, str]] = {
    "Uncapped Liability": {
        "present": "Vendor shall have unlimited liability for any breach of this Agreement.",
        "paraphrase": "Vendor's liability for third-party claims is not subject to any monetary cap.",
        "negated": "In no event shall Vendor have unlimited liability.",
        "ambiguous": "Support obligations are uncapped under the Service Levels.",
    },
    "Liquidated Damages": {
        "present": "Vendor shall pay liquidated damages of $500 per day of delay.",
        "paraphrase": "Vendor shall pay stipulated damages in the amount of $10,000 for each late "
                      "milestone.",
        "negated": "No liquidated damages shall be payable for any delay.",
        "ambiguous": "A penalty may be assessed under the Service Level Schedule.",
    },
    "Most Favored Nation": {
        "present": "Buyer shall receive most favored nation pricing for all Services.",
        "paraphrase": "Vendor shall ensure its prices are at least as favorable as those offered to "
                      "any other customer.",
        "negated": "No most favored nation pricing applies to this Agreement.",
        "ambiguous": "Pricing shall be reviewed against benchmark pricing annually.",
    },
    "Non-Compete": {
        "present": "Vendor shall not compete with Buyer in the Territory during the Term.",
        "paraphrase": "Supplier is restricted from engaging in any competing business for two years.",
        "negated": "No non-compete applies and each party is free to compete.",
        "ambiguous": "Buyer shall not solicit or hire Vendor's employees during the Term.",
    },
    "Exclusivity": {
        "present": "Vendor shall be the exclusive supplier of the Products to Buyer.",
        "paraphrase": "Buyer shall purchase all of its requirements for the Products from Vendor.",
        "negated": "Vendor is a non-exclusive supplier and Buyer may source elsewhere.",
        "ambiguous": "Vendor shall use the Marks exclusively for the purposes of this Agreement.",
    },
    "Minimum Commitment": {
        "present": "Buyer commits to purchase a minimum annual volume of 10,000 units.",
        "paraphrase": "Buyer shall purchase at least 5,000 units in each contract year.",
        "negated": "There are no minimum commitments under this Agreement.",
        "ambiguous": "Buyer shall provide non-binding forecasts each quarter.",
    },
    "Joint Ip Ownership": {
        "present": "The parties shall jointly own all Foreground IP.",
        "paraphrase": "Each party shall hold an undivided one-half interest in the Improvements.",
        "negated": "No joint ownership of intellectual property is created by this Agreement.",
        "ambiguous": "The parties may jointly develop new features under a separate statement of "
                     "work.",
    },
    "Irrevocable Or Perpetual License": {
        "present": "Buyer is granted a perpetual, irrevocable licence to the Software.",
        "paraphrase": "The licence granted under Section 3 continues in perpetuity.",
        "negated": "The licence is revocable and terminable at will.",
        "ambiguous": "The rights granted in this Section are perpetual.",
    },
}

# needs_review: routing flags. Never a fault, so only presence and absence matter.
NEEDS_REVIEW_CORPUS: dict[str, dict[str, str]] = {
    "Covenant Not To Sue": {
        "present": "Buyer covenants not to sue Vendor in respect of the Background IP.",
    },
    "Change Of Control": {
        "present": "Vendor shall notify Buyer of any change of control.",
    },
    "Volume Restriction": {
        "present": "Buyer's usage is subject to a volume limit of 10,000 transactions per month.",
    },
    "Source Code Escrow": {
        "present": "Vendor shall deposit the source code in escrow with a third-party agent.",
    },
}

ASSIGNMENTS = [
    (policy, kind, category)
    for policy, rules in POLICIES.items()
    for kind in (MUST_HAVE, MUST_NOT_HAVE, NEEDS_REVIEW)
    for category in rules[kind]
]
MUST_HAVE_ASSIGNMENTS = [(p, c) for p, k, c in ASSIGNMENTS if k == MUST_HAVE]
MUST_NOT_HAVE_ASSIGNMENTS = [(p, c) for p, k, c in ASSIGNMENTS if k == MUST_NOT_HAVE]
NEEDS_REVIEW_ASSIGNMENTS = [(p, c) for p, k, c in ASSIGNMENTS if k == NEEDS_REVIEW]
VIOLATION_ASSIGNMENTS = MUST_HAVE_ASSIGNMENTS + MUST_NOT_HAVE_ASSIGNMENTS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build(policy: str, omit: tuple[str, ...] = (), extra: tuple[str, ...] = (),
          replace: dict[str, str] | None = None) -> str:
    """A document satisfying every must_have of `policy`, with per-test edits.

    `omit` drops a required clause, `replace` swaps its wording (paraphrase, negation, ambiguous
    signal), `extra` appends sentences. Nothing else changes, so each test isolates one variable.
    """
    parts = [HEADER, FILLER]
    for category in POLICIES[policy][MUST_HAVE]:
        if category in omit:
            continue
        text = (replace or {}).get(category, MUST_HAVE_CORPUS[category]["canonical"])
        parts.append(text if text.endswith("\n") else text + "\n")
    parts.extend(text if text.endswith("\n") else text + "\n" for text in extra)
    return "".join(parts)


def post(document: str, policy: str | None = None, *, raw_messages: list | None = None):
    if raw_messages is not None:
        return client.post("/respond", json={"messages": raw_messages})
    payload: dict = {"document": document}
    if policy is not None:
        payload["policy"] = policy
    return client.post(
        "/respond",
        json={"messages": [{"role": "user", "content": json.dumps(payload)}]},
    )


def verdict_of(response) -> dict:
    assert response.status_code == 200, response.text
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    assert content.lstrip() == content and content.startswith("{"), "content must be bare JSON"
    parsed = json.loads(content)
    assert set(parsed) == VERDICT_KEYS, parsed
    return parsed


def review(document: str, policy: str) -> dict:
    return verdict_of(post(document, policy))


def categories_of(entries: list[dict]) -> set[str]:
    return {entry["category"] for entry in entries}


def find(entries: list[dict], category: str) -> dict | None:
    for entry in entries:
        if entry["category"] == category:
            return entry
    return None


# ---------------------------------------------------------------------------
# Transport contract
# ---------------------------------------------------------------------------


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_response_envelope():
    response = post(build("Risk-Averse Standard"), "Risk-Averse Standard")
    assert response.status_code == 200
    body = response.json()
    message = body["choices"][0]["message"]
    assert message["role"] == "assistant"
    assert isinstance(message["content"], str)


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_clean_document_is_compliant(policy):
    """A boilerplate document that satisfies every required clause must not 422 or false-flag."""
    result = review(build(policy), policy)
    assert result == {"compliant": True, "violations": [], "needs_review": []}


# ---------------------------------------------------------------------------
# A. The 22-assignment matrix -- must_have (8 assignments)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy,category", MUST_HAVE_ASSIGNMENTS)
@pytest.mark.parametrize("variant", ["canonical", "paraphrase"])
def test_must_have_satisfied(policy, category, variant):
    document = build(policy, replace={category: MUST_HAVE_CORPUS[category][variant]})
    result = review(document, policy)
    assert category not in categories_of(result["violations"])


@pytest.mark.parametrize("policy,category", MUST_HAVE_ASSIGNMENTS)
def test_must_have_absent_is_violation(policy, category):
    result = review(build(policy, omit=(category,)), policy)
    entry = find(result["violations"], category)
    assert entry is not None, result
    assert entry["rule"] == MUST_HAVE
    assert entry["present"] is False
    assert entry["evidence_spans"] == []
    assert entry["rationale"]
    assert result["compliant"] is False


@pytest.mark.parametrize("policy,category", MUST_HAVE_ASSIGNMENTS)
@pytest.mark.parametrize("variant", ["negated", "ambiguous"])
def test_must_have_not_satisfied_by_negation_or_ambiguity(policy, category, variant):
    """A clean negation and a bare heading both leave the requirement unmet (correction 5)."""
    document = build(policy, replace={category: MUST_HAVE_CORPUS[category][variant]})
    response = post(document, policy)
    if response.status_code == 422:      # an ambiguous signal may also leave a prohibition open
        assert response.json()["error"]["code"] == "unresolved_evidence"
        return
    result = verdict_of(response)
    entry = find(result["violations"], category)
    assert entry is not None, result
    assert entry["present"] is False and entry["evidence_spans"] == []


# ---------------------------------------------------------------------------
# A. The 22-assignment matrix -- must_not_have (10 assignments)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy,category", MUST_NOT_HAVE_ASSIGNMENTS)
@pytest.mark.parametrize("variant", ["present", "paraphrase"])
def test_must_not_have_present_is_violation_with_exact_span(policy, category, variant):
    sentence = MUST_NOT_HAVE_CORPUS[category][variant]
    document = build(policy, extra=(sentence,))
    result = review(document, policy)
    entry = find(result["violations"], category)
    assert entry is not None, result
    assert entry["rule"] == MUST_NOT_HAVE
    assert entry["present"] is True
    assert entry["evidence_spans"], "a prohibited clause is never reported without a span"
    for span in entry["evidence_spans"]:
        assert span in document, f"span is not exact document text: {span!r}"
        assert len(span) <= detect.MAX_SPAN_CHARS
    assert len(entry["evidence_spans"]) <= detect.MAX_SPANS_PER_FINDING
    assert result["compliant"] is False


@pytest.mark.parametrize("policy,category", MUST_NOT_HAVE_ASSIGNMENTS)
def test_must_not_have_absent_is_clean(policy, category):
    result = review(build(policy), policy)
    assert category not in categories_of(result["violations"])


@pytest.mark.parametrize("policy,category", MUST_NOT_HAVE_ASSIGNMENTS)
def test_must_not_have_negated_is_benign(policy, category):
    sentence = MUST_NOT_HAVE_CORPUS[category]["negated"]
    document = build(policy, extra=(sentence,))
    result = review(document, policy)
    assert category not in categories_of(result["violations"])
    assert result["compliant"] is True, "a clean negation must not 422 or flag"


@pytest.mark.parametrize("policy,category", MUST_NOT_HAVE_ASSIGNMENTS)
def test_must_not_have_ambiguous_only_is_unresolved(policy, category):
    """Vocabulary without confirmed meaning cannot be quoted, so it must not read as clean."""
    document = build(policy, extra=(MUST_NOT_HAVE_CORPUS[category]["ambiguous"],))
    response = post(document, policy)
    assert response.status_code == 422, response.text
    body = response.json()
    assert "compliant" not in json.dumps(body)
    assert body["error"]["code"] == "unresolved_evidence"
    assert category in {case["category"] for case in body["error"]["unresolved"]}


# ---------------------------------------------------------------------------
# A. The 22-assignment matrix -- needs_review (4 assignments)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy,category", NEEDS_REVIEW_ASSIGNMENTS)
def test_needs_review_present_is_not_a_fault(policy, category):
    document = build(policy, extra=(NEEDS_REVIEW_CORPUS[category]["present"],))
    result = review(document, policy)
    entry = find(result["needs_review"], category)
    assert entry is not None, result
    assert set(entry) == {"category", "evidence_spans"}, entry
    assert entry["evidence_spans"]
    for span in entry["evidence_spans"]:
        assert span in document
    assert result["compliant"] is True
    assert result["violations"] == []


@pytest.mark.parametrize("policy,category", NEEDS_REVIEW_ASSIGNMENTS)
def test_needs_review_absent(policy, category):
    result = review(build(policy), policy)
    assert result["needs_review"] == []


# ---------------------------------------------------------------------------
# A. Policy isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_policy_isolation(policy):
    """Categories that belong only to other policies are never reported."""
    own = set()
    for kind in (MUST_HAVE, MUST_NOT_HAVE, NEEDS_REVIEW):
        own |= set(POLICIES[policy][kind])
    foreign = {c for _p, _k, c in ASSIGNMENTS} - own

    sentences = []
    for category in sorted(foreign):
        for corpus in (MUST_HAVE_CORPUS, MUST_NOT_HAVE_CORPUS, NEEDS_REVIEW_CORPUS):
            if category in corpus:
                sentences.append(corpus[category].get("canonical") or corpus[category]["present"])
                break
    document = HEADER + FILLER + "".join(s + "\n" for s in sentences)

    response = post(document, policy)
    if response.status_code == 422:
        body = response.json()
        assert not (foreign & {c["category"] for c in body["error"].get("unresolved", [])})
        return
    result = verdict_of(response)
    reported = categories_of(result["violations"]) | categories_of(result["needs_review"])
    assert not (reported & foreign), reported & foreign


@pytest.mark.parametrize("sentence", [
    "Buyer is granted an irrevocable licence to the Software.",
    "Buyer is granted a non-revocable licence to the Software.",
    "Buyer is granted a non-terminable licence to the Software.",
])
def test_negation_guards_do_not_match_inside_the_prohibited_word(sentence):
    """"irrevocable" and "non-revocable" contain "revocable": a guard must not read them as benign."""
    document = build("IP-Protective Standard", extra=(sentence,))
    result = review(document, "IP-Protective Standard")
    assert "Irrevocable Or Perpetual License" in categories_of(result["violations"])


def test_cap_and_uncapped_are_independent():
    """A cap plus an uncapped carve-out satisfies the must_have and violates the must_not_have."""
    document = build(
        "Vendor Contract Standard",
        extra=("Notwithstanding the foregoing, Vendor shall have unlimited liability for breaches "
               "of confidentiality.",),
    )
    result = review(document, "Vendor Contract Standard")
    assert "Cap On Liability" not in categories_of(result["violations"])
    assert "Uncapped Liability" in categories_of(result["violations"])


# ---------------------------------------------------------------------------
# B. False-clean probes -- the 18 violation assignments only
# ---------------------------------------------------------------------------

PROBE_RESULTS: list[tuple[str, str, str, bool]] = []


def _probe_documents(policy: str, kind: str, category: str) -> list[tuple[str, str]]:
    if kind == MUST_HAVE:
        corpus = MUST_HAVE_CORPUS[category]
        return [
            ("absent", build(policy, omit=(category,))),
            ("negated", build(policy, replace={category: corpus["negated"]})),
            ("ambiguous_only", build(policy, replace={category: corpus["ambiguous"]})),
        ]
    corpus = MUST_NOT_HAVE_CORPUS[category]
    return [
        ("present", build(policy, extra=(corpus["present"],))),
        ("paraphrase", build(policy, extra=(corpus["paraphrase"],))),
        ("ambiguous_only", build(policy, extra=(corpus["ambiguous"],))),
    ]


@pytest.mark.parametrize("policy,category", VIOLATION_ASSIGNMENTS)
def test_no_false_clean(policy, category):
    """No document built to violate an assignment may come back compliant: true."""
    kind = MUST_HAVE if (policy, category) in MUST_HAVE_ASSIGNMENTS else MUST_NOT_HAVE
    for variant, document in _probe_documents(policy, kind, category):
        response = post(document, policy)
        if response.status_code == 200:
            result = verdict_of(response)
            clean = result["compliant"] is True
        else:
            clean = False               # a non-2xx carries no verdict at all
        PROBE_RESULTS.append((policy, category, variant, not clean))
        assert not clean, f"false clean: {policy} / {category} / {variant}"


# ---------------------------------------------------------------------------
# C. Error contract -- no body on these paths may carry a verdict key
# ---------------------------------------------------------------------------


def test_malformed_body_is_400():
    response = client.post("/respond", content=b"not json", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_request"


def test_missing_messages_is_400():
    response = client.post("/respond", json={"prompt": "review this"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_request"


def test_no_user_message_is_422():
    response = post("", None, raw_messages=[{"role": "assistant", "content": "hello"}])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_missing"


def test_prose_without_boundary_is_422():
    document = build("Risk-Averse Standard")
    response = post("", None, raw_messages=[{
        "role": "user",
        "content": "Please check this against the Risk-Averse Standard.\n" + document,
    }])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_missing"


def test_prose_with_explicit_marker_is_reviewed():
    document = build("Risk-Averse Standard")
    response = post("", None, raw_messages=[{
        "role": "user",
        "content": "Check this against the Risk-Averse Standard.\nDocument:\n" + document,
    }])
    result = verdict_of(response)
    assert result["compliant"] is True


def test_empty_document_is_422():
    response = post("   ", "Risk-Averse Standard")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_empty"


def test_no_policy_named_is_422():
    response = post(build("Risk-Averse Standard"), None)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_unspecified"


def test_unknown_policy_is_422():
    response = post(build("Risk-Averse Standard"), "Ultra Cautious Standard")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_unknown"


def test_named_standard_in_prose_is_422():
    document = build("Risk-Averse Standard")
    response = post("", None, raw_messages=[{
        "role": "user",
        "content": "Review against the Ultra Cautious Standard.\nDocument:\n" + document,
    }])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_unknown"


def test_two_policies_named_is_422():
    document = build("Risk-Averse Standard")
    response = post("", None, raw_messages=[{
        "role": "user",
        "content": "Check the Risk-Averse Standard and the IP-Protective Standard.\nDocument:\n"
                   + document,
    }])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_ambiguous"


def test_internal_error_is_500_without_a_verdict(monkeypatch):
    def boom(document, policy):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(server, "review", boom)
    response = post(build("Risk-Averse Standard"), "Risk-Averse Standard")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "compliant" not in json.dumps(response.json())


ERROR_CASES = [
    "malformed", "no_messages", "no_user", "no_boundary", "empty", "no_policy", "unknown_policy",
    "ambiguous_policy", "unresolved",
]


@pytest.mark.parametrize("case", ERROR_CASES)
def test_error_bodies_carry_no_verdict(case):
    document = build("Risk-Averse Standard")
    if case == "malformed":
        response = client.post("/respond", content=b"{", headers={"content-type": "application/json"})
    elif case == "no_messages":
        response = client.post("/respond", json={})
    elif case == "no_user":
        response = post("", None, raw_messages=[{"role": "system", "content": "hi"}])
    elif case == "no_boundary":
        response = post("", None, raw_messages=[{"role": "user", "content": "Risk-Averse Standard: "
                                                                            + document}])
    elif case == "empty":
        response = post("", "Risk-Averse Standard")
    elif case == "no_policy":
        response = post(document, None)
    elif case == "unknown_policy":
        response = post(document, "Nonexistent Standard")
    elif case == "ambiguous_policy":
        response = post("", None, raw_messages=[{
            "role": "user",
            "content": "Risk-Averse Standard and Vendor Contract Standard\nDocument:\n" + document,
        }])
    else:
        response = post(
            build("Risk-Averse Standard",
                  extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["ambiguous"],)),
            "Risk-Averse Standard",
        )
    assert response.status_code >= 400, case
    body = response.json()
    assert "choices" not in body
    for key in VERDICT_KEYS:
        assert key not in body
    assert "compliant" not in json.dumps(body), case
    assert body["error"]["known_policies"]


# ---------------------------------------------------------------------------
# C. Robustness
# ---------------------------------------------------------------------------


def test_surrogate_input_does_not_crash():
    document = build("Risk-Averse Standard") + "Vendor n\udcc3ame is Acme.\n"
    response = post(document, "Risk-Averse Standard")
    assert response.status_code == 200
    verdict_of(response)


def test_only_user_messages_are_reviewed():
    """A prohibited clause quoted by the assistant is not the document under review."""
    messages = [
        {"role": "assistant", "content": MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"]},
        {"role": "user", "content": json.dumps({
            "policy": "Risk-Averse Standard", "document": build("Risk-Averse Standard")})},
    ]
    result = verdict_of(post("", None, raw_messages=messages))
    assert result["compliant"] is True


def test_newest_user_message_wins():
    old = json.dumps({"policy": "Risk-Averse Standard", "document": build(
        "Risk-Averse Standard", extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))})
    new = json.dumps({"policy": "Risk-Averse Standard", "document": build("Risk-Averse Standard")})
    result = verdict_of(post("", None, raw_messages=[
        {"role": "user", "content": old}, {"role": "user", "content": new}]))
    assert result["compliant"] is True


def test_content_list_parts_are_accepted():
    payload = json.dumps({"policy": "Risk-Averse Standard",
                          "document": build("Risk-Averse Standard")})
    result = verdict_of(post("", None, raw_messages=[
        {"role": "user", "content": [{"type": "text", "text": payload}]}]))
    assert result["compliant"] is True


def test_logs_carry_no_document_text(caplog):
    nonce = "ZQX-NONCE-77"
    document = build(
        "Risk-Averse Standard",
        extra=(f"A penalty may be assessed under Schedule {nonce}.",),
    )
    with caplog.at_level(logging.INFO, logger="compliance_review"):
        response = post(document, "Risk-Averse Standard")
    assert response.status_code == 422
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged, "unresolved cases must be logged as metadata"
    assert "unresolved_evidence" in logged
    assert nonce not in logged
    assert "penalty" not in logged.lower()
    assert "Schedule" not in logged


def test_unresolved_alongside_violations_still_returns_the_verdict():
    document = build("Risk-Averse Standard", extra=(
        MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"],
        MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["ambiguous"],
    ))
    result = review(document, "Risk-Averse Standard")
    assert "Uncapped Liability" in categories_of(result["violations"])
    assert result["compliant"] is False
    assert "Liquidated Damages" not in categories_of(result["needs_review"])


def test_policy_alias_is_accepted():
    result = review(build("Risk-Averse Standard"), "risk averse")
    assert result["compliant"] is True


# ---------------------------------------------------------------------------
# C. Escalation -- fake client only, never a live call
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, behaviour) -> None:
        self._behaviour = behaviour

    async def create(self, **kwargs):
        if isinstance(self._behaviour, BaseException):
            raise self._behaviour
        return _Response(self._behaviour)


class FakeClient:
    """Mirrors only the shape escalate.adjudicate uses: with_options().messages.create()."""

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls = 0

    def with_options(self, **kwargs):
        self.options = kwargs
        return self

    @property
    def messages(self):
        self.calls += 1
        return _Messages(self.behaviour)


ENABLED_ENV = {escalate.ENABLE_ENV: "1", escalate.MODEL_ENV: "test-model-id"}


def _findings(*items) -> str:
    return json.dumps({"findings": list(items)})


def test_escalation_is_disabled_without_both_switches():
    assert escalate.escalation_config({})[0] is False
    assert escalate.escalation_config({escalate.ENABLE_ENV: "1"})[0] is False
    assert escalate.escalation_config({escalate.MODEL_ENV: "x"})[0] is False
    assert escalate.escalation_config(ENABLED_ENV)[0] is True
    assert escalate.is_enabled() is False, "escalation must be off in this environment"


def test_escalation_schema_cannot_say_absent():
    enum = RESULT = escalate.RESULT_SCHEMA["properties"]["findings"]["items"]["properties"]
    assert enum["resolution"]["enum"] == ["present", "unsure"]


@pytest.mark.anyio
async def test_escalation_disabled_returns_no_upgrades():
    document = "Vendor shall use the Marks exclusively for the purposes of this Agreement."
    fake = FakeClient(_findings({"category": "Exclusivity", "resolution": "present",
                                "span": document}))
    upgrades = await escalate.adjudicate(
        document, "Procurement Clean Standard",
        [{"category": "Exclusivity"}], client=fake, env={})
    assert upgrades == []
    assert fake.calls == 0, "a disabled escalation must not call out at all"


@pytest.mark.anyio
@pytest.mark.parametrize("behaviour,expected", [
    ("verifiable", 1),
    ("unverifiable", 0),
    ("unsure", 0),
    ("malformed", 0),
    ("timeout", 0),
    ("other_category", 0),
])
async def test_escalation_failure_paths_leave_the_case_unresolved(behaviour, expected):
    document = "Vendor shall use the Marks exclusively for the purposes of this Agreement."
    payloads = {
        "verifiable": _findings({"category": "Exclusivity", "resolution": "present",
                                 "span": document}),
        "unverifiable": _findings({"category": "Exclusivity", "resolution": "present",
                                   "span": "Vendor is the exclusive supplier."}),
        "unsure": _findings({"category": "Exclusivity", "resolution": "unsure", "span": ""}),
        "malformed": "not json at all",
        "timeout": TimeoutError("timed out"),
        "other_category": _findings({"category": "Non-Compete", "resolution": "present",
                                     "span": document}),
    }
    fake = FakeClient(payloads[behaviour])
    upgrades = await escalate.adjudicate(
        document, "Procurement Clean Standard",
        [{"category": "Exclusivity"}], client=fake, env=ENABLED_ENV)
    assert len(upgrades) == expected
    if expected:
        assert upgrades[0]["span"] in document


def test_escalation_upgrades_unresolved_to_a_violation(monkeypatch):
    sentence = MUST_NOT_HAVE_CORPUS["Exclusivity"]["ambiguous"]
    document = build("Procurement Clean Standard", extra=(sentence,))
    monkeypatch.setenv(escalate.ENABLE_ENV, "1")
    monkeypatch.setenv(escalate.MODEL_ENV, "test-model-id")
    fake = FakeClient(_findings({"category": "Exclusivity", "resolution": "present",
                                 "span": sentence}))
    monkeypatch.setattr(server.app.state, "escalation_client", fake, raising=False)
    result = review(document, "Procurement Clean Standard")
    entry = find(result["violations"], "Exclusivity")
    assert entry is not None and entry["present"] is True
    assert entry["evidence_spans"] == [sentence]
    assert result["compliant"] is False


def test_escalation_failure_keeps_the_422(monkeypatch):
    sentence = MUST_NOT_HAVE_CORPUS["Exclusivity"]["ambiguous"]
    document = build("Procurement Clean Standard", extra=(sentence,))
    monkeypatch.setenv(escalate.ENABLE_ENV, "1")
    monkeypatch.setenv(escalate.MODEL_ENV, "test-model-id")
    monkeypatch.setattr(server.app.state, "escalation_client", FakeClient(TimeoutError()),
                        raising=False)
    response = post(document, "Procurement Clean Standard")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unresolved_evidence"


def test_escalation_enabled_without_a_client_makes_no_network_call(monkeypatch):
    """No SDK is installed, so the default client cannot be built -- the case stays unresolved."""
    sentence = MUST_NOT_HAVE_CORPUS["Exclusivity"]["ambiguous"]
    document = build("Procurement Clean Standard", extra=(sentence,))
    monkeypatch.setenv(escalate.ENABLE_ENV, "1")
    monkeypatch.setenv(escalate.MODEL_ENV, "test-model-id")
    monkeypatch.setattr(server.app.state, "escalation_client", None, raising=False)
    response = post(document, "Procurement Clean Standard")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Transport handshake -- the one 200 that is not a verdict
# ---------------------------------------------------------------------------

PROBE = server.TRANSPORT_PROBE


def probe_content(response) -> dict:
    """The handshake reply: a bare JSON object that is not, and cannot be read as, a verdict."""
    assert response.status_code == 200, response.text
    content = response.json()["choices"][0]["message"]["content"]
    assert content.lstrip() == content and content.startswith("{")
    parsed = json.loads(content)
    assert not (VERDICT_KEYS & set(parsed)), parsed
    assert "compliant" not in response.text
    return parsed


def test_handshake_probe_is_answered_without_a_verdict():
    response = client.post("/respond", json={"messages": [{"role": "user", "content": PROBE}]})
    assert probe_content(response) == {"status": "ok"}
    assert response.json()["choices"][0]["message"]["role"] == "assistant"


@pytest.mark.parametrize("wording", [
    PROBE,
    f"  {PROBE}  ",
    f"{PROBE}\n",
    "Conformance Probe: Please Respond",
    "conformance probe:  please   respond",
])
def test_handshake_survives_whitespace_and_case(wording):
    """Transport reformatting must not turn the handshake back into a 422."""
    response = client.post("/respond", json={"messages": [{"role": "user", "content": wording}]})
    assert probe_content(response) == {"status": "ok"}


def test_handshake_accepts_content_parts():
    response = client.post("/respond", json={
        "messages": [{"role": "user", "content": [{"type": "text", "text": PROBE}]}]})
    assert probe_content(response) == {"status": "ok"}


# --- non-interception: probe wording must never buy a bypass or a clean verdict ---


def test_probe_plus_a_policy_is_not_a_handshake():
    response = client.post("/respond", json={"messages": [
        {"role": "user", "content": f"{PROBE} Use the Risk-Averse Standard."}]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_missing"
    assert "status" not in response.json()


def test_probe_plus_a_document_is_reviewed():
    document = build("Risk-Averse Standard",
                     extra=(MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"],))
    response = post("", None, raw_messages=[{"role": "user", "content":
        f"{PROBE}\nRisk-Averse Standard\nDocument:\n{document}"}])
    result = verdict_of(response)
    assert "Uncapped Liability" in categories_of(result["violations"])
    assert result["compliant"] is False


def test_probe_inside_a_json_envelope_is_reviewed_as_the_document():
    """The probe as document text is a document: an empty one fails every required clause."""
    result = review(PROBE, "Risk-Averse Standard")
    assert result["compliant"] is False
    assert categories_of(result["violations"]) == set(POLICIES["Risk-Averse Standard"][MUST_HAVE])


def test_probe_with_a_policy_field_beside_it_is_not_a_handshake():
    response = client.post("/respond", json={
        "messages": [{"role": "user", "content": PROBE}], "policy": "Risk-Averse Standard"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_missing"


def test_probe_with_a_document_field_beside_it_is_not_a_handshake():
    response = client.post("/respond", json={
        "messages": [{"role": "user", "content": PROBE}],
        "document": build("Risk-Averse Standard")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_missing"


@pytest.mark.parametrize("history", [
    [{"role": "assistant", "content": "Ready."}, {"role": "user", "content": PROBE}],
    [{"role": "user", "content": PROBE}, {"role": "user", "content": PROBE}],
    [{"role": "system", "content": "You review contracts."}, {"role": "user", "content": PROBE}],
])
def test_probe_with_conversation_history_is_not_a_handshake(history):
    """A live conversation is real work; only a bare opening probe is a handshake."""
    response = client.post("/respond", json={"messages": history})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_missing"


def test_probe_in_a_conversation_carrying_a_document_still_reviews():
    document = build("Procurement Clean Standard",
                     extra=(MUST_NOT_HAVE_CORPUS["Non-Compete"]["present"],))
    result = verdict_of(post("", None, raw_messages=[
        {"role": "user", "content": PROBE},
        {"role": "user", "content": json.dumps(
            {"policy": "Procurement Clean Standard", "document": document})},
    ]))
    assert "Non-Compete" in categories_of(result["violations"])
    assert result["compliant"] is False


@pytest.mark.parametrize("wording", [
    "conformance probe",
    "please respond",
    "conformance probe: please respond now",
    "conformance probe: please respond with a compliance verdict",
    "",
])
def test_near_miss_wordings_are_not_a_handshake(wording):
    response = client.post("/respond", json={"messages": [{"role": "user", "content": wording}]})
    assert response.status_code == 422, wording
    assert response.json()["error"]["code"] == "document_missing"


def test_probe_from_a_non_user_role_is_not_a_handshake():
    response = client.post("/respond", json={"messages": [{"role": "assistant", "content": PROBE}]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_missing"


def test_malformed_requests_keep_their_status_after_the_handshake():
    """The handshake sits after body validation, so 400s stay 400s."""
    assert client.post("/respond", content=b"not json",
                       headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/respond", json={"prompt": PROBE}).status_code == 400
    assert client.post("/respond", json={"messages": PROBE}).status_code == 400


def test_ordinary_review_is_unaffected_by_the_handshake():
    clean = review(build("Vendor Contract Standard"), "Vendor Contract Standard")
    assert clean == {"compliant": True, "violations": [], "needs_review": []}
    flagged = review(
        build("Vendor Contract Standard",
              extra=(MUST_NOT_HAVE_CORPUS["Most Favored Nation"]["present"],
                     NEEDS_REVIEW_CORPUS["Change Of Control"]["present"])),
        "Vendor Contract Standard")
    assert categories_of(flagged["violations"]) == {"Most Favored Nation"}
    assert categories_of(flagged["needs_review"]) == {"Change Of Control"}
    assert flagged["compliant"] is False


# ---------------------------------------------------------------------------
# D. Regression -- the reported Governing Law miss, and the wording pairs found auditing detection.
#
# Every row below was run against the artifact before it was written down. The "satisfies" rows were
# missed by the detector; each is paired with a lookalike that must NOT satisfy the same requirement,
# because a recall fix that also admits a lookalike trades a false violation for a false clean -- the
# worse error. The pairs are therefore asserted together, in one test per direction.
# ---------------------------------------------------------------------------

# Reported verbatim by the baseliner: under the Vendor Contract Standard this document was reported
# as missing Governing Law. "Governed by California law" is the adjectival form of the clause, and
# "Aggregate liability shall not exceed the fees paid" is the other required clause of that policy,
# so nothing here is absent.
REPORTED_DOCUMENT = ("The agreement is governed by California law. "
                     "Aggregate liability shall not exceed the fees paid.")


def test_reported_document_is_compliant_under_vendor_contract_standard():
    assert review(REPORTED_DOCUMENT, "Vendor Contract Standard") == {
        "compliant": True, "violations": [], "needs_review": []}


def test_reported_document_does_not_report_governing_law_absent():
    """The exact reported failure: Governing Law came back present: false with no span."""
    result = review(REPORTED_DOCUMENT, "Vendor Contract Standard")
    assert find(result["violations"], "Governing Law") is None


# The adjectival form ("California law"), "governed under", and "subject to the laws of" were all
# missed: the confirming set only had the postpositive "laws of <forum>" form. detect.py compiles
# every pattern IGNORECASE, so the forum cannot be recognised by capitalisation -- these forms are
# admitted by blocking the vacuous qualifiers in the slot before "law" instead.
GOVERNING_LAW_SATISFIED = [
    "This Agreement is governed by California law.",
    "This Agreement shall be governed by and construed in accordance with Delaware law.",
    "This Agreement shall be governed under the laws of Ontario.",
    "This MSA is governed by New York law.",
    "This Agreement shall be subject to the laws of the Republic of Ireland.",
    "The governing law shall be the law of England and Wales.",
    "THE AGREEMENT IS GOVERNED BY CALIFORNIA LAW.",
    "the agreement is governed by california law.",
]

GOVERNING_LAW_NOT_SATISFIED = [
    # Nothing here chooses a forum, and each is a wording that a bare "governed by ... law" pattern
    # would wrongly accept.
    "This Agreement shall be governed by applicable law.",
    "This Agreement is governed by the terms of the Master Services Agreement.",
    "Vendor shall comply with all applicable data protection laws.",
    # Complying with a jurisdiction's law, or performing services under it, is not choosing it as
    # the law governing the agreement.
    "Vendor shall comply with the laws of the State of California.",
    "The Services shall be performed in accordance with the laws of the State of New York.",
    # The agreement, not a party, has to be the thing made subject to the law.
    "Vendor is subject to the laws of Ireland.",
]


@pytest.mark.parametrize("wording", GOVERNING_LAW_SATISFIED)
def test_governing_law_jurisdiction_forms_satisfy_the_requirement(wording):
    result = review(build("Vendor Contract Standard", replace={"Governing Law": wording}),
                    "Vendor Contract Standard")
    assert result == {"compliant": True, "violations": [], "needs_review": []}, wording


@pytest.mark.parametrize("wording", GOVERNING_LAW_NOT_SATISFIED)
def test_governing_law_lookalikes_stay_violations(wording):
    result = review(build("Vendor Contract Standard", replace={"Governing Law": wording}),
                    "Vendor Contract Standard")
    entry = find(result["violations"], "Governing Law")
    assert entry is not None, wording
    assert entry["rule"] == MUST_HAVE and entry["present"] is False
    assert entry["evidence_spans"] == []


# (policy, category, wording, does it satisfy the requirement). Same shape for both directions so a
# new wording cannot be added in one direction and forgotten in the other.
REQUIRED_CLAUSE_WORDINGS = [
    ("Risk-Averse Standard", "Cap On Liability",
     "Each party's total liability shall be limited to $1,000,000.", True),
    ("Risk-Averse Standard", "Cap On Liability",
     "Vendor's liability is subject to an aggregate cap of $500,000.", True),
    ("Risk-Averse Standard", "Cap On Liability",
     "In no event shall Vendor be liable for amounts in excess of the fees paid.", True),
    # A cap that is explicitly disclaimed is not a cap.
    ("Risk-Averse Standard", "Cap On Liability",
     "Vendor's liability is not subject to any monetary cap.", False),
    ("Risk-Averse Standard", "Audit Rights",
     "Buyer shall have the right to conduct an annual audit of Vendor's compliance.", True),
    ("Risk-Averse Standard", "Audit Rights",
     "Vendor shall provide Buyer with access to its books and records.", True),
    # Access to something that is not an audit object is not an audit right.
    ("Risk-Averse Standard", "Audit Rights",
     "Vendor shall provide Buyer with access to the user documentation.", False),
    ("Risk-Averse Standard", "Insurance",
     "Vendor shall be insured against professional liability claims.", True),
    ("Risk-Averse Standard", "Insurance",
     "Vendor shall not be insured for any losses under this Agreement.", False),
    ("IP-Protective Standard", "License Grant",
     "Licensor licenses the Software to Licensee for internal business use.", True),
    # A licence to third parties is not the grant this policy requires.
    ("IP-Protective Standard", "License Grant",
     "Licensor licenses the Software to any third party at its sole discretion.", False),
]


@pytest.mark.parametrize("policy,category,wording,satisfies", REQUIRED_CLAUSE_WORDINGS)
def test_required_clause_wording_pairs(policy, category, wording, satisfies):
    result = review(build(policy, replace={category: wording}), policy)
    entry = find(result["violations"], category)
    if satisfies:
        assert entry is None, wording
    else:
        assert entry is not None, wording
        assert entry["rule"] == MUST_HAVE and entry["present"] is False
        assert entry["evidence_spans"] == []


def test_disclaimed_cap_fails_the_requirement_and_trips_the_prohibition():
    """Cap On Liability and Uncapped Liability are resolved independently, in both directions."""
    wording = "Vendor's liability is not subject to any monetary cap."
    document = build("Risk-Averse Standard", replace={"Cap On Liability": wording})
    result = review(document, "Risk-Averse Standard")
    assert find(result["violations"], "Cap On Liability")["present"] is False
    uncapped = find(result["violations"], "Uncapped Liability")
    assert uncapped is not None and uncapped["present"] is True
    assert uncapped["evidence_spans"] and all(s in document for s in uncapped["evidence_spans"])


# Prohibited-clause wordings the detector previously left unmatched. A must_not_have violation needs
# a verifiable span, so each is asserted to carry one and to be an exact document substring.
ADDITIONAL_PROHIBITED_WORDINGS = [
    ("Vendor Contract Standard", "Most Favored Nation",
     "No other customer shall receive better pricing than Buyer."),
    ("Vendor Contract Standard", "Most Favored Nation",
     "Vendor shall not offer lower prices to any other customer."),
    ("Vendor Contract Standard", "Uncapped Liability",
     "Vendor's liability under this Section is uncapped."),
    ("Risk-Averse Standard", "Uncapped Liability",
     "Indemnification obligations of Vendor are uncapped."),
    ("Procurement Clean Standard", "Exclusivity",
     "Vendor is granted exclusivity for the Territory."),
]

# The same patterns must stay silent here: "any other customer" without a pricing comparison is not
# a most-favoured-nation clause, and a denied or licence-scoped exclusivity is not an exclusivity
# obligation.
ADDITIONAL_PROHIBITION_GUARDS = [
    ("Vendor Contract Standard", "Vendor shall not offer the Services to any other customer during "
                                 "the Term."),
    ("Vendor Contract Standard", "No other customer shall receive priority support ahead of Buyer."),
    ("Procurement Clean Standard", "Vendor grants Buyer an exclusive licence to use the Marks."),
    ("Procurement Clean Standard", "The parties acknowledge that no exclusivity is granted."),
]


@pytest.mark.parametrize("policy,category,wording", ADDITIONAL_PROHIBITED_WORDINGS)
def test_additional_prohibited_wordings_are_violations_with_exact_spans(policy, category, wording):
    document = build(policy, extra=(wording,))
    result = review(document, policy)
    entry = find(result["violations"], category)
    assert entry is not None, wording
    assert entry["rule"] == MUST_NOT_HAVE and entry["present"] is True
    assert entry["evidence_spans"], wording
    for span in entry["evidence_spans"]:
        assert span in document, span
    assert result["compliant"] is False


@pytest.mark.parametrize("policy,wording", ADDITIONAL_PROHIBITION_GUARDS)
def test_additional_prohibition_patterns_do_not_fire_on_lookalikes(policy, wording):
    result = review(build(policy, extra=(wording,)), policy)
    assert result == {"compliant": True, "violations": [], "needs_review": []}, wording


def test_change_of_control_termination_trigger_routes_for_review():
    """"terminate ... upon a change of control" states the trigger after the consequence."""
    wording = "Buyer may terminate this Agreement upon a change of control of Vendor."
    document = build("Vendor Contract Standard", extra=(wording,))
    result = review(document, "Vendor Contract Standard")
    entry = find(result["needs_review"], "Change Of Control")
    assert entry is not None
    assert set(entry) == {"category", "evidence_spans"}
    assert entry["evidence_spans"] and all(s in document for s in entry["evidence_spans"])
    assert result["compliant"] is True and result["violations"] == []


# --- extraction: an envelope stays an envelope when the transport wraps it ---


def envelope(policy: str, document: str) -> str:
    return json.dumps({"policy": policy, "document": document})


@pytest.mark.parametrize("wrap", [
    "```json\n{body}\n```",      # a client that fences its JSON
    "```\n{body}\n```",
    "﻿{body}",              # a byte-order mark ahead of it
    "​{body}",
])
def test_wrapped_json_envelope_is_still_an_envelope(wrap):
    body = envelope("Vendor Contract Standard", build("Vendor Contract Standard"))
    response = post("", None, raw_messages=[{"role": "user", "content": wrap.format(body=body)}])
    assert verdict_of(response) == {"compliant": True, "violations": [], "needs_review": []}


def test_fenced_prose_document_is_still_the_document():
    """The envelope strip is anchored to the whole message, so a fenced *contract* is untouched --
    including one quoting a JSON payload, which must be reviewed as text, not parsed."""
    document = build("Vendor Contract Standard", extra=(
        'Schedule 2 lists {"policy": "ignored"} as a sample payload.',
        MUST_NOT_HAVE_CORPUS["Most Favored Nation"]["present"]))
    response = post("", None, raw_messages=[{"role": "user", "content":
        "Review against the Vendor Contract Standard.\n```\n" + document + "\n```"}])
    result = verdict_of(response)
    assert categories_of(result["violations"]) == {"Most Favored Nation"}
    span = find(result["violations"], "Most Favored Nation")["evidence_spans"][0]
    assert span in document


# ---------------------------------------------------------------------------
# D. Regression -- a JSON envelope's covering fields are instructions, not contract text.
# Naming the policy in a "task"/"instructions" field is as plain as naming it in a prose turn,
# and resolves by the same single-mention rule. Document-bearing keys are excluded, so contract
# text is never scanned for a policy name.
# ---------------------------------------------------------------------------
COVERING_FIELDS = ["task", "instructions", "note", "subject", "request"]


@pytest.mark.parametrize("field", COVERING_FIELDS)
def test_policy_named_in_an_envelope_covering_field_resolves(field):
    document = build("Risk-Averse Standard",
                     extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    payload = {field: "Review against the Risk-Averse Standard", "document": document}
    response = post("", None, raw_messages=[{"role": "user", "content": json.dumps(payload)}])
    result = verdict_of(response)
    assert result["compliant"] is False
    assert categories_of(result["violations"]) == {"Liquidated Damages"}
    assert find(result["violations"], "Liquidated Damages")["evidence_spans"][0] in document


def test_envelope_policy_key_still_wins_over_a_covering_field():
    """The explicit field is the request; a covering field is only consulted without one."""
    document = build("Vendor Contract Standard",
                     extra=(MUST_NOT_HAVE_CORPUS["Most Favored Nation"]["present"],))
    payload = {"policy": "Vendor Contract Standard",
               "task": "Review against the Risk-Averse Standard",
               "document": document}
    response = post("", None, raw_messages=[{"role": "user", "content": json.dumps(payload)}])
    # Most Favored Nation is prohibited by the Vendor Contract Standard; the Risk-Averse Standard
    # does not carry it at all, so the verdict itself shows which policy was applied.
    assert categories_of(verdict_of(response)["violations"]) == {"Most Favored Nation"}


def test_envelope_covering_field_wins_over_an_older_prose_turn():
    """Newest-first precedence is unchanged: the envelope's own fields are the newest text."""
    document = build("Risk-Averse Standard",
                     extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    payload = {"task": "Review against the Risk-Averse Standard", "document": document}
    response = post("", None, raw_messages=[
        {"role": "user", "content": "Earlier I asked about the Vendor Contract Standard."},
        {"role": "user", "content": json.dumps(payload)},
    ])
    assert categories_of(verdict_of(response)["violations"]) == {"Liquidated Damages"}


def test_policy_named_only_inside_the_document_does_not_resolve():
    """The guard on the fix: contract text is not an instruction. A contract that mentions a
    standard by name must not thereby select the policy it is reviewed against."""
    document = ("This agreement is administered under the Risk-Averse Standard.\n"
                + build("Risk-Averse Standard"))
    response = post("", None, raw_messages=[{"role": "user", "content":
        json.dumps({"document": document})}])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_unspecified"
    assert "compliant" not in response.json()


def test_a_second_document_bearing_field_is_not_scanned_for_a_policy():
    """Every DOCUMENT_KEYS field is excluded, not just the one that supplied the document."""
    response = post("", None, raw_messages=[{"role": "user", "content": json.dumps({
        "document": build("Risk-Averse Standard"),
        "full_text": "Annex B of the Risk-Averse Standard is reproduced here.",
    })}])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_unspecified"


def test_two_policies_in_one_envelope_field_are_ambiguous():
    response = post("", None, raw_messages=[{"role": "user", "content": json.dumps({
        "task": "Review against the Risk-Averse Standard and the Vendor Contract Standard",
        "document": build("Risk-Averse Standard"),
    })}])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_ambiguous"


def test_unknown_named_standard_in_an_envelope_field_is_policy_unknown():
    response = post("", None, raw_messages=[{"role": "user", "content": json.dumps({
        "task": "Review against the Widget Safety Standard",
        "document": build("Risk-Averse Standard"),
    })}])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_unknown"


def test_envelope_without_any_policy_mention_is_still_unspecified():
    response = post("", None, raw_messages=[{"role": "user", "content": json.dumps({
        "task": "Please review this and report back", "id": 17, "urgent": True,
        "document": build("Risk-Averse Standard"),
    })}])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_unspecified"


def test_non_string_envelope_fields_are_ignored_not_crashed_on():
    document = build("Risk-Averse Standard",
                     extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    response = post("", None, raw_messages=[{"role": "user", "content": json.dumps({
        "id": 17, "retries": None, "tags": ["risk"], "meta": {"policy": "Vendor Contract Standard"},
        "task": "Review against the Risk-Averse Standard", "document": document,
    })}])
    assert categories_of(verdict_of(response)["violations"]) == {"Liquidated Damages"}


# ---------------------------------------------------------------------------
# D. Revised boundary: an ordinary request is reviewable when the instruction and the contract
# body separate unambiguously -- a short leading instruction block ending in a colon, a blank line,
# then the contract. Instruction text is never contract evidence, the policy is never taken from
# the contract block, and anything less clear-cut stays 422.
# ---------------------------------------------------------------------------
RAS = "Risk-Averse Standard"


def prose(instruction: str, document: str, trailing: str = "") -> str:
    message = f"{instruction}\n\n{document}"
    if trailing:
        message += f"\n\n{trailing}"
    return message


def post_prose(content: str, *, history: list | None = None):
    messages = list(history or []) + [{"role": "user", "content": content}]
    return client.post("/respond", json={"messages": messages})


ACCEPTED_INSTRUCTIONS = [
    f"Please review this contract against the {RAS}:",
    f"Review against the {RAS}:",
    f"{RAS} -- contract follows:",
    f"Hi.\nCheck the attached against the {RAS}:",
    f"Compliance check requested.\nPolicy: {RAS}\nContract text:",
]


@pytest.mark.parametrize("instruction", ACCEPTED_INSTRUCTIONS)
def test_colon_instruction_then_blank_line_is_reviewable(instruction):
    document = build(RAS, extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    result = verdict_of(post_prose(prose(instruction, document)))
    assert result["compliant"] is False
    assert categories_of(result["violations"]) == {"Liquidated Damages"}
    entry = find(result["violations"], "Liquidated Damages")
    assert entry["rule"] == "must_not_have" and entry["present"] is True
    assert entry["evidence_spans"] and all(span in document for span in entry["evidence_spans"])


def test_colon_instruction_clean_document_is_clean():
    document = build(RAS)
    result = verdict_of(post_prose(prose(f"Review against the {RAS}:", document)))
    assert result == {"compliant": True, "violations": [], "needs_review": []}


def test_colon_instruction_takes_the_policy_from_an_earlier_turn():
    document = build(RAS, extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    response = post_prose(prose("Here is the contract:", document), history=[
        {"role": "user", "content": f"We review everything against the {RAS}."},
        {"role": "assistant", "content": "Understood."},
    ])
    assert categories_of(verdict_of(response)["violations"]) == {"Liquidated Damages"}


def test_needs_review_entry_from_the_prose_path_keeps_exactly_two_keys():
    document = build(RAS, extra=(NEEDS_REVIEW_CORPUS["Covenant Not To Sue"]["present"],))
    result = verdict_of(post_prose(prose(f"Review against the {RAS}:", document)))
    assert result["compliant"] is True and result["violations"] == []
    entry = find(result["needs_review"], "Covenant Not To Sue")
    assert set(entry) == {"category", "evidence_spans"}
    assert all(span in document for span in entry["evidence_spans"])


AMBIGUOUS_MESSAGES = [
    ("colon but no blank line", "{instruction}\n{document}"),
    ("blank line but no colon", "Review against the Risk-Averse Standard\n\n{document}"),
    ("contract only", "{document}"),
    ("colon not on the last line of the block", "{instruction}\nand thanks\n\n{document}"),
    ("leading block too long to be an instruction", ("filler " * 90) + ":\n\n{document}"),
    ("colon appears only after the contract", "{document}\n\nReviewed against the policy:"),
]


@pytest.mark.parametrize("label,template", AMBIGUOUS_MESSAGES, ids=[m[0] for m in AMBIGUOUS_MESSAGES])
def test_ambiguous_boundary_is_still_unreviewable(label, template):
    document = build(RAS, extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    content = template.format(instruction=f"Review against the {RAS}:", document=document)
    response = post_prose(content)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] in {"document_missing", "policy_unspecified"}
    assert "compliant" not in response.json()


def test_policy_is_never_taken_from_the_contract_block():
    document = f"This agreement is administered under the {RAS}.\n" + build(RAS)
    response = post_prose(prose("Here is the contract:", document))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "policy_unspecified"


# --- the mandated case: an instruction that quotes a required or prohibited clause -------------
QUOTES_REQUIRED = "Please confirm whether aggregate liability shall not exceed the fees paid."
QUOTES_PROHIBITED = "Flag any uncapped liability or liquidated damages you find."


@pytest.mark.parametrize("wrap", ["split", "marker"])
def test_trailing_instruction_quoting_a_required_clause_makes_no_false_clean(wrap):
    """The document omits Cap On Liability. A trailing request that quotes the clause must not
    satisfy the requirement -- that would be a clean verdict on a document that lacks it."""
    document = build(RAS, omit=("Cap On Liability",))
    if wrap == "split":
        content = prose(f"Review against the {RAS}:", document, QUOTES_REQUIRED)
    else:
        content = f"{RAS}\nDocument:\n{document}\n\n{QUOTES_REQUIRED}"
    result = verdict_of(post_prose(content))
    assert result["compliant"] is False
    assert "Cap On Liability" in categories_of(result["violations"])
    entry = find(result["violations"], "Cap On Liability")
    assert entry["present"] is False and entry["evidence_spans"] == []


@pytest.mark.parametrize("wrap", ["split", "marker"])
def test_trailing_instruction_naming_prohibited_clauses_fabricates_no_span(wrap):
    """A clean document plus a request naming prohibited concepts must stay clean: the request is
    not contract text, so it can be neither a violation nor an evidence span."""
    document = build(RAS)
    if wrap == "split":
        content = prose(f"Review against the {RAS}:", document, QUOTES_PROHIBITED)
    else:
        content = f"{RAS}\nDocument:\n{document}\n\n{QUOTES_PROHIBITED}"
    result = verdict_of(post_prose(content))
    assert result == {"compliant": True, "violations": [], "needs_review": []}
    spans = [span for entry in result["violations"] + result["needs_review"]
             for span in entry["evidence_spans"]]
    assert not any(span in QUOTES_PROHIBITED for span in spans)


def test_leading_instruction_quoting_a_required_clause_is_not_evidence_either():
    document = build(RAS, omit=("Cap On Liability",))
    content = prose(f"Confirm whether aggregate liability shall not exceed the fees paid, "
                    f"per the {RAS}:", document)
    result = verdict_of(post_prose(content))
    assert "Cap On Liability" in categories_of(result["violations"])


def test_trailing_question_is_not_contract_text():
    document = build(RAS, extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    result = verdict_of(post_prose(prose(f"Review against the {RAS}:", document,
                                         "Does this cap liability?")))
    assert categories_of(result["violations"]) == {"Liquidated Damages"}
    for entry in result["violations"]:
        assert all(span in document for span in entry["evidence_spans"])


def test_a_closing_contract_clause_is_not_mistaken_for_a_request():
    """The trailing trim must not eat contract text. Audit Rights is satisfied only by the final
    block, whose wording ("may review the records") deliberately resembles a request."""
    body = (build(RAS, omit=("Audit Rights",)).replace("\n", "\n\n")
            + "\n\nVendor may review the records once per year.")
    result = verdict_of(post_prose(prose(f"Review against the {RAS}:", body)))
    assert result == {"compliant": True, "violations": [], "needs_review": []}


# --- the shapes that already worked must keep working -----------------------------------------
PRESERVED_SHAPES = {
    "json envelope": lambda d: json.dumps({"policy": RAS, "document": d}),
    "fence": lambda d: f"{RAS}\n```\n{d}\n```",
    "triple quotes": lambda d: f'{RAS}\n"""{d}"""',
    "document start/end": lambda d: f"{RAS}\nDOCUMENT START\n{d}\nDOCUMENT END",
    "begin/end contract": lambda d: f"{RAS}\n--- BEGIN CONTRACT ---\n{d}\n--- END CONTRACT ---",
    "document marker": lambda d: f"{RAS}\nDocument:\n{d}",
    "contract marker": lambda d: f"{RAS}\nContract:\n{d}",
}


@pytest.mark.parametrize("shape", sorted(PRESERVED_SHAPES))
def test_previously_accepted_shapes_are_unchanged(shape):
    document = build(RAS, extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    result = verdict_of(post_prose(PRESERVED_SHAPES[shape](document)))
    assert categories_of(result["violations"]) == {"Liquidated Damages"}
    assert find(result["violations"], "Liquidated Damages")["evidence_spans"][0] in document


# ---------------------------------------------------------------------------
# D. TXT attachments: the documented transport form where the document text arrives base64-encoded
# in a top-level "files" array while the instruction and the policy stay in the messages. Two
# properties are load-bearing throughout: nothing here may depend on file_name, and an attachment
# the artifact cannot read must fail visibly rather than degrade into a review of the instruction.
# ---------------------------------------------------------------------------

VCS = "Vendor Contract Standard"
ATTACH_INSTRUCTION = f"Review the attached contract against the {VCS}."


def attach(text: str, *, file_type: str | None = "txt", file_name: str | None = "agreement.txt",
           raw: bytes | None = None, data: object = None) -> dict:
    """One attachment entry. `raw` supplies the exact bytes; `data` bypasses encoding entirely."""
    entry: dict = {}
    if file_type is not None:
        entry["file_type"] = file_type
    if file_name is not None:
        entry["file_name"] = file_name
    if data is not None:
        entry["file_data"] = data
    else:
        payload = raw if raw is not None else text.encode("utf-8")
        entry["file_data"] = base64.b64encode(payload).decode("ascii")
    return entry


def post_attached(files: object, *, instruction: str | None = ATTACH_INSTRUCTION,
                  messages: list | None = None):
    if messages is None:
        messages = [] if instruction is None else [{"role": "user", "content": instruction}]
    return client.post("/respond", json={"messages": messages, "files": files})


def assert_no_verdict(response, code: str, status: int = 422) -> dict:
    """A failure path: a real status, a named code, and not one byte of a compliance verdict."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    assert body["error"]["code"] == code, body
    assert not VERDICT_KEYS & set(body["error"]), body
    assert "compliant" not in json.dumps(body), body
    return body


def assert_spans_exact(result: dict, document: str) -> None:
    """Every span the verdict carries must be a literal slice of the decoded document."""
    for entry in result["violations"] + result["needs_review"]:
        for span in entry["evidence_spans"]:
            assert span in document, (entry["category"], span)


def test_attachment_only_review_finds_the_prohibited_clause():
    prohibited = MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"]
    document = build(VCS, extra=(prohibited,))
    result = verdict_of(post_attached([attach(document)]))
    assert result["compliant"] is False
    assert categories_of(result["violations"]) == {"Uncapped Liability"}
    assert_spans_exact(result, document)


def test_attachment_span_comes_from_the_document_not_the_instruction():
    """The instruction names the clause; the span must still be the contract's own words."""
    prohibited = MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"]
    document = build(VCS, extra=(prohibited,))
    instruction = (f"{ATTACH_INSTRUCTION} I am worried about unlimited liability for any breach; "
                   "flag it if the vendor has unlimited liability.")
    result = verdict_of(post_attached([attach(document)], instruction=instruction))
    spans = find(result["violations"], "Uncapped Liability")["evidence_spans"]
    assert spans
    for span in spans:
        assert span in document
        assert span not in instruction


def test_attachment_only_clean_document_is_compliant():
    document = build(VCS)
    result = verdict_of(post_attached([attach(document)]))
    assert result == {"compliant": True, "violations": [], "needs_review": []}


def test_attachment_only_missing_required_clause_is_a_violation_without_a_span():
    document = build(VCS, omit=("Governing Law",))
    result = verdict_of(post_attached([attach(document)]))
    entry = find(result["violations"], "Governing Law")
    assert entry is not None and entry["present"] is False
    assert entry["evidence_spans"] == []


def test_attachment_needs_review_entry_keeps_its_exact_shape():
    document = build(VCS, extra=(NEEDS_REVIEW_CORPUS["Change Of Control"]["present"],))
    result = verdict_of(post_attached([attach(document)]))
    entry = find(result["needs_review"], "Change Of Control")
    assert entry is not None and set(entry) == {"category", "evidence_spans"}
    assert result["compliant"] is True
    assert_spans_exact(result, document)


# -- file_name carries no meaning ------------------------------------------------------------


@pytest.mark.parametrize("file_name", [
    "agreement.txt", "contract.txt", "untitled", "", "instructions.txt", None,
])
def test_the_file_name_never_changes_the_review(file_name):
    document = build(VCS, extra=(MUST_NOT_HAVE_CORPUS["Most Favored Nation"]["present"],))
    result = verdict_of(post_attached([attach(document, file_name=file_name)]))
    assert categories_of(result["violations"]) == {"Most Favored Nation"}


@pytest.mark.parametrize("file_type", ["txt", "TXT", ".txt", " txt ", "text", "text/plain", None])
def test_text_file_types_and_an_absent_type_are_all_read(file_type):
    document = build(VCS)
    result = verdict_of(post_attached([attach(document, file_type=file_type)]))
    assert result["compliant"] is True


# -- unreadable attachments fail visibly ------------------------------------------------------


def test_malformed_base64_is_rejected_not_reviewed():
    assert_no_verdict(post_attached([attach("", data="not base64 at all!!")]),
                      "attachment_undecodable")


@pytest.mark.parametrize("data", [
    "QQ==QQ==",            # two concatenated padded blocks
    "QUJD*",               # an illegal character
    "QUJDR",               # a length that cannot be base64
])
def test_base64_that_cannot_decode_is_rejected(data):
    assert_no_verdict(post_attached([attach("", data=data)]), "attachment_undecodable")


def test_non_utf8_bytes_are_rejected_not_silently_mangled():
    assert_no_verdict(post_attached([attach("", raw=b"\xff\xfe\x00contract")]),
                      "attachment_undecodable")


def test_base64_of_nothing_is_document_empty():
    assert_no_verdict(post_attached([attach("")]), "document_empty")


def test_whitespace_only_attachment_is_document_empty():
    assert_no_verdict(post_attached([attach("   \n\t\n  ")]), "document_empty")


def test_missing_file_data_is_attachment_invalid():
    assert_no_verdict(post_attached([{"file_type": "txt", "file_name": "a.txt"}]),
                      "attachment_invalid")


@pytest.mark.parametrize("data", [None, 17, ["QUJD"], {"b64": "QUJD"}])
def test_non_string_file_data_is_attachment_invalid(data):
    assert_no_verdict(post_attached([{"file_type": "txt", "file_data": data}]),
                      "attachment_invalid")


@pytest.mark.parametrize("entry", ["agreement.txt", 42, ["QUJD"], None])
def test_an_attachment_that_is_not_an_object_is_rejected(entry):
    assert_no_verdict(post_attached([entry]), "attachment_invalid")


@pytest.mark.parametrize("file_type", ["pdf", "docx", "application/pdf", "png"])
def test_non_text_file_types_are_refused_rather_than_guessed(file_type):
    document = build(VCS)
    assert_no_verdict(post_attached([attach(document, file_type=file_type)]),
                      "attachment_unsupported_type")


def test_non_string_file_type_is_attachment_invalid():
    assert_no_verdict(post_attached([{"file_type": 3, "file_data": "QUJD"}]),
                      "attachment_invalid")


def test_two_attachments_are_ambiguous_rather_than_concatenated():
    document = build(VCS)
    response = post_attached([attach(document), attach(build("Risk-Averse Standard"))])
    assert_no_verdict(response, "attachment_ambiguous")


def test_files_that_is_not_a_list_is_a_malformed_request():
    assert_no_verdict(post_attached("agreement.txt"), "malformed_request", status=400)


def test_an_unreadable_attachment_never_yields_a_clean_verdict():
    """The one outcome that must be impossible: undecodable bytes reported as compliant."""
    for files in (
        [attach("", data="!!!")],
        [attach("", raw=b"\xff\xfe")],
        [attach("")],
        [{"file_data": None}],
        [attach(build(VCS), file_type="pdf")],
        [attach(build(VCS)), attach(build(VCS))],
    ):
        response = post_attached(files)
        assert response.status_code != 200, response.text
        assert "compliant" not in response.text


# -- the attachment is the document, and the messages are instructions ------------------------


def test_an_empty_files_array_falls_through_to_the_text_only_path():
    document = build(VCS, extra=(MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"],))
    response = client.post("/respond", json={
        "messages": [{"role": "user", "content": json.dumps({"policy": VCS, "document": document})}],
        "files": [],
    })
    result = verdict_of(response)
    assert categories_of(result["violations"]) == {"Uncapped Liability"}


def test_an_empty_files_array_does_not_invent_a_document():
    assert_no_verdict(post_attached([]), "document_missing")


def test_an_attachment_without_a_named_policy_is_policy_unspecified():
    assert_no_verdict(post_attached([attach(build(VCS))], instruction="Please review this."),
                      "policy_unspecified")


def test_an_attachment_with_no_messages_at_all_is_policy_unspecified():
    assert_no_verdict(post_attached([attach(build(VCS))], instruction=None),
                      "policy_unspecified")


def test_an_unknown_named_standard_alongside_an_attachment_is_policy_unknown():
    response = post_attached([attach(build(VCS))],
                             instruction="Review the attachment against the Acme Vendor Standard.")
    assert_no_verdict(response, "policy_unknown")


def test_an_envelope_policy_key_resolves_the_policy_for_an_attachment():
    document = build(VCS, extra=(MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"],))
    messages = [{"role": "user", "content": json.dumps({"policy": VCS, "task": "review the file"})}]
    result = verdict_of(post_attached([attach(document)], messages=messages))
    assert categories_of(result["violations"]) == {"Uncapped Liability"}


def test_a_policy_named_only_inside_an_envelope_document_field_is_not_used():
    """The envelope's document field is contract text; contract text never chooses the policy."""
    messages = [{"role": "user", "content": json.dumps({
        "document": f"This agreement is reviewed under the {VCS}.",
        "note": "the real contract is attached",
    })}]
    assert_no_verdict(post_attached([attach(build(VCS))], messages=messages),
                      "policy_unspecified")


def test_the_policy_may_come_from_an_older_message_than_the_attachment_request():
    document = build(VCS, extra=(MUST_NOT_HAVE_CORPUS["Most Favored Nation"]["present"],))
    messages = [
        {"role": "user", "content": f"We are working under the {VCS} this quarter."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "Here is the contract; please review the attachment."},
    ]
    result = verdict_of(post_attached([attach(document)], messages=messages))
    assert categories_of(result["violations"]) == {"Most Favored Nation"}


def test_an_attachment_outranks_a_document_in_the_message_text():
    """Precedence, as implemented: the attachment is the document, message text is instruction."""
    attached = build(VCS, extra=(MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"],))
    in_message = build(VCS, extra=(MUST_NOT_HAVE_CORPUS["Most Favored Nation"]["present"],))
    instruction = (f"{ATTACH_INSTRUCTION}\nDOCUMENT START\n{in_message}\nDOCUMENT END")
    result = verdict_of(post_attached([attach(attached)], instruction=instruction))
    assert categories_of(result["violations"]) == {"Uncapped Liability"}
    assert_spans_exact(result, attached)


def test_instruction_text_quoting_a_required_clause_cannot_satisfy_it_for_an_attachment():
    document = build(VCS, omit=("Cap On Liability",))
    instruction = (f"{ATTACH_INSTRUCTION} Confirm whether aggregate liability shall not exceed "
                   "the total fees paid in the twelve months preceding the claim.")
    result = verdict_of(post_attached([attach(document)], instruction=instruction))
    entry = find(result["violations"], "Cap On Liability")
    assert entry is not None and entry["present"] is False


# -- encoding details the transport may impose ------------------------------------------------


def test_mime_wrapped_base64_with_newlines_decodes():
    document = build(VCS)
    encoded = base64.b64encode(document.encode("utf-8")).decode("ascii")
    wrapped = "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60))
    result = verdict_of(post_attached([{"file_type": "txt", "file_data": wrapped}]))
    assert result["compliant"] is True


def test_a_bom_prefixed_attachment_is_read_and_spans_stay_exact():
    prohibited = MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"]
    document = build(VCS, extra=(prohibited,))
    raw = ("\ufeff" + document).encode("utf-8")
    result = verdict_of(post_attached([attach("", raw=raw)]))
    assert categories_of(result["violations"]) == {"Uncapped Liability"}
    assert_spans_exact(result, document)


def test_crlf_and_non_ascii_attachment_text_keeps_spans_exact():
    prohibited = MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"]
    document = (build(VCS, extra=(prohibited,)) + "Fees are quoted in euros (\u20ac).\n")
    raw = document.replace("\n", "\r\n").encode("utf-8")
    decoded = raw.decode("utf-8")
    result = verdict_of(post_attached([attach("", raw=raw)]))
    assert categories_of(result["violations"]) == {"Uncapped Liability"}
    assert_spans_exact(result, decoded)


# -- the handshake keeps its own behaviour ----------------------------------------------------


def test_the_bare_probe_is_still_answered_when_no_files_key_is_present():
    response = client.post("/respond", json={"messages": [{"role": "user", "content": PROBE}]})
    assert probe_content(response) == {"status": "ok"}


def test_a_probe_with_an_empty_files_array_is_still_a_probe():
    response = client.post("/respond",
                           json={"messages": [{"role": "user", "content": PROBE}], "files": []})
    assert probe_content(response) == {"status": "ok"}


def test_a_probe_string_carrying_an_attachment_is_real_work_not_a_handshake():
    """An attachment means a document was sent; the handshake must not swallow it."""
    response = post_attached([attach(build(VCS))], instruction=PROBE)
    assert_no_verdict(response, "policy_unspecified")


def test_a_probe_string_with_an_attachment_and_a_policy_is_reviewed():
    document = build(VCS, extra=(MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"],))
    response = post_attached([attach(document)], instruction=f"{PROBE}\nPolicy: {VCS}")
    assert categories_of(verdict_of(response)["violations"]) == {"Uncapped Liability"}


# -- text-only requests are untouched ---------------------------------------------------------


def test_text_only_json_envelope_is_unchanged_without_a_files_key():
    document = build(VCS, extra=(MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"],))
    result = verdict_of(post(document, VCS))
    assert categories_of(result["violations"]) == {"Uncapped Liability"}
    assert_spans_exact(result, document)


def test_text_only_delimited_prose_is_unchanged_without_a_files_key():
    document = build("Risk-Averse Standard",
                     extra=(MUST_NOT_HAVE_CORPUS["Liquidated Damages"]["present"],))
    response = post("", None, raw_messages=[{
        "role": "user",
        "content": f"Check this against the Risk-Averse Standard.\n"
                   f"DOCUMENT START\n{document}\nDOCUMENT END",
    }])
    result = verdict_of(response)
    assert categories_of(result["violations"]) == {"Liquidated Damages"}
    assert_spans_exact(result, document)


def test_the_same_document_reviews_identically_as_text_and_as_an_attachment():
    document = build(VCS, extra=(
        MUST_NOT_HAVE_CORPUS["Uncapped Liability"]["present"],
        NEEDS_REVIEW_CORPUS["Change Of Control"]["present"],
    ))
    as_text = verdict_of(post(document, VCS))
    as_attachment = verdict_of(post_attached([attach(document)]))
    assert as_text == as_attachment


# ---------------------------------------------------------------------------
# E. Latency -- measured and reported, not asserted
# ---------------------------------------------------------------------------

LATENCY_MS: list[float] = []


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_latency_is_measured(policy):
    documents = [
        build(policy),
        build(policy, extra=tuple(
            MUST_NOT_HAVE_CORPUS[c]["present"] for c in POLICIES[policy][MUST_NOT_HAVE])),
        HEADER + FILLER * 40 + build(policy),
    ]
    for document in documents:
        start = time.perf_counter()
        post(document, policy)
        LATENCY_MS.append((time.perf_counter() - start) * 1000)


def test_zz_report_probe_coverage(capsys):
    """Report measured coverage. This is a finite corpus, not a general guarantee."""
    assert PROBE_RESULTS, "run the full module: the probes populate this report"
    probed = {(p, c) for p, c, _v, _ok in PROBE_RESULTS}
    assert probed == set(VIOLATION_ASSIGNMENTS)
    passed = sum(1 for *_x, ok in PROBE_RESULTS if ok)
    variants = sorted({v for _p, _c, v, _ok in PROBE_RESULTS})
    with capsys.disabled():
        print(f"\nfalse-clean probes: {len(VIOLATION_ASSIGNMENTS)} violation assignments "
              f"({len(MUST_HAVE_ASSIGNMENTS)} must_have + {len(MUST_NOT_HAVE_ASSIGNMENTS)} "
              f"must_not_have), {len(PROBE_RESULTS)} documents, variants={variants}, "
              f"no-false-clean {passed}/{len(PROBE_RESULTS)}")
        if LATENCY_MS:
            print(f"latency ms: p50={statistics.median(LATENCY_MS):.1f} "
                  f"max={max(LATENCY_MS):.1f} n={len(LATENCY_MS)} (SLO 2000)")
    assert passed == len(PROBE_RESULTS)
