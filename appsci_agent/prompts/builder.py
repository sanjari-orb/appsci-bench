"""Prompt builder for the Applied AI Scientist agent.

Renders Jinja2 templates with task-specific context to generate
the system prompt for the agent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

# Default template directory (same directory as this module)
TEMPLATES_DIR = Path(__file__).parent


def _format_number(value: int | float) -> str:
    """Format a number with thousands separators."""
    return f"{int(value):,}"


class PromptBuilder:
    """Builds system prompts from Jinja2 templates."""

    def __init__(self, templates_dir: str | Path | None = None):
        """Initialize the prompt builder.

        Args:
            templates_dir: Directory containing Jinja2 templates.
                          Defaults to the 'prompts' directory in this package.
        """
        self.templates_dir = Path(templates_dir) if templates_dir else TEMPLATES_DIR

        self.env = Environment(
            loader=FileSystemLoader(self.templates_dir),
            autoescape=select_autoescape(),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Add custom filters
        self.env.filters["format_number"] = _format_number

    def render(
        self,
        template_name: str,
        **context: Any,
    ) -> str:
        """Render a template with the given context.

        Args:
            template_name: Name of the template file (e.g., 'system_prompt.jinja2')
            **context: Variables to pass to the template

        Returns:
            The rendered template string
        """
        template = self.env.get_template(template_name)
        return template.render(**context)

    def build_system_prompt(
        self,
        task_brief: str,
        environment: dict | None = None,
        budgets: dict | None = None,
        business_goals: list[str] | None = None,
        dataset_files: list[dict] | None = None,
        technical_docs: list[dict] | None = None,
        interface_design: str | None = None,
        artifacts: dict | None = None,
        optimization_enabled: bool = True,
        user_sim_enabled: bool = True,
        requirements_dir: str | None = None,
        requirements_docs_dir: str | None = None,
        requirements_policies_dir: str | None = None,
        **extra_context: Any,
    ) -> str:
        """Build the system prompt for the AppSci agent.

        Args:
            task_brief: The task description/brief
            environment: Environment resources (databases, files, etc.)
            budgets: Budget constraints (turns, tokens, cost, time)
            business_goals: High-level objectives for the task
            dataset_files: Available data files for the agent
            technical_docs: Technical documentation available
            interface_design: Expected artifact interface description
            artifacts: Expected artifacts and output directory
            optimization_enabled: Whether the self-evaluation tools (`eval_generator`,
                `optimize_agent`) are available this run. False renders a prompt that never mentions
                them -- the pre-#126 tool surface, for ablations. Defaults to True and is a NAMED
                parameter rather than left to `extra_context`, because an unset Jinja variable is
                falsy: a caller that forgot it would silently get the ablation prompt while the tools
                were still registered, and the agent would be told it has four tools while holding six.
            user_sim_enabled: Whether `talk_to_user` is available this run. False is the NO-USER
                ablation arm: the prompt never mentions a stakeholder and points at
                `requirements_dir` instead. NAMED for the same reason as `optimization_enabled` --
                an unset Jinja variable is falsy, so a forgotten keyword would silently render the
                ablation prompt over a registry that still holds the tool.
            requirements_docs_dir: Container path of the DOCUMENTS half (the task's user_sim_docs/
                tree), when it was mounted. Named separately from the parent so the prompt can say
                what kind of material sits there.
            requirements_policies_dir: Container path of the POLICY half (the personas'
                domain_knowledge extracts), when it was mounted. Either half may be absent.
            requirements_dir: Container path the task's requirement documents are mounted at, when
                they are. Rendered only when `user_sim_enabled` is False; the orchestrator passes
                the exact path it bound, so the prompt cannot name a directory that does not exist.
            **extra_context: Additional context variables

        Returns:
            The rendered system prompt
        """
        return self.render(
            "system_prompt.jinja2",
            task_brief=task_brief,
            environment=environment or {},
            budgets=budgets or {},
            business_goals=business_goals or [],
            dataset_files=dataset_files or [],
            technical_docs=technical_docs or [],
            interface_design=interface_design,
            artifacts=artifacts or {},
            optimization_enabled=optimization_enabled,
            user_sim_enabled=user_sim_enabled,
            requirements_dir=requirements_dir,
            requirements_docs_dir=requirements_docs_dir,
            requirements_policies_dir=requirements_policies_dir,
            **extra_context,
        )

    @classmethod
    def from_task_spec(cls, task_spec: Any) -> str:
        """Build a system prompt from a TaskSpec object.

        Args:
            task_spec: A TaskSpec object from appsci_bench.core.spec

        Returns:
            The rendered system prompt
        """
        builder = cls()
        return builder.build_system_prompt(
            task_brief=task_spec.description,
            environment=task_spec.environment,
            budgets=task_spec.budgets,
        )


# Convenience function for quick prompt building
def build_system_prompt(
    task_brief: str,
    environment: dict | None = None,
    budgets: dict | None = None,
    business_goals: list[str] | None = None,
    dataset_files: list[dict] | None = None,
    technical_docs: list[dict] | None = None,
    interface_design: str | None = None,
    artifacts: dict | None = None,
    optimization_enabled: bool = True,
    user_sim_enabled: bool = True,
    requirements_dir: str | None = None,
    requirements_docs_dir: str | None = None,
    requirements_policies_dir: str | None = None,
    **extra_context: Any,
) -> str:
    """Build the system prompt for the AppSci agent.

    This is a convenience function that creates a PromptBuilder
    and renders the system prompt in one call.

    Args:
        task_brief: The task description/brief
        environment: Environment resources (databases, files, etc.)
        budgets: Budget constraints (turns, tokens, cost, time)
        business_goals: High-level objectives for the task
        dataset_files: Available data files for the agent
        technical_docs: Technical documentation available
        interface_design: Expected artifact interface description
        artifacts: Expected artifacts and output directory
        optimization_enabled: Whether the self-evaluation tools are available (see the method of the
            same name on PromptBuilder).
        user_sim_enabled: Whether `talk_to_user` is available (see the method of the same name).
        requirements_dir: Where the requirement documents are mounted (see the method of the same
            name).
        requirements_docs_dir: Where the documents half is mounted (see the method of the same name).
        requirements_policies_dir: Where the policy half is mounted (see the method of the same
            name).
        **extra_context: Additional context variables

    Returns:
        The rendered system prompt
    """
    builder = PromptBuilder()
    return builder.build_system_prompt(
        task_brief=task_brief,
        environment=environment,
        budgets=budgets,
        business_goals=business_goals,
        dataset_files=dataset_files,
        technical_docs=technical_docs,
        interface_design=interface_design,
        artifacts=artifacts,
        optimization_enabled=optimization_enabled,
        user_sim_enabled=user_sim_enabled,
        requirements_dir=requirements_dir,
        requirements_docs_dir=requirements_docs_dir,
        requirements_policies_dir=requirements_policies_dir,
        **extra_context,
    )
