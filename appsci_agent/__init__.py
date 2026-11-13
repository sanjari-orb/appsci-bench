"""Applied AI Scientist agent for the AppSci benchmark.

This package provides a baseline implementation of an Applied AI Scientist agent
that builds task-specific artifacts using an LLM-powered meta-agent with three tools:
- talk_to_user: Communicate with the appsci agent user sim to gather requirements
  (absent in the no-user ablation arm; see AgentConfig.user_sim_enabled)
- mini_swe_agent: Delegate coding and shell tasks to a specialized subagent
- complete: Signal that the artifact is built, deployed, and ready for evaluation

Example usage:
    from appsci_agent import AppliedScienceAgent, TaskContext

    agent = AppliedScienceAgent()
    result = agent.build(context)   # run-result dict; the deployed artifact is queried over HTTP
"""
# Core components
from .core import (
    AgentConfig,
    AgentState,
    AppSciAgent,
    LLMClient,
    LLMResponse,
    TaskContext,
    ToolCall,
)

# Entry point
from .applied_science_agent import AppliedScienceAgent, build

# Artifact building and deployment
from .artifact import (
    ArtifactBuilder,
    ArtifactValidationResult,
    DeploymentResult,
    MockDeployer,
)

# Prompt building
from .prompts import PromptBuilder, build_system_prompt

# Tools
from .tools import (
    BaseTool,
    MiniSweAgentTool,
    CompleteTool,
    TalkToUserTool,
    ToolContext,
    ToolResult,
)

# External clients
from .clients import AppsciAgentUserSimClient, AppsciAgentUserSimClientError

__all__ = [
    # Main agent classes
    "AppliedScienceAgent",
    "AppSciAgent",
    "AgentState",
    "build",
    # Configuration
    "AgentConfig",
    # LLM client
    "LLMClient",
    "LLMResponse",
    "TaskContext",
    "ToolCall",
    # Prompt building
    "PromptBuilder",
    "build_system_prompt",
    # Tools
    "BaseTool",
    "ToolContext",
    "ToolResult",
    "TalkToUserTool",
    "MiniSweAgentTool",
    "CompleteTool",
    # Appsci agent user sim client
    "AppsciAgentUserSimClient",
    "AppsciAgentUserSimClientError",
    # Artifact building and deployment
    "ArtifactBuilder",
    "ArtifactValidationResult",
    "MockDeployer",
    "DeploymentResult",
]
