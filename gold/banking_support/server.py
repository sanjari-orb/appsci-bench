"""Locally-hosted banking artifact (FastAPI) -- the artifact CONTAINER stand-in for banking_support.

Implements the artifact contract the evaluator queries:
  GET  /health   -> {"status": "ok"}                                        (open; the deployer health-checks it)
  POST /respond  {messages} -> {choices: [{message: {role, content}}]}   (completions protocol)

`/respond` runs the shared banking tool-calling loop (.._banking_agent), which searches the knowledge
base with the PROVIDED retrieval tools, verifies identity, and unlocks/calls the discoverable tool the
documents name -- all over the task-env tool-API (BANKING_TOOLS_URL / BANKING_TOOLS_TOKEN).

Binds 0.0.0.0:$PORT (never a hardcoded port) so the host-side deployer can publish it, matching the
contract in the task spec's interface_design.

Claude-only, so serving a request needs credentials; importing this module does not.
"""
from __future__ import annotations

import os
import pathlib
import sys

# When run as `python server.py` (the deployer's entry point), make the repo root importable.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI                                    # noqa: E402
from pydantic import BaseModel                                 # noqa: E402

from gold.banking_support import _banking_agent as core        # noqa: E402


class RespondRequest(BaseModel):
    messages: list[dict] = []


def create_app(client=None) -> FastAPI:
    """Build the artifact app. Pass a client for tests; else the Claude client is built lazily on
    the first request, so importing this module (and health-checking it) needs no credentials."""
    app = FastAPI(title="banking-artifact")
    app.state.client = client
    app.state.schemas = None      # discovered on first use (the tool API may expose a listing)

    def _client():
        if app.state.client is None:
            app.state.client = core.build_client()
        return app.state.client

    def _schemas():
        if app.state.schemas is None:
            app.state.schemas = core.tool_schemas()
        return app.state.schemas

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/respond")
    def respond(req: RespondRequest) -> dict:
        messages = [{"role": "system", "content": core.system_prompt()}]
        messages.extend(m for m in (req.messages or []) if m.get("role") != "system")
        text = core.run_chat(_client(), messages, _schemas())
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}

    return app


if __name__ == "__main__":
    import uvicorn

    # $PORT is injected by the deployer; bind 0.0.0.0 so the published port reaches us.
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
