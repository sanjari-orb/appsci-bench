# TODO: this file duplicates appsci_bench/eval/datatypes.py, but the standalone solution workspace does not contain the `appsci_bench` package 
# improve by refactoring the solution to not require the datatypes, or by vendoring them into a shared location (e.g. `human_baseline/pilot/eval/datatypes.py`) that both the solution and the real evaluator can import from.
"""Vendored subset of appsci_bench/eval/datatypes.py for the standalone solution workspace.

The standalone workspace deliberately does NOT contain the `appsci_bench` package (its
`evaluators/` are answer-key-adjacent), so `detector.py` cannot import the real datatypes there.
This file is a faithful copy of the three dataclasses the `respond()` contract touches, so the
solution has no `appsci_bench` dependency while staying wire-compatible with the real evaluator's
`InProcessArtifactClient._coerce_output` (appsci_bench/eval/artifact_client.py).

KEEP IN SYNC by hand with appsci_bench/eval/datatypes.py if those dataclasses change.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

# Allowed file types for the session API (design: /query_session). Anything else is rejected.
ALLOWED_FILE_TYPES: tuple[str, ...] = ("pdf", "docx", "pptx", "csv", "xlsx", "jpg", "png", "txt", "md")


class EvalTypeError(ValueError):
    """Raised when a payload cannot be parsed into a well-formed evaluator type."""


@dataclass
class File:
    """A file passed to / produced by the artifact agent. `file_data` is raw bytes in memory."""
    file_type: str
    file_name: str
    file_data: bytes = b""

    def __post_init__(self) -> None:
        if self.file_type not in ALLOWED_FILE_TYPES:
            raise EvalTypeError(
                f"file_type {self.file_type!r} not allowed; must be one of {ALLOWED_FILE_TYPES}"
            )

    def metadata(self) -> dict:
        """Sim-visible view: type + name + existence only -- never the bytes (design: G3)."""
        return {"file_type": self.file_type, "file_name": self.file_name}

    def to_dict(self, *, include_data: bool = True) -> dict:
        d = self.metadata()
        if include_data:
            d["file_data"] = base64.b64encode(self.file_data).decode("ascii")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "File":
        raw = d.get("file_data")
        data = base64.b64decode(raw) if isinstance(raw, str) else (raw or b"")
        return cls(file_type=d["file_type"], file_name=d["file_name"], file_data=data)


def _parse_files(items: Any) -> list[File]:
    return [File.from_dict(f) for f in (items or [])]


@dataclass
class ArtifactAgentQuery:
    """Input to /query_session (the `query` field). All fields optional; `files` may be empty."""
    text: str | None = None
    json: str | None = None
    files: list[File] = field(default_factory=list)

    def to_dict(self, *, include_file_data: bool = True) -> dict:
        return {"text": self.text, "json": self.json,
                "files": [f.to_dict(include_data=include_file_data) for f in self.files]}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ArtifactAgentQuery":
        d = d or {}
        return cls(text=d.get("text"), json=d.get("json"), files=_parse_files(d.get("files")))


@dataclass
class ArtifactAgentOutput:
    """Output of /query_session. `text`/`json` are exposed to the sim; file bytes are NOT."""
    text: str | None = None
    json: str | None = None
    files: list[File] = field(default_factory=list)

    def to_dict(self, *, include_file_data: bool = True) -> dict:
        return {"text": self.text, "json": self.json,
                "files": [f.to_dict(include_data=include_file_data) for f in self.files]}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ArtifactAgentOutput":
        d = d or {}
        return cls(text=d.get("text"), json=d.get("json"), files=_parse_files(d.get("files")))

    def sim_view(self) -> dict:
        """What the eval-time user simulator may see: text + json + per-file (type, name) only.
        Never the file bytes -- files may be large and are metadata-only to the sim (design: G3)."""
        return {"text": self.text, "json": self.json, "files": [f.metadata() for f in self.files]}
