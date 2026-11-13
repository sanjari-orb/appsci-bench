"""Artifact builder for the Applied AI Scientist agent.

Handles workspace management, file validation, and artifact assembly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ArtifactValidationResult:
    """Result of validating an artifact."""
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files_found: list[str] = field(default_factory=list)


class ArtifactBuilder:
    """Manages artifact assembly and validation.

    The artifact builder handles:
    - Workspace directory management
    - File validation (Dockerfile, code, requirements.txt)
    """

    # Required files for a valid artifact
    REQUIRED_FILES = ["Dockerfile", "requirements.txt"]

    # Optional but recommended files
    RECOMMENDED_FILES = ["README.md"]

    def __init__(
        self,
        workspace_dir: str | Path = "/workspace",
        artifact_dir: str | Path | None = None,
    ):
        self.workspace_dir = Path(workspace_dir)
        self.artifact_dir = Path(artifact_dir) if artifact_dir else self.workspace_dir / "artifact"

    def ensure_workspace(self) -> Path:
        """Ensure the workspace directory exists.

        Returns:
            Path to the workspace directory
        """
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        return self.workspace_dir

    def ensure_artifact_dir(self) -> Path:
        """Ensure the artifact directory exists.

        Returns:
            Path to the artifact directory
        """
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        return self.artifact_dir

    def validate_artifact(self) -> ArtifactValidationResult:
        """Validate that the artifact has all required files.

        Returns:
            ArtifactValidationResult with validation status and details
        """
        errors = []
        warnings = []
        files_found = []

        if not self.artifact_dir.exists():
            return ArtifactValidationResult(
                is_valid=False,
                errors=[f"Artifact directory does not exist: {self.artifact_dir}"],
            )

        # Check required files
        for required_file in self.REQUIRED_FILES:
            file_path = self.artifact_dir / required_file
            if file_path.exists():
                files_found.append(required_file)
            else:
                errors.append(f"Missing required file: {required_file}")

        # Check recommended files
        for recommended_file in self.RECOMMENDED_FILES:
            file_path = self.artifact_dir / recommended_file
            if file_path.exists():
                files_found.append(recommended_file)
            else:
                warnings.append(f"Missing recommended file: {recommended_file}")

        # List all files found
        for item in self.artifact_dir.rglob("*"):
            if item.is_file():
                rel_path = item.relative_to(self.artifact_dir)
                if str(rel_path) not in files_found:
                    files_found.append(str(rel_path))

        # Validate Dockerfile content if present
        dockerfile_path = self.artifact_dir / "Dockerfile"
        if dockerfile_path.exists():
            dockerfile_errors = self._validate_dockerfile(dockerfile_path)
            errors.extend(dockerfile_errors)

        return ArtifactValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            files_found=files_found,
        )

    def _validate_dockerfile(self, dockerfile_path: Path) -> list[str]:
        """Validate Dockerfile content.

        Args:
            dockerfile_path: Path to the Dockerfile

        Returns:
            List of error messages (empty if valid)
        """
        errors = []

        content = dockerfile_path.read_text()

        # Check for basic Dockerfile structure
        if not content.strip():
            errors.append("Dockerfile is empty")
            return errors

        # Check for FROM instruction
        lines = content.strip().split("\n")
        has_from = any(line.strip().upper().startswith("FROM") for line in lines)
        if not has_from:
            errors.append("Dockerfile missing FROM instruction")

        # Check for CMD or ENTRYPOINT
        has_cmd = any(
            line.strip().upper().startswith(("CMD", "ENTRYPOINT"))
            for line in lines
        )
        if not has_cmd:
            errors.append("Dockerfile missing CMD or ENTRYPOINT instruction")

        return errors
