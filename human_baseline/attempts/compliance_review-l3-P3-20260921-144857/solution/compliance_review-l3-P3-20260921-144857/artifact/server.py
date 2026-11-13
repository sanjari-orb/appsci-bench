#!/usr/bin/env python
"""Compliance review artifact.

Reviews one document against one named policy and returns a machine-parsed verdict as the
assistant message content: exactly one bare JSON object with exactly three keys --

    {"compliant": bool, "violations": [...], "needs_review": [...]}

compliant is true only when violations is empty AND the document was genuinely checkable.
A document that cannot be checked never comes back as a clean verdict: it returns a non-2xx JSON
error carrying no verdict keys at all.

Transport contract (from the scaffold, unchanged):
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]} => {"choices": [{"message": {"role", "content"}}]}
  Binds 0.0.0.0:$PORT, injected by the deployer.

The one reply that is not a verdict is the transport handshake (see TRANSPORT_PROBE): it answers a
session client's fixed connectivity probe with a status object. It carries none of the three verdict
keys, precisely because no contract was reviewed.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import escalate
from detect import MUST_NOT_HAVE, resolve
from policies import (
    find_policy_mentions,
    known_policies,
    looks_like_named_standard,
    resolve_policy_name,
)

logging.basicConfig(level=os.environ.get("COMPLIANCE_LOG_LEVEL", "INFO"))
logger = logging.getLogger("compliance_review")

app = FastAPI()
app.state.escalation_client = None  # tests inject a fake; production leaves escalation disabled

DOCUMENT_KEYS = ("document", "text", "full_text", "contract")
POLICY_KEYS = ("policy", "policy_name", "standard")

# Documented transport contract: an optional top-level "files" array may carry the document as a
# base64 TXT attachment, with the instruction and policy left in the messages. Only file_type and
# file_data are read -- never file_name, which carries no contract meaning. A request that attaches
# something the artifact cannot read fails visibly; it never falls back to reviewing the
# instruction text, which would be a verdict about text nobody sent as a contract.
ATTACHMENT_TEXT_TYPES = frozenset({"txt", "text", "plain", "text/plain", "text/txt", "ascii", "utf-8"})

# An explicit document boundary is required for prose input. There is deliberately no
# "longest text block" fallback: guessing which text is the contract risks reviewing the
# instructions instead of the document.
FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", re.S)
TRIPLE_QUOTE_RE = re.compile(r'"""(.*?)"""', re.S)
DELIMITED_RE = re.compile(
    r"(?:-{3,}\s*)?(?:BEGIN|START)\s+(?:OF\s+)?(?:DOCUMENT|CONTRACT|AGREEMENT)\s*(?:-{3,})?\s*(.*?)"
    r"\s*(?:-{3,}\s*)?(?:END)\s+(?:OF\s+)?(?:DOCUMENT|CONTRACT|AGREEMENT)",
    re.S | re.I,
)
DOCUMENT_START_RE = re.compile(r"DOCUMENT\s+START\s*(.*?)\s*DOCUMENT\s+END", re.S | re.I)
# An envelope is still an envelope when the transport wraps it: a client that fences its JSON, or
# prefixes a byte-order mark, is sending the same request. Anchored to the whole message, so a
# fenced *contract* is untouched -- that content does not parse as an object carrying a document.
ENVELOPE_FENCE_RE = re.compile(r"\A```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?```\Z", re.S)
INVISIBLE_PREFIX = "\ufeff\u200b\u200e\u200f"

MARKER_RE = re.compile(
    r"(?:\A|\n)[ \t]*(?:document|contract|agreement)(?:\s+text)?[ \t]*:[ \t]*\r?\n?(.+)\Z",
    re.S | re.I,
)

# Revised interface decision: prose no longer needs the literal "Document:" marker. An ordinary
# request is accepted when the instruction and the contract body separate unambiguously -- a short
# leading instruction block whose last line ends in a colon, then a blank line, then the contract
# block. Everything before the split is instruction: it is never contract evidence, and it is the
# only part scanned for a policy name. Anything less clear-cut stays un-checkable (422): no blank
# line, no colon, or a leading block too long to be an instruction.
_INSTRUCTION_LINE = r"[^\n]*\S[^\n]*"
INSTRUCTION_SPLIT_RE = re.compile(
    rf"\A(?P<instruction>(?:{_INSTRUCTION_LINE}\r?\n){{0,2}}[^\n]*\S[ \t]*:)"
    r"[ \t]*\r?\n[ \t]*\r?\n(?P<document>.+)\Z",
    re.S,
)
INSTRUCTION_MAX_CHARS = 400

# The colon forms are open-ended -- the contract block runs to the end of the message -- so a
# request written *after* it would otherwise be reviewed as contract text. That is not hypothetical:
# a request quoting a required clause ("confirm whether aggregate liability shall not exceed the
# fees paid") would satisfy that requirement and produce a clean verdict on a document that lacks
# it, and a request naming prohibited concepts would be quoted back as an evidence span. A final
# short block addressed to the reviewer is therefore returned to the instructions. Cues are either
# second-person address or a leading imperative verb; a contract clause states what a party does
# ("Buyer may review the records"), so it matches neither. Terminated boundaries never reach here.
REQUEST_CUE_RE = re.compile(
    r"\b(?:please|kindly|let me know|tell me|i need|i want|i'd like|can you|could you|would you|"
    r"do you|did you|should i|shall i|what do you|any thoughts)\b"
    r"|\A(?:also|and|now|then|finally|additionally|lastly)?[ \t,]*"
    r"(?:flag|check|confirm|verify|assess|review|identify|list|highlight|summarise|summarize|note)\b"
    r"|\?[ \t]*\Z",
    re.I,
)
TRAILING_REQUEST_MAX_CHARS = 400
_BLANK_LINE_RE = re.compile(r"(\r?\n[ \t]*\r?\n)")


def strip_trailing_request(document: str) -> str:
    """Return `document` without any trailing block that is a request to the reviewer."""
    blocks = _BLANK_LINE_RE.split(document)   # content, separator, content, ...
    while len(blocks) >= 3:
        tail = blocks[-1].strip()
        if len(tail) > TRAILING_REQUEST_MAX_CHARS or not REQUEST_CUE_RE.search(tail):
            break
        blocks = blocks[:-2]
    return "".join(blocks)


# The session client opens a session with a fixed probe and needs a 200 before it will send any
# case. The probe carries no contract and no policy, so every review path would reject it as
# un-checkable (422 document_missing) and the session would never start. It is answered ahead of
# extraction with a status object that is deliberately *not* a verdict: nothing was reviewed, so it
# must be impossible to read the reply as "this document is clean".
TRANSPORT_PROBE = "conformance probe: please respond"
PROBE_RESPONSE = {"status": "ok"}


def _probe_form(text: str) -> str:
    """Whitespace- and case-insensitive form, so the probe survives transport reformatting."""
    return " ".join(text.split()).casefold()


_PROBE_FORM = _probe_form(TRANSPORT_PROBE)


def is_transport_probe(body: dict) -> bool:
    """True only for a request that is nothing but the probe.

    Each test below is a way the request could be carrying real work; any one of them sends the
    request down the ordinary review path instead -- a document or policy field beside the messages,
    conversation history, a non-user role, or any text at all besides the probe itself. The probe
    wording is never enough on its own to skip a review.
    """
    if any(key in body for key in DOCUMENT_KEYS + POLICY_KEYS):
        return False
    if body.get("files"):
        return False               # an attachment is real work, whatever the message says
    messages = body["messages"]
    if len(messages) != 1:
        return False
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    return _probe_form(message_text(message.get("content"))) == _PROBE_FORM


class ArtifactError(Exception):
    """A request that cannot produce a trustworthy verdict. Never answered with a verdict."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def sanitize(value: str) -> str:
    """Drop lone surrogates. Without this, json.dumps() raises on pasted mojibake."""
    return value.encode("utf-8", "replace").decode("utf-8")


