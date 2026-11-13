#!/usr/bin/env python
"""Build + run the CURRENT artifact/ against the live task-env, for iterative dev -- no scoring.

This is the missing step between "task-env is up" (eval_artifact.py up) and "score my submission"
(checkpoint.py / score_final.py / eval_artifact.py eval): while coding, the human needs to run their
own artifact/Dockerfile + server.py against the real task resources (the DB, the tool API, ...) the
same way the deployed artifact does during a real agent run or a real eval -- reachable at
db:5432, airline-workspace-1:8080, etc. via bench-network, not from the host.

Reuses appsci_bench.orchestrator.deployer.DockerArtifactDeployer -- the SAME build->run->health-poll
sequence eval_artifact.py and the live BenchmarkOrchestrator use to deploy an artifact. Nothing here
is reimplemented: this script only supplies its arguments (artifact dir, task's declared container
name/port/network/agent_env) and skips straight to printing a reachable URL instead of scoring.

Requires a running task-env (bring one up first):
    python -m appsci_bench.orchestrator.eval_artifact up --phase build --task <task_id>

Usage (run from the appsci-bench repo root):
    python dev_deploy.py --task order_to_cash --artifact-dir ~/Documents/baseline-work/otc-01/artifact
    python dev_deploy.py --task order_to_cash --artifact-dir ... --stop   # tear down the dev container

Redeploy after every edit -- it's idempotent (docker rm -f runs before docker run), so re-running
this command is the normal edit/test loop, not a special "reset" step.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from appsci_bench.orchestrator.deployer import DockerArtifactDeployer, llm_credential_env  # noqa: E402
from appsci_bench.orchestrator.run import _translate_docker_url_to_host  # noqa: E402
from appsci_bench.task_runner.config import TaskConfig  # noqa: E402

# Suffix keeps the dev deploy's container name distinct from the real artifact_container name
# eval_artifact.py / the orchestrator use, so a dev-loop deploy never collides with (or gets torn
# down by) an actual scoring run of the same task.
_DEV_SUFFIX = "-dev"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", required=True, help="Task id under tasks/ (e.g. order_to_cash)")
    parser.add_argument("--artifact-dir", required=True, type=pathlib.Path,
                         help="Path to artifact/ (Dockerfile + server.py) to build and run")
    parser.add_argument("--tasks-dir", default=REPO_ROOT / "tasks", type=pathlib.Path)
    parser.add_argument("--stop", action="store_true", help="Stop and remove the dev container, then exit")
    args = parser.parse_args()

    config = TaskConfig.from_spec(args.task, args.tasks_dir.resolve())
    container_name = f"{config.artifact_container}{_DEV_SUFFIX}"

    if args.stop:
        import subprocess
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)
        print(f"Stopped {container_name}")
        return 0

    artifact_dir = args.artifact_dir.expanduser().resolve()
    if not artifact_dir.exists():
        print(f"ERROR: artifact dir not found: {artifact_dir}", file=sys.stderr)
        return 1

    agent_env = {e["name"]: e["value"] for e in config.agent_env}
    deploy_env = {**llm_credential_env(), **agent_env}

    deployer = DockerArtifactDeployer(
        artifact_dir=artifact_dir,
        image_name=f"{container_name}:latest",
        container_name=container_name,
        network=config.network_name,
        port=config.artifact_port,
        env=deploy_env,
    )
    print(f"Building + running {artifact_dir} as {container_name} on {config.network_name} ...")
    result = deployer.deploy()
    if not result.success:
        print(f"DEPLOY FAILED: {result.error}", file=sys.stderr)
        return 1

    host_url = _translate_docker_url_to_host(result.endpoint_url)
    print(f"\nUp at: {host_url}")
    print(f"Health: {'OK' if result.healthy else 'NOT HEALTHY -- ' + result.health_detail}")
    print(f"\nTry it:  curl -s -X POST {host_url} -H 'content-type: application/json' \\")
    print("           -d '{\"messages\": [{\"role\": \"user\", \"content\": \"hello\"}]}'")
    print(f"\nRe-run this command after each edit to rebuild + redeploy.")
    print(f"When done for now: python dev_deploy.py --task {args.task} --artifact-dir {artifact_dir} --stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
