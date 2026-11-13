"""Abstract base class for all single-file transformation processors."""
import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class BaseProcessor(ABC):
    """Abstract base class for all single-file transformation processors.

    Each subclass transforms one input file into one output file.
    The shared validation helpers enforce a consistent failure mode across
    all processors: loud errors rather than silent no-ops.
    """

    @abstractmethod
    def execute(self, src: Path) -> Path:
        """Transform *src* and return the path of the generated output file.

        Args:
            src: Path to the input file.  Must exist and have the correct
                 extension for this processor.

        Returns:
            Path to the output file.  Guaranteed to exist when this method
            returns without raising.

        Raises:
            FileNotFoundError: If *src* does not exist.
            ValueError: If *src* has the wrong extension.
            RuntimeError: If the output file was not created (silent tool failure).
        """

    def _require_file(self, path: Path) -> None:
        """Raise FileNotFoundError if *path* does not exist."""
        if not path.exists():
            raise FileNotFoundError(f"Input file not found: {path}")

    def _require_ext(self, path: Path, *extensions: str) -> None:
        """Raise ValueError if *path* suffix is not in *extensions*."""
        if path.suffix.lower() not in extensions:
            raise ValueError(
                f"Expected file with extension {extensions}, got: {path.name}"
            )

    def _require_output(self, path: Path) -> None:
        """Raise RuntimeError if *path* was not created (silent tool failure)."""
        if not path.exists():
            raise RuntimeError(
                f"Output file was not created: {path}. "
                "The underlying tool may have failed silently."
            )