def error_body(code: str, message: str, **extra: Any) -> dict:
    body: dict[str, Any] = {"code": code, "message": message, "known_policies": known_policies()}
    body.update(extra)
    return {"error": body}


def message_text(content: Any) -> str:
    """Flatten OpenAI-style content (string, or a list of text parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def user_texts(messages: list) -> list[str]:
    """Text of every user message, oldest first. Only user messages are ever reviewed."""
    out: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        text = message_text(message.get("content"))
        if text:
            out.append(sanitize(text))
    return out


def attachment_text(body: dict) -> str | None:
    """Decoded text of the request's TXT attachment, or None when it carries none.

    Every failure raises instead of returning None: an attachment the artifact cannot read is a
    request it cannot review, and must not quietly become a review of the instruction text.
    """
    files = body.get("files")
    if files is None:
        return None
    if not isinstance(files, list):
        raise ArtifactError(400, "malformed_request", "'files' must be a list of attachments.")
    if not files:
        return None                # an empty array is no attachment; the messages still decide

    decoded: list[str] = []
    for index, entry in enumerate(files):
        where = f"Attachment {index}"
        if not isinstance(entry, dict):
            raise ArtifactError(422, "attachment_invalid", f"{where} is not a JSON object.")
        file_type = entry.get("file_type")
        if file_type is not None:
            if not isinstance(file_type, str):
                raise ArtifactError(
                    422, "attachment_invalid", f"{where} has a non-string 'file_type'.")
            if file_type.strip().lstrip(".").casefold() not in ATTACHMENT_TEXT_TYPES:
                raise ArtifactError(
                    422, "attachment_unsupported_type",
                    f"{where} has file_type {file_type!r}; only TXT text attachments can be "
                    "reviewed. Send the contract text as a TXT attachment or in the messages.",
                )
        data = entry.get("file_data")
        if not isinstance(data, str):
            raise ArtifactError(
                422, "attachment_invalid",
                f"{where} has no 'file_data' string; it must carry base64-encoded text bytes.",
            )
        try:
            raw = base64.b64decode("".join(data.split()), validate=True)
        except (binascii.Error, ValueError):
            raise ArtifactError(
                422, "attachment_undecodable",
                f"{where} 'file_data' is not valid base64, so the document could not be read.",
            )
        try:
            decoded.append(raw.decode("utf-8-sig"))   # strict, and drops a leading BOM
        except UnicodeDecodeError:
            raise ArtifactError(
                422, "attachment_undecodable",
                f"{where} did not decode as UTF-8 text; only TXT attachments can be reviewed.",
            )

    if len(decoded) > 1:
        # Concatenating would invent a document that nobody sent. Refusing is visible; guessing
        # which attachment is the contract is not.
        raise ArtifactError(
            422, "attachment_ambiguous",
            f"{len(decoded)} attachments were supplied; attach exactly one contract per review.",
        )
    return decoded[0]


def _first_key(payload: dict, keys: tuple[str, ...]) -> tuple[str | None, Any]:
    for key in keys:
        if key in payload:
            return key, payload[key]
    return None, None


def envelope_fields(text: str) -> dict | None:
    """The JSON object a message carries, if it is one. A fence or a BOM does not hide it."""
    stripped = text.strip().lstrip(INVISIBLE_PREFIX).strip()
    fenced = ENVELOPE_FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def envelope_instructions(payload: dict) -> str:
    """The envelope's string fields except every document-bearing one -- instructions, not text."""
    return "\n".join(
        value
        for key, value in payload.items()
        if isinstance(value, str) and key not in DOCUMENT_KEYS
    )


