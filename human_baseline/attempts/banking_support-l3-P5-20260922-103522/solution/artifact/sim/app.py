"""Local simulation app for manual integration testing.

Lets you role-play the customer: type messages, watch the agent's replies, and inspect a customer's
live records before and after a turn -- so you can eyeball whether the end-state records match what
the request required (the basis the bank actually scores on, V-1/V-2).

Runs on its own port (SIM_PORT, default 8090), separate from the artifact. It talks to:
  * the artifact's /respond  (ARTIFACT_URL, default http://localhost:8080)
  * the banking tool API directly, for the read-only record-inspection panel, reusing the same
    Config + BankingToolClient the agent uses.

This app is a TEST HARNESS, not part of the deployed artifact. It never mutates records itself; the
inspect panel calls only base read tools.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent.config import Config
from agent.tool_client import BankingToolClient

ARTIFACT_URL = os.environ.get("ARTIFACT_URL", "http://localhost:8080").rstrip("/")
_HERE = Path(__file__).parent

app = FastAPI(title="banking-support sim")
_tools: BankingToolClient | None = None

# Read-only tools the inspect panel is allowed to call. Kept to reads so the sim never mutates.
_READ_TOOLS = {
    "get_user_information_by_id",
    "get_user_information_by_email",
    "get_user_information_by_name",
    "get_credit_card_accounts_by_user",
    "get_credit_card_transactions_by_user",
    "get_referrals_by_user",
}


def _tool_client() -> BankingToolClient:
    global _tools
    if _tools is None:
        _tools = BankingToolClient(Config.from_env())
    return _tools


class SendReq(BaseModel):
    messages: list[dict[str, Any]]


class InspectReq(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_HERE / "index.html")


def _envelope(text: str) -> dict[str, Any]:
    """Wrap text in the OpenAI-style shape the FE expects, so the chat can always render it."""
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@app.post("/send")
def send(req: SendReq) -> dict[str, Any]:
    """Proxy the running conversation to the artifact's /respond and return its reply.

    Always returns the OpenAI-style JSON envelope -- if the artifact is down, misconfigured, or
    replies with a non-JSON body (e.g. a raw 500 "Internal Server Error"), we surface that as an
    assistant message instead of letting the browser fail on `response.json()`.
    """
    try:
        resp = httpx.post(f"{ARTIFACT_URL}/respond", json={"messages": req.messages}, timeout=120.0)
    except httpx.RequestError as exc:
        return _envelope(f"[sim] cannot reach artifact at {ARTIFACT_URL}: {exc}")
    if resp.status_code != 200:
        return _envelope(f"[sim] artifact returned HTTP {resp.status_code}: {resp.text.strip()[:500]}")
    try:
        return resp.json()
    except ValueError:
        return _envelope(f"[sim] artifact returned non-JSON body: {resp.text.strip()[:500]}")


@app.post("/inspect")
def inspect(req: InspectReq) -> dict[str, Any]:
    """Call a base READ tool so the tester can see a customer's live records."""
    if req.tool not in _READ_TOOLS:
        return {"error": f"{req.tool!r} is not an allowed read tool"}
    return _tool_client().call(req.tool, req.arguments)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SIM_PORT", "8090")))
