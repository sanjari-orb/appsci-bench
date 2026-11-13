#!/usr/bin/env python
"""Artifact endpoint stub -- starting point for a human-baseline solution.

Implements the artifact contract every task expects (docs/task_runner.md "Artifact Interface"):
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]}  =>  OpenAI-style
                     {"choices": [{"message": {"role": "assistant", "content": ...}}]}

Must bind 0.0.0.0:$PORT -- the deployer publishes the container's $PORT and expects the server to
listen on exactly that port (see appsci_bench/orchestrator/deployer.py). Replace respond() below
with the real solution logic; the health check and request/response shapes should not need to change.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request

app = FastAPI()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    body = await request.json()
    messages = body.get("messages", [])

    # TODO: replace with the real solution. `messages` is the running conversation
    # (OpenAI-style [{"role": ..., "content": ...}, ...]); return the assistant's next turn.
    reply = "TODO: not yet implemented"

    return {"choices": [{"message": {"role": "assistant", "content": reply}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