def extract_from_json(text: str) -> tuple[str, Any, bool, str] | None:
    """(document, policy_value, policy_key_present, instructions) for a document-carrying object.

    `instructions` is the envelope's other string fields. A covering field such as "task" or
    "instructions" names the policy just as plainly as a prose turn does, and is resolved by the
    same single-mention rule. Every document-bearing key is excluded from it, so contract text is
    never scanned for a policy name.
    """
    payload = envelope_fields(text)
    if payload is None:
        return None
    doc_key, doc_value = _first_key(payload, DOCUMENT_KEYS)
    if doc_key is None:
        return None
    if not isinstance(doc_value, str):
        raise ArtifactError(
            422, "document_missing",
            f"JSON field '{doc_key}' must be a string containing the document text.",
        )
    policy_key, policy_value = _first_key(payload, POLICY_KEYS)
    return doc_value, policy_value, policy_key is not None, envelope_instructions(payload)


def extract_from_prose(text: str) -> str | None:
    """The document, only when the message separates it from the instruction unambiguously."""
    for pattern in (DOCUMENT_START_RE, DELIMITED_RE, FENCE_RE, TRIPLE_QUOTE_RE):
        match = pattern.search(text)     # terminated: the document ends where the sender said
        if match:
            return match.group(1)
    match = INSTRUCTION_SPLIT_RE.match(text)
    if match and len(match.group("instruction")) <= INSTRUCTION_MAX_CHARS:
        return strip_trailing_request(match.group("document"))
    match = MARKER_RE.search(text)
    if match:
        return strip_trailing_request(match.group(1))
    return None


