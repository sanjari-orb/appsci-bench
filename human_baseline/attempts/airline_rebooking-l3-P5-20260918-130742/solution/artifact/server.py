#!/usr/bin/env python
"""Artifact HTTP endpoint for the airline reservations agent.

Deliberately thin: it owns the HTTP contract and nothing else. All behaviour lives in
`agent/orchestrator.py` (the turn loop), `actions.py` (the write gating) and `policy/` (the rules), so a
wrong outcome can be traced to one layer.

Contract (see docs/task_runner.md "Artifact Interface"):
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]}  =>  {"choices": [{"message": {"role", "content"}}]}

Binds 0.0.0.0:$PORT, which the deployer publishes.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request

from agent.orchestrator import FALLBACK_MESSAGE, Orchestrator
from config import load_config
from env_client import EnvClient
from logging_setup import get_logger, new_correlation_id, setup_logging

config = load_config()
setup_logging(config.log_level)
log = get_logger("server")

app = FastAPI()

# A missing LLM credential is not fatal -- /health must still pass so the deploy does not look broken --
# but it is logged loudly at start-up, because every turn will degrade to the fallback message.
if not config.llm_available:
    log.error(
        "no LLM credential found in the environment; /respond will return the safe fallback",
        extra={"checked": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY",
                           "LLM_API_KEY"]},
    )
log.info(
    "artifact starting",
    extra={"env_url": config.env_url, "model": config.model,
           "llm_available": config.llm_available},
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    """Produce the assistant's next turn for the conversation in the request body.

    A fresh `Orchestrator` (and so a fresh `ActionService`) is built per request: the verified-facts
    cache and the pending-action table must not leak between conversations, and `cancel_reservation`'s
    write-once guard is per conversation by design.
    """
    correlation_id = new_correlation_id()
    try:
        body = await request.json()
    except Exception:
        log.exception("could not parse request body")
        return _reply(FALLBACK_MESSAGE)

    messages = body.get("messages") or []
    log.info("respond", extra={"turns": len(messages)})

    client = EnvClient(config.env_url, config.env_token)
    orchestrator = Orchestrator(config, client)
    try:
        reply = orchestrator.respond(messages)
    except Exception:
        # Never surface an internal error to the customer, and never leave a half-finished impression.
        log.exception("unhandled error while producing a turn")
        reply = FALLBACK_MESSAGE

    log.info(
        "responded",
        extra={
            "steps": orchestrator.steps_taken,
            "tool_calls": len(client.call_log),
            "writes": [c["tool"] for c in client.call_log if c["is_write"] and not c["error"]],
            "dropped_extra_calls": orchestrator.dropped_extra_calls,
            "correlation_id": correlation_id,
        },
    )
    return _reply(reply)


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
