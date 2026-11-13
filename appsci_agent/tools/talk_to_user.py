"""Tool for the AppSci agent to communicate with the appsci agent user sim.

The appsci agent user sim represents a customer/stakeholder who can answer questions
about requirements, preferences, and domain knowledge.
"""
from __future__ import annotations

from typing import Any

from ..clients import AppsciAgentUserSimClient, AppsciAgentUserSimClientError
from .base import BaseTool, ToolContext, ToolResult, load_tool_config


class TalkToUserTool(BaseTool):
    """Send a message to the user to gather requirements or ask clarifying questions."""

    def __init__(self):
        self._config = load_tool_config("talk_to_user")

    @property
    def name(self) -> str:
        return self._config["name"]

    @property
    def description(self) -> str:
        return self._config["description"]

    @property
    def parameters(self) -> dict[str, Any]:
        return self._config["parameters"]

    def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the talk_to_user tool.

        Sends the question to the appsci agent user sim and returns their answer.
        The AppsciAgentUserSimClient reads APPSCI_USERSIM_URL and APPSCI_USERSIM_TOKEN from environment
        variables (injected by the orchestrator).
        """
        question = params.get("question", "")
        question_context = params.get("context", "")

        if not question:
            return ToolResult(
                success=False,
                output={},
                error="Question is required",
                # Rejected before the user-sim was contacted, so nothing ran. Same contract as the
                # coding sub-agent's argument rejection: the loop counts these toward its stuck-run
                # breaker, and work that ran and failed toward neither.
                invalid_call=True,
            )

        # Build the full question with context if provided
        full_question = question
        if question_context:
            full_question = f"{question}\n\nContext: {question_context}"

        try:
            # Create client - reads APPSCI_USERSIM_URL and APPSCI_USERSIM_TOKEN from env
            with AppsciAgentUserSimClient() as client:
                # Ask the question (service manages its own conversation state). Forward this tool
                # call's observability span so the user-sim exchange nests under this turn in the trace.
                answer = client.ask(full_question, parent_span_id=context.parent_span_id)

            # Update agent's conversation history (for agent's own reasoning)
            context.conversation_history.append({
                "role": "agent",
                "content": question,
            })
            context.conversation_history.append({
                "role": "user_sim",
                "content": answer,
            })

            return ToolResult(
                success=True,
                output={"answer": answer, "question_asked": question},
            )
        except AppsciAgentUserSimClientError as e:
            # Covers both "never reachable" and "answered with an error": the client now carries the
            # service's own reason in this exception, so report it verbatim rather than asserting
            # which of the two it was.
            return ToolResult(
                success=False,
                output={"question_asked": question},
                error=f"Could not get an answer from the stakeholder: {e}",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output={"question_asked": question},
                error=f"Error communicating with user: {e}",
            )
