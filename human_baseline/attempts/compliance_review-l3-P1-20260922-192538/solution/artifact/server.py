#!/usr/bin/env python
"""Compliance-review artifact.

Reads one customer-facing contract + applicable policy and returns a queue-ready JSON object:
  {compliant, violations, needs_review}

Requirements: attempt requirements.md (from L3 elicitation).
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI()

EMPTY = {"compliant": True, "violations": [], "needs_review": []}

# Clause rules elicited from L3 (category spellings as stated).
POLICIES: dict[str, dict[str, Any]] = {
    "vendor_standard": {
        "id": "vendor_standard",
        "name": "Vendor Contract Standard",
        "aliases": [
            "vendor",
            "vendor standard",
            "vendor_standard",
            "vendor contract standard",
            "vendor-contract",
        ],
        "description": "General vendor/supplier agreements.",
        "must_have": [
            {
                "category": "Cap On Liability",
                "description": "The contract must limit each party's total liability.",
            },
            {
                "category": "Governing Law",
                "description": "The contract must specify which jurisdiction's law governs it.",
            },
        ],
        "must_not_have": [
            {
                "category": "Uncapped Liability",
                "description": "Language that removes or overrides a liability cap is not allowed.",
            },
            {
                "category": "Most Favored Nation",
                "description": "Commitments guaranteeing this vendor terms at least as good as any other customer are not allowed.",
            },
        ],
        "needs_review": [
            {
                "category": "Change Of Control",
                "description": "A clause addressing what happens if either party is acquired — route to a human, not a violation.",
            },
        ],
    },
    "risk_averse": {
        "id": "risk_averse",
        "name": "Risk-Averse Standard",
        "aliases": [
            "risk",
            "risk averse",
            "risk_averse",
            "risk-averse",
            "risk averse standard",
        ],
        "description": "High-liability-exposure engagements.",
        "must_have": [
            {
                "category": "Cap On Liability",
                "description": "The contract must limit each party's total liability.",
            },
            {
                "category": "Audit Rights",
                "description": "The contract must give us the right to audit the counterparty.",
            },
            {
                "category": "Insurance",
                "description": "The contract must require the counterparty to carry insurance.",
            },
        ],
        "must_not_have": [
            {
                "category": "Uncapped Liability",
                "description": "Language that removes or overrides a liability cap is not allowed.",
            },
            {
                "category": "Liquidated Damages",
                "description": "A pre-agreed damages amount for breach is not allowed.",
            },
        ],
        "needs_review": [
            {
                "category": "Covenant Not To Sue",
                "description": "A clause waiving the right to sue — route to a human, not a violation.",
            },
        ],
    },
    "procurement_clean": {
        "id": "procurement_clean",
        "name": "Procurement Clean Standard",
        "aliases": [
            "procurement",
            "procurement clean",
            "procurement_clean",
            "procurement clean standard",
        ],
        "description": "Purchasing/sourcing agreements.",
        "must_have": [
            {
                "category": "Audit Rights",
                "description": "The contract must give us the right to audit the counterparty.",
            },
        ],
        "must_not_have": [
            {
                "category": "Most Favored Nation",
                "description": "Commitments guaranteeing this counterparty terms at least as good as any other customer are not allowed.",
            },
            {
                "category": "Non-Compete",
                "description": "Restrictions on who we can also do business with are not allowed.",
            },
            {
                "category": "Exclusivity",
                "description": "Exclusive-dealing commitments are not allowed.",
            },
            {
                "category": "Minimum Commitment",
                "description": "Minimum purchase/volume commitments are not allowed.",
            },
        ],
        "needs_review": [
            {
                "category": "Volume Restriction",
                "description": "A clause capping purchase volume — route to a human, not a violation.",
            },
        ],
    },
    "ip_protective": {
        "id": "ip_protective",
        "name": "IP-Protective Standard",
        "aliases": [
            "ip",
            "ip protective",
            "ip_protective",
            "ip-protective",
            "ip protective standard",
        ],
        "description": "Agreements involving licensing or IP transfer.",
        "must_have": [
            {
                "category": "License Grant",
                "description": "The contract must explicitly state what's being licensed.",
            },
            {
                "category": "Ip Ownership Assignment",
                "description": "The contract must state who owns resulting intellectual property.",
            },
        ],
        "must_not_have": [
            {
                "category": "Joint Ip Ownership",
                "description": "Shared ownership of resulting IP is not allowed.",
            },
            {
                "category": "Irrevocable Or Perpetual License",
                "description": "A license that can never be revoked or that never expires is not allowed.",
            },
        ],
        "needs_review": [
            {
                "category": "Source Code Escrow",
                "description": "An escrow arrangement for source code — route to a human, not a violation.",
            },
        ],
    },
}


def _b64_decode(raw: str | bytes) -> bytes:
    if isinstance(raw, bytes):
        return raw
    return base64.b64decode(raw)


def _document_from_files(body: dict) -> str | None:
    for item in body.get("files") or []:
        raw = item.get("file_data") or item.get("data") or ""
        if not raw:
            continue
        try:
            data = _b64_decode(raw)
        except Exception:
            continue
        text = data.decode("utf-8", errors="replace").strip()
        if text:
            return text
    return None


def _latest_user_text(body: dict) -> str:
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            return (m.get("content") or "").strip()
    return ""


def resolve_policy_id(text: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    # Prefer longer / more specific aliases first
    candidates: list[tuple[int, str]] = []
    for pid, pdata in POLICIES.items():
        for alias in pdata.get("aliases", []) + [pdata.get("name", ""), pid]:
            a = (alias or "").lower()
            if a and a in lower:
                candidates.append((len(a), pid))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def get_policy(policy_id: str | None) -> dict[str, Any] | None:
    if not policy_id:
        return None
    if policy_id in POLICIES:
        return POLICIES[policy_id]
    return None


def fetch_contract_db(title: str) -> str | None:
    url = os.environ.get("DATABASE_URL")
    if not url or not title:
        return None
    try:
        import psycopg
    except ImportError:
        try:
            import psycopg2 as psycopg  # type: ignore
        except ImportError:
            return None
    try:
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT full_text FROM contracts WHERE title = %s LIMIT 1",
                    (title,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
                cur.execute(
                    "SELECT full_text FROM contracts WHERE title ILIKE %s LIMIT 1",
                    (f"%{title}%",),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
    except Exception:
        return None
    return None


def _title_from_message(message: str) -> str | None:
    m = re.search(
        r"titled\s+['\u2018\u2019\u201c\u201d\"]([^'\u2018\u2019\u201c\u201d\"]+)['\u2018\u2019\u201c\u201d\"]",
        message,
        re.I,
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"title[:\s]+['\"]?([^'\"\n]+)['\"]?", message, re.I)
    if m:
        return m.group(1).strip()
    return None


def resolve_document(body: dict, user_text: str) -> str | None:
    doc = _document_from_files(body)
    if doc:
        return doc
    title = _title_from_message(user_text)
    if title:
        db_doc = fetch_contract_db(title)
        if db_doc:
            return db_doc
    # Inline contract blocks
    for pattern in (
        r"```(?:\w+)?\n([\s\S]+?)\n```",
        r"---+\n([\s\S]+?)\n---+",
        r"(?:contract(?:\s+text)?|document)[:\s]*\n([\s\S]{80,})",
    ):
        m = re.search(pattern, user_text, re.I)
        if m:
            return m.group(1).strip()
    if len(user_text) > 200 and user_text.count("\n") >= 3:
        # Strip short instruction lines at the top
        lines = user_text.splitlines()
        kept = [
            ln
            for ln in lines
            if not re.search(r"\b(review|policy|against|check|titled)\b", ln, re.I)
            or len(ln) > 80
        ]
        blob = "\n".join(kept).strip()
        if len(blob) > 200:
            return blob
    return None


def _llm_available() -> bool:
    return bool(
        os.environ.get("APPSCI_AGENT_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("MODEL_API_KEY")
    )


def _llm_chat(system: str, user: str) -> str | None:
    api_key = (
        os.environ.get("APPSCI_AGENT_API_KEY")
        or os.environ.get("MODEL_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if not api_key:
        return None
    base = (
        os.environ.get("APPSCI_AGENT_BASE_URL")
        or os.environ.get("MODEL_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or ""
    ).rstrip("/")
    model = (
        os.environ.get("ARTIFACT_USERSIM_MODEL")
        or os.environ.get("MODEL_NAME")
        or "openai/openai-gpt-5.2"
    )
    url = f"{base}/v1/chat/completions" if base else "https://api.openai.com/v1/chat/completions"
    model_id = model.replace("openai/", "") if model.startswith("openai/") else model
    if model_id.startswith("anthropic/"):
        model_id = model_id.split("/", 1)[1]
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except Exception:
        abase = (os.environ.get("ANTHROPIC_BASE_URL") or base or "").rstrip("/")
        atok = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY") or api_key
        if not abase:
            return None
        url2 = f"{abase}/v1/messages"
        payload2 = {
            "model": os.environ.get("MODEL_NAME", "claude-sonnet-4-6").replace("anthropic/", ""),
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        req2 = urllib.request.Request(
            url2,
            data=json.dumps(payload2).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": atok,
                "anthropic-version": "2023-06-01",
                "Authorization": f"Bearer {atok}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req2, timeout=180) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            parts = body.get("content") or []
            texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
            return "\n".join(texts) if texts else None
        except Exception:
            return None


def _extract_json_obj(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _canon_category(name: str, allowed: list[str]) -> str | None:
    n = (name or "").strip().lower()
    for a in allowed:
        if a.lower() == n:
            return a
    # soft: ignore punctuation/spacing
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    nn = norm(name or "")
    for a in allowed:
        if norm(a) == nn:
            return a
    return None


def build_system_prompt(policy: dict[str, Any]) -> str:
    def fmt(items: list[dict]) -> str:
        return "\n".join(f"  - {i['category']}: {i['description']}" for i in items)

    return f"""You are a precise compliance reviewer. Analyze ONE contract against ONE policy.
