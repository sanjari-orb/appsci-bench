"""FastAPI service exposing the banking-support task environment (the task-container resource).

The artifact under test connects here to ACT (agent tools -- including tau2's retrieval tools
KB_search_bm25 / KB_search_dense / shell); the eval-time user
simulator connects to execute customer-held tools (apply_for_credit_card, ...). Both mutate one
shared in-memory TransactionalDB. Retrieval runs READ-only over the corpus, so it never changes the
DB state hash; the doc ids it returns are logged per case for retrieval-recall scoring.

Routes (all bearer-token gated):
  GET  /health                       -> {"status": "ok"}                       (tool token)
  POST /tools/{name} {arguments, requestor} -> {"result": ...} | 400           (tool token)
  POST /admin/reset                  -> {"status": "reset"}                    (ADMIN token)
  POST /admin/seed {initial_state, read_log_allowlist} -> {"status":"seeded"}  (ADMIN token)
  POST /admin/load {path}            -> {"status": "loaded", "counts": {...}}  (ADMIN token; setup-only)
  GET  /admin/state_hash             -> {"state_hash": "..."}                  (ADMIN token)
  GET  /admin/retrieved_docs         -> {"retrieved_documents": [...]}         (ADMIN token)
  GET  /admin/action_trace           -> {"agent_actions": [{name,arguments,requestor}, ...]}  (ADMIN)
  GET/POST /admin/simulation_steps   -> shared eval step budget (ADMIN)
  POST /admin/simulation_steps/advance -> account for a speaker message (ADMIN)

Two tokens on purpose: the artifact + user sim get the tool token; the evaluator holds the admin
token to reset/seed the per-case DB and read the resulting state for scoring.

The admin routes are also `include_in_schema=False`, which is a SECOND, independent control and not
redundant with the token. The token stops the artifact CALLING them; it does nothing about the
auto-generated OpenAPI schema at /openapi.json, which needs no token and described them in full. A real
run proved it: the build agent fetched /openapi.json into its workspace, so its model context came to
contain every admin route, the `SeedRequest` shape with its `golden_actions` field, and the sentence
explaining that ACTION-basis cases are scored by matching the golden action trajectory. That hands the
agent under test the measurement design -- which routes exist, that a DB state hash is compared, that a
read-log allowlist decides what counts. Withholding the ORACLE is not enough if the SCORING MECHANISM is
published. The evaluator loses nothing: it holds the token and calls these routes by path, never by
schema discovery. The agent keeps /health and /tools in the schema, which is its whole legitimate surface.

THERE IS DELIBERATELY NO RAW-CORPUS ROUTE. The knowledge base reaches the artifact ONLY through the
retrieval tools (KB_search_bm25 / KB_search_dense / shell). Two routes used to serve it directly
-- a document index and a fetch-by-id -- left over from an earlier design in which retrieval was
withheld and the agent had to build its own search over a corpus we handed it. When that design was
reversed and upstream's retrieval tools were adopted 1:1, the routes were never removed, and they cost
two things. They put the task in a configuration that exists in NO upstream variant (a superset of
`all_tools`: index browsing plus fetch-by-id on demand), so our numbers stopped being comparable to any
published banking figure -- which is the whole reason the tools are adopted 1:1. And they were invisible
to `retrieval_recall`, which counts only what a SEARCH returned, so an artifact could ground perfectly
by fetching documents by id and still score zero on the objective built to measure exactly that.

Startup env (set by the orchestrator / task_env):
  BANKING_ENV_TOKEN         tool bearer token (REQUIRED)
  BANKING_ENV_ADMIN_TOKEN   admin bearer token (REQUIRED)
  BANKING_DB_PATH           optional transactional store to load AT START-UP. Unset in Docker, where
                            the service boots holding NO records and each phase's setup script loads
                            its own store via POST /admin/load -- the phase's bundle arrives only
                            after the container is running, so a start-up read could serve nothing but
                            a store baked into the image, which is present in the BUILD container
                            whatever was bundled. Kept for the localhost/pre-Docker path and for tests.
  BANKING_DOCS_PATH         optional documents/ dir override. Unlike the store, the corpus IS still
                            baked in and this IS set in Docker: it is shared whole with both phases,
                            so there is nothing to carve and nothing to load per phase.
  BANKING_EMBED_API_KEY     embeddings API key for KB_search_dense (falls back to OPENAI_API_KEY);
                            optional -- bm25/shell need no key and dense degrades without one
  BANKING_EMBED_MODEL       optional dense embedding-model override (default text-embedding-3-large)
  BANKING_EMBEDDINGS_CACHE_DIR  optional on-disk dense-embeddings cache dir
"""
from __future__ import annotations

