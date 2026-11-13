#!/usr/bin/env python
"""Rho-Bank customer-service agent -- HTTP service.

Preserves the artifact contract exactly:
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]}  =>  {"choices": [{"message": {"role": "assistant",
                                                                        "content": ...}}]}
Binds 0.0.0.0:$PORT (see config.port()).

Phase split: this file is the stable HTTP surface. The agent core (the LLM tool-use loop) lives in
`agent.py` and is wired in Phase B, once the runtime interface (model id + wire protocol, BASE tool
names/schemas, unlock mechanism, retrieval arg names) is confirmed from the build environment.

Honesty on failure (a hard requirement): we NEVER convert an internal failure into a reassuring
success. While the agent core is absent, /respond returns an explicit HTTP 503 error envelope -- not
a fabricated assistant answer. If the agent core is present but raises, we return HTTP 500 with an
honest error, again never a fake answer.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config

app = FastAPI()


class RespondRequest(BaseModel):
    # OpenAI-style running conversation. Kept permissive on purpose; the agent core interprets it.
    messages: List[Any] = []


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _load_agent() -> Optional[Callable[[List[Any]], str]]:
    """Return the agent-core entry point if Phase B has been wired, else None.

    Kept as a lazy import so the HTTP surface (and /health) works in Phase A, where `agent.py` does
    not yet exist. We do not stub a fake agent -- absence is reported honestly by /respond.
    """
    try:
        import agent  # type: ignore
    except ImportError:
        return None
    return getattr(agent, "run_agent", None)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


@app.post("/respond")
def respond(request: RespondRequest) -> Any:
    run_agent = _load_agent()
    if run_agent is None:
        # Phase A: the agent core is intentionally not wired yet. Report this honestly instead of
        # returning a fabricated assistant turn.
        return _error(
            503,
            "agent_core_unavailable",
            "The agent core is not yet configured in this build. /respond cannot produce a grounded "
            "answer until the runtime interface is confirmed and the agent is wired (Phase B).",
        )
    try:
        content = run_agent(request.messages)
    except Exception as exc:  # noqa: BLE001 -- surface any core failure honestly, never as success
        # Never return exception text to the customer: it can contain credentials or endpoints, and
        # redact() only covers the banking token. Log the exception TYPE (no message, no values)
        # server-side for debugging and return a fixed, generic message to the client.
        print(f"[respond] agent core error: {type(exc).__name__}", file=sys.stderr)
        return _error(
            500,
            "agent_core_error",
            "The agent encountered an internal error and could not complete your request.",
        )
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.port())
