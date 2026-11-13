"""Applied AI Scientist agent that builds task-specific artifacts.

This is the main entry point for the benchmark. The agent receives a TaskContext
and produces an artifact that can be queried via the completions protocol.
"""
from __future__ import annotations

import logging
from typing import Any

from .core import TaskContext

from .core import MAX_CONSECUTIVE_UNUSABLE_TURNS, AgentConfig, AppSciAgent, model_access_brief
from .prompts import PromptBuilder
from .tools.workspace_layout import HANDOFF_DIRNAME, OPTIMIZE_DIRNAME

logger = logging.getLogger(__name__)


class AppliedScienceAgent:
    """A single applied-science agent that handles ALL tasks.

    This agent uses an LLM-powered meta-agent with three tools:
    - talk_to_user: Gather requirements from the appsci agent user sim (absent in the no-user arm,
      where the same documents are mounted in the container instead)
    - mini_swe_agent: Delegate coding/shell tasks to a subagent
    - complete: Signal that the artifact is built, deployed, and ready for evaluation

    The build() method orchestrates the full workflow:
    1. Initialize the agent with task context
    2. Run the agentic loop until the agent signals completion
    3. Deploy and return the artifact
    """

    def __init__(self, config: AgentConfig | None = None):
        """Initialize the Applied Science Agent.

        Args:
            config: Optional agent configuration. If not provided,
                   configuration is loaded from environment variables.
        """
        self.config = config or AgentConfig.from_env()
        self.last_result: dict[str, Any] | None = None

    def build(self, context: TaskContext) -> dict[str, Any]:
        """Entry point the benchmark calls. Runs the agent (author + deploy the artifact container,
        then submit its URL) and returns the raw run-result dict.

        Args:
            context: TaskContext with description, environment, and user_sim

        Returns:
            The agent run-result dict (status, turns_used, artifact_endpoint, ...). The deployed
            artifact itself is a separate container the evaluator queries over HTTP.
        """
        logger.info(f"Starting build for task: {context.description[:50]}...")

        # Build system prompt from task context
        prompt_builder = PromptBuilder()
        system_prompt = prompt_builder.build_system_prompt(
            task_brief=context.description,
            environment=context.environment,
            budgets=context.budgets,  # Fixed: was context.environment.get("budgets", {})
            business_goals=context.business_goals,
            dataset_files=context.dataset_files,
            technical_docs=context.technical_docs,
            interface_design=context.interface_design,
            artifacts=context.artifacts,
            # How the artifact reaches a model (variable names only, never values). Stated rather
            # than left to be discovered: the artifact is itself an agent, and the harness's own
            # source -- where this used to be readable -- is off limits to the coding sub-agent.
            model_access=model_access_brief(self.config),
            # MUST be passed. This is the path production uses (appsci_agent/run.py -> here), and
            # because it hands `AppSciAgent` a pre-built prompt, the registry-derived default inside
            # that class never applies. Omitting it left an ablation run advertising `eval_generator`
            # and `optimize_agent` in the prompt while the registry had already dropped them: the agent
            # called eval_generator twice, got "Unknown tool" twice, and adapted -- which is not the
            # pre-tools agent the ablation was supposed to measure.
            optimization_enabled=self.config.optimization_enabled,
            # MUST be passed, for exactly the reason spelled out above: this path hands `AppSciAgent`
            # a pre-built prompt, so nothing inside that class fills it in. Omitted, the template's
            # falsy default would render the NO-USER prompt -- "there is nobody to ask" -- over a
            # registry that still holds `talk_to_user`, on every ordinary run.
            user_sim_enabled=self.config.user_sim_enabled,
            requirements_dir=self.config.requirements_dir,
            # MUST be passed, same reason again: this path pre-builds the prompt. Omitted, the two
            # halves would be mounted and never named, and the agent would be told to read
            # "everything under /requirements" without being told that half of it is the decision
            # rules -- which is the case the policy mount exists to fix.
            requirements_docs_dir=self.config.requirements_docs_dir,
            requirements_policies_dir=self.config.requirements_policies_dir,
            # See core/agent.py's `_build_system_prompt` for why this states the limits generically
            # rather than per-failure-type.
            max_output_tokens=self.config.max_tokens,
            max_consecutive_unusable_turns=MAX_CONSECUTIVE_UNUSABLE_TURNS,
            # MUST be passed here too, and for the same reason as `optimization_enabled` above: this
            # is the path production takes, and it hands `AppSciAgent` a pre-built prompt, so nothing
            # inside that class fills this in. The prompt tells the agent which directory to write the
            # eval handoff to; omitted, the template's fallback covers it, but a wrong workspace on a
            # future layout would silently send the handoff where eval_generator does not look.
            workspace_dir=self.config.workspace_dir,
            handoff_dirname=HANDOFF_DIRNAME,
            optimize_dirname=OPTIMIZE_DIRNAME,
        )

        # Create and run the agent
        agent = AppSciAgent(
            config=self.config,
            system_prompt=system_prompt,
        )

        # Run the agent
        result = agent.run(
            task_description=context.description,
            environment=context.environment,
            budgets=context.budgets,  # Fixed: was context.environment.get("budgets", {})
        )

        logger.info(f"Agent completed with status: {result['status']}")
        logger.info(f"Turns used: {result['turns_used']}")

        # Expose the raw run result so the container entrypoint (appsci_agent/run.py) can tell whether
        # an artifact was actually submitted (and thus needs to be kept alive for the evaluator).
        self.last_result = result

        # Return the raw run result. The deployed artifact is a separate container the evaluator
        # reaches over HTTP via the submitted artifact_endpoint; the agent no longer wraps it in an
        # Artifact object. run.py keys off result["status"].
        return result


def build() -> AppliedScienceAgent:
    """`build(context)` factory for the AppSci agent (invoked by the containerized agent runner,
    appsci_agent/run.py)."""
    return AppliedScienceAgent()