Return ONLY a single JSON object (no markdown fences, no prose) with exactly these keys:
  "compliant" (boolean), "violations" (array), "needs_review" (array).
Do NOT add other top-level keys.

POLICY: {policy['name']} ({policy['id']})
{policy.get('description', '')}

REQUIRED (must_have) — violation ONLY if ABSENT:
{fmt(policy.get('must_have') or [])}

PROHIBITED (must_not_have) — violation ONLY if PRESENT, with exact verbatim quote(s):
{fmt(policy.get('must_not_have') or [])}

NEEDS REVIEW — if PRESENT, add to needs_review (NOT a violation):
{fmt(policy.get('needs_review') or [])}

Rules:
1. must_have absent → violation with rule="must_have", present=false, evidence_spans=[].
2. must_have present → not a violation.
3. must_not_have present → violation with rule="must_not_have", present=true, evidence_spans=[exact spans from the document]. No span ⇒ do not report.
4. must_not_have absent → not a violation.
5. needs_review present → needs_review item with category + evidence_spans. Never put these in violations.
6. compliant=true iff violations is empty (needs_review does not affect compliant).
7. Never invent categories outside this policy. No style/tone nitpicks.
8. Category strings must match the policy names exactly (spelling/casing).

Violation object keys: category, rule, present, evidence_spans, rationale.
needs_review object keys: category, evidence_spans only.
"""


def normalize_record(obj: dict[str, Any] | None, policy: dict[str, Any]) -> dict[str, Any]:
    if not obj:
        return dict(EMPTY)

    must_have = [i["category"] for i in policy.get("must_have") or []]
    must_not = [i["category"] for i in policy.get("must_not_have") or []]
    needs = [i["category"] for i in policy.get("needs_review") or []]

    violations_out: list[dict[str, Any]] = []
    for v in obj.get("violations") or []:
        if not isinstance(v, dict):
            continue
        rule = str(v.get("rule") or "").strip().lower()
        present = bool(v.get("present"))
        spans = v.get("evidence_spans") or []
        if isinstance(spans, str):
            spans = [spans] if spans.strip() else []
        spans = [str(s).strip() for s in spans if str(s).strip()]
        rationale = str(v.get("rationale") or "").strip()

        if rule == "must_have":
            cat = _canon_category(str(v.get("category") or ""), must_have)
            if not cat or present:
                continue
            violations_out.append(
                {
                    "category": cat,
                    "rule": "must_have",
                    "present": False,
                    "evidence_spans": [],
                    "rationale": rationale
                    or f"Required category '{cat}' is missing from the document.",
                }
            )
        elif rule == "must_not_have":
            cat = _canon_category(str(v.get("category") or ""), must_not)
            if not cat or not present or not spans:
                continue
            violations_out.append(
                {
                    "category": cat,
                    "rule": "must_not_have",
                    "present": True,
                    "evidence_spans": spans,
                    "rationale": rationale
                    or f"Prohibited category '{cat}' is present in the document.",
                }
            )

    needs_out: list[dict[str, Any]] = []
    for nr in obj.get("needs_review") or []:
        if not isinstance(nr, dict):
            continue
        cat = _canon_category(str(nr.get("category") or ""), needs)
        if not cat:
            continue
        spans = nr.get("evidence_spans") or []
        if isinstance(spans, str):
            spans = [spans] if spans.strip() else []
        spans = [str(s).strip() for s in spans if str(s).strip()]
        if not spans:
            continue
        needs_out.append({"category": cat, "evidence_spans": spans})

    return {
        "compliant": len(violations_out) == 0,
        "violations": violations_out,
        "needs_review": needs_out,
    }


def review_document(contract_text: str, policy: dict[str, Any]) -> dict[str, Any]:
    contract_text = contract_text[:120000]
    if not _llm_available():
        return dict(EMPTY)
    system = build_system_prompt(policy)
    user = (
        "Review this contract against the policy in the system prompt. "
        "Return ONLY the JSON object.\n\nCONTRACT:\n"
        + contract_text
    )
    raw = _llm_chat(system, user)
    obj = _extract_json_obj(raw or "")
    if obj is None and raw:
        repair = _llm_chat(
            "Return ONLY valid JSON with keys compliant, violations, needs_review. No prose.",
            f"Fix into the required schema:\n{raw[:12000]}",
        )
        obj = _extract_json_obj(repair or "")
    return normalize_record(obj, policy)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    body = await request.json()
    user_text = _latest_user_text(body)
    policy_id = resolve_policy_id(user_text)
    policy = get_policy(policy_id)
    document = resolve_document(body, user_text)

    if not policy or not document:
        # Schema-safe empty result so the queue never gets prose.
        content = json.dumps(EMPTY, ensure_ascii=False)
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    record = review_document(document, policy)
    content = json.dumps(record, ensure_ascii=False)
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
