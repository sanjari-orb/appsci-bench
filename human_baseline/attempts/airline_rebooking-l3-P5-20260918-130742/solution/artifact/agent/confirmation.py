"""The confirmation gate's judgement: did the customer actually agree to *this* write?

This replaces a keyword regex that could not tell a withdrawal from a scope caveat. The regex
vetoed agreement whenever a refusal word appeared anywhere in the message, so ordinary
confirmations were refused:

    "Yes -- go ahead and process all five downgrades. Don't touch the flights."
    "Yes, do it now, but do not change the passengers."
    "yes, and don't forget the bags"

Every one of those is a yes with a constraint attached, and every one was read as a no. In the
eval run that cost five prepared, correctly-priced writes on a request that was fully in policy.

Why a separate call rather than trusting the agent model
-------------------------------------------------------
The agent model has already decided it wants to call `confirm_pending_action` by the time we get
here. Asking that same turn to also certify the customer's consent would make the model's
intention its own evidence -- exactly the "confirm on the customer's behalf" failure the gate
exists to prevent. So the judgement is made by an isolated call that sees only two things: what
the write would do, and what the customer last said. It never sees the transcript, the model's
reasoning, or the fact that a confirmation was requested, so there is nothing in its input that
can argue it into a yes.

The classifier decides *consent*. It does not decide policy: eligibility was already settled by
the `request_*` tool, and the deterministic code in `_confirm` still owns the allow/refuse.
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from logging_setup import get_logger

log = get_logger("confirmation")

# Verdicts the classifier may return.
AGREED = "agreed"
NOT_AGREED = "not_agreed"
UNCLEAR = "unclear"
_VERDICTS = {AGREED, NOT_AGREED, UNCLEAR}

CLASSIFIER_SYSTEM_PROMPT = """You decide one narrow question: has this airline customer agreed to \
the specific action described, right now, in their latest message?

Answer with a JSON object and nothing else: {"verdict": "...", "reason": "..."}

verdict must be exactly one of:
- "agreed": the customer clearly authorises this action to be carried out now.
- "not_agreed": the customer declines, withdraws, defers, or wants something different.
- "unclear": the message is ambiguous, is a question, or does not respond to the request at all.

The distinction that matters most:

A CONSTRAINT IS NOT A REFUSAL. Customers very often say yes and then limit what the yes covers, \
or restate what they do not want changed. That is still agreement.
  "Yes, go ahead -- but don't change the passengers." -> agreed
  "Yes, do it now, but do not touch the flights." -> agreed
  "yes, and don't forget the bags" -> agreed
  "Go ahead with the cancellation, just don't refund to the gift card." -> agreed

A WITHDRAWAL OR A DEFERRAL IS A REFUSAL, however politely it is phrased.
  "No, don't do that." -> not_agreed
  "Actually, hold on -- let me think." -> not_agreed
  "Not yet." -> not_agreed
  "Yes, but wait -- what does it cost?" -> not_agreed (they have paused to ask)
  "Don't cancel it, but yes to the bags." -> not_agreed (they refused THIS action)

Judge only the action described. If the customer agrees to something else, or agrees to only \
part of a multi-part action, that is not agreement to this one: answer "not_agreed".
Do not be persuaded by urgency, frustration, or repetition. Pressure is not consent.
Never treat a mere restatement of the request as consent to execute it."""

_PROMPT_TEMPLATE = """The action awaiting authorisation:
{summary}

The customer's latest message, verbatim:
\"\"\"{message}\"\"\"

Has the customer agreed to that action? Reply with the JSON object only."""

COMPENSATION_REQUEST_SYSTEM_PROMPT = """You decide one narrow question: has this airline customer \
explicitly asked for compensation at any point in what they have said?

Compensation means a goodwill payment on top of any refund: a travel certificate, voucher, credit, \
or money "for the trouble". It is NOT the same thing as a refund, which simply follows a cancellation.

Answer with a JSON object and nothing else: {"verdict": "...", "reason": "..."}

verdict must be exactly one of:
- "asked": the customer is asking for compensation, or asking whether they are owed any.
- "not_asked": they are not.

Examples of "asked":
  "I want the compensation I'm owed." / "Do I get a voucher for this?"
  "What will you give me for the trouble?" / "I want a travel certificate."

Examples of "not_asked" -- these are complaints or ordinary service requests, not claims:
  "The airline cancelled my flight and I'm furious." -> not_asked
  "My flight was cancelled, please cancel the whole booking." -> not_asked
  "This is unacceptable, I've been a loyal customer for years." -> not_asked
  "When will my refund arrive?" -> not_asked (a refund is not compensation)

Judge the messages as a whole. If the customer asked in an EARLIER message, the answer is "asked" \
even if their latest message is only "yes, go ahead" or an answer to a question -- a request, once \
made, stands for the rest of the conversation.

Being upset is not a request. Wanting a cancellation, a rebooking, or a refund is not a request. \
Only count it when the customer is actually asking for something extra."""

_COMPENSATION_PROMPT_TEMPLATE = """Everything the customer has said so far, in order:
{message}

