"""Operator-owned configuration for an external build harness container."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class HarnessConfig:
    """An image and its command, using the same container lifecycle as AppSci Agent.

    command is Docker CMD argv (the image's ENTRYPOINT still applies). Credentials
    are forwarded by environment-variable name, never stored in this config.
    """

    name: str
    image: str
    command: tuple[str, ...] = ()
    passthrough_env: tuple[str, ...] = ()
    adapter: str | None = None
    auth: str | None = None

    def __post_init__(self) -> None:
        if self.adapter is not None:
            if (not isinstance(self.adapter, str) or self.adapter not in {"codex", "claude-code"}
                    or not isinstance(self.auth, str) or self.auth not in {"api", "gateway"}):
                raise ValueError("native adapter must be codex/claude-code with auth api/gateway")
            if self.command:
                raise ValueError("native adapters use the image entry point; command must be omitted")
        elif self.auth is not None:
            raise ValueError("harness auth requires a native adapter")
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.name):
            raise ValueError("harness name must contain lowercase letters, digits, _ or -")
        if (not isinstance(self.image, str) or not self.image or self.image.startswith("-")
                or any(c.isspace() or c == "\x00" for c in self.image)):
            raise ValueError("harness image must be a non-empty Docker image reference")
        for field_name in ("command", "passthrough_env"):
            value = getattr(self, field_name)
            if not isinstance(value, (list, tuple)) or any(
                not isinstance(item, str) or not item or "\x00" in item for item in value
            ):
                raise ValueError(f"harness {field_name} must be a list of non-empty strings")
            object.__setattr__(self, field_name, tuple(value))
        for name in self.passthrough_env:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid harness environment variable name: {name!r}")
            if name.startswith(("TASK_", "ORCHESTRATOR_", "OBSERVABILITY_", "AGENT_",
                                "APPSCI_USERSIM_", "APPSCI_HARNESS_", "APPSCI_AGENT_ARTIFACT_")):
                raise ValueError(f"{name} is supplied by the benchmark and cannot be overridden")

    @classmethod
    def load(cls, path: str | Path) -> HarnessConfig:
        try:
            data = yaml.safe_load(Path(path).read_text())
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid harness YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("harness config must be a YAML object")
        allowed = {"name", "image", "command", "passthrough_env", "adapter", "auth"}
        if unknown := data.keys() - allowed:
            raise ValueError(f"unknown harness config keys: {sorted(unknown)}")
        if missing := {"name", "image"} - data.keys():
            raise ValueError(f"missing harness config keys: {sorted(missing)}")
        return cls(**data)
