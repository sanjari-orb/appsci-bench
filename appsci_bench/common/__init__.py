"""Shared leaf utilities with no dependency on appsci_bench.core/eval/appsci_agent_user_sim.

Anything here is safe for a decoupled subpackage (e.g. appsci_agent_user_sim/) to import without
compromising its own standalone-deployability: modules in this package depend only on third-party
libraries (litellm, pydantic, ...), never on the rest of appsci_bench.
"""
from .model_config import ModelConfig, ModelConfigError, complete, config_for

__all__ = ["ModelConfig", "ModelConfigError", "complete", "config_for"]