import logging
import os
import pathlib

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import BankingEnvironment
from .data_model import TransactionalDB
from .simulation_steps import SimulationSteps, StepLimitReached

logger = logging.getLogger(__name__)


class ToolCallRequest(BaseModel):
    arguments: dict = {}
    requestor: str = "assistant"  # "user" routes to the customer-held toolkit


class SeedRequest(BaseModel):
    initial_state: dict | None = None
    read_log_allowlist: list[str] | None = None
    # The case's golden action trajectory. Sent by the evaluator's per-case reset seam so the env can
    # derive its OWN required-reads allowlist with the same function the golden replay uses (see
    # BankingEnvironment.read_log_allowlist_for -- the two sides MUST agree or a perfect agent scores
    # zero). Only the derived tool names are retained; the trajectory itself is never stored and no
    # route exposes it, so nothing reachable on the tool token can read the oracle back out.
    golden_actions: list[dict] | None = None


class LoadRequest(BaseModel):
    """A phase's transactional-store path, INSIDE the container. See the /admin/load route."""

    path: str


class SimulationLimitRequest(BaseModel):
    max_steps: int = Field(gt=0, strict=True)


class SimulationAdvanceRequest(BaseModel):
    steps: int = Field(default=1, ge=1, le=2, strict=True)


def create_app(base_env: BankingEnvironment | None = None) -> FastAPI:
    """Create the task-env FastAPI app.

    Pass a base env for tests. Otherwise the transactional store comes from ONE of two places:

      * `BANKING_DB_PATH`, if set -- the localhost/pre-Docker path, unchanged;
      * otherwise NOTHING. The service boots with the whole knowledge base and NO records, and the
        phase's setup script loads that phase's own store via `POST /admin/load`.

    BOOTING WITH NO RECORDS IS THE POINT, not a fallback. Both phases run from one image and the
    harness copies a phase's data in only after the container is running, so a service that read its
    store at start-up could serve only what was baked into the image -- which is how a carved build
    share ends up decorative while the agent reads the full store. The corpus is different in kind: it
    is shared whole with both phases, so it stays baked in and is present from the first request.
    """
    app = FastAPI(title="banking-support-task-env")
    if base_env is not None:
        base = base_env
    elif os.environ.get("BANKING_DB_PATH"):
        base = BankingEnvironment.from_fixture(
            os.environ["BANKING_DB_PATH"], os.environ.get("BANKING_DOCS_PATH") or None)
    else:
        base = BankingEnvironment.with_empty_store(os.environ.get("BANKING_DOCS_PATH") or None)
    app.state.base = base           # pristine fixture
    app.state.env = base.fresh()    # live per-case copy (reset/seeded between cases)
    # Per-case tool-call trace, in call order. Lives on app.state rather than on the env because it
    # records what was ASKED of the service, which is not part of the environment's state -- and
    # because `fresh()` already owns clearing the DB and the retrieved-doc log. Cleared by
    # /admin/reset. This is the only way the 9 ACTION-basis cases are scorable: their reward matches
    # the golden action trajectory, which no DB hash can show.
    app.state.action_trace = []
    simulation = SimulationSteps()

    tool_token = os.environ.get("BANKING_ENV_TOKEN")
    admin_token = os.environ.get("BANKING_ENV_ADMIN_TOKEN")

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
            with simulation.tool():
                return execute_tool(name, req)
        except StepLimitReached as exc:
            raise HTTPException(status_code=409, detail="simulation step limit reached") from exc

    def execute_tool(name: str, req: ToolCallRequest) -> dict:
        try:
            result = app.state.env.execute(name, req.arguments or {}, requestor=req.requestor)
        except ValueError as exc:  # unknown tool / bad args -> surface to the caller, not a 500
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TypeError as exc:
            # An ARGUMENT MISMATCH lands here, not above: calling a tool with a missing or unexpected
            # keyword raises TypeError, so it escaped the ValueError branch and became a bare 500. The
            # intent above was always to surface bad arguments to the caller; only the exception type
            # was wrong. This matters most for the eval-time customer simulator, which now does act but
            # does not know the tools' signatures: in run banking_support_20260820_124530 all 20 of its
            # actions came back "Internal Server Error", which tells it nothing it could repair, is
            # indistinguishable from the environment being broken, and reads as an infrastructure
            # failure rather than as the customer's action not landing. Python's own message names the
            # offending parameters, which is exactly the repair signal the caller needs.
            #
            # Logged as well as returned: a TypeError raised DEEP inside a working tool would also land
            # here, and while its message still reaches the caller intact, it deserves to be visible as
            # a possible defect rather than filed silently as a caller error.
            logger.warning("tool %r rejected an argument mismatch: %s", name, exc)
            raise HTTPException(
                status_code=400,
                detail=f"arguments did not match tool {name!r}: {exc}",
            ) from exc
        # Record only calls that SUCCEEDED: a rejected call (unknown tool, bad arguments) never
        # happened as an action, so putting it in the trace would let a failed attempt satisfy a
        # golden action. `requestor` is kept because golden actions distinguish agent-side from
        # customer-side calls.
        app.state.action_trace.append(
            {"name": name, "arguments": req.arguments or {}, "requestor": req.requestor}
        )
        return {"result": result}

    @app.post("/admin/reset", dependencies=[Depends(require_admin)], include_in_schema=False)
    def reset() -> dict:
        # A fresh env starts with an empty retrieved-doc log, so the per-case retrieval record is
        # cleared here alongside the DB. The action trace is separate state and must be cleared too,
        # or case N+1 would inherit case N's calls and could match its golden actions.
        with simulation.tool_lock:
            simulation.reset()
            app.state.env = app.state.base.fresh()
            app.state.action_trace = []
        return {"status": "reset"}

    @app.post("/admin/simulation_steps", dependencies=[Depends(require_admin)], include_in_schema=False)
    def configure_steps(req: SimulationLimitRequest) -> dict:
        simulation.reset(req.max_steps)
        return simulation.snapshot()

    @app.get("/admin/simulation_steps", dependencies=[Depends(require_admin)], include_in_schema=False)
    def simulation_steps() -> dict:
        return simulation.snapshot()

    @app.post("/admin/simulation_steps/advance", dependencies=[Depends(require_admin)], include_in_schema=False)
    def advance_steps(req: SimulationAdvanceRequest) -> dict:
        try:
            simulation.consume(req.steps)
        except StepLimitReached as exc:
            raise HTTPException(status_code=409, detail="simulation step limit reached") from exc
        return simulation.snapshot()

    @app.post("/admin/load", dependencies=[Depends(require_admin)], include_in_schema=False)
    def load(req: LoadRequest) -> dict:
        """Load this phase's transactional store from a path inside the container, and report what
        landed.

        ADMIN-gated, and that is the whole difference between the two phases: WHICH RECORDS EXIST is
        what the carve decides, so the artifact must be able neither to load them nor to count them.
        The setup script and the evaluator hold this token; the artifact and the customer simulator
        hold only the tool token.

        Kept out of the published OpenAPI schema for the same reason the other admin routes are, and it
        is a second control rather than a redundant one: the token stops the artifact CALLING the route,
        and does nothing about /openapi.json, which needs no token. A real run proved the difference --
        the build agent fetched the schema into its workspace, so its context came to contain every
        admin route and the sentence explaining how the cases are scored. A load route in the schema
        would additionally tell it that the store it is exploring was chosen for it and arrived from
        somewhere else, which is the one fact the split depends on not disclosing.

        Takes a PATH rather than a body of records: the file is already in the container, bundled there
        by the phase, and the full store is ~270 KB of JSON that has no business going through a request
        body.

        REPLACES BOTH COPIES, and that is load-bearing. `app.state.base` is the pristine store and
        `app.state.env` the live per-case copy; the between-case reset restores the pristine one, so a
        load that merged into it -- or that replaced only the live copy -- would make every reset
        restore boot-time state instead of this phase's, and the second eval case onward would run
        against an empty store. Replacing both also makes the load idempotent.

        LEAVES THE ACTION TRACE ALONE. That is separate state, recording what was ASKED of the service
        rather than what the environment holds, and it is the only way the nine ACTION-basis cases are
        scorable at all. Clearing it belongs to the between-case reset, not here; a load happens once at
        setup, before any case, so there is nothing of a case's to clear and nothing to be gained by
        touching it.
        """
        try:
            db = TransactionalDB.load(req.path)
        except FileNotFoundError:
            raise HTTPException(status_code=400, detail=f"no such store: {req.path}") from None
        except (ValueError, OSError) as exc:
            # A truncated file, one that is not JSON, or one whose shape the model rejects. 400 rather
            # than 500: the caller named a bad file, and the setup script's own manifest assertion is
            # what turns this into an aborted run rather than a phase that quietly serves nothing.
            raise HTTPException(status_code=400, detail=f"unreadable store: {exc}") from None
        with simulation.tool_lock:
            simulation.reset()  # loading a phase's store leaves evaluation's step budget disabled
            base = app.state.base.with_store(db)
            app.state.base = base
            app.state.env = base.fresh()
        return {"status": "loaded", "counts": base.store_counts()}

    @app.post("/admin/seed", dependencies=[Depends(require_admin)], include_in_schema=False)
    def seed(req: SeedRequest) -> dict:
        # An explicit allowlist wins (a test or a caller that derived it elsewhere); otherwise derive
        # it from the golden trajectory using the same function the golden replay calls, so both
        # sides log the same required reads. Neither given -> the allowlist stays empty, which is the
        # lenient-but-consistent default.
        allowlist = req.read_log_allowlist
        if allowlist is None and req.golden_actions is not None:
            allowlist = app.state.env.read_log_allowlist_for(req.golden_actions)
        if allowlist is not None:
            app.state.env.set_read_log_allowlist(allowlist)
        app.state.env.seed(req.initial_state)
        return {"status": "seeded", "read_log_allowlist": sorted(allowlist or [])}

    @app.get("/admin/state_hash", dependencies=[Depends(require_admin)], include_in_schema=False)
    def state_hash() -> dict:
        return {"state_hash": app.state.env.state_hash()}

    @app.get("/admin/retrieved_docs", dependencies=[Depends(require_admin)], include_in_schema=False)
    def retrieved_docs() -> dict:
        """Doc ids the artifact retrieved this case via KB_search_bm25 / KB_search_dense.
        The evaluator reads this to measure retrieval recall against the case's required_documents.
        Retrieval is read-only, so this never affects /admin/state_hash. Cleared by /admin/reset."""
        return {"retrieved_documents": sorted(app.state.env.retrieved_document_ids)}

    @app.get("/admin/action_trace", dependencies=[Depends(require_admin)], include_in_schema=False)
    def action_trace() -> dict:
        """The artifact's (and user sim's) successful tool calls this case, in call order.

        This is what makes the ACTION-basis cases measurable: their reward matches the golden action
        trajectory rather than a DB end-state, so there is nothing for a state hash to compare. The
        evaluator merges this into `final_state["agent_actions"]`. Cleared by /admin/reset."""
        return {"agent_actions": list(app.state.action_trace)}

    return app
