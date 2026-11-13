#!/usr/bin/env python
"""Turn a single customer-call transcript into the strict CRM record.

The record is a JSON object with exactly four keys -- `next_steps`, `risks`, `commitments`
(arrays of short, distinct strings) and `sentiment` (one of POSITIVE / NEUTRAL / NEGATIVE).
A downstream fan-out job ingests it verbatim with no human in between, so the shape is
non-negotiable and both failure modes cost the same:
  - inventing a detail that was never said, and
  - dropping a commitment we actually made.

Extraction is done by Claude with the schema forced through tool-use, so the model can only
return the four keys with the right types. `validate_and_normalize` is the second line of
defence, and every failure path degrades to a schema-valid EMPTY_RECORD rather than raising --
the endpoint must never hand the downstream job something it cannot parse.

The request wire format is not guaranteed, so `extract_transcript` is deliberately shape-agnostic
(see its docstring for the shapes A-H it recovers the transcript from).
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any
from urllib.parse import unquote

log = logging.getLogger("cci.insights")

SENTIMENTS = ("POSITIVE", "NEUTRAL", "NEGATIVE")
LIST_KEYS = ("next_steps", "risks", "commitments")

# Field names that carry the transcript when the payload is structured rather than a bare string.
# "Strong" names are unambiguous transcript carriers and are preferred over the messages array;
# "weak" names are generic and only consulted after messages.
_STRONG_FIELDS = ("transcript", "call", "dialogue", "conversation")
_WEAK_FIELDS = ("input", "prompt", "text")
_TRANSCRIPT_FIELDS = _STRONG_FIELDS + _WEAK_FIELDS

# A valid record that asserts nothing -- the safe answer when there is no transcript, no model
# credentials, or the model call fails. Empty lists + NEUTRAL is always ingestible and never
# invents detail (it only ever risks the recall KPI, which is unavoidable without a model).
EMPTY_RECORD: dict[str, Any] = {
    "next_steps": [],
    "risks": [],
    "commitments": [],
    "sentiment": "NEUTRAL",
}

# Default model: strong enough for faithful extraction, fast enough to land before the rep writes
# their own notes. Override with CCI_MODEL (e.g. claude-opus-5) if recall needs it.
MODEL = os.environ.get("CCI_MODEL", "claude-sonnet-5")

# Forcing tool_choice onto this tool guarantees the four keys, the array-of-strings types, and the
# sentiment enum at the API boundary -- the model cannot emit prose or a malformed shape.
RECORD_TOOL = {
    "name": "emit_call_record",
    "description": "Emit the structured CRM record for this one call. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "next_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Every open forward action from the call (superset): our commitments plus customer-side actions and awaited outcomes.",
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Explicitly surfaced threats to the account or deal.",
            },
            "commitments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The subset of next_steps our side is on the hook for (our own promises).",
            },
            "sentiment": {
                "type": "string",
                "enum": list(SENTIMENTS),
                "description": "The customer's overall tone on the call.",
            },
        },
        "required": ["next_steps", "risks", "commitments", "sentiment"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """\
You convert ONE customer-call transcript into a structured CRM record. The record is written
straight into the CRM by an automated job with no human reviewing it, so two rules are absolute and
matter equally:

1. NEVER invent. Every item you output must trace to something explicitly said in this transcript.
   Do not infer, assume, guess, or add detail that "was probably meant". When in doubt, leave it
   out -- omission is safer than an unsupported claim.
2. NEVER drop a promise. Every commitment or agreement made on the call must appear -- especially
   ones OUR side (the rep / account manager / company) made to the customer.

Emit the record only by calling the `emit_call_record` tool, exactly once. Fill every field:

- next_steps: EVERY open, forward-looking action from the call -- everything that still needs to happen
  next, no matter who owns it: things we will do, things the customer will do, and outcomes being
  awaited. One action per string, short.
  e.g. "Send the revised quote by tomorrow." / "Customer to get finance sign-off before signing."
- commitments: the SUBSET of next_steps that WE (the rep / account manager / company) are on the hook
  for -- our own promises. One per string, phrased as what we will do.
  e.g. "We will send a revised quote by tomorrow."

Every commitment is also a next step, but not every next step is a commitment: an open item we are not
on the hook for -- for example the customer getting back to us after checking with their own team -- is
a next step only. So list each of our own promises in BOTH fields (as an action in next_steps and as a
promise in commitments), and list customer-side actions or awaited outcomes in next_steps only. Do not
restate a risk as a next step, and do not add any action that was not actually said. next_steps is empty
only when the call ended with nothing still open.
- risks: threats to the account or deal that were actually surfaced on the call -- dissatisfaction,
  escalations, churn signals, competitive evaluation, blocking bugs, or a material downsell.
  e.g. "Renewal at risk; customer is evaluating competitors after repeated outages."
