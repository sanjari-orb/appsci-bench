"""The database credentials the AppSci agent needs, and the one channel that actually delivers them.

Order-to-cash explores a real Postgres server on the shared Docker network, so it cannot do anything at
all without a DSN. The DSN is written in four places and only ONE of them reaches the agent:

  * `task_runner.agent_env`  -- injected into the agent's container as an env var. THE ONLY LIVE ROUTE.
  * the `oltp_db` resource's `location` -- stripped by spec redaction, which drops that field wholesale
    because it is where host paths live.
  * `RESOURCES.md` -- a repo document, bind-mounted into no container.
  * `docker-compose.yml` -- sets it on the WORKSPACE service, which the agent reached by shelling into
    that container until the Docker socket was removed and the network became the only route.

The entry these tests pin was missing, and the cost was measured rather than argued: on
`order_to_cash_l2_20260909_163729` the agent found the server reachable, found no credential anywhere in
its environment, concluded they arrive only at deploy time, and spent a large share of 28 turns
hand-writing a SCRAM client to brute-force common user/password pairs.

Nothing here needs Docker, a database or credentials of its own -- it reads the committed declarations.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
TASK = REPO / "tasks" / "order_to_cash"
DSN_VAR = "DATABASE_URL"


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load((TASK / "spec.yaml").read_text())


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((TASK / "environment" / "docker-compose.yml").read_text())


def _agent_env(spec: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in (spec["task_runner"].get("agent_env") or [])}


def test_the_spec_injects_a_dsn_into_the_agents_container(spec):
    """The whole fix. Without this the task is unplayable: no other route reaches the agent."""
    assert DSN_VAR in _agent_env(spec), (
        f"order_to_cash declares no {DSN_VAR} in task_runner.agent_env, so nothing hands the agent "
        "its database credentials -- the spec's `location`, RESOURCES.md and the compose file are all "
        "unreachable from the agent's container.")


def test_the_injected_dsn_matches_the_database_the_compose_file_actually_starts(spec, compose):
    """Two hand-written copies of one credential set, and nothing but this test keeps them equal.

    A mismatch has no startup symptom. It surfaces as an authentication failure inside the agent's own
    exploration, which the agent reads as a bug in its code rather than in the task's wiring -- which is
    precisely the failure mode that cost the run named in the module docstring its build phase.
    """
    dsn = _agent_env(spec)[DSN_VAR]
    db = compose["services"]["db"]["environment"]
    user, password, name = db["POSTGRES_USER"], db["POSTGRES_PASSWORD"], db["POSTGRES_DB"]
    assert dsn == f"postgres://{user}:{password}@db:5432/{name}", (
        f"the injected {DSN_VAR} ({dsn}) does not match the credentials the compose file gives the db "
        f"service (user={user}, db={name}); one of the two was changed without the other")


def test_the_injected_dsn_agrees_with_the_resource_location_the_agent_never_sees(spec):
    """The resource entry stays the human-readable record of the same fact, so it must not drift either.

    It is stripped from the agent's copy of the spec, which is what made this bug invisible: the DSN was
    right there in the file a reviewer reads and absent from the file the agent reads.
    """
    resources = {r["name"]: r for r in spec["environment"]["resources"]}
    assert _agent_env(spec)[DSN_VAR] == resources["oltp_db"]["location"], (
        "the oltp_db resource's location and the injected DSN disagree; they are the same fact written "
        "twice, one for reviewers and one for the agent")


def test_the_dsn_does_not_reach_the_agent_through_the_spec(spec):
    """The env var is the channel, and it has to be the only one.

    The agent's shell can read whatever is mounted, `/spec/spec.yaml` included, so a DSN surviving
    redaction would be a second source of truth that no longer has to agree with the injected one. This
    asserts the redaction still holds after adding the credential to the spec -- the block it lives in
    (`task_runner`) is dropped wholesale, which is what makes it safe to keep a password there at all.
    """
    from appsci_bench.core.spec import build_agent_spec

    visible = yaml.safe_dump(build_agent_spec(spec), sort_keys=False, allow_unicode=True)
    for secret in (DSN_VAR, "sandbox", "postgres://"):
        assert secret not in visible, f"{secret!r} survives redaction into the agent-facing spec"


def test_the_loader_hands_the_dsn_to_the_container_builder():
    """Declaring it is not the same as delivering it. Pins the one hop from spec to container env.

    The orchestrator turns this list into `docker run -e` arguments for the agent container and the
    host-side deployer forwards the same map to the artifact, so a task whose config drops the entry
    would look correctly declared and still start an agent with nothing in its environment.
    """
    from appsci_bench.task_runner.config import TaskConfig

    config = TaskConfig.from_spec("order_to_cash", REPO / "tasks")
    assert {e["name"]: e["value"] for e in config.agent_env}[DSN_VAR].startswith("postgres://")
