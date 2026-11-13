"""Deterministic gold reference artifact for the slides_generator task.

Serves each eval sample's own expected_output.json presentation + sibling gold.pptx directly.
No LLM, no generation -- replays the known-good output per eval sample, proving the
eval pipeline (eval loop + deterministic checkers) end-to-end.

The deck is returned as a File carrying gold.pptx's BYTES (not a path): the task's eval functions
read `response.files`, so a path string never reaches them.

Used as --applied-scientist gold/slides_generator/reference_artifact.py.
"""
from __future__ import annotations

import json
import pathlib

from appsci_bench.eval.artifact import ArtifactQuery, ArtifactResponse, InProcessArtifact
from appsci_bench.core.spec import REPO_ROOT, load_task_spec

TASK_ID = "slides_generator"


def _build_lookup() -> dict[str, dict]:
    """Build {query_text: {presentation, pptx_path}} from eval_data/<id>/{sample.json,gold.pptx}.
    Keyed by the sample's own single_turn_input.text, matching what `_respond_fn` sees as
    `query.intended_task` (the bridge populates that from the raw query text -- see
    eval_system.py's `_legacy_respond`)."""
    spec = load_task_spec(TASK_ID)
    dataset_dir = pathlib.Path(spec.dataset)
    if not dataset_dir.is_absolute():
        dataset_dir = REPO_ROOT / dataset_dir
    lookup: dict[str, dict] = {}
    for sample_file in sorted(dataset_dir.glob("*/sample.json")):
        d = json.loads(sample_file.read_text())
        query_text = (d.get("single_turn_input") or {}).get("text")
        pptx_path = sample_file.parent / "gold.pptx"
        if not query_text or not pptx_path.exists():
            continue
        raw = (d.get("expected_output") or {}).get("json")
        try:
            expected = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            expected = {}
        presentation = expected.get("presentation") if isinstance(expected, dict) else None
        if not presentation:
            continue
        lookup[query_text] = {"presentation": presentation, "pptx_path": str(pptx_path)}
    return lookup


def _respond_fn(lookup: dict[str, dict]):
    def respond(query: ArtifactQuery) -> ArtifactResponse:
        entry = lookup.get(query.intended_task)
        if entry is None:
            # Fallback: return a minimal valid presentation so the run doesn't crash
            return ArtifactResponse(
                output={
                    "json": json.dumps({"presentation": {"slides": [
                        {"slideNumber": 1, "layout": "blank",
                         "elements": [{"type": "text", "content": "Gold files not found for this task."}]}
                    ]}}),
                    "files": [],
                },
                messages=[{"role": "assistant", "content": "No gold file found for this task."}],
            )
        # The deck must cross back as a FILE with bytes, not as a path: eval functions read
        # `response.files` (see tasks/slides_generator/eval.py's `_pptx_file`) and a path string on
        # the output is invisible to them -- `functional_success` scored a hard 0.0 and both vision
        # judges returned None while this returned only `pptx_path`. Keys are `json`/`files` so the
        # in-process bridge takes `ArtifactAgentOutput.from_dict` (which parses both) rather than the
        # json.dumps fallback branch, which drops files.
        return ArtifactResponse(
            output={
                "json": json.dumps({"presentation": entry["presentation"]}),
                "files": [{
                    "file_type": "pptx",
                    "file_name": "deck.pptx",
                    "file_data": pathlib.Path(entry["pptx_path"]).read_bytes(),
                }],
            },
            messages=[{"role": "assistant", "content": "Deck generated."}],
        )
    return respond


class ReferenceAppliedScientist:
    """Stands in for the AppSci agent: 'builds' the gold reference artifact for slides_generator."""

    def build(self, context):  # noqa: ANN001
        lookup = _build_lookup()
        return InProcessArtifact(_respond_fn(lookup))


def build() -> ReferenceAppliedScientist:
    """`build(context)`-factory entry point (reference solution; the benchmark's supported entrypoint
    is the Docker orchestrator, which builds via the containerized AppSci agent)."""
    return ReferenceAppliedScientist()
