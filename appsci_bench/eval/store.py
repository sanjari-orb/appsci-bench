"""The transcript "database" for a run.

Each conversation (one (data point, repeat) unit) is written as its own JSON file under
<root>/conversations/, and that unit's scores as its own JSON under <root>/results/. Produced file
bytes (artifacts) are dumped under <root>/artifacts/<unit>/ and referenced by relative path from the
JSON (`file_ref`) rather than inlined -- files may be large. Defaults to a fresh temp directory. See
docs/evaluator_system_design.md (Evaluation System, step 1: "Log everything generated in a database
... each conversation should be their own json file").

EVERY WRITE HERE IS ATOMIC AND HAPPENS AS THE CASE LANDS, because the reader is a run that may be
killed mid-eval. Two failures follow from getting either half wrong, and both have been paid for:

  * batching the writes until the end loses everything. A 97-case banking run was SIGKILLed by its
    worker timeout with 80 cases through, and because the store was only written after the last unit
    completed, all 80 were lost -- 4.3M tokens and four hours for zero recorded cases.
  * writing in place makes a kill corrupting rather than merely truncating. The tracer and the
    experiment layer parse `report.json` and `progress.json` on a live run, so a half-written file is
    read, not skipped; `_write_json` therefore writes a sibling temp file and `os.replace`s it, which
    is atomic on POSIX and Windows alike. A killed run leaves the previous good version, never a
    partial one.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
from typing import Any

from .datatypes import (
    ArtifactAgentOutput, ArtifactAgentQuery, DataPointMetricResult, GenerationRecord,
)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(s: str) -> str:
    return _SAFE.sub("_", s) or "unit"


class TranscriptStore:
    """Filesystem store for generation records. One JSON per conversation + per unit's scores,
    plus dumped artifacts and the run's own report/progress files."""

    #: Live counter file. Small and rewritten per case, so a reader tailing a run gets the count
    #: without parsing the report.
    PROGRESS = "progress.json"

    def __init__(self, root: str | pathlib.Path | None = None, *, run_id: str | None = None):
        # When no root is given we mint a temp dir; tag it with the run_id (when known) so the path
        # is traceable back to the report rather than an anonymous appsci_eval_XXXX. A temp root is
        # the wrong home for a run that might be killed -- the orchestrator passes runs/<trace>/eval
        # so the incremental writes land somewhere the tracer reads and the S3 mirror ships.
        if root:
            self.root = pathlib.Path(root)
        else:
            prefix = f"appsci_eval_{run_id}_" if run_id else "appsci_eval_"
            self.root = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
        self.conversations_dir = self.root / "conversations"
        self.artifacts_dir = self.root / "artifacts"
        self.results_dir = self.root / "results"
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def unit_name(self, rec: GenerationRecord) -> str:
        return f"{_slug(rec.data_point_id)}__r{rec.repeat_index}"

    def save(self, rec: GenerationRecord) -> pathlib.Path:
        """Write one record's conversation JSON (with artifacts dumped + referenced). Returns path."""
        unit = self.unit_name(rec)
        doc = {
            "data_point_id": rec.data_point_id,
            "repeat_index": rec.repeat_index,
            "error": rec.error,
            "final_state": rec.final_state,
            "termination_reason": rec.termination_reason,
            "simulation_steps": rec.simulation_steps,
            "lane_cleanup_error": rec.lane_cleanup_error,
            "infrastructure_error": rec.infrastructure_error,
            "generation_attempt": rec.generation_attempt,
            # Which copy of the task environment served this case; None on a single-environment run.
            "lane": rec.lane,
            "conversation": [
                {"query": self._io_doc(t.query, unit, i, "query"),
                 "response": self._io_doc(t.response, unit, i, "response")}
                for i, t in enumerate(rec.conversation)
            ],
        }
        path = self.conversations_dir / f"{unit}.json"
        self._write_json(path, doc)
        return path

    def save_infrastructure_attempt(self, rec: GenerationRecord) -> pathlib.Path:
        """Keep failed attempts outside the normal scored conversation/result listings.

        The final scored attempt keeps the normal unit name. Earlier attempts have their own
        immutable paths and never overwrite the transcript or score of a completed case.
        """
        path = (self.root / "infrastructure_attempts"
                / f"{self.unit_name(rec)}__a{rec.generation_attempt}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(path, rec.to_dict())
        return path

    def save_results(self, rec: GenerationRecord,
                     results: list[DataPointMetricResult]) -> pathlib.Path:
        """Write one unit's scores as its own JSON, the moment that unit finishes scoring.

        The per-unit file is the durable record of a case: `report.json` is rebuilt from the whole
        accumulated set on every case and is the thing readers render, but it is one file that a
        killed run can only leave in its last-written state. These are append-only in effect -- one
        file per unit, never rewritten -- so the set on disk is the ground truth for what was scored,
        and a report that lags by a case can always be rebuilt from them."""
        unit = self.unit_name(rec)
        doc = {
            "data_point_id": rec.data_point_id,
            "repeat_index": rec.repeat_index,
            "error": rec.error,
            "termination_reason": rec.termination_reason,
            "lane": rec.lane,
            "generation_attempt": rec.generation_attempt,
            "results": [r.to_dict() for r in results],
        }
        path = self.results_dir / f"{unit}.json"
        self._write_json(path, doc)
        return path

    def list_results(self) -> list[pathlib.Path]:
        return sorted(self.results_dir.glob("*.json"))

    def write_json(self, name: str, payload: Any) -> pathlib.Path:
        """Write one of the run's own JSON files (report.json / progress.json / elicitation.json)
        into the store root, atomically."""
        path = self.root / name
        self._write_json(path, payload)
        return path

    def write_text(self, name: str, text: str) -> pathlib.Path:
        """Write one of the run's own text files (report.html) into the store root, atomically."""
        path = self.root / name
        self._write(path, text.encode("utf-8"))
        return path

    def _write_json(self, path: pathlib.Path, payload: Any) -> None:
        self._write(path, json.dumps(payload, indent=2).encode("utf-8"))

    @staticmethod
    def _write(path: pathlib.Path, data: bytes) -> None:
        """Write `data` to `path` atomically: a sibling temp file, then `os.replace`.

        The temp file is a SIBLING rather than in the system temp dir because `os.replace` is only
        atomic within a filesystem, and the store root can be a bind-mounted or S3-synced run
        directory on a different device from /tmp. Cleaned up on failure so a crashed write leaves no
        litter next to the real files -- `list_conversations`/`list_results` glob `*.json`, which a
        stray `.json.tmp` would not match, but a reader listing the directory would still see it."""
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _io_doc(self, io: ArtifactAgentQuery | ArtifactAgentOutput, unit: str, turn: int,
                role: str) -> dict:
        files = []
        for f in io.files:
            ref = None
            if f.file_data:
                unit_dir = self.artifacts_dir / unit
                unit_dir.mkdir(parents=True, exist_ok=True)
                fname = f"t{turn}_{role}_{_slug(f.file_name)}"
                (unit_dir / fname).write_bytes(f.file_data)
                ref = str((unit_dir / fname).relative_to(self.root))
            files.append({"file_type": f.file_type, "file_name": f.file_name, "file_ref": ref})
        doc = {"text": io.text, "json": io.json, "files": files}
        trace = getattr(io, "trace", None)          # outputs may carry a self-reported LLM trace
        if trace:
            doc["trace"] = trace
        return doc

    def load(self, path: str | pathlib.Path) -> dict:
        """Read back one stored conversation JSON (as a plain dict; artifacts stay on disk)."""
        return json.loads(pathlib.Path(path).read_text())

    def list_conversations(self) -> list[pathlib.Path]:
        return sorted(self.conversations_dir.glob("*.json"))
