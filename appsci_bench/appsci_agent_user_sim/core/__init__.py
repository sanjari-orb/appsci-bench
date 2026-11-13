"""Appsci-agent-user-sim core logic: persona rendering, model config/call, and the AppsciAgentLLMUserSim simulator."""
from .persona import (
    HARD_DOC_TOKENS,
    SUGGESTED_DOC_TOKENS,
    TEMPLATE_MAP,
    TEMPLATES_DIR,
    REPO_ROOT,
    TaskContextLike,
    UserSimDocsError,
    build_template_context,
    estimate_tokens,
    load_user_persona,
    render_persona_prompt,
    resolve_known_docs,
    resolve_known_resources,
)
from appsci_bench.common.model_config import ModelConfig, ModelConfigError, complete
from .usersim_core import AppsciAgentLLMUserSim, AppsciAgentUserSimError, ROLE_AGENT, ROLE_SIM
from .task_context import TaskContext, load_task_context

__all__ = [
    "HARD_DOC_TOKENS",
    "SUGGESTED_DOC_TOKENS",
    "TEMPLATE_MAP",
    "TEMPLATES_DIR",
    "REPO_ROOT",
    "TaskContextLike",
    "UserSimDocsError",
    "build_template_context",
    "estimate_tokens",
    "load_user_persona",
    "render_persona_prompt",
    "resolve_known_docs",
    "resolve_known_resources",
    "ModelConfig",
    "ModelConfigError",
    "complete",
    "AppsciAgentLLMUserSim",
    "AppsciAgentUserSimError",
    "ROLE_AGENT",
    "ROLE_SIM",
    "TaskContext",
    "load_task_context",
]