def extract(messages: list, attachment: str | None = None) -> tuple[str, str]:
    """Resolve (document, policy) or raise ArtifactError. Newest user message wins.

    An attachment, when one was supplied, *is* the document: the transport contract says the
    document text may live only in the attachment while the instruction and policy stay in the
    messages, so every message then counts as instruction text and none of it is reviewed as
    contract text. Message text is still searched for the policy name.
    """
    texts = user_texts(messages)
    if not texts and attachment is None:
        raise ArtifactError(
            422, "document_missing",
            "No user message carrying text was supplied; nothing to review.",
        )

    document: str | None = attachment
    instruction_texts: list[str] = []      # message text outside the document, newest first
    json_policy: Any = None
    json_policy_present = False

    for text in reversed(texts):           # newest first
        if attachment is not None:
            # The document is settled. Read an envelope field by field so that a JSON message
            # contributes its policy and its instructions without its document text ever being
            # scanned for a policy name.
            payload = envelope_fields(text)
            if payload is None:
                instruction_texts.append(text)
                continue
            if not json_policy_present:
                policy_key, policy_value = _first_key(payload, POLICY_KEYS)
                if policy_key is not None:
                    json_policy, json_policy_present = policy_value, True
            envelope_text = envelope_instructions(payload)
            if envelope_text:
                instruction_texts.append(envelope_text)
            continue
        if document is None:
            parsed = extract_from_json(text)
            if parsed is not None:
                document, json_policy, json_policy_present, envelope_text = parsed
                if envelope_text:
                    # Newest-first order is preserved: the envelope's own fields are the newest
                    # instructions seen so far.
                    instruction_texts.append(envelope_text)
                continue
            prose_document = extract_from_prose(text)
            if prose_document is not None:
                document = prose_document
                instruction_texts.append(text.replace(prose_document, " "))
                continue
        instruction_texts.append(text)

    if document is None:
        raise ArtifactError(
            422, "document_missing",
            "No document boundary found. Send the document as JSON "
            "({\"policy\": ..., \"document\": ...}), mark it with a fenced block or "
            "DOCUMENT START/END, or write the instruction as a single leading block ending in a "
            "colon followed by a blank line and then the contract. The document is never guessed "
            "from surrounding text.",
        )

    document = sanitize(document).strip()
    if not document:
        raise ArtifactError(422, "document_empty", "The supplied document contains no text.")

    policy = resolve_policy(json_policy, json_policy_present, instruction_texts)
    return document, policy


def resolve_policy(json_policy: Any, json_policy_present: bool, instruction_texts: list[str]) -> str:
    """Canonical names and defined aliases only: no fuzzy matching, inference, or default."""
    if json_policy_present:
        resolved = resolve_policy_name(json_policy) if isinstance(json_policy, str) else None
        if resolved is None:
            raise ArtifactError(
                422, "policy_unknown",
                f"Policy {json_policy!r} is not a known compliance policy. "
                "Name one of the known policies exactly.",
            )
        return resolved

    for text in instruction_texts:         # already newest first
        mentions = find_policy_mentions(text)
        if len(mentions) == 1:
            return mentions[0]
        if len(mentions) > 1:
            raise ArtifactError(
                422, "policy_ambiguous",
                "More than one policy is named in the same request; review one policy at a time.",
                policies_named=mentions,
            )

    for text in instruction_texts:
        named = looks_like_named_standard(text)
        if named:
            raise ArtifactError(
                422, "policy_unknown",
                f"Policy {named!r} is not a known compliance policy. "
                "Name one of the known policies exactly.",
            )

    raise ArtifactError(
        422, "policy_unspecified",
        "No compliance policy was named. Name exactly one policy to review against; "
        "the artifact never assumes a default policy or infers one from the deal type.",
    )


