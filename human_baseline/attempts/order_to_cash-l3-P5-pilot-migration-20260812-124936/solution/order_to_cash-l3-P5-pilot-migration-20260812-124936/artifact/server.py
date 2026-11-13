#!/usr/bin/env python
"""Artifact endpoint for the order_to_cash discrepancy detector.

Implements the artifact contract every task expects:
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]}  =>  OpenAI-style
                     {"choices": [{"message": {"role": "assistant", "content": ...}}]}

/respond is stateless: this task is single_turn, so only the last message (the review
instruction) is used. All the review logic lives in solution/ (detector.py -> agent.pipeline);
this file only adapts that logic to the HTTP contract -- it does not reimplement it.

The reply content is the detector's `{"flagged": ..., "reason": ...}` JSON string, unchanged --
the evaluator's extract_json_object requires the WHOLE reply body to be that JSON object.

Must bind 0.0.0.0:$PORT -- the deployer publishes the container's $PORT and expects the server to
listen on exactly that port.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request

from solution.datatypes import ArtifactAgentQuery
from solution.detector import respond as detector_respond

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("order_to_cash.server")

app = FastAPI()


@app.get("/health")
def health() -> dict:
    """Liveness probe. Intentionally free of external dependencies."""
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    """Produce the assistant turn for the supplied conversation's last message."""
    body = await request.json()
    messages = body.get("messages", [])
    text = messages[-1].get("content", "") if messages else ""

    try:
        output = detector_respond(ArtifactAgentQuery(text=text))
        reply = output.json
    except Exception:
        logger.exception("detector_respond failed")
        reply = '{"flagged": true, "reason": "internal error while reviewing this invoice"}'

    return {"choices": [{"message": {"role": "assistant", "content": reply}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
