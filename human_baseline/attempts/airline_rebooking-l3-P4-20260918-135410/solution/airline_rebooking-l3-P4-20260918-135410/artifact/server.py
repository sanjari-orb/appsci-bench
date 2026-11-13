#!/usr/bin/env python
"""The artifact: a conversational airline reservations agent behind /respond.

The scored contract is two endpoints (GET /health, POST /respond) and is unchanged from the scaffold.
What this file adds is the agent loop: take the running conversation, let the model work with the
airline tools one call at a time through gate.py, and return exactly one assistant message.

The hard constraint that shapes the design: /respond is stateless from the caller's point of view --
it receives the whole customer conversation each time -- but our internal tool calls are NOT part of
that conversation. Re-deriving the loop from the text alone on every request would re-issue writes
that already happened, double-booking or double-cancelling. Hence the conversation cache below.
"""
from __future__ import annotations

import hashlib
import json
import os
import traceback

from fastapi import FastAPI, Request

import gate
import llm
import prompt
import tools

app = FastAPI()

# Internal tool iterations allowed per customer turn. The benchmark counts customer<->agent
# exchanges, not our internal calls, but a loop that never yields a message burns the ceiling and
# scores zero -- so it is bounded, and exhaustion still produces a reply.
MAX_TOOL_ITERATIONS = 10

FALLBACK_MESSAGE = (
    "I'm sorry -- I'm having trouble completing that right now. Could you tell me again what you'd "
    "like to do with your reservation?"
)

# conversation key -> {"messages": internal transcript, "writes": [...], "reply": last reply}
_CONVERSATIONS: dict[str, dict] = {}


def _text_of(content) -> str:
    """Flatten a message's content, which may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return "" if content is None else str(content)


def _key(user_texts: list[str]) -> str:
    return hashlib.sha256(json.dumps(user_texts).encode()).hexdigest()


def _rebuild(messages: list[dict]) -> list[dict]:
    """Fall back to a text-only transcript when the cache has no record of this conversation.

    Used on a cache miss (a restarted process, or a conversation that began elsewhere). Tool history
    is unrecoverable here, so the model resumes without it -- and because any earlier write is
    invisible, it must not be repeated speculatively. The system prompt's confirmation rule plus the
    gate's own confirmation check are what stop that.
    """
    rebuilt = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue  # a system turn from the caller: ours governs instead
        text = _text_of(message.get("content")).strip()
        if text:
            rebuilt.append({"role": role, "content": text})
    return rebuilt


def _assistant_turn(response: dict, keep_tool_use: dict) -> dict:
    """The assistant turn as we record it: its text, plus exactly ONE tool_use block.

    Rewriting rather than merely ignoring the extra blocks is the point. A tool_use block with no
    matching tool_result in the following turn makes the next request invalid, so dropping the extras
    silently would break the conversation one turn later. Thinking blocks are dropped for the same
    reason -- they carry signatures that only validate in the turn that produced them.
    """
    blocks = [
        block
        for block in response.get("content") or []
        if block.get("type") == "text" and block.get("text", "").strip()
    ]
    blocks.append(keep_tool_use)
    return {"role": "assistant", "content": blocks}


def _run_turn(internal: list[dict], writes: list[dict], last_user_text: str) -> str:
    """Drive the model until it produces a customer-facing message. Returns that message."""
    last_assistant_text = ""
    for message in reversed(internal[:-1]):
        if message.get("role") == "assistant":
            last_assistant_text = _text_of(message.get("content"))
            if last_assistant_text.strip():
                break

    for _ in range(MAX_TOOL_ITERATIONS):
        response = llm.create(internal, prompt.SYSTEM_PROMPT, tools.TOOL_SCHEMAS)
        calls = llm.tool_uses(response)

        if not calls:
            reply = llm.text_of(response)
            internal.append({"role": "assistant", "content": reply or FALLBACK_MESSAGE})
            return reply or FALLBACK_MESSAGE

        call = calls[0]  # one tool call per turn, enforced here regardless of the model
        internal.append(_assistant_turn(response, call))

        state = gate.ConversationState(
            last_user_text=last_user_text,
            last_assistant_text=last_assistant_text or llm.text_of(response),
            prior_writes=writes,
        )
        outcome = gate.run_tool(call.get("name", ""), call.get("input") or {}, state)

        internal.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": call.get("id"),
                "content": outcome.content,
                "is_error": outcome.is_error,
            }],
        })

        if outcome.write_performed:
            writes.append(outcome.write_performed)

        if outcome.transferred:
            internal.append({"role": "assistant", "content": gate.TRANSFER_MESSAGE})
            return gate.TRANSFER_MESSAGE

    # Iterations exhausted. Ask for a closing message with no tools available, so the turn ends with
    # something the customer can act on rather than silence.
    try:
        closing = llm.create(
            internal + [{
                "role": "user",
                "content": "Stop using tools. Tell the customer where things stand now, in one short "
                           "message, and what you need from them next.",
            }],
            prompt.SYSTEM_PROMPT,
        )
        reply = llm.text_of(closing) or FALLBACK_MESSAGE
    except Exception:  # noqa: BLE001 -- a reply must always come back
        reply = FALLBACK_MESSAGE
    internal.append({"role": "assistant", "content": reply})
    return reply


def _envelope(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    messages = body.get("messages") or []

    try:
        user_texts = [
            _text_of(m.get("content")) for m in messages if m.get("role") == "user"
        ]
        full_key = _key(user_texts)

        # Same conversation, asked twice: return the reply already given rather than re-running the
        # loop, which could repeat a write.
        cached = _CONVERSATIONS.get(full_key)
        if cached and cached.get("reply"):
            return _envelope(cached["reply"])

        prior = _CONVERSATIONS.get(_key(user_texts[:-1])) if user_texts else None
        if prior:
            internal = list(prior["messages"])
            writes = list(prior["writes"])
            internal.append({"role": "user", "content": user_texts[-1]})
        else:
            internal = _rebuild(messages)
            writes = []

        if not internal:
            internal = [{"role": "user", "content": "Hello"}]

        reply = _run_turn(internal, writes, user_texts[-1] if user_texts else "")
        _CONVERSATIONS[full_key] = {
            "messages": internal,
            "writes": writes,
            "reply": reply,
        }
        return _envelope(reply)

    except Exception:  # noqa: BLE001 -- never 500 at the customer; a graceful turn scores better
        print("ERROR in /respond:\n" + traceback.format_exc(), flush=True)
        return _envelope(FALLBACK_MESSAGE)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
