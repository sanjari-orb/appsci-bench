"""Configuration dataclasses for the Task Runner.

This module defines TaskConfig which parses task configuration from spec.yaml.
The TaskRunner uses TaskConfig to understand how to set up a task environment.

A task declares its environment once per RUN PHASE (`task_runner.environments.build` /
`.eval`). The build env serves the build phase and is torn down when the agent submits; the eval env
is started for the eval phase only. Both come up from the same compose file under the same
workspace_name, so they share a container name and network alias and the artifact needs no
re-pointing across the handover. See docs/task_env_split.md.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# Phase names, in the order a run visits them. The orchestrator starts BUILD before the agent and
# EVAL after it submits; nothing else is a valid phase.
PHASE_BUILD = "build"
PHASE_EVAL = "eval"
PHASES = (PHASE_BUILD, PHASE_EVAL)

# The literal a task writes for `environments.eval` when its eval phase needs no environment at all
# (slides_generator: its eval inputs travel as attachments on the case). Spelled out rather than
# omitted, so a missing block stays distinguishable from an author who forgot.
NO_ENV = "none"

# Placeholders a task may write into a `task_runner.agent_env` VALUE. They exist because the eval
# phase can run several copies of an environment at once (see appsci_bench/eval/lanes.py), and the two
# facts that differ between copies are exactly these: which container the tools are on, and which
# token opens them. A spec that spells either out literally pins every copy to the first one.
AGENT_ENV_PLACEHOLDERS = ("workspace_container", "tool_token")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class TaskEnvPhase:
    """One phase's environment: what data goes into the workspace container, and how it is made live.

    Attributes:
        bundle: Source-to-destination mappings, packaged into a tar.gz, copied into the container and
            extracted. Each phase points at its own data directory so the other phase's records never
            enter this container.
        setup_script: Container path to the bash script run after extraction, which makes the
            DB/KB/API live for this phase. End it with assertions that the data actually landed --
            record counts above zero for each collection you expect. A service can answer its health
            check before holding any records (airline's starts empty by design), so "the container is
            up" is not evidence that the load worked, and a load that half-applied otherwise surfaces
            only as a run of zeros with nothing to explain it. A failing setup script aborts the run,
            which is the right outcome: there is nothing worth measuring against missing data.
    """

    bundle: list[dict]
    setup_script: Path

    @classmethod
    def from_dict(cls, data: dict, *, task_id: str, phase: str) -> "TaskEnvPhase":
        missing = [k for k in ("bundle", "setup_script") if k not in data]
        if missing:
            raise KeyError(
                f"{task_id}: task_runner.environments.{phase} is missing {missing}"
            )
        return cls(
            bundle=data["bundle"],
            setup_script=Path(data["setup_script"]),
        )


@dataclass
class TaskConfig:
    """Configuration parsed from spec.yaml for TaskRunner.

    This dataclass holds all the configuration needed to spin up a task
    environment. It combines fields from the 'environment' section (existing)
    and the 'task_runner' section (new) of spec.yaml.

    Attributes:
        task_id: Unique identifier for the task (e.g., "order_to_cash").

        # From environment section
        docker_compose: Path to docker-compose.yml for starting containers. The image is built
            from this file's own build context, so the Dockerfile path lives there, not here.

        # From task_runner section
        network_name: Docker network name for container communication.
            Both task containers and appsci-agent must be on this network.
        workspace_name: Container name suffix for the workspace container.
            Used to identify the container for docker exec commands.
        environments: One TaskEnvPhase per run phase, keyed "build" / "eval". The value is None for a
            phase the task declares as needing no environment. Read it through phase().

    Example spec.yaml structure:
        environment:
          docker_compose: tasks/order_to_cash/environment/docker-compose.yml

        task_runner:
          network_name: bench-network
          workspace_name: o2c-workspace
          environments:
            build:
              bundle:
                - { src: environment/build/data/, dest: /fixture/ }
              setup_script: /scripts/setup.sh
            eval:
              bundle:
                - { src: environment/eval/data/, dest: /fixture/ }
              setup_script: /scripts/setup.sh
    """

    task_id: str

    # From environment section
    docker_compose: Path

    # From task_runner section
    network_name: str
    workspace_name: str
    environments: dict[str, TaskEnvPhase | None]
    # Optional env vars to inject into the appsci-agent container (and thus the
    # artifact it deploys). Each entry is {"name": ..., "value": ...}. Used for
    # task-env connection info the agent can't read from the static spec (e.g.
    # airline's AIRLINE_TOOLS_URL / AIRLINE_TOOLS_TOKEN). Empty for most tasks.
    agent_env: list[dict] = field(default_factory=list)
    # Optional: the NAME of the env var (set on the workspace container) that holds the task-env's
    # evaluator-only ADMIN token. When a task sets `evaluation_data.reset_env_per_sample`, the
    # orchestrator recovers this token (via `docker inspect`) to drive per-case /admin/reset +
    # /admin/state_hash. Task-agnostic: airline sets "AIRLINE_ENV_ADMIN_TOKEN"; other tasks name
    # their own. None (default) => the orchestrator can't measure a DB component for that task.
    env_admin_token_var: str | None = None
    # Optional: the NAME of the env var (set on the workspace container) holding the task-env's TOOL
    # token. Declared only by tasks whose eval-time user simulator must call CUSTOMER-held tools
    # against the environment -- a domain where the deciding action is the customer's own (submitting
    # their application, say), so a sim that can only talk could never reach the golden state. The
    # evaluator recovers it the same way as the admin token (`docker inspect`). Unlike the admin token
    # this one is also handed to the agent via agent_env, so naming it grants the evaluator nothing
    # the artifact does not already hold. None (default) => the sim only talks.
    env_tool_token_var: str | None = None
    # Optional: the NAME of the env var the task's compose file reads (`${VAR}`) to set the workspace
    # container's TOOL token. Distinct from `env_tool_token_var`, which says where the evaluator can
    # READ that token; this one says where it can WRITE it. Declaring it is what lets parallel eval
    # give each copy of the environment its own token, so an artifact that hardcoded the token instead
    # of reading `agent_env` gets a 401 rather than silently transacting against copy 0's store.
    # None (default) => the task can only run one environment at a time.
    lane_token_var: str | None = None
    # Docker container name under which the artifact is deployed (host-side deploy service +
    # eval-only pipeline). Declaring it per-task makes teardown deterministic (target it by name)
    # rather than recovering it from the submitted URL. Defaults to "appsci-artifact-<task_id>".
    artifact_container: str = ""
    # Container port the artifact server binds (0.0.0.0:<port>) and the deploy service publishes to
    # the host for health checks / eval. Defaults to 8080.
    artifact_port: int = 8080

    def __post_init__(self) -> None:
        # Default the artifact container name off the task id when the spec doesn't declare one, so
        # every existing task keeps working without a spec edit while the name stays deterministic.
        if not self.artifact_container:
            self.artifact_container = f"appsci-artifact-{self.task_id}"

    @property
    def default_workspace_container(self) -> str:
        """The workspace container name for the task's ONE (lane 0) environment.

        Mirrors the name TaskRunner derives, for callers that have to resolve `agent_env` without a
        running environment dict in hand (a task whose eval phase declares `eval: none`).
        """
        base = (self.workspace_name.rsplit("-", 1)[0]
                if "-" in self.workspace_name else self.workspace_name)
        return f"{base}-workspace-1"

    def resolved_agent_env(
        self, *, workspace_container: str, tool_token: str | None = None,
    ) -> dict[str, str]:
        """`agent_env` as a name->value dict, with the per-environment placeholders filled in.

        The one place `task_runner.agent_env` is turned into real values, so the build-phase agent
        container and every eval-phase artifact container resolve it identically. Substituting
        `{workspace_container}` with the name the environment ACTUALLY came up under is what makes a
        second copy of that environment addressable at all -- a spec that writes the container name
        literally hands copy 1's artifact copy 0's address, and the two then transact on one store with
        nothing reporting it.

        Args:
            workspace_container: The workspace container this consumer should talk to.
            tool_token: The tool token for that container, required only if a value uses
                `{tool_token}`.

        Raises:
            KeyError: A value names a placeholder that is not in `AGENT_ENV_PLACEHOLDERS`. Refused
                rather than passed through, because an unsubstituted `{...}` reaches the artifact as a
                literal and fails as an unexplained connection error much later.
            ValueError: A value uses `{tool_token}` but none was supplied.
        """
        values = {"workspace_container": workspace_container, "tool_token": tool_token}

        def substitute(match: re.Match) -> str:
            key = match.group(1)
            if key not in AGENT_ENV_PLACEHOLDERS:
                raise KeyError(
                    f"{self.task_id}: task_runner.agent_env uses unknown placeholder {{{key}}}; "
                    f"supported placeholders are {AGENT_ENV_PLACEHOLDERS}"
                )
            if values[key] is None:
                raise ValueError(
                    f"{self.task_id}: task_runner.agent_env uses {{{key}}} but no {key} was supplied"
                )
            return values[key]

        return {
            entry["name"]: _PLACEHOLDER_RE.sub(substitute, str(entry["value"]))
            for entry in self.agent_env
        }

    def parallel_env_blockers(self) -> list[str]:
        """Why this task cannot run several copies of its environment at once, or [] if it can.

        Returned as reasons rather than a bool because the caller has to print them: a run asked for
        four copies and given one would score every case against one store, which is a plausible
        number and a wrong one. Refusing with the spec key to fix is the only safe answer.
        """
        blockers: list[str] = []
        env_text = " ".join(str(e.get("value", "")) for e in self.agent_env)
        if self.agent_env and "{workspace_container}" not in env_text:
            blockers.append(
                f"{self.task_id}: task_runner.agent_env pins the environment address literally. "
                "Write the container name as {workspace_container} so each copy is addressed by its "
                "own container."
            )
        if "{tool_token}" in env_text and not self.lane_token_var:
            blockers.append(
                f"{self.task_id}: task_runner.agent_env uses {{tool_token}} but "
                "task_runner.lane_token_var is not set, so there is nowhere to write a per-copy token."
            )
        return blockers

    def phase(self, phase: str) -> TaskEnvPhase | None:
        """This task's environment for ``phase``, or None when it declares it needs none.

        Args:
            phase: "build" or "eval".

        Raises:
            ValueError: For any other phase name -- a typo must not silently read through as "this
                phase has no environment", which would skip the data load and leave an empty store
                that only shows up as a run full of zeros.
        """
        if phase not in PHASES:
            raise ValueError(f"unknown run phase {phase!r}; expected one of {PHASES}")
        return self.environments.get(phase)

    @staticmethod
    def _parse_environments(runner: dict, *, task_id: str) -> dict[str, TaskEnvPhase | None]:
        """Parse ``task_runner.environments`` into one TaskEnvPhase per phase (None = no environment).

        Both phases must be declared. A task whose eval phase needs no environment writes the literal
        "none" rather than omitting the key, so an author who forgot is distinguishable from one who
        decided (schema: task_runner.environments.eval).
        """
        declared = runner.get("environments")
        if not isinstance(declared, dict):
            raise KeyError(
                f"{task_id}: task_runner.environments is missing. The task environment is now "
                f"declared once per run phase -- see docs/task_env_split.md for the shape."
            )
        out: dict[str, TaskEnvPhase | None] = {}
        for phase in PHASES:
            if phase not in declared:
                raise KeyError(
                    f"{task_id}: task_runner.environments.{phase} is missing. Declare it, or write "
                    f"{phase}: {NO_ENV} if this phase needs no environment."
                )
            value = declared[phase]
            if value == NO_ENV or value is None:
                out[phase] = None
                continue
            out[phase] = TaskEnvPhase.from_dict(value, task_id=task_id, phase=phase)
        return out

    @classmethod
    def from_spec(cls, task_id: str, tasks_dir: Path) -> "TaskConfig":
        """Parse spec.yaml and return a TaskConfig instance.

        This method reads the task's spec.yaml file and extracts configuration
        from both the 'environment' and 'task_runner' sections.

        Args:
            task_id: The task identifier (e.g., "order_to_cash").
                Used to locate the task directory at tasks_dir/task_id/.
            tasks_dir: Path to the tasks/ directory containing all task folders.

        Returns:
            A TaskConfig instance populated with values from spec.yaml.

        Raises:
            FileNotFoundError: If spec.yaml does not exist for the given task_id.
            KeyError: If required fields are missing from spec.yaml.
            yaml.YAMLError: If spec.yaml contains invalid YAML.

        Example:
            config = TaskConfig.from_spec("order_to_cash", Path("tasks"))
            print(config.network_name)  # "bench-network"
            print(config.bundle)  # [{"src": "seed_data/fixture/", "dest": "/fixture/"}, ...]
        """
        spec_path = tasks_dir / task_id / "spec.yaml"

        if not spec_path.exists():
            raise FileNotFoundError(f"spec.yaml not found at {spec_path}")

        with open(spec_path) as f:
            spec = yaml.safe_load(f)

        env = spec["environment"]
        runner = spec["task_runner"]

        return cls(
            task_id=task_id,
            docker_compose=Path(env["docker_compose"]),
            network_name=runner["network_name"],
            workspace_name=runner["workspace_name"],
            environments=cls._parse_environments(runner, task_id=task_id),
            agent_env=runner.get("agent_env", []),
            env_admin_token_var=runner.get("env_admin_token_var"),
            env_tool_token_var=runner.get("env_tool_token_var"),
            lane_token_var=runner.get("lane_token_var"),
            artifact_container=runner.get("artifact_container", ""),
            artifact_port=runner.get("artifact_port", 8080),
        )
