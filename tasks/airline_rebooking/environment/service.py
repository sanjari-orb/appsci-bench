"""FastAPI service exposing the airline task environment (the design's "task container" resource).

The artifact under test connects here to ACT: it calls the airline tools over HTTP; the calls mutate
an in-memory FlightDB held by the service. This is the localhost (pre-Docker) realization of the
3-container design's task environment.

Routes (all bearer-token gated):
  GET  /health                 -> {"status": "ok"}                     (tool token)
  POST /tools/{name}  {arguments} -> {"result": ...} | 400 {detail}    (tool token; artifact-facing)
  POST /admin/reset            -> {"status": "reset"}                   (ADMIN token; evaluator-only)
  POST /admin/load    {path}   -> {"status": "loaded", "counts": {...}} (ADMIN token; setup-only)
  GET  /admin/state_hash       -> {"state_hash": "..."}                (ADMIN token; evaluator-only)

Two tokens on purpose: the artifact gets the tool token only; the evaluator holds the admin token to
reset the per-case DB and read the resulting state for scoring. The artifact never sees admin routes.

Startup env (set by the orchestrator):
  AIRLINE_ENV_TOKEN         tool bearer token the artifact must present (REQUIRED)
  AIRLINE_ENV_ADMIN_TOKEN   admin bearer token the evaluator presents (REQUIRED)
  AIRLINE_DB_PATH           optional fixture to load AT START-UP. Unset in Docker, where the
                            service boots EMPTY and each phase's setup script loads its own data
                            via POST /admin/load -- the bundle arrives after the container is
                            running, so a start-up read could only serve a baked-in store.
"""
from __future__ import annotations

import os
import pathlib

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import AirlineEnvironment
from .tools import ToolError


class ToolCallRequest(BaseModel):
    arguments: dict = {}


class LoadRequest(BaseModel):
    """A phase's fixture path, inside the container. See the /admin/load route."""

    path: str


def create_app(base_env: AirlineEnvironment | None = None) -> FastAPI:
    """Create the task-env FastAPI app.

    Pass a base env for tests. Otherwise the store comes from ONE of two places:

      * `AIRLINE_DB_PATH`, if set -- the localhost/pre-Docker path, unchanged;
      * otherwise NOTHING. The service boots empty and its phase's setup script loads the data via
        `POST /admin/load`.

    BOOTING EMPTY IS THE POINT, not a fallback. Both phases run from one image and the harness copies
    a phase's data in only after the container is running, so a service that read its fixture at
    start-up could serve only what was baked into the image -- which is how a carved build share ends
    up decorative while the agent reads the full store (see docs/task_env_carve_spec.md steps 5-6).
    """
    app = FastAPI(title="airline-task-env")
    if base_env is not None:
        base = base_env
    elif os.environ.get("AIRLINE_DB_PATH"):
        base = AirlineEnvironment.from_fixture(os.environ["AIRLINE_DB_PATH"])
    else:
        base = AirlineEnvironment.empty()
    # `base` is the pristine fixture; `app.state.env` is the live per-case copy (reset between cases).
    app.state.base = base
    app.state.env = base.fresh()

    tool_token = os.environ.get("AIRLINE_ENV_TOKEN")
    admin_token = os.environ.get("AIRLINE_ENV_ADMIN_TOKEN")

    def _check(authorization: str | None, expected: str | None) -> None:
        if not expected or authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="unauthorized")

    def require_tool(authorization: str | None = Header(default=None)) -> None:
        _check(authorization, tool_token)

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        _check(authorization, admin_token)

    @app.get("/health", dependencies=[Depends(require_tool)])
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/tools/{name}", dependencies=[Depends(require_tool)])
    def call_tool(name: str, req: ToolCallRequest) -> dict:
        try:
            result = app.state.env.tools.execute(name, req.arguments or {})
        except ToolError as exc:
            # Mirror tau2: a precondition failure is surfaced to the caller (the agent), not a 500.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"result": result}

    @app.post("/admin/reset", dependencies=[Depends(require_admin)])
    def reset() -> dict:
        app.state.env = app.state.base.fresh()
        return {"status": "reset"}

    @app.post("/admin/load", dependencies=[Depends(require_admin)])
    def load(req: LoadRequest) -> dict:
        """Load this phase's store from a path inside the container, and report what landed.

        ADMIN-gated, like reset and state_hash: which records exist is the difference between the
        build and eval phases, so the artifact must be able neither to load them nor to discover how
        many there are. The setup script holds the admin token; the artifact never does.

        Takes a PATH rather than a body of records -- the eval store is ~2 MB of JSON and the file is
        already in the container, bundled there by the phase. Returns the per-collection counts so the
        caller can assert the load actually happened: `/health` answers while the store is empty, by
        design, so readiness proves nothing about data.

        Replaces both the pristine base and the live copy, so a load is idempotent and a phase that
        loads twice is not additive.
        """
        path = pathlib.Path(req.path)
        try:
            base = AirlineEnvironment.from_fixture(path)
        except FileNotFoundError:
            raise HTTPException(status_code=400, detail=f"no such fixture: {req.path}") from None
        except (ValueError, OSError) as e:
            # A truncated or non-JSON fixture. 400 rather than 500: the caller passed a bad file, and
            # the setup script's own assertions are what turn this into an aborted run.
            raise HTTPException(status_code=400, detail=f"unreadable fixture: {e}") from None
        app.state.base = base
        app.state.env = base.fresh()
        return {"status": "loaded", "counts": base.db.counts()}

    @app.get("/admin/state_hash", dependencies=[Depends(require_admin)])
    def state_hash() -> dict:
        return {"state_hash": app.state.env.state_hash()}

    return app
