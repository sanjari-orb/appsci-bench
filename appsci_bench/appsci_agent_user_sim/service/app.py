"""FastAPI app for the appsci-agent-user-sim service: one session, two routes (/health, /ask).

The service process IS the session (see service/session.py). At startup it reads its run params from
the environment, builds the bound AppsciAgentLLMUserSim + the single Session, and serves:

  GET  /health  -> {"status": "ok"}            (readiness; bearer-token required like every route)
  POST /ask     {question} -> {message, turn}  (history-aware: the Session accumulates the transcript)

Startup env (set by the orchestrator; never seen by the agent):
  APPSCI_USERSIM_TOKEN        per-run bearer token the agent must present (REQUIRED).
  APPSCI_USERSIM_SPEC_PATH    path to the task spec.yaml (REQUIRED) -- read for persona-rendering fields.
  APPSCI_USERSIM_PERSONA_PATH path to the bound persona YAML (REQUIRED).
  APPSCI_USERSIM_MODEL -- this sim's model id (optional; falls back to the shared MODEL_NAME).
  APPSCI_USERSIM_DOCS_DIR     the task's user_sim_docs/ mount, holding the documents the persona
                              declares in `known_docs` (OPTIONAL, defaults to /user_sim_docs). This
                              directory is mounted ONLY into this container -- it is what the agent
                              must elicit rather than read. A persona that declares documents the
                              mount cannot supply fails startup (loud, not a doc-less sim).
  MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY -- shared model config default. For a barebone
  first-party provider, leave MODEL_API_KEY unset and set the provider-standard key
  (ANTHROPIC_API_KEY / OPENAI_API_KEY / ...), which LiteLLM reads itself.

Contract + error codes mirror docs/user_simulator_api.md. Part of the standalone appsci_agent_user_sim
package -- imports nothing from appsci_bench other than the appsci_bench.common leaf (ModelConfig).
"""
from __future__ import annotations

import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from appsci_bench.common.model_config import config_for
from ..core.task_context import load_task_context
from ..core.usersim_core import AppsciAgentLLMUserSim, AppsciAgentUserSimError
from ..core.persona import load_user_persona
from .session import Session

logger = logging.getLogger(__name__)


#: Upper bound on one question, in characters. The agent under test is the only writer here and this is
#: the last place anything bounds it: the request is emitted to the event log before the model is
#: called, that log is a host bind-mount covered by no size budget, and the whole file is later read
#: into memory and rendered into the judge's prompt. Two orders of magnitude above any real question
#: observed (the longest is under 2,000 characters), so it rejects only abuse -- and rejecting at the
#: door means an oversized request never reaches disk. Deliberately NOT a truncation: clipping the text
#: is what the payload cap used to do, and a clipped answer reads as a fact the persona never gave.
_MAX_QUESTION_CHARS = 100_000


class AskRequest(BaseModel):
    question: str = Field(max_length=_MAX_QUESTION_CHARS)


class AskResponse(BaseModel):
    message: str
    turn: int


#: Where the task's user_sim_docs/ tree is mounted in this container. Only this container gets it --
#: the agent has no path to it, which is the whole point (see issue #76).
DEFAULT_DOCS_DIR = "/user_sim_docs"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required to start the appsci-agent-user-sim service")
    return value


def build_session() -> Session:
    """Build the single Session from environment config (called once at startup; fails fast)."""
    spec_path = _require_env("APPSCI_USERSIM_SPEC_PATH")
    persona_path = _require_env("APPSCI_USERSIM_PERSONA_PATH")
    # Not required: a task whose personas declare no `known_docs` needs no docs mount. If a persona
    # DOES declare documents and this directory cannot supply them, from_persona raises and the
    # service does not start -- a sim that silently lacks its policy is indistinguishable in a trace
    # from an agent that failed to elicit, which is the failure this replaced.
    docs_dir = os.environ.get("APPSCI_USERSIM_DOCS_DIR") or DEFAULT_DOCS_DIR
    task = load_task_context(spec_path)
    sim = AppsciAgentLLMUserSim.from_persona(
        task, persona_path, config_for("APPSCI_USERSIM_MODEL"), docs_dir)
    # The persona's role is the user-sim's trace name (e.g. "Product Manager"); stamped on its events.
    role = load_user_persona(persona_path, validate=False).get("role") or "user-sim"
    return Session(sim, name=role)


def create_app(session: Session | None = None) -> FastAPI:
    """Create the FastAPI app. Pass a Session for tests; otherwise one is built from the env."""
    app = FastAPI(title="appsci-agent-user-sim")
    expected_token = os.environ.get("APPSCI_USERSIM_TOKEN")
    app.state.session = session if session is not None else build_session()

    def require_token(authorization: str | None = Header(default=None)) -> None:
        # Every route (including /health) is behind the per-run bearer token.
        if not expected_token or authorization != f"Bearer {expected_token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health", dependencies=[Depends(require_token)])
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/ask", response_model=AskResponse, dependencies=[Depends(require_token)])
    def ask(req: AskRequest, x_obs_parent_span: str | None = Header(default=None)) -> AskResponse:
        if not req.question or not req.question.strip():
            raise HTTPException(status_code=400, detail="question must be non-empty")
        sess: Session | None = app.state.session
        if sess is None:
            raise HTTPException(status_code=409, detail="no active session")
        try:
            # x_obs_parent_span (X-Obs-Parent-Span header) is the agent's talk_to_user tool span,
            # forwarded so this exchange nests under that turn in the trace. None = no parent.
            message, turn = sess.ask(req.question, parent_span_id=x_obs_parent_span)
        except AppsciAgentUserSimError as exc:
            # LOG before converting. The detail below reaches the caller in the 500 body, but the
            # agent's talk_to_user reports only httpx's "Server error '500 ...' for url ..." and drops
            # it -- so without this line the reason a build stalled exists nowhere: not in this
            # container's log, not in the run dir, not in the trace.
            logger.error("user-sim /ask failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return AskResponse(message=message, turn=turn)

    return app
