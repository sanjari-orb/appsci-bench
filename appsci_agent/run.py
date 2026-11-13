"""Entry point for running the appsci-agent in a Docker container.

This module is invoked when the agent container starts. It:
1. Reads configuration from environment variables
2. Loads the task context from the spec.yaml
3. Runs the AppliedScienceAgent to build the artifact
4. The agent will call complete which POSTs to the orchestrator

Environment Variables (set by orchestrator):
    APPSCI_USERSIM_URL: URL of the appsci agent user sim service. CONTROL ARM ONLY -- the no-user
        arm starts no user-sim, so neither this nor the token below is injected there.
    APPSCI_USERSIM_TOKEN: Bearer token for authenticating with the appsci agent user sim
        (control arm only, as above)
    AGENT_USER_SIM_ENABLED: falsy selects the no-user arm, which decides whether the two above
        are required
    TASK_WORKSPACE: Name of the task workspace container
    TASK_SPEC_PATH: Path to the task spec.yaml file
    ORCHESTRATOR_SUBMIT_URL: URL for artifact submission
    ANTHROPIC_API_KEY: API key for LLM (read by LiteLLM)
    APPSCI_AGENT_API_KEY: Optional explicit API key (overrides ANTHROPIC_API_KEY)
    APPSCI_AGENT_BASE_URL: Optional custom LLM endpoint
    AGENT_MODEL: Optional model override
    AGENT_MAX_TURNS: Optional max turns override
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys

import yaml

from appsci_agent.applied_science_agent import AppliedScienceAgent
from appsci_agent.core import AgentConfig, TaskContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    """Get a required environment variable or exit with error."""
    value = os.environ.get(name)
    if not value:
        logger.error(f"Required environment variable {name} is not set")
        sys.exit(1)
    return value


def load_task_context_from_spec(spec_path: str) -> TaskContext:
    """Load TaskContext from a task spec.yaml file.

    This reads the spec and extracts fields relevant to the agent,
    applying access control filtering.

    Args:
        spec_path: Path to the task spec.yaml file.

    Returns:
        TaskContext populated from the spec.
    """
    data = yaml.safe_load(pathlib.Path(spec_path).read_text()) or {}

    # Access control filtering: restricts which resources/files the agent can see.
    # The schema (schema/task_spec.schema.json) defines environment.access_control.denies
    # as a list of resource names/types to hide from the agent (user personas see everything).
    # No current task specs use this yet - the filtering is a no-op when denies is empty.
    # TODO: Finalize access control design - decide on allows vs denies semantics,
    #       whether to support wildcards, and add access_control to at least one task spec.
    denies = set(data.get("environment", {}).get("access_control", {}).get("denies", []))

    # Filter resources
    resources = data.get("environment", {}).get("resources", [])
    filtered_resources = [
        r for r in resources
        if r.get("name") not in denies and r.get("type") not in denies
    ]

    # Filter dataset_files
    dataset_files = list(data.get("dataset_files", []))
    filtered_dataset_files = [
        f for f in dataset_files
        if f.get("name") not in denies
    ]

    # Filter technical_docs
    technical_docs = list(data.get("technical_docs", []))
    filtered_technical_docs = [
        d for d in technical_docs
        if d.get("name") not in denies
    ]

    # Build environment dict with filtered resources
    environment = dict(data.get("environment", {}))
    environment["resources"] = filtered_resources

    return TaskContext(
        description=data.get("description", ""),
        business_goals=list(data.get("business_goals", [])),
        environment=environment,
        dataset_files=filtered_dataset_files,
        technical_docs=filtered_technical_docs,
        interface_design=data.get("interface_design"),
        artifacts=dict(data.get("artifacts", {})),
        budgets=dict(data.get("budgets", {})),
    )


def main() -> int:
    """Main entry point for the agent container.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    logger.info("Starting appsci-agent container")

    # Build agent config FIRST (reads APPSCI_AGENT_*, AGENT_MODEL, AGENT_MAX_TURNS,
    # AGENT_USER_SIM_ENABLED from env). It has to come before the environment checks below because
    # it owns the arm flag, and which variables are required depends on the arm. It reads nothing
    # but the environment, so nothing here depends on the spec being loaded first.
    config = AgentConfig.from_env()

    # Read required environment variables
    task_workspace = _require_env("TASK_WORKSPACE")
    spec_path = _require_env("TASK_SPEC_PATH")
    submit_url = os.environ.get("ORCHESTRATOR_SUBMIT_URL", "")

    # THE STAKEHOLDER'S ADDRESS IS REQUIRED ONLY IN THE CONTROL ARM. In the no-user arm there is no
    # user-sim process to address, so the container manager deliberately injects neither variable
    # (see `agent_container.py` -- injecting them empty would be worse, because `TalkToUserTool`
    # would then build a client pointed at nothing). Requiring them unconditionally exited this
    # container with code 1 before the agent started, and the orchestrator reported the whole run
    # `crashed`: the arm could not complete a single run.
    #
    # Still required in the control arm, and deliberately so. Neither value is read again in this
    # function -- `TalkToUserTool` reads the environment itself -- but a control run that reached
    # the agent with no stakeholder address would fail later, one turn at a time, as an agent that
    # could not elicit anything. That is a far more expensive way to learn the same fact.
    if config.user_sim_enabled:
        appsci_usersim_url = _require_env("APPSCI_USERSIM_URL")
        _require_env("APPSCI_USERSIM_TOKEN")
    else:
        appsci_usersim_url = ""

    logger.info(
        "Appsci agent user sim URL: "
        + (appsci_usersim_url or "(no-user arm -- no stakeholder to address)")
    )
    logger.info(f"Task workspace: {task_workspace}")
    logger.info(f"Spec path: {spec_path}")
    logger.info(f"Submit URL: {submit_url or '(not set)'}")

    # Load task context from spec
    try:
        context = load_task_context_from_spec(spec_path)
        logger.info(f"Loaded task context: {context.description[:100]}...")
    except Exception as e:
        logger.error(f"Failed to load task context: {e}")
        return 1

    # The artifact output location is a per-task contract declared in the spec
    # (artifacts.output_dir). Honor it so the agent writes its deliverable where
    # the evaluator's FileArtifact and the run's artifact snapshot both read from
    # -- otherwise the agent is told one path while eval/snapshot look at another.
    output_dir = context.artifacts.get("output_dir")
    if output_dir:
        config.artifact_dir = output_dir
        logger.info(f"Artifact output dir (from spec): {output_dir}")

    # Create and run the agent
    try:
        agent = AppliedScienceAgent(config=config)
        logger.info("Starting agent build...")

        # Build the artifact
        # The agent will eventually call complete which POSTs to callback
        result = agent.build(context)

        logger.info("Agent build completed")
        logger.info(f"Agent build result status: {(result or {}).get('status')}")

        # Keep the container alive after the build completes. If main() returns, PID 1 exits and the
        # orchestrator's submission wait sees the container die; blocking here instead lets the
        # orchestrator drive teardown on its own schedule (the artifact itself is a sibling container
        # deployed host-side, so it outlives this process either way). When running under the
        # orchestrator (ORCHESTRATOR_SUBMIT_URL set) and the build actually completed, block until
        # the orchestrator stops the container (SIGTERM).
        build_complete = (getattr(agent, "last_result", None) or {}).get("status") == "complete"
        if submit_url and build_complete:
            import signal
            logger.info("Build complete; keeping container alive for evaluation "
                        "(blocking until stopped)...")
            signal.pause()      # wait for SIGTERM/SIGINT from the orchestrator's container stop
        elif submit_url and not build_complete:
            logger.warning("Under orchestrator but the build never completed; exiting so the "
                           "orchestrator detects completion rather than hanging until timeout.")

        return 0

    except Exception as e:
        logger.exception(f"Agent build failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
