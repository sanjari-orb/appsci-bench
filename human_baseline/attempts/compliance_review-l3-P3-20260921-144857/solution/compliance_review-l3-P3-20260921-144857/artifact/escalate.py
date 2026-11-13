#!/usr/bin/env python
"""Optional LLM adjudication for unresolved prohibited-clause cases. DISABLED BY DEFAULT.

Gating (both required, neither defaulted -- absence means disabled):

  COMPLIANCE_ESCALATION_ENABLED=1     explicit opt-in
  COMPLIANCE_ESCALATION_MODEL=<id>    explicit model id; no model is hard-coded here

Local credentials in the environment do NOT imply an approved runtime endpoint. Escalation stays
off for real customer documents until the privacy/endpoint decision is confirmed; the tests
exercise it only through an injected fake client over synthetic text.

Safety rule: escalation may only ever UPGRADE an unresolved case to a violation, and only when it
returns a span that verifies as an exact substring of the document. It can never downgrade an
unresolved case to clean, and it can never remove a violation the deterministic core confirmed.
Every failure path (disabled, missing dependency, timeout, refusal, malformed JSON, unverifiable
span) leaves the unresolved case unresolved.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

ENABLE_ENV = "COMPLIANCE_ESCALATION_ENABLED"
MODEL_ENV = "COMPLIANCE_ESCALATION_MODEL"
TIMEOUT_ENV = "COMPLIANCE_ESCALATION_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 1.2  # the artifact's own latency SLO is 2000 ms end to end
MAX_DOCUMENT_CHARS = 60_000

_TRUE = {"1", "true", "yes", "on"}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "resolution": {"type": "string", "enum": ["present", "unsure"]},
                    "span": {"type": "string"},
                },
                "required": ["category", "resolution", "span"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You adjudicate whether a named prohibited contract clause is actually present in a document. "
    "For each category you are asked about, answer 'present' ONLY if the document contains text "
    "that establishes the clause, and quote that text verbatim from the document as `span`. "
    "If you cannot quote verbatim supporting text, answer 'unsure' with an empty span. "
    "Never answer that a clause is absent, and never invent or paraphrase a span."
)


def escalation_config(env: Mapping[str, str] | None = None) -> tuple[bool, str | None, float]:
    """(enabled, model_id, timeout_s). Enabled requires the flag AND an explicit model id."""
    env = os.environ if env is None else env
    flag = str(env.get(ENABLE_ENV, "")).strip().lower() in _TRUE
    model = (env.get(MODEL_ENV) or "").strip() or None
    try:
        timeout = float(env.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    return (flag and model is not None), model, timeout


def is_enabled(env: Mapping[str, str] | None = None) -> bool:
    return escalation_config(env)[0]


def _build_prompt(document: str, policy: str, categories: list[str]) -> str:
    return (
        f"Policy under review: {policy}\n"
        f"Prohibited clause categories to adjudicate: {', '.join(categories)}\n\n"
        "DOCUMENT START\n"
        f"{document[:MAX_DOCUMENT_CHARS]}\n"
        "DOCUMENT END"
    )


def _default_client() -> Any:
    from anthropic import AsyncAnthropic  # imported lazily: not a declared dependency

    return AsyncAnthropic()


async def adjudicate(
    document: str,
    policy: str,
    unresolved: list[dict],
    client: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> list[dict]:
    """Try to upgrade unresolved cases to confirmed violations.

    Returns a list of {"category", "span"} for cases the adjudicator confirmed with a span that
    verifies against `document`. Returns [] on every failure path, which leaves the unresolved
    cases unresolved -- so a document that could not be checked never becomes compliant: true.
    """
    enabled, model, timeout = escalation_config(env)
    if not enabled or not unresolved:
        return []

    categories = [case["category"] for case in unresolved]
    try:
        if client is None:
            client = _default_client()
        response = await client.with_options(timeout=timeout, max_retries=0).messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_prompt(document, policy, categories)}],
            output_config={"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
        )
        text = next(block.text for block in response.content if block.type == "text")
        payload = json.loads(text)
        findings = payload["findings"]
    except Exception:
        return []  # disabled, missing dep, timeout, refusal, malformed output -> stay unresolved

    upgrades: list[dict] = []
    wanted = set(categories)
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, dict):
            continue
        category = finding.get("category")
        span = finding.get("span")
        if finding.get("resolution") != "present":
            continue
        if category not in wanted or not isinstance(span, str):
            continue
        span = span.strip()
        if not span or span not in document:
            continue  # unverifiable quote: the case stays unresolved
        upgrades.append({"category": category, "span": span[:400]})
    return upgrades