- sentiment: the CUSTOMER's overall tone across the call. Exactly one of POSITIVE, NEUTRAL, NEGATIVE.

Each list holds short, distinct strings; use an empty list [] for any category with nothing to
report. A quiet check-in where nothing was agreed is legitimately all empty lists with NEUTRAL
sentiment -- do not manufacture items to fill it.

If the input contains no actual call content -- only an identifier, a reference, a filename, or a
request with no transcript to read -- return all empty lists with NEUTRAL sentiment. Never guess
what such a call might have contained.
"""


# --------------------------------------------------------------------------------------------------
# Transcript extraction -- shape-agnostic.
#
# The POST /respond envelope is fixed ({"messages": [...]}), but where the transcript sits inside the
# request body is not guaranteed. extract_transcript recovers it from any of:
#   A  plain string in the last user message
#   B  content as a list of blocks ([{"type":"text","text":...}, ...])
#   C  a system message plus the transcript in the user message
#   D  the transcript wrapped in framing prose
#   E  a bare call_id / reference with no transcript -> no DB dependency, so we pass it through and
#      the prompt makes the model return an empty record rather than inventing a call it cannot see
#   F  a JSON / structured payload (e.g. a serialized DB row) carrying a `transcript` field
#   G  a decodable attachment -- a data: URI or a file/document block with inline text
#   H  the payload at the top level of the body (body["transcript"] / body["input"] / ...)
# An opaque external attachment (a bare file_id or https URL with no inline bytes) cannot be fetched
# from here; it degrades to the safe empty record rather than guessing.
# --------------------------------------------------------------------------------------------------


def extract_transcript(payload: Any) -> str:
    """Return the best transcript text from a request body (dict), a messages list, or a string.

    Yields candidates from the most authoritative location to the least and returns the first
    non-empty one. Returns "" when nothing usable is present (-> EMPTY_RECORD downstream).
    """
    for candidate in _candidate_texts(payload):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _candidate_texts(payload: Any):
    """Yield transcript-text candidates in priority order for the given payload."""
    if isinstance(payload, dict):
        # H/F: explicit transcript-bearing fields at the top level of the body.
        for key in _STRONG_FIELDS:
            if key in payload:
                text = _coerce_field(payload[key])
                if text and text.strip():
                    yield text
        # A-D: the standard messages array.
        if isinstance(payload.get("messages"), list):
            yield from _from_messages(payload["messages"])
        # H: generic fallback fields, only after messages.
        for key in _WEAK_FIELDS:
            if key in payload:
                text = _coerce_field(payload[key])
                if text and text.strip():
                    yield text
        return
    if isinstance(payload, list):  # a messages list passed directly (back-compat)
        yield from _from_messages(payload)
        return
    if isinstance(payload, str):  # a bare string body
        yield _unwrap(payload)


def _from_messages(messages: Any):
    """Yield transcript candidates from an OpenAI-style messages list (last user, last, all)."""
    if not isinstance(messages, list):
        return
    users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    if users:
        yield _unwrap(_flatten_content(users[-1].get("content")))
    if messages and isinstance(messages[-1], dict):
        yield _unwrap(_flatten_content(messages[-1].get("content")))
    joined = "\n".join(
        _flatten_content(m.get("content")) for m in messages if isinstance(m, dict)
    )
    if joined.strip():
        yield _unwrap(joined)


def _coerce_field(value: Any, depth: int = 0) -> str:
    """Turn an arbitrary field value (string / list of blocks / nested object) into text."""
    if isinstance(value, str):
        return _unwrap(value, depth)
    if isinstance(value, list):
        return _unwrap(_flatten_content(value), depth)
    if isinstance(value, dict):
        return _from_obj(value, depth)
    return ""


def _flatten_content(content: Any) -> str:
    """Turn a message `content` (string, or a list of content/attachment blocks) into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_text_from_block(block) for block in content]
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return _text_from_block(content)
    return str(content)


