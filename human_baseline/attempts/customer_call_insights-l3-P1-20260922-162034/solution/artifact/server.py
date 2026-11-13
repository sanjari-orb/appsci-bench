#!/usr/bin/env python
"""Customer-call insights artifact.

Reads one call transcript and returns a CRM-ready JSON object with:
  next_steps, risks, commitments, sentiment

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

SENTIMENTS = {"POSITIVE", "NEUTRAL", "NEGATIVE"}
EMPTY = {
    "next_steps": [],
    "risks": [],
    "commitments": [],
    "sentiment": "NEUTRAL",
}

SYSTEM_PROMPT = """You extract a structured CRM record from ONE customer-call transcript.

Return ONLY a single JSON object (no markdown fences, no prose) with exactly these keys:
- "next_steps": array of short strings — things still open when the call ends, with a real owner.
  Include waits on the customer as well as what we owe. Skip unowned "someone should…" items,
  world-facts that aren't actions, and things already handled.
- "commitments": array of short strings — undertakings OUR side took on toward the customer.
  Keep hedges ("look into a discount", not "provide a discount"). Internal work the customer is
  still owed counts. Do NOT log requests we never accepted.
- "risks": array of short strings — warning signs actually SAID on the call that the account/deal
  could go badly (competitor eval, money pulled, blocked sign-off, unresolved issue at end,
  date sliding, champion leaving). Do NOT treat normal buying, plain negotiation, resolved
  issues, or tone alone as risks. A warm call can still have a real risk — log both.
- "sentiment": one of "POSITIVE", "NEUTRAL", "NEGATIVE" (uppercase). NEUTRAL is the default for
  businesslike calls. One sharp moment does not flip an otherwise fine call.

Hard rules:
- NEVER invent. Every item must be traceable to something said on the call.
- NEVER drop a real promise or risk.
- Quiet check-ins with nothing agreed → empty arrays, not padding. If the call had real content, be complete.
- No speaker labels, quotes, or turn numbers in items. Phrase like a CRM note ("send revised pricing by Friday"), not "Alex will…".
- One distinct thing per string (don't join with "and"). Do not repeat the same item twice in a list.
- CRITICAL: a promise is BOTH a commitment AND a next step. Put the SAME phrasing in BOTH lists.

Empty lists are written []. Extra keys are ignored — do not add them.
"""


def _b64_decode(raw: str | bytes) -> bytes:
    if isinstance(raw, bytes):
        return raw
    return base64.b64decode(raw)


def _transcript_from_files(body: dict) -> str | None:
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


def _looks_like_transcript(text: str) -> bool:
    if len(text) < 80:
        return False
    # common shapes: Speaker: line, or many newlines of dialogue
    if re.search(r"(?m)^[A-Za-z][A-Za-z ._-]{0,40}:\s+\S", text):
        return True
    if text.count("\n") >= 4 and len(text) > 200:
        return True
    return False


def _call_id_from_text(text: str) -> str | None:
    m = re.search(r"\bcall[_\s-]?id\b[:\s#]*([A-Za-z0-9_-]+)", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", text, re.I)
    if m:
        return m.group(1)
    # bare alphanumeric id if message is short
    m = re.fullmatch(r"[A-Za-z0-9_-]{4,64}", text.strip())
    if m:
        return m.group(0)
    return None


def _fetch_transcript_db(call_id: str) -> str | None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT transcript FROM calls WHERE call_id = %s LIMIT 1",
                    (call_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
    except Exception:
        return None
    return None


def resolve_transcript(body: dict) -> str | None:
    t = _transcript_from_files(body)
    if t:
        return t
    user = _latest_user_text(body)
    if not user:
        return None
    cid = _call_id_from_text(user)
    if cid:
        db_t = _fetch_transcript_db(cid)
        if db_t:
            return db_t
    if _looks_like_transcript(user):
        return user
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
    # OpenAI-compatible
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
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
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
            "max_tokens": 2000,
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
            with urllib.request.urlopen(req2, timeout=120) as resp:
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


def _norm_key(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\b(alex|jordan|rep|customer|i|we|they)\b", " ", s)
    s = re.sub(r"\b(will|to|the|a|an|and|our|their)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _as_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, list):
        out: list[str] = []
        seen: set[str] = set()
        for x in val:
            s = str(x).strip()
            if not s:
                continue
            k = _norm_key(s)
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return out
    return []


def _has_similar(items: list[str], candidate: str) -> bool:
    ck = _norm_key(candidate)
    if not ck:
        return True
    for s in items:
        sk = _norm_key(s)
        if not sk:
            continue
        if ck == sk or ck in sk or sk in ck:
            return True
    return False


def normalize_record(obj: dict[str, Any] | None) -> dict[str, Any]:
    if not obj:
        return dict(EMPTY)
    next_steps = _as_str_list(obj.get("next_steps") or obj.get("next_step"))
    risks = _as_str_list(obj.get("risks") or obj.get("risk"))
    commitments = _as_str_list(obj.get("commitments") or obj.get("commitment"))
    sent = str(obj.get("sentiment") or "NEUTRAL").strip().upper()
    if sent not in SENTIMENTS:
        low = sent.lower()
        if "pos" in low:
            sent = "POSITIVE"
        elif "neg" in low:
            sent = "NEGATIVE"
        else:
            sent = "NEUTRAL"

    # Dual-list: every commitment still open belongs in next_steps (same phrasing preferred).
    for c in commitments:
        if not _has_similar(next_steps, c):
            next_steps.append(c)

    return {
        "next_steps": next_steps,
        "risks": risks,
        "commitments": commitments,
        "sentiment": sent,
    }


def extract_record(transcript: str) -> dict[str, Any]:
    transcript = transcript[:60000]
    user = (
        "Extract the CRM record from this transcript. "
        "Remember: promises go in BOTH next_steps and commitments.\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )
    if not _llm_available():
        return dict(EMPTY)
    raw = _llm_chat(SYSTEM_PROMPT, user)
    obj = _extract_json_obj(raw or "")
    if obj is None and raw:
        # one repair pass
        repair = _llm_chat(
            "Return ONLY valid JSON with keys next_steps, risks, commitments, sentiment. No prose.",
            f"Fix this into the required schema:\n{raw[:8000]}",
        )
        obj = _extract_json_obj(repair or "")
    return normalize_record(obj)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    body = await request.json()
    transcript = resolve_transcript(body)
    if not transcript:
        # Still return valid schema so downstream doesn't choke on prose errors.
        content = json.dumps(EMPTY, ensure_ascii=False)
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    record = extract_record(transcript)
    content = json.dumps(record, ensure_ascii=False)
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