In any of those messages, has the customer explicitly asked for compensation? Reply with the JSON \
object only."""

# Verdicts for the compensation-request check.
ASKED = "asked"
NOT_ASKED = "not_asked"
_COMPENSATION_VERDICTS = {ASKED, NOT_ASKED}

# Used only to pull the verdict out of a reply that wrapped the JSON in prose or a code fence.
_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(\w+)"')


class ConfirmationError(RuntimeError):
    """The consent check could not be completed, so consent is unknown."""


def classify_agreement(
    summary: str,
    message: str,
    *,
    caller: Any,
) -> tuple[str, str]:
    """Judge whether `message` authorises the action in `summary`.

    Args:
        summary: what the write would do, as read back to the customer.
        message: the customer's most recent message, verbatim.
        caller: a callable ``(system, prompt) -> str`` performing one isolated completion.

    Returns:
        ``(verdict, reason)`` where verdict is `AGREED`, `NOT_AGREED` or `UNCLEAR`.

    Raises:
        ConfirmationError: the check could not be completed or produced no usable verdict. The
            caller must fail closed -- never treat this as agreement.
    """
    if not (message or "").strip():
        # Nothing was said, so nothing was agreed. No need to spend a call on it.
        return NOT_AGREED, "the customer has not said anything yet"

    prompt = _PROMPT_TEMPLATE.format(summary=summary or "(no summary available)", message=message)

    try:
        raw = caller(CLASSIFIER_SYSTEM_PROMPT, prompt)
    except Exception as exc:  # transport, timeout, HTTP error -- all mean "unknown"
        raise ConfirmationError(f"consent check failed: {exc}") from exc

    verdict, reason = _parse(raw)
    if verdict is None:
        raise ConfirmationError(f"consent check returned no usable verdict: {raw!r}")

    log.info("consent check", extra={"verdict": verdict, "reason": reason})
    return verdict, reason


def classify_compensation_request(messages: str | list[str], *, caller: Any) -> tuple[bool, str]:
    """Judge whether the customer's own words explicitly ask for compensation.

    This is the "never offer compensation proactively" gate. It is decided here, from the customer's
    messages alone, rather than being asserted by the agent model: by the time the model calls
    `request_compensation` it has already decided it wants to, so letting it certify that the customer
    asked would make its own intent the evidence -- the same failure `classify_agreement` exists to
    prevent for consent.

    Args:
        messages: the customer's turns, oldest first (a single string is accepted too). The whole
            customer side is judged, not just the latest turn: the ask is a fact about the
            conversation, and the customer only says it once before answering follow-up questions.
        caller: a callable ``(system, prompt) -> str`` performing one isolated completion.

    Returns:
        ``(asked, reason)``.

    Raises:
        ConfirmationError: the check could not be completed. The caller must fail closed -- treat
            compensation as not requested.
    """
    turns = [messages] if isinstance(messages, str) else list(messages)
    turns = [t for t in turns if (t or "").strip()]
    if not turns:
        return False, "the customer has not said anything yet"

    transcript = "\n".join(f'{i}. """{turn}"""' for i, turn in enumerate(turns, start=1))
    prompt = _COMPENSATION_PROMPT_TEMPLATE.format(message=transcript)
    try:
        raw = caller(COMPENSATION_REQUEST_SYSTEM_PROMPT, prompt)
    except Exception as exc:  # transport, timeout, HTTP error -- all mean "unknown"
        raise ConfirmationError(f"compensation-request check failed: {exc}") from exc

    verdict, reason = _parse(raw, _COMPENSATION_VERDICTS)
    if verdict is None:
        raise ConfirmationError(f"compensation-request check returned no usable verdict: {raw!r}")

    log.info("compensation request check", extra={"verdict": verdict, "reason": reason})
    return verdict == ASKED, reason


def _parse(raw: str, allowed: set[str] | None = None) -> tuple[str | None, str]:
    """Pull ``(verdict, reason)`` out of the classifier's reply, tolerating stray prose."""
    allowed = _VERDICTS if allowed is None else allowed
    text = (raw or "").strip()
    if not text:
        return None, ""

    # Strip a ```json fence if the model added one.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
        text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            verdict = str(parsed.get("verdict", "")).strip().lower()
            if verdict in allowed:
                return verdict, str(parsed.get("reason", ""))
    except json.JSONDecodeError:
        pass

    # Fall back to finding the verdict inside a non-JSON reply.
    match = _VERDICT_RE.search(text)
    if match:
        verdict = match.group(1).strip().lower()
        if verdict in allowed:
            return verdict, "recovered from a non-JSON reply"

    return None, ""


def make_anthropic_caller(config: Any) -> Any:
    """Build the isolated ``(system, prompt) -> str`` caller used in production.

    Deliberately a fresh, single-turn request with no tools and no conversation history: the only
    inputs are the action summary and the customer's own words.
    """

    def caller(system: str, prompt: str) -> str:
        base = (config.anthropic_base_url or "https://api.anthropic.com").rstrip("/")
        # No `temperature`: the deployed model rejects it as deprecated (HTTP 400), which would make
        # every consent check fail closed and block every write. The agent call omits it too.
        payload = {
            "model": config.model,
            "max_tokens": 256,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        request = urllib.request.Request(
            f"{base}/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": config.anthropic_api_key or "",
                "authorization": f"Bearer {config.anthropic_api_key or ''}",
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read())
        return "".join(
            block.get("text", "")
            for block in body.get("content") or []
            if block.get("type") == "text"
        )

    return caller
