"""Locally-hosted airline artifact agent (FastAPI) -- the localhost stand-in for the artifact CONTAINER.

A real HTTP server implementing the artifact contract the evaluator queries:
  GET  /health   -> {"status": "ok"}                             (open; MockDeployer health-checks it)
  POST /respond  {messages} -> {choices: [{message: {role, content}}]}   (completions protocol)

`/respond` runs the shared airline tool-calling loop (.._airline_agent), which acts on the
task-environment via the airline tool-API (AIRLINE_TOOLS_URL/AIRLINE_TOOLS_TOKEN) and follows the
policy. Claude-only (the LLM client is appsci_agent's). Deployed locally via MockDeployer
(`python server.py`).
"""
from __future__ import annotations

import os
import pathlib
import sys

# When run as `python server.py` (MockDeployer), ensure the repo root is importable.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI                                   # noqa: E402
from pydantic import BaseModel                               # noqa: E402

from gold.airline_rebooking import _airline_agent as core    # noqa: E402


class RespondRequest(BaseModel):
    messages: list[dict] = []


def create_app(client=None) -> FastAPI:
    """Build the artifact app. Pass a client for tests; else the Claude client is built lazily."""
    app = FastAPI(title="airline-artifact")
    app.state.client = client
    app.state.schemas = core.tool_schemas()

    def _client():
        if app.state.client is None:
            app.state.client = core.build_client()    # lazy: only needs creds when first queried
        return app.state.client

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/respond")
    def respond(req: RespondRequest) -> dict:
        messages = list(req.messages)
        if not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": core.system_prompt()}, *messages]
        content, trace = core.run_chat(_client(), messages, app.state.schemas, collect_trace=True)
        return {"choices": [{"message": {"role": "assistant", "content": content}}], "trace": trace}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("APPSCI_AGENT_ARTIFACT_HOST", "127.0.0.1")
    port = int(os.environ.get("APPSCI_AGENT_ARTIFACT_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="warning")
