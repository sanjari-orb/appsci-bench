"""Host-side Docker deployer for artifact agents.

This is the single deploy mechanism shared by BOTH callers (see docs/artifact_design.md):
  - the agent's `deploy_artifact` tool, via the orchestrator's `POST /deploy` route, and
  - the standalone eval-only pipeline (`eval_artifact` CLI).

It takes an artifact directory the agent authored (a Dockerfile + a server entry) and turns it into a
healthy, reachable container endpoint -- or fails with a specific, actionable reason at whichever step
broke. Deployment is deliberately "not the thing under test": every failure names the step and carries
the underlying docker/stderr/log output so the caller can fix its code and redeploy.

A deploy that succeeds is one whose container *answers*; whether it PASSED its health check is
reported separately on the result (`healthy` / `health_detail`) rather than assumed. See
`DeploymentResult` -- an artifact that answers 404 on /health is reachable but not conformant, and
callers must report that difference to the agent instead of flattening both to "healthy".

The deployer runs on the HOST (in the orchestrator process), so reachability is always host-vantage:
the container publishes its port, and health is polled at http://localhost:<published-port>. The
returned `endpoint_url` is the container-DNS URL (http://<name>:<port><path>) -- what the agent submits
and what the orchestrator/eval later translate to a host URL via `docker port`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# LLM / proxy credentials the deployed artifact needs -- it is itself an agent, so it must reach a
# model provider or it crashes at import for lack of a key. These mirror exactly what
# `core/agent_container.py` forwards to the meta-agent. The difference: agent_container can use bare
# `docker run -e NAME` (the agent container inherits the orchestrator's own env), but the deployer
# builds `-e NAME=VALUE`, so we must resolve the VALUES from the host env here. Injecting these at
# deploy time is what removes any incentive for the agent to bake a plaintext token into its image
# (the credential-leak failure mode we hit before this was wired up).
LLM_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",      # first-party Anthropic
    "ANTHROPIC_AUTH_TOKEN",   # Anthropic gateway/proxy token -- the SDK's own auth fallback
    "ANTHROPIC_BASE_URL",     # its matching endpoint -- forwarded as a pair, never alone
    "OPENAI_API_KEY",         # first-party OpenAI -- or a gateway token (see OPENAI_BASE_URL)
    "OPENAI_BASE_URL",        # custom OpenAI-compatible endpoint; must travel with the key above
    "APPSCI_AGENT_API_KEY",   # gateway/proxy token
    "APPSCI_AGENT_BASE_URL",  # custom LLM endpoint (the proxy)
    # Fireworks, when the agent under test runs there (AGENT_MODEL=fireworks_ai/...). A first-party
    # key: LiteLLM resolves the endpoint from the model id, so there is no base-url var to pair with
    # it. Both spellings LiteLLM accepts are listed, since the artifact only needs whichever the
    # operator set.
    "FIREWORKS_API_KEY",
    "FIREWORKS_AI_API_KEY",
    # Gateway identity headers, e.g. "X-Gateway-User: <email>". Not a credential (auth is the token
    # above), but it rides the same forwarding path because the gateway can reject a request that
    # omits it -- and a deployed artifact making its own LLM calls needs it just as much as the
    # meta-agent does. Absent from the host env => simply not forwarded.
    "APPSCI_AGENT_CUSTOM_HEADERS",
)


def llm_credential_env() -> dict[str, str]:
    """Return the subset of LLM/proxy credential vars actually present in the host environment.

    Only keys that are set are returned (absent ones are omitted, not blanked) -- the artifact's
    client picks whichever provider's key is present, same as the meta-agent. Callers merge this into
    the deploy env so the artifact inherits the same credentials the meta-agent runs with.
    """
    return {name: os.environ[name] for name in LLM_CREDENTIAL_ENV_VARS if name in os.environ}


#: Directories `artifact_fingerprint` skips by default. Kept as the historic pair so every existing
#: caller (the deploy staleness check) hashes exactly what it always did.
_FINGERPRINT_DEFAULT_EXCLUDE_DIRS = frozenset({"__pycache__", ".git"})


def artifact_fingerprint(
    artifact_dir: str | Path, exclude_dirs: frozenset[str] | set[str] | None = None
) -> str:
    """Content hash of an artifact directory, for "is the running container still this code?".

    Deploy is agent-driven and idempotent: the agent may deploy, keep editing, and deploy again. What
    nothing guaranteed is that the container eval queries was built from what the agent FINALLY wrote.
    An agent that deploys a server naming a model the gateway does not serve, corrects the file, and
    never redeploys leaves eval scoring code the agent itself abandoned -- which reads as a task
    failure rather than the stale deployment it is.

    Hashes each file's repo-relative path together with its bytes, so a rename counts as a change.
    Skipped, because they change without the build changing and would force a pointless redeploy:
    `__pycache__` directories and `.pyc` files (writing them is a side effect of merely importing the
    server), and `.git`. Unreadable files are folded in by path alone rather than raising -- a
    fingerprint is a comparison aid, and failing the run over an odd file mode would be worse than
    comparing slightly less.

    Args:
        artifact_dir: Directory to hash.
        exclude_dirs: Directory names to skip anywhere in the tree, replacing the default pair. Pass a
            wider set to hash a tree the same way a pruned COPY of it was hashed -- otherwise the two
            differ by exactly the directories the copy dropped, and comparing them always reports a
            mismatch. `.pyc` files are skipped regardless.
    """
    excluded = _FINGERPRINT_DEFAULT_EXCLUDE_DIRS if exclude_dirs is None else exclude_dirs
    root = Path(artifact_dir)
    digest = hashlib.sha256()
    if not root.is_dir():
        return ""
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        parts = set(path.relative_to(root).parts)
        if parts & set(excluded) or path.suffix == ".pyc":
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8", "replace"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def _redact_env_values(argv: list[str]) -> list[str]:
    """Return `argv` with every `-e NAME=VALUE` value replaced by `<redacted>`.

    The deploy argv carries the artifact's LLM credentials (see `llm_credential_env`), so it can only
    be logged with the values stripped -- otherwise diagnosing a `docker run` failure would write a
    plaintext token into the orchestrator log, the same leak we inject at deploy time to avoid.

    This is name-agnostic (every `-e` value goes), so non-secret forwards like
    APPSCI_AGENT_CUSTOM_HEADERS are redacted here too. Slightly over-eager for an identity header,
    but the var NAME still shows in the log, which is all this line is for -- so it stays as-is
    rather than growing a per-name allowlist.
    """
    out: list[str] = []
    for i, arg in enumerate(argv):
        if i > 0 and argv[i - 1] == "-e" and "=" in arg:
            out.append(f"{arg.split('=', 1)[0]}=<redacted>")
        else:
            out.append(arg)
    return out


# Server-entry candidates the agent may author, in priority order. Mirrors the list the agent-side
# MockDeployer (`appsci_agent/artifact/deployer.py:_find_entry_point`) already uses, so the deployment
# contract is the same whichever path builds the artifact. The Dockerfile's CMD is what actually
# launches the server; we only verify a recognized entry is present so a missing one fails fast with a
# clear message rather than as an opaque build/run error.
SERVER_ENTRY_CANDIDATES = ("server.py", "app.py", "main.py")


@dataclass
class DeploymentResult:
    """Outcome of a deploy attempt.

    On success: `endpoint_url` is the container-DNS completions URL and `container_name` names the
    running container. On failure: `error` names the step and carries the underlying output; every
    other field is empty. `logs` holds any container-log tail captured on a health failure.

    `success` and `healthy` are deliberately DIFFERENT claims, and callers must not conflate them:
      - `success` means the container is up and *answering* on its published port. That's what the
        deploy gates on, because a server that answers is one the evaluator can query (eval's own
        health gate falls back to the base endpoint when there's no health route -- see
        `appsci_bench/eval/artifact.py`).
      - `healthy` is the strictly stronger claim that `health_path` returned HTTP 200. An artifact
        answering 404/500 there is reachable but not conformant, and reporting it as "healthy" would
        hide a real defect from the agent until eval.
    `health_detail` carries the human-readable reason either way, for the agent to act on.
    """

    success: bool
    endpoint_url: str | None = None
    container_name: str | None = None
    error: str | None = None
    logs: str = ""
    healthy: bool = False
    health_detail: str = ""


class DockerArtifactDeployer:
    """Validate -> build -> run -> health-poll an artifact directory into a reachable container.

    Idempotent: `docker rm -f <name>` runs before `docker run`, so a crashed prior attempt never
    wedges the next deploy (the agent can redeploy freely while iterating). That force-remove targets
    `container_name` only, so several deployers of one artifact under DIFFERENT container names (the
    parallel-eval lanes) do not disturb each other.
    """

    # How long to wait for the container to become reachable before giving up (seconds).
    HEALTH_TIMEOUT_S = 60.0
    HEALTH_POLL_INTERVAL_S = 1.0

    def __init__(
        self,
        artifact_dir: str | Path,
        image_name: str,
        container_name: str,
        network: str | None,
        port: int,
        *,
        endpoint_path: str = "/respond",
        health_path: str = "/health",
        env: dict[str, str] | None = None,
        build: bool = True,
    ):
        self.artifact_dir = Path(artifact_dir)
        self.image_name = image_name
        self.container_name = container_name
        self.network = network
        self.port = port
        self.endpoint_path = endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
        self.health_path = health_path if health_path.startswith("/") else f"/{health_path}"
        self.env = dict(env or {})
        # Whether this deploy builds the image or runs one already built. Only the parallel-eval lane
        # pool passes False: it stands up W containers from ONE artifact directory, and building W
        # times would be W cache-warm rebuilds in the best case and, if they ever overlapped, two
        # `docker build -t <same tag>` racing so a container starts the loser's image. The validation
        # below still runs either way -- it is about the directory, not the build.
        self.build = build

    @property
    def endpoint_url(self) -> str:
        """Container-DNS completions URL the agent submits / the caller translates for eval."""
        return f"http://{self.container_name}:{self.port}{self.endpoint_path}"

    def deploy(self) -> DeploymentResult:
        """Run the invariant deploy sequence and return endpoint-or-reason."""
        # 1. validate: the directory and the two required files exist BEFORE any docker work, so a
        #    missing piece is reported precisely instead of surfacing as an opaque build error.
        if not self.artifact_dir.exists():
            return DeploymentResult(
                success=False,
                error=f"artifact directory not found: {self.artifact_dir}",
            )
        if not (self.artifact_dir / "Dockerfile").exists():
            return DeploymentResult(
                success=False,
                error=(
                    f"no Dockerfile in {self.artifact_dir} -- the artifact must ship a Dockerfile "
                    "whose CMD launches the server bound to 0.0.0.0:<port>."
                ),
            )
        if not any((self.artifact_dir / c).exists() for c in SERVER_ENTRY_CANDIDATES):
            return DeploymentResult(
                success=False,
                error=(
                    f"no server entry ({' / '.join(SERVER_ENTRY_CANDIDATES)}) in {self.artifact_dir} "
                    "-- the artifact must include the agentic endpoint server (GET /health, "
                    "POST /respond)."
                ),
            )

        # 2. build (skipped when the caller already built this tag -- see `self.build`)
        if self.build:
            build = subprocess.run(
                ["docker", "build", "-t", self.image_name, str(self.artifact_dir)],
                capture_output=True,
                text=True,
            )
            if build.returncode != 0:
                tail = (build.stderr or build.stdout or "")[-2000:]
                logger.error(
                    "docker build failed for artifact %s (image %s, dir %s), exit %d:\n%s",
                    self.container_name, self.image_name, self.artifact_dir, build.returncode, tail,
                )
                return DeploymentResult(success=False, error=f"docker build failed: {tail}")

        # 3. run (idempotent: clear any stale container of the same name first)
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True, text=True)
        run_cmd = ["docker", "run", "-d", "--name", self.container_name]
        if self.network:
            run_cmd += ["--network", self.network]
        # Publish the container port with NO host port, so the Docker daemon assigns a free ephemeral
        # host port when the container starts. The host can then health-poll it and eval can reach it,
        # both by reading the assignment back with `docker port` -- so the host port must be looked up
        # per deploy, never cached or assumed (Docker walks the ephemeral range and may reuse a number
        # a later deploy). Inside the container the port stays self.port, which the artifact is
        # expected to bind on 0.0.0.0 (see the $PORT injection below).
        run_cmd += ["-p", str(self.port)]
        for name, value in self.env.items():
            run_cmd += ["-e", f"{name}={value}"]
        # Tell the container which port to bind, via $PORT. The published port above is self.port, so
        # the server MUST listen on self.port; a server that honors $PORT then binds the right port
        # with no hardcoding. (A hardcoded default that differs from self.port -- e.g. a server bound
        # to 8000 while the contract publishes 8080 -- is the classic "never became reachable" failure:
        # docker's proxy accepts on the published port, finds nothing on the container port, and closes
        # the socket with no response.) Injected LAST so it stays authoritative even if a task's
        # agent_env also sets PORT.
        run_cmd += ["-e", f"PORT={self.port}"]
        run_cmd.append(self.image_name)
        run = subprocess.run(run_cmd, capture_output=True, text=True)
        if run.returncode != 0:
            detail = run.stderr.strip() or run.stdout.strip()
            logger.error(
                "docker run failed for artifact %s, exit %d: %s\n  argv: %s",
                self.container_name, run.returncode, detail,
                " ".join(_redact_env_values(run_cmd)),
            )
            return DeploymentResult(
                success=False,
                error=f"docker run failed: {detail}",
            )

        # 4. health: poll the host-published port until the server answers, or surface its logs.
        reachable, healthy, detail = self._wait_reachable()
        if not reachable:
            logs = self._container_logs()
            logger.error(
                "Artifact %s never became reachable on GET %s within %.0fs (%s). Container logs:\n%s",
                self.container_name, self.health_path, self.HEALTH_TIMEOUT_S, detail, logs,
            )
            return DeploymentResult(
                success=False,
                error=(
                    f"artifact container {self.container_name!r} never became reachable "
                    f"({detail}). Container logs:\n{logs}"
                ),
                logs=logs,
            )

        # 5. success -- the container answers. Whether it PASSED the health check is reported
        #    separately (see DeploymentResult): a reachable-but-not-200 artifact still deploys, but
        #    saying "healthy" about it would hide a defect the agent could still fix during BUILD.
        if healthy:
            logger.info("Deployed artifact %s at %s", self.container_name, self.endpoint_url)
        else:
            logger.warning(
                "Deployed artifact %s at %s, but it did NOT pass the health check: %s",
                self.container_name, self.endpoint_url, detail,
            )
        return DeploymentResult(
            success=True,
            endpoint_url=self.endpoint_url,
            container_name=self.container_name,
            healthy=healthy,
            health_detail=detail,
        )

    # -- internal seams (patched in unit tests) ---------------------------------------------------

    def _host_health_url(self) -> str | None:
        """Resolve the host URL for the container's published port via `docker port`.

        Returns None if the mapping can't be read yet (container still starting / gone)."""
        result = subprocess.run(
            ["docker", "port", self.container_name, str(self.port)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            # Normal for the first poll or two while the container starts, so DEBUG -- but carry
            # docker's own stderr, since a mapping that NEVER appears is the usual cause of a
            # "never became reachable" timeout and the reason is otherwise discarded here.
            logger.debug(
                "Could not resolve host port for %s:%s yet (exit %d): %s",
                self.container_name, self.port, result.returncode,
                result.stderr.strip() or "no output",
            )
            return None
        # Output looks like "0.0.0.0:49153" (possibly multiple lines for IPv4/IPv6). Take the first.
        mapping = result.stdout.strip().splitlines()[0]
        host_port = mapping.rsplit(":", 1)[-1]
        return f"http://localhost:{host_port}{self.health_path}"

    def _wait_reachable(self) -> tuple[bool, bool, str]:
        """Poll the container's host-published health endpoint until it answers or times out.

        Returns `(reachable, healthy, detail)`:
          - `reachable` -- the server answered at all. Any HTTP status counts, including 404/405
            (no health route) and 5xx: something is listening and routing, which is what the deploy
            gates on. Only a connection that never answers before the deadline is unreachable.
          - `healthy` -- the strictly stronger claim that `health_path` returned HTTP 200.
          - `detail` -- the reason, phrased for the agent to act on.

        Any answer ends the poll, whatever its status: the two cases are treated alike because both
        prove the server is up. (Previously a non-200 *success* status kept polling to the deadline
        and so failed the deploy, while a 4xx/5xx returned immediately and passed -- the same fact
        about the server producing opposite outcomes.)
        """
        deadline = time.time() + self.HEALTH_TIMEOUT_S
        started = time.time()
        last = "no response"
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            health_url = self._host_health_url()
            if health_url is None:
                last = "port not published yet"
            else:
                try:
                    req = urllib.request.Request(health_url, method="GET")
                    req.add_header("User-Agent", "appsci-bench-deployer")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        logger.debug(
                            "Health poll %d for %s: GET %s -> HTTP %s after %.1fs",
                            attempts, self.container_name, health_url, resp.status,
                            time.time() - started,
                        )
                        if resp.status == 200:
                            return True, True, "healthy"
                        return True, False, self._unhealthy_detail(resp.status)
                except urllib.error.HTTPError as e:
                    logger.debug(
                        "Health poll %d for %s: GET %s -> HTTP %s after %.1fs",
                        attempts, self.container_name, health_url, e.code, time.time() - started,
                    )
                    return True, False, self._unhealthy_detail(e.code)
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    last = str(getattr(e, "reason", e))
                    # Expected while the server boots, so DEBUG per attempt rather than a warning
                    # per second; the caller logs one ERROR if we never get an answer at all.
                    logger.debug(
                        "Health poll %d for %s: %s not answering yet (%s: %s)",
                        attempts, self.container_name, health_url, type(e).__name__, last,
                    )
            time.sleep(self.HEALTH_POLL_INTERVAL_S)
        logger.debug(
            "Health poll for %s gave up after %d attempts over %.1fs; last reason: %s",
            self.container_name, attempts, time.time() - started, last,
        )
        return False, False, last

    def _unhealthy_detail(self, status: int) -> str:
        """Phrase a reachable-but-not-200 health response as something the agent can act on."""
        return (
            f"server is up but GET {self.health_path} returned HTTP {status}, not 200 -- "
            f"the artifact contract requires {self.health_path} to answer 200"
        )

    def _container_logs(self, tail: int = 100) -> str:
        """Return the tail of the container's logs for a health-failure error message."""
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), self.container_name],
            capture_output=True,
            text=True,
        )
        return (result.stdout + result.stderr).strip()