def _text_from_block(block: Any) -> str:
    """Extract text from one content block: a string, a text block, or a decodable attachment (G)."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    for key in ("text", "input_text", "output_text"):
        val = block.get(key)
        if isinstance(val, str) and val.strip():
            return val
    inner = block.get("content")
    if isinstance(inner, str) and inner.strip():
        return inner
    if isinstance(inner, list):
        return _flatten_content(inner)
    # Attachment / file / document blocks: recover only inline or decodable text.
    for src_key in ("source", "file", "file_data", "data", "document", "attachment"):
        text = _text_from_source(block.get(src_key))
        if text:
            return text
    for url_key in ("url", "file_url"):
        url = block.get(url_key)
        if isinstance(url, str):
            decoded = _try_data_uri(url)
            if decoded:
                return decoded
    return ""


def _text_from_source(src: Any) -> str:
    """Recover text from an attachment source: a data: URI, or a dict with inline text bytes."""
    if isinstance(src, str):
        return _try_data_uri(src) or ""
    if not isinstance(src, dict):
        return ""
    media = str(src.get("media_type") or src.get("mime_type") or "")
    is_base64 = "base64" in str(src.get("type") or "") or "base64" in media
    for key in ("text", "data", "content"):
        val = src.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        decoded = _try_data_uri(val)
        if decoded is not None:
            return decoded
        if is_base64:  # raw base64 bytes (e.g. {"type":"base64","media_type":"text/plain",...})
            try:
                return base64.b64decode(val, validate=False).decode("utf-8")
            except Exception:  # noqa: BLE001 -- not decodable text; fall through
                pass
        if media.startswith("text") or key in ("text", "content"):
            return val
    for url_key in ("url", "file_url"):
        url = src.get(url_key)
        if isinstance(url, str):
            decoded = _try_data_uri(url)
            if decoded:
                return decoded
    return ""


def _try_data_uri(s: str) -> str | None:
    """Decode a `data:` URI to text, or return None if it isn't a decodable text data URI."""
    if not isinstance(s, str) or not s.startswith("data:"):
        return None
    try:
        header, sep, data = s.partition(",")
        if not sep:
            return None
        if ";base64" in header:
            text = base64.b64decode(data, validate=False).decode("utf-8")
        else:
            text = unquote(data)
        return text if text.strip() else None
    except Exception:  # noqa: BLE001 -- non-text or malformed data URIs are simply skipped
        return None


def _unwrap(s: Any, depth: int = 0) -> str:
    """Unwrap a string that is itself a data: URI or a JSON blob carrying a transcript field (F/G)."""
    if not isinstance(s, str):
        return ""
    text = s.strip()
    if not text or depth > 3:
        return text
    decoded = _try_data_uri(text)
    if decoded is not None:
        return decoded.strip()
    if text[0] in "{[":
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if obj is not None:
            inner = _from_obj(obj, depth + 1)
            if inner and inner.strip():
                return inner.strip()
            # Not a recognised carrier -- hand the raw JSON to the model, which can still read it.
    return text


def _from_obj(obj: Any, depth: int = 0) -> str:
    """Find transcript text inside a parsed JSON object/list (transcript field, or nested messages)."""
    if depth > 4:
        return ""
    if isinstance(obj, str):
        return _unwrap(obj, depth + 1)
    if isinstance(obj, list):
        if any(isinstance(x, dict) and "role" in x for x in obj):
            for text in _from_messages(obj):
                if text.strip():
                    return text
        return _flatten_content(obj)
    if isinstance(obj, dict):
        for key in _TRANSCRIPT_FIELDS:
            if key in obj:
                text = _coerce_field(obj[key], depth + 1)
                if text and text.strip():
                    return text.strip()
        if isinstance(obj.get("messages"), list):
            for text in _from_messages(obj["messages"]):
                if text.strip():
                    return text
    return ""


# --------------------------------------------------------------------------------------------------
# Record construction and validation.
# --------------------------------------------------------------------------------------------------


def validate_and_normalize(obj: Any) -> dict[str, Any]:
    """Coerce arbitrary model/tool output into a contract-valid record.

    Guarantees: exactly the four keys; the three list fields are deduped (case-insensitive,
    order-preserving) lists of trimmed non-empty strings; sentiment is upper-cased and forced into
    the enum (NEUTRAL if unrecognised). Never raises.
    """
    if not isinstance(obj, dict):
        obj = {}

    record: dict[str, Any] = {}
    for key in LIST_KEYS:
        record[key] = _clean_list(obj.get(key))

    sentiment = obj.get("sentiment")
    sentiment = sentiment.strip().upper() if isinstance(sentiment, str) else ""
    record["sentiment"] = sentiment if sentiment in SENTIMENTS else "NEUTRAL"
    return record


def _clean_list(value: Any) -> list[str]:
    """Normalise a value into a deduped list of trimmed, non-empty strings."""
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            item = str(item) if item is not None else ""
        item = item.strip()
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _client():
    """Construct the Anthropic client. Reads ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL from env."""
    import anthropic  # imported lazily so the module loads even if the SDK is absent

    return anthropic.Anthropic()


def build_record(transcript: str) -> dict[str, Any]:
    """Produce the CRM record for one transcript. Never raises -- degrades to EMPTY_RECORD."""
    if not transcript or not transcript.strip():
        return dict(EMPTY_RECORD)

    try:
        client = _client()
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[RECORD_TOOL],
            tool_choice={"type": "tool", "name": "emit_call_record"},
            messages=[{"role": "user", "content": transcript}],
        )
        for block in message.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "emit_call_record":
                return validate_and_normalize(block.input)
        log.warning("model returned no emit_call_record tool_use block; using empty record")
        return dict(EMPTY_RECORD)
    except Exception:  # noqa: BLE001 -- any failure must still yield a valid record
        log.exception("build_record failed; returning empty record")
        return dict(EMPTY_RECORD)
