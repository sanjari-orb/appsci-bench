#!/usr/bin/env python
"""Artifact endpoint for the airline reservation agent.

Implements the artifact contract every task expects:
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]}  =>  OpenAI-style
                     {"choices": [{"message": {"role": "assistant", "content": ...}}]}

/respond is stateless: it receives the whole conversation so far and returns the next assistant
turn. All tool calling happens inside a single /respond call (see agent.py), because the caller
only ever observes the final assistant text.

/health deliberately touches neither the task environment nor the LLM, so a readiness probe never
fails because of a transient dependency blip.

Must bind 0.0.0.0:$PORT -- the deployer publishes the container's $PORT and expects the server to
listen on exactly that port.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request

from agent import Agent

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("airline.server")

app = FastAPI()

# One Agent per process: it holds the LLM and env clients, both safe to reuse and both stateless
# with respect to the conversation (the full transcript arrives with every request).
_agent: Agent | None = None


def get_agent() -> Agent:
    """Return the process-wide Agent, constructing it on first use."""
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


@app.get("/health")
def health() -> dict:
    """Liveness probe. Intentionally free of external dependencies."""
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    """Produce the next assistant turn for the supplied conversation."""
    body = await request.json()
    messages = body.get("messages", [])

    try:
        reply = get_agent().run(messages)
    except Exception:
        # Never surface a stack trace to a customer, and never leave the turn empty: a failure
        # here means we could not establish the facts, so the safe outcome is a handoff.
        logger.exception("agent turn failed")
        reply = (
            "I'm sorry, I'm having trouble accessing the reservation system right now. "
            "Let me get you to a human agent who can help."
        )

    return {"choices": [{"message": {"role": "assistant", "content": reply}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
