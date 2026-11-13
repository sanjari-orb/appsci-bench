#!/usr/bin/env python
"""CRM Insights artifact endpoint.

  GET  /health    -> 200 once the process is up
  GET  /selftest  -> database reachable? model reachable? (diagnostics, not part of the contract)
  POST /respond   -> {"messages": [...]} -> {"choices": [{"message": {"role", "content"}}]}
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI

import agent
import db
import llm

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("crm-insights")

app = FastAPI(title="CRM Insights")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/selftest")
def selftest() -> dict:
    out: dict = {"model": llm.probe()}
    try:
        rows = db.query("SELECT COUNT(*) AS n FROM crm_case")
        out["database"] = {"ok": True, "cases": rows[0]["n"]}
    except Exception as exc:  # noqa: BLE001
        out["database"] = {"ok": False, "error": str(exc)[:300]}
    return out


def _question(messages: list) -> str:
    """The running conversation, flattened to the text the analyst is asking about."""
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") not in ("user", "human"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(b.get("text", "") for b in content if isinstance(b, dict))
    return "\n\n".join(p for p in parts if p).strip()


@app.post("/respond")
def respond(body: dict) -> dict:
    question = _question(body.get("messages") or [])
    log.info("question: %s", question.replace("\n", " ")[:300])
    if not question:
        content = "I did not receive a question to answer."
    else:
        try:
            content = agent.answer(question)
        except Exception as exc:  # noqa: BLE001 -- never hand back a 500
            log.exception("failed to answer")
            content = (f"I could not complete this query against the CRM ({type(exc).__name__}: "
                       f"{exc}), so I have no answer to give you rather than an unverified guess.")
    log.info("answer: %s", content.replace("\n", " ")[:300])
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