def log_metadata(kind: str, policy: str, entries: list[dict]) -> None:
    """Log category metadata only -- never contract text, never a candidate span."""
    if not entries:
        return
    logger.warning(
        "%s policy=%s cases=%s",
        kind,
        policy,
        [
            {
                "category": entry["category"],
                "rule": entry.get("rule"),
                "reason": entry.get("reason"),
                "signals": entry.get("signals"),
            }
            for entry in entries
        ],
    )


async def apply_escalation(document: str, policy: str, outcome: dict, client: Any) -> None:
    """Optional, gated off: may only upgrade unresolved cases to violations, never to clean."""
    unresolved = outcome["unresolved"]
    if not unresolved or not escalate.is_enabled():
        return
    upgrades = await escalate.adjudicate(document, policy, unresolved, client=client)
    if not upgrades:
        return
    upgraded = {item["category"]: item["span"] for item in upgrades}
    for case in list(unresolved):
        span = upgraded.get(case["category"])
        if span is None or span not in document:
            continue
        outcome["violations"].append({
            "category": case["category"],
            "rule": MUST_NOT_HAVE,
            "present": True,
            "evidence_spans": [span],
            "rationale": (
                f"{policy} prohibits {case['category']}; adjudication confirmed it with the quoted "
                f"text. Remove or renegotiate the clause."
            ),
        })
        unresolved.remove(case)


def review(document: str, policy: str) -> dict:
    return resolve(document, policy)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> JSONResponse:
    try:
        try:
            body = await request.json()
        except Exception:
            raise ArtifactError(400, "malformed_request", "Request body is not valid JSON.")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            raise ArtifactError(
                400, "malformed_request",
                "Request body must be a JSON object with a 'messages' list.",
            )

        if is_transport_probe(body):
            logger.info("transport_probe answered; no document was reviewed")
            return JSONResponse({"choices": [{"message": {
                "role": "assistant",
                "content": json.dumps(PROBE_RESPONSE),
            }}]})

        # After the probe check, so a bare handshake never parses attachments, and before
        # extraction, so an unreadable attachment fails instead of falling back to message text.
        attachment = attachment_text(body)
        document, policy = extract(body["messages"], attachment)
        outcome = review(document, policy)
        await apply_escalation(document, policy, outcome, app.state.escalation_client)

        violations = outcome["violations"]
        unresolved = outcome["unresolved"]
        log_metadata("needs_review_signal_dropped", policy, outcome["dropped"])

        if unresolved and not violations:
            # Nothing found, but part of the document could not actually be checked. The one
            # thing that must never happen is this returning {"compliant": true}.
            log_metadata("unresolved_evidence", policy, unresolved)
            raise ArtifactError(
                422, "unresolved_evidence",
                "The document could not be fully checked: evidence for the categories below was "
                "found but could not be confirmed or quoted, so no clean verdict can be issued. "
                "Route to a human reviewer.",
                policy=policy,
                unresolved=[
                    {"category": case["category"], "rule": case["rule"], "reason": case["reason"]}
                    for case in unresolved
                ],
            )

        if unresolved:
            # Real violations exist, so the document is already non-compliant and no false clean
            # can arise. The unresolved cases are recorded as metadata; the schema has no fourth
            # key and needs_review must not absorb uncertainty.
            log_metadata("unresolved_evidence_with_violations", policy, unresolved)

        verdict = {
            "compliant": not violations,
            "violations": violations,
            "needs_review": outcome["needs_review"],
        }
        content = json.dumps(verdict, ensure_ascii=False)
        return JSONResponse({"choices": [{"message": {"role": "assistant", "content": content}}]})

    except ArtifactError as error:
        return JSONResponse(
            status_code=error.status,
            content=error_body(error.code, error.message, **error.extra),
        )
    except Exception:
        logger.exception("unhandled error while reviewing")
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "The review failed before a verdict was reached."),
        )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Belt and braces: no exception may ever surface as a 200 verdict."""
    logger.exception("unhandled error outside the review path")
    return JSONResponse(
        status_code=500,
        content=error_body("internal_error", "The review failed before a verdict was reached."),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
