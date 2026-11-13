"""HTTP server for receiving artifact submissions from the agent.

The orchestrator starts this server before launching the agent container.
When the agent finishes building the artifact and calls complete,
that tool POSTs to this server's /submit endpoint with the artifact endpoint URL.

The same server also hosts POST /deploy (see docs/artifact_design.md): the agent's
`deploy_artifact` tool POSTs there with no body, and the orchestrator -- which holds the
host-visible artifact dir (bind-mounted from the agent container) and the task's deploy
contract -- runs the shared DockerArtifactDeployer host-side and returns endpoint-or-reason.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import checkpoint as artifact_checkpoint
from .deployer import DockerArtifactDeployer, artifact_fingerprint

logger = logging.getLogger(__name__)


class SubmissionRequest(BaseModel):
    """Request body for the /submit endpoint."""

    artifact_endpoint: str


@dataclass
class DeployContext:
    """Everything the host-side deployer needs to stand up the agent's artifact.

    The orchestrator populates this once the task config is known and the host artifact dir
    (bind-mounted into the agent container) exists, before the agent starts. The /deploy route
    reads it at request time and hands it to DockerArtifactDeployer. Deploy is eval-agnostic:
    this carries only what's needed to make an endpoint reachable, nothing about scoring.
    """

    artifact_dir: str
    image_name: str
    container_name: str
    network: str | None
    port: int
    endpoint_path: str = "/respond"
    health_path: str = "/health"
    env: dict[str, str] = field(default_factory=dict)
    # The run's own directory (``runs/<run_tag>``). Set only so /deploy can keep a per-deploy copy of
    # the artifact under it; deploy itself never reads it. The orchestrator always sets it -- capture
    # does not depend on observability -- so None is the seam for a caller that has no run directory at
    # all, which today means tests. Do NOT read it as "the local flow's setting": there is no in-tree
    # caller that leaves it unset, and gating capture on observability would leave the default
    # command-line run with no candidates to score later.
    run_dir: str | None = None
    # S3 target for pushing a fresh checkpoint before the run ends -- the same config the end-of-run
    # mirror uses. None means local copies only, which is what a run with observability or uploads off
    # gets: a deliberate configuration, so `upload_checkpoint` skips rather than failing.
    checkpoint_s3_config: Any | None = None


@dataclass
class ArtifactSubmissionServer:
    """FastAPI server that receives artifact submissions from the agent.

    The server runs in a background thread and exposes a /submit endpoint
    that the agent POSTs to when it has finished building the artifact.

    Example usage:
        server = ArtifactSubmissionServer(host="0.0.0.0", port=9000)
        submit_url = await server.start()
        # ... launch agent with ORCHESTRATOR_SUBMIT_URL=submit_url ...
        result = await server.wait_for_completion(timeout=3600)
        artifact_endpoint = result["artifact_endpoint"]
        await server.stop()

    Attributes:
        host: Host to bind the server to.
        port: Port to listen on.
        container_name: Name to use in submit URL (for Docker networking).
    """

    host: str = "0.0.0.0"
    port: int = 9000
    container_name: str = "host.docker.internal"  # Default for host access from Docker
    # (resolves inside the agent container because core/agent_container.py passes
    # --add-host=host.docker.internal:host-gateway -- required on Linux, see HOST_GATEWAY_RUN_ARGS)

    # Internal state
    _completion_event: asyncio.Event = field(default_factory=asyncio.Event)
    _artifact_endpoint: str | None = field(default=None)
    _deploy_context: DeployContext | None = field(default=None)
    # Fingerprint of the artifact directory as of the last SUCCESSFUL deploy, so the orchestrator can
    # tell whether the running container still corresponds to what the agent finally wrote. An agent
    # that deploys, then keeps editing, and never redeploys leaves a container running abandoned code
    # -- and eval would score that instead of the deliverable. Observed: an artifact deployed while it
    # still named a model the gateway does not serve, corrected minutes later, never redeployed; every
    # one of its 50 eval samples then failed against the stale container and scored 0.
    _deployed_fingerprint: str | None = field(default=None)
    # Endpoint of the last SUCCESSFUL deploy through this route. Distinct from `_artifact_endpoint`,
    # which is what the agent finally submitted and stays None for the whole build phase -- so the
    # deploy-status route must not report that one as "what is deployed".
    _deployed_endpoint: str | None = field(default=None)
    # Set when a per-deploy artifact checkpoint could not be captured. Fatal to the run, not to the
    # deploy: the orchestrator's wait loop reads it and aborts, because a lost candidate means this run
    # can no longer produce a complete set for meta-eval and nothing downstream could tell.
    _checkpoint_error: str | None = field(default=None)
    _server: uvicorn.Server | None = field(default=None)
    _thread: threading.Thread | None = field(default=None)
    _app: FastAPI | None = field(default=None)
    _loop: asyncio.AbstractEventLoop | None = field(default=None)

    def __post_init__(self) -> None:
        """Initialize mutable fields."""
        self._completion_event = asyncio.Event()
        self._artifact_endpoint = None
        self._deploy_context = None
        self._deployed_endpoint = None
        self._checkpoint_error = None
        self._server = None
        self._thread = None
        self._app = None
        self._loop = None

    def configure_deploy(self, context: DeployContext | None) -> None:
        """Enable (or clear) the POST /deploy route with the given deploy contract.

        Called by the orchestrator after the task config is known and the host artifact dir
        exists, before the agent container starts. Until this is set, /deploy returns a
        descriptive error (deploy not configured for this run)."""
        self._deploy_context = context

    def _checkpoint_deploy(self, ctx: DeployContext, result: Any) -> None:
        """Keep a copy of the just-deployed artifact directory under the run.

        Never raises: a capture failure is recorded for the orchestrator to abort on, so the deploy
        response stays truthful about the deploy.

        Only THIS route checkpoints. The pre-eval redeploy and the eval-only deploy path build the same
        shared deployer, but neither is an agent decision: the first re-runs bytes already captured
        here, and the second stands up a checkpoint in order to score it, in a context with no run
        directory to write into. See `appsci_bench/orchestrator/checkpoint.py`.
        """
        if ctx.run_dir is None:
            return
        try:
            record = artifact_checkpoint.write_checkpoint(
                ctx.artifact_dir,
                ctx.run_dir,
                ctx.container_name,
                success=result.success,
                healthy=result.healthy,
            )
            if record is not None:
                # Synchronous, and fatal if it fails: a candidate that exists only on this host is not
                # one a later pass can read, so the checkpoint is not captured until it is mirrored.
                artifact_checkpoint.upload_checkpoint(
                    ctx.run_dir, record.name, ctx.checkpoint_s3_config
                )
        except artifact_checkpoint.CheckpointError as e:
            # Record it and let the orchestrator abort. NOT reported to the agent as a deploy failure:
            # the container is up, and telling the system under test its code broke when the host's
            # disk did would have it rewrite working code -- corrupting the measurement on the way out
            # of a run that is ending anyway.
            #
            # Traceback for everything EXCEPT an expired AWS session, whose message is already the
            # remediation and would be buried under a botocore stack. This is the last place the cause
            # is visible -- the error travels onward as a string -- and it now ends the run, so an
            # undiagnosable one costs a whole run. Same split as `_mirror_run_to_s3`.
            logger.error("%s", e, exc_info=not artifact_checkpoint.is_credentials_failure(e))
            self._checkpoint_error = str(e)
            return

    @property
    def checkpoint_error(self) -> str | None:
        """Why a per-deploy artifact checkpoint could not be captured, if one couldn't.

        Non-None means the run must not continue: a candidate the optimizer deployed is permanently
        gone, so no later pass could distinguish an incomplete candidate set from a short one.
        """
        return self._checkpoint_error

    def artifact_dir_or_none(self) -> Path | None:
        """The artifact SOURCE directory this run deploys from, or None if it has no host deploy.

        Parallel eval needs it to stand up more copies of the artifact, one per copy of the task
        environment. None means the agent submitted an endpoint it stood up some other way, so there
        is nothing for the harness to replicate -- the same condition `redeploy_before_eval` refuses
        on, and parallel eval refuses on it too rather than scoring a narrower run than was asked for.
        """
        ctx = self._deploy_context
        return Path(ctx.artifact_dir) if ctx is not None else None

    def redeploy_before_eval(self) -> tuple[str | None, str]:
        """Rebuild + restart the artifact container before eval. ALWAYS redeploys.

        Called by the orchestrator at the build->eval handover, and it has two jobs.

        1. **Point the artifact at the eval environment.** The build env is torn down and the eval env
           brought up in its place, under the same container name and network alias -- so the
           artifact's connection string still resolves, but any connection it had already established
           is a dead socket, and whether it recovers depends on code the agent wrote. Restarting it
           re-establishes every connection regardless of how its author managed them.
        2. **Score the code the agent actually finished with.** Deploy is agent-driven, so an agent
           can deploy, keep editing, and never redeploy -- leaving eval to score code the agent
           replaced. Observed: an artifact deployed while it still named a model the gateway does not
           serve, corrected minutes later, never redeployed; all 50 of its eval samples failed against
           the stale container and the cell scored 0.05 with nothing in the score to say why.

        Returns `(endpoint_url, detail)`. `endpoint_url` is non-None when the redeploy succeeded, in
        which case the caller MUST use it -- the artifact's host port is published ephemerally and is
        reassigned on every start, so the old endpoint stops resolving. `detail` always carries a
        human-readable reason, for the log and the run record.

        A failed rebuild deliberately does NOT abort: the previously deployed container is still the
        agent's own work, so eval proceeds against it with the failure recorded. Aborting would turn a
        partially-recoverable run into no measurement at all.

        This does NOT write an artifact checkpoint, and should not start: it is the harness redeploying
        bytes the agent already deployed through /deploy, so a checkpoint here would duplicate the
        previous one on every single run. Only agent-initiated deploys are checkpointed.
        """
        ctx = self._deploy_context
        if ctx is None:
            return None, "no host deploy configured for this run"
        if self._deployed_fingerprint is None:
            # The agent submitted an endpoint it stood up some other way (or never deployed through
            # this route). Rebuilding would replace a container this code did not create -- and the
            # artifact is then also responsible for its own reconnection across the handover.
            return None, "artifact was never deployed through the host deploy route"

        current = artifact_fingerprint(ctx.artifact_dir)
        changed = current != self._deployed_fingerprint
        if changed:
            logger.warning(
                "Artifact directory changed after the agent's last deploy -- rebuilding so eval "
                "scores the final code rather than the abandoned container (dir=%s)", ctx.artifact_dir,
            )
        else:
            logger.info(
                "Redeploying the artifact before eval so it reconnects to the eval environment "
                "(directory unchanged since the agent's last deploy)."
            )
        deployer = DockerArtifactDeployer(
            artifact_dir=ctx.artifact_dir,
            image_name=ctx.image_name,
            container_name=ctx.container_name,
            network=ctx.network,
            port=ctx.port,
            endpoint_path=ctx.endpoint_path,
            health_path=ctx.health_path,
            env=ctx.env,
        )
        result = deployer.deploy()
        if not result.success:
            stale = (" and its directory had ALSO changed since that deploy, so it does not match "
                     "the submitted artifact") if changed else ""
            return None, (
                f"pre-eval artifact redeploy FAILED ({result.error}); eval ran against the "
                f"previously deployed container, which was not restarted against the eval "
                f"environment{stale}"
            )
        self._deployed_fingerprint = current
        why = ("the directory had changed since the agent's last deploy, and to reconnect it to the "
               "eval environment") if changed else "to reconnect it to the eval environment"
        detail = (
            f"artifact rebuilt from its final directory before eval, {why} "
            f"(healthy={result.healthy}: {result.health_detail})"
        )
        return result.endpoint_url, detail

    def _create_app(self) -> FastAPI:
        """Create the FastAPI application with submit endpoint."""
        app = FastAPI(title="orchestrator-submission")

        @app.get("/health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/submit")
        def submit(req: SubmissionRequest) -> dict[str, str]:
            if self._artifact_endpoint is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Artifact already submitted",
                )

            if not req.artifact_endpoint.strip():
                raise HTTPException(
                    status_code=400,
                    detail="artifact_endpoint is required",
                )

            # The normal benchmark always configures host deployment. Enforce the same
            # submission invariants for every caller, including raw curl requests; client
            # libraries and prompts cannot enforce them. Keep standalone endpoint-only
            # callers (no deploy context) compatible with their existing contract.
            ctx = self._deploy_context
            if ctx is not None:
                if not self._deployed_fingerprint or not self._deployed_endpoint:
                    raise HTTPException(status_code=409, detail="Deploy the artifact successfully before submitting")
                if req.artifact_endpoint != self._deployed_endpoint:
                    raise HTTPException(status_code=409, detail="Submit the endpoint returned by the latest successful deployment")
                if artifact_fingerprint(ctx.artifact_dir) != self._deployed_fingerprint:
                    raise HTTPException(status_code=409, detail="Artifact source changed after deployment; deploy and test it again")

            logger.info(f"Received artifact submission: {req.artifact_endpoint}")
            self._artifact_endpoint = req.artifact_endpoint

            # Signal completion in the event loop
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._completion_event.set)

            return {
                "status": "accepted",
                "message": "Artifact endpoint registered. Evaluation will begin.",
            }

        @app.get("/deploy/status")
        def deploy_status() -> dict[str, Any]:
            """Report what is currently deployed, whether it is stale, and which checkpoint it is.

            Read-only, and the authority for three things the agent container cannot work out for
            itself:

            1. **Has anything been deployed through this route.** The agent's own tool context is lost
               if the agent process restarts, and an endpoint submitted some other way was never built
               here at all.
            2. **Is the artifact directory still what was deployed.** The fingerprint is computed here
               because the orchestrator owns the one implementation of it (`artifact_fingerprint`); a
               second copy inside the agent would drift, and the two would disagree about staleness
               exactly when it mattered. The agent's `optimize_agent` refuses to score a stale
               candidate, since a score attributed to code that is not running is worse than no score.
            3. **Which checkpoint the running container was built from.** Checkpoints live under the
               run directory, which is deliberately never mounted into the agent container, so the
               agent can only learn its candidate's identity by asking. `checkpoint` is null whenever
               there is honestly no answer -- no run directory, nothing deployed, a stale directory, or
               simply no match -- and callers must record that rather than assume the latest one.

            Never 500s on a missing artifact dir or an unreadable manifest: a status call that fails is
            read by the agent as "cannot tell", which is the same conclusion but with a worse message.
            """
            ctx = self._deploy_context
            if ctx is None:
                return {
                    "configured": False,
                    "deployed": False,
                    "reason": (
                        "deploy is not configured for this run -- the orchestrator did not "
                        "provision a host artifact dir / deploy contract."
                    ),
                }

            deployed_fingerprint = self._deployed_fingerprint
            current_fingerprint = artifact_fingerprint(ctx.artifact_dir)
            stale = bool(
                deployed_fingerprint is not None and current_fingerprint != deployed_fingerprint
            )
            # Looked up by re-hashing the live directory rather than by handing over
            # `deployed_fingerprint`: that one is taken with the staleness exclusion set, and
            # checkpoints are hashed with a wider one, so the two differ on any run where the agent
            # left a `.venv` or `node_modules` in its output directory. Only valid while the directory
            # matches what was deployed, hence the `not stale` guard -- otherwise this would name the
            # checkpoint for code that is not running.
            checkpoint = (
                artifact_checkpoint.find_for_artifact(ctx.run_dir, ctx.artifact_dir)
                if ctx.run_dir and deployed_fingerprint and not stale else None
            )
            return {
                "configured": True,
                # False when the agent never deployed through this route: there is then no fingerprint
                # to compare against and no container this code stood up.
                "deployed": deployed_fingerprint is not None,
                "endpoint_url": self._deployed_endpoint,
                "container_name": ctx.container_name,
                "deployed_fingerprint": deployed_fingerprint,
                "current_fingerprint": current_fingerprint or None,
                # Only meaningful once something has been deployed. Recording a score does NOT make the
                # artifact stale -- `optimize_agent` keeps its score history outside this directory
                # precisely so that a measurement never looks like an edit.
                "stale": stale,
                "checkpoint": checkpoint,
            }

        @app.post("/deploy")
        def deploy() -> dict[str, Any]:
            """Build + run the agent's artifact host-side and return endpoint-or-reason.

            Empty body: the artifact dir, container name, port, network and env are all ambient
            on the orchestrator (set via configure_deploy). Idempotent -- the deployer clears any
            stale container first -- so the agent may call this repeatedly while iterating. On any
            failure returns {"success": false, "error": ...}; the tool turns that into a tool
            failure with the descriptive message, and the agent fixes its code and redeploys."""
            ctx = self._deploy_context
            if ctx is None:
                return {
                    "success": False,
                    "error": (
                        "deploy is not configured for this run -- the orchestrator did not "
                        "provision a host artifact dir / deploy contract."
                    ),
                }

            deployer = DockerArtifactDeployer(
                artifact_dir=ctx.artifact_dir,
                image_name=ctx.image_name,
                container_name=ctx.container_name,
                network=ctx.network,
                port=ctx.port,
                endpoint_path=ctx.endpoint_path,
                health_path=ctx.health_path,
                env=ctx.env,
            )
            logger.info("Deploy requested for artifact dir %s", ctx.artifact_dir)
            result = deployer.deploy()
            # Keep a copy of what was just deployed, before the agent's next edit overwrites the
            # directory. Attempts that FAILED are checkpointed too: that source is the state the next
            # attempt destroys, and it is never scored, so the files are the only record it existed.
            self._checkpoint_deploy(ctx, result)
            if result.success:
                # Remember what was deployed, so a later edit-without-redeploy is detectable before
                # eval scores the abandoned code. Recorded only on success: a failed deploy leaves
                # whatever was running before, and claiming that fingerprint as deployed would make a
                # subsequent staleness check pass on code that never shipped.
                self._deployed_fingerprint = artifact_fingerprint(ctx.artifact_dir)
                self._deployed_endpoint = result.endpoint_url
                return {
                    "success": True,
                    "endpoint_url": result.endpoint_url,
                    "container_name": result.container_name,
                    # Report what was actually observed, not a constant: `healthy` is True only when
                    # the artifact's health route answered HTTP 200. A reachable-but-not-200 artifact
                    # still deploys (it answers, and eval's health gate falls back to the base
                    # endpoint), but telling the agent "healthy" would hide a defect it could still
                    # fix during BUILD -- so `health_detail` carries the reason either way.
                    "healthy": result.healthy,
                    "health_detail": result.health_detail,
                }
            return {"success": False, "error": result.error}

        return app

    async def start(self) -> str:
        """Start the submission server and return the submit URL.

        Returns:
            The submit URL that agents should POST to (e.g., http://host.docker.internal:9000/submit).

        Raises:
            RuntimeError: If the server fails to start.
        """
        self._loop = asyncio.get_event_loop()
        self._app = self._create_app()

        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)

        # Run server in a background thread
        def run_server() -> None:
            asyncio.run(self._server.serve())  # type: ignore[union-attr]

        self._thread = threading.Thread(target=run_server, daemon=True)
        self._thread.start()

        # Wait for server to be ready
        await asyncio.sleep(0.5)  # Give uvicorn time to start

        submit_url = f"http://{self.container_name}:{self.port}/submit"
        logger.info(f"Submission server started at {submit_url}")
        return submit_url

    async def wait_for_completion(self, timeout: int = 3600) -> dict[str, Any]:
        """Wait for the agent to POST to /submit.

        Args:
            timeout: Maximum seconds to wait for submission (default 1 hour).

        Returns:
            Dict containing the artifact_endpoint from the submission.

        Raises:
            TimeoutError: If no submission received within timeout.
            RuntimeError: If submission received but artifact_endpoint was empty.
        """
        try:
            await asyncio.wait_for(
                self._completion_event.wait(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"No submission received within {timeout} seconds"
            ) from None

        if not self._artifact_endpoint:
            raise RuntimeError("Submission received but artifact_endpoint was empty")

        return {"artifact_endpoint": self._artifact_endpoint}

    async def stop(self) -> None:
        """Stop the submission server."""
        if self._server is not None:
            self._server.should_exit = True
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=5)
            logger.info("Submission server stopped")

    def reset(self) -> None:
        """Reset the server state for reuse (e.g., in tests)."""
        self._artifact_endpoint = None
        self._completion_event.clear()
