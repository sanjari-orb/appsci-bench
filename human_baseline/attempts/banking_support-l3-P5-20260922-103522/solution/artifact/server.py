#!/usr/bin/env python
"""Artifact endpoint -- L3 banking support agent.

Implements the artifact contract (docs/task_runner.md "Artifact Interface"):
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]}  =>  OpenAI-style
                     {"choices": [{"message": {"role": "assistant", "content": ...}}]}

The reasoning lives in the ``agent`` package; this module is the thin HTTP shell. It binds
0.0.0.0:$PORT (the deployer publishes exactly that port).

The Agent is constructed lazily on the first /respond so that /health stays answerable even if the
model/tool env vars are not yet injected.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request

from agent import Agent

app = FastAPI()
_agent: Agent | None = None


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    body = await request.json()
    messages = body.get("messages", [])
    return _get_agent().run_turn(messages)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
