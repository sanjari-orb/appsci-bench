#!/usr/bin/env python
"""Artifact endpoint for the Customer-Call Insights task.

Implements the artifact contract (docs/task_runner.md "Artifact Interface"):
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]}  =>  OpenAI-style
                     {"choices": [{"message": {"role": "assistant", "content": ...}}]}

The assistant `content` is the CRM record as a single JSON object and nothing else -- a downstream
job ingests it verbatim. All extraction/validation lives in `insights`; this layer only unpacks the
request and serialises the record. It never raises past the handler: a bad request still returns a
valid (empty) record so the downstream job is never handed something unparseable.

Must bind 0.0.0.0:$PORT -- the deployer publishes the container's $PORT (see
appsci_bench/orchestrator/deployer.py).
"""
from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI, Request

from insights import EMPTY_RECORD, build_record, extract_transcript

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("cci.server")

app = FastAPI()


def _as_content(record: dict) -> str:
    """Serialise the record as a bare, single-line JSON object (no prose, no fences)."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/respond")
async def respond(request: Request) -> dict:
    try:
        body = await request.json()
        # Pass the whole body: the transcript may live in messages, a top-level field, or a
        # nested/encoded payload. extract_transcript is shape-agnostic (see insights.py).
        transcript = extract_transcript(body)
        record = build_record(transcript)
    except Exception:  # noqa: BLE001 -- always answer with a contract-valid record
        log.exception("respond failed; returning empty record")
        record = dict(EMPTY_RECORD)

    return {"choices": [{"message": {"role": "assistant", "content": _as_content(record)}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
