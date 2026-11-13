"""Base tool class and shared types for AppSci agent tools."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def load_tool_config(tool_name: str) -> dict:
    """Load tool configuration from YAML file.

    Args:
        tool_name: Name of the tool (without .yaml extension)

    Returns:
        Dictionary containing the tool configuration

    Raises:
        FileNotFoundError: If the config file doesn't exist
    """
    config_dir = Path(__file__).parent / "tool_configs"
    config_file = config_dir / f"{tool_name}.yaml"

    if not config_file.exists():
        raise FileNotFoundError(f"Tool config not found: {config_file}")

    with open(config_file) as f:
        return yaml.safe_load(f)


@dataclass
class ToolContext:
    """Context passed to tools during execution.

    Contains references to shared resources like the workspace directory,
    conversation history, and configuration.

    Note: The appsci agent user sim is accessed via AppsciAgentUserSimClient which reads
    APPSCI_USERSIM_URL and APPSCI_USERSIM_TOKEN from environment variables (injected
    by the orchestrator).
    """
    workspace_dir: str = "/workspace"
    artifact_dir: str = "/workspace/artifact"
    environment: dict = field(default_factory=dict)
    conversation_history: list[dict] = field(default_factory=list)  # Agent's memory
    config: Any = None  # AgentConfig, but avoid circular import

    # Artifact state
    artifact_endpoint: str | None = None
    # Incremented by `deploy_artifact` on each successful deploy, so a persisted eval report can
    # be tagged with which version of the artifact it measured.
    artifact_idx: int = 0
    # Set by the `complete` tool once the BUILD phase has been ended (guards a second call).
    build_complete: bool = False
    # Set by the `eval_generator` tool once it has written a verified eval/ harness.
    eval_ready: bool = False
    # Ceiling on how many optimization cycles `optimize_agent` may spend, or None for "no ceiling --
    # keep going until the scores saturate". Resolved by the agent loop from the run's build budgets,
    # NOT chosen by the agent: an optimization cycle costs a sub-agent run plus one or two docker
    # builds, so the cap belongs to whoever pays for the run. A cycle is one attempt to change the
    # candidate; a call that only measures spends nothing and does not count.
    optimize_cycles_allowed: int | None = None

    # Observability: span_id of the AGENT_TOOL_INVOCATION event for the tool call
    # currently executing. The agent loop sets this before each execute(); a tool that
    # opens its own sub-spans (e.g. mini-swe's swe.task_start) parents them under it, so
    # the trace forms a real turn -> tool -> task tree rather than relying on event order.
    # None when observability is off.
    parent_span_id: str | None = None


@dataclass
class ToolResult:
    """Result from executing a tool."""
    success: bool
    output: dict[str, Any]
    error: str | None = None
    # True when the tool rejected the call's arguments and therefore never ran. This is a different
    # kind of failure from work that ran and failed, and the agent loop needs to tell them apart: a
    # deploy that fails because the image will not build hands the agent a real error to fix and such
    # runs recover, whereas a call rejected before it ran means the agent is not making contact with
    # the environment at all. Only the latter counts toward the loop's stuck-run breaker.
    invalid_call: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for LLM consumption."""
        if self.success:
            return self.output
        else:
            return {"error": self.error, **self.output}


class BaseTool(ABC):
    """Base class for all AppSci agent tools.

    Each tool must define:
    - name: The tool name used in tool calls
    - description: Human-readable description for the LLM
    - parameters: JSON Schema for tool parameters
    - execute(): The actual tool implementation

    A tool MAY also declare `trace_output_keys`, naming the parts of its result worth recording in
    the trace.
    """

    #: Result fields to copy into this tool's `agent.tool_result` trace event, in display order.
    #:
    #: Empty by default, which means the trace records only that the call succeeded and how long it
    #: took. That default was once the ONLY behaviour, and it made a tool's actual finding invisible:
    #: whatever it reported reached the model and the log line, but nothing a reader of the run could
    #: see afterwards. `optimize_agent` is the case that showed it up -- its whole point is a score and
    #: an accept/reject verdict, and the trace recorded "ok".
    #:
    #: Declared per tool rather than dumping the whole output because some outputs are large by
    #: design: a per-case eval breakdown, or a sub-agent's full transcript. An event is written per
    #: call, so the choice of what belongs in one is the tool's to make. Keep these SMALL and
    #: summary-shaped; anything a reader would page through belongs on disk with a path in the output.
    trace_output_keys: tuple[str, ...] = ()

    @property
    @abstractmethod
    def name(self) -> str:
        """The name of the tool (used in tool calls)."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema defining the tool's parameters."""
        ...

    @abstractmethod
    def execute(self, params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool with the given parameters.

        Args:
            params: Parameters passed to the tool (validated against schema)
            context: Shared context with user_sim, workspace, etc.

        Returns:
            ToolResult indicating success/failure and output
        """
        ...

    def to_openai_tool(self) -> dict[str, Any]:
        """Convert to OpenAI tool format for litellm.

        Returns a tool definition in the format:
        {
            "type": "function",
            "function": {
                "name": "...",
                "description": "...",
                "parameters": {...}
            }
        }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
