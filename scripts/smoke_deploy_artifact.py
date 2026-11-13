#!/usr/bin/env python3
"""Creds-free deploy smoke for the shared artifact deploy capability.

Runnable only where a Docker daemon is available -- it really builds + runs a container. It exercises
the infra the unit tests mock out: DockerArtifactDeployer (validate -> build -> run -> health) and
_translate_docker_url_to_host, against an artifact directory YOU point it at (required argument). The
self-contained echo fixture in tests/fixtures/echo_artifact/ (pure stdlib; no task creds, no task-env,
no evaluator spec) is the creds-free thing to smoke first:

    PYTHONPATH=. python scripts/smoke_deploy_artifact.py tests/fixtures/echo_artifact

Any conformant artifact dir works too (a Dockerfile + a server entry exposing GET /health and
POST /respond, binding 0.0.0.0:<port>):

    PYTHONPATH=. python scripts/smoke_deploy_artifact.py runs/<trace>/artifact --port 8080

This is only the deploy half of the plan's creds-free E2E; the full
`eval_artifact eval --task <task> --artifact-dir <dir>` flow additionally needs a task's real env +
eval config and is exercised per-task with the real tasks.

Prints PASS/FAIL, exits non-zero on failure, and always removes the container + throwaway network.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from appsci_bench.orchestrator.deployer import DockerArtifactDeployer
from appsci_bench.orchestrator.run import (
    _cleanup_artifact_containers,
    _translate_docker_url_to_host,
)


def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _request(url: str, data: bytes | None = None):
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="smoke_deploy_artifact",
        description="Build + run an artifact dir with the real DockerArtifactDeployer and smoke it.",
    )
    p.add_argument(
        "artifact_dir",
        help="Artifact directory to deploy (Dockerfile + server entry). Use "
        "tests/fixtures/echo_artifact for the creds-free default smoke.",
    )
    p.add_argument("--port", type=int, default=8080,
                   help="Container port the server binds to (default: 8080)")
    p.add_argument("--container", default="appsci-smoke-artifact",
                   help="Container name to run under (default: appsci-smoke-artifact)")
    p.add_argument("--network", default="appsci-smoke-net",
                   help="Throwaway docker network to create + use (default: appsci-smoke-net)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    artifact_dir = Path(args.artifact_dir).resolve()
    if not artifact_dir.exists():
        print(f"[smoke] FAIL: artifact directory not found: {artifact_dir}")
        return 1

    if not _docker_available():
        print("[smoke] SKIP: no reachable Docker daemon (this smoke really builds + runs a container).")
        return 0

    # A throwaway network so the container-DNS name resolves and translation has a port to map.
    subprocess.run(["docker", "network", "create", args.network], capture_output=True, text=True)
    deployer = DockerArtifactDeployer(
        artifact_dir=artifact_dir,
        image_name=f"{args.container}:latest",
        container_name=args.container,
        network=args.network,
        port=args.port,
    )
    try:
        print(f"[smoke] deploying {artifact_dir} as {args.container}:{args.port} on {args.network} ...")
        result = deployer.deploy()
        if not result.success:
            print(f"[smoke] FAIL: deploy error: {result.error}")
            return 1

        # container-DNS URL -> localhost:<published port> (same translation eval uses).
        host = _translate_docker_url_to_host(result.endpoint_url)
        base = host.rsplit("/", 1)[0]
        print(f"[smoke] deployed: container-DNS={result.endpoint_url}  host={host}")

        # /health is the contract the deployer itself gates on -- it must answer 200.
        try:
            hcode, hbody = _request(base + "/health")
        except urllib.error.URLError as e:
            print(f"[smoke] FAIL: /health unreachable: {e}")
            return 1
        if hcode != 200:
            print(f"[smoke] FAIL: /health returned {hcode} {hbody}")
            return 1
        print(f"[smoke] /health ok: {hcode} {hbody}")

        # /respond: the request/response schema is task-specific (the evaluator derives it from the
        # task's own eval config), so a generic smoke only asserts the round-trip reaches the server
        # and it answers -- any HTTP status means the server is up. We POST a generic payload; the echo
        # fixture returns {"response": ...}, real artifacts vary.
        try:
            rcode, rbody = _request(
                host, data=json.dumps({"messages": [{"role": "user", "content": "ping"}],
                                       "text": "ping"}).encode()
            )
            print(f"[smoke] /respond reachable: {rcode} {rbody}")
        except urllib.error.HTTPError as e:
            print(f"[smoke] /respond reachable: HTTP {e.code} (server responded)")
        except urllib.error.URLError as e:
            print(f"[smoke] FAIL: /respond unreachable: {e}")
            return 1

        print("[smoke] PASS")
        return 0
    finally:
        # Teardown always runs: remove the artifact container by name, then the throwaway network.
        _cleanup_artifact_containers(args.container, None, None)
        subprocess.run(["docker", "network", "rm", args.network], capture_output=True, text=True)


if __name__ == "__main__":
    sys.exit(main())
