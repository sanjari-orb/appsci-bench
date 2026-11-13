"""Regression tests for the slides_generator gold reference artifact.

The gold artifact's whole job is to prove the eval pipeline end to end, so a gold run must score a
perfect deterministic pass. It previously returned the deck as a `pptx_path` STRING, which no eval
function reads -- `functional_success` scored a hard 0.0 and both vision objectives returned None on
every sample, while the artifact looked like it was serving the gold deck correctly. These tests pin
the contract that actually matters: the deck crosses back as a File with bytes.

Uses a synthetic lookup (query text -> presentation JSON + in-memory pptx bytes) built directly via
`Presentation()`, not the shipped `gold.pptx` fixtures -- those are Git LFS-backed and read as
pointer files whenever LFS content hasn't been pulled, which must not be a reason for this test to
fail. `_build_lookup()` (the disk-reading half) is exercised separately below and skips itself if the
shipped fixtures turn out to be pointer files rather than real pptx bytes.
"""
from __future__ import annotations

import io
import json

import pytest
from pptx import Presentation

from appsci_bench.eval.datatypes import ArtifactAgentQuery, ConversationTurn

gold_artifact = pytest.importorskip("gold.slides_generator.reference_artifact")
sg_eval = pytest.importorskip("tasks.slides_generator.eval")

from appsci_bench.eval.eval_system import _legacy_respond  # noqa: E402


def _pptx_bytes(n_slides: int = 1) -> bytes:
    prs = Presentation()
    for _ in range(n_slides):
        prs.slides.add_slide(prs.slide_layouts[1])
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _slide(num=1, layout="content", content="hi"):
    return {"slideNumber": num, "layout": layout,
            "elements": [{"type": "text", "content": content}]}


@pytest.fixture()
def lookup(tmp_path):
    pptx_path = tmp_path / "gold.pptx"
    pptx_path.write_bytes(_pptx_bytes(2))
    return {
        "build the Q1 deck": {
            "presentation": {"slides": [_slide(1), _slide(2)]},
            "pptx_path": str(pptx_path),
        },
    }


@pytest.fixture()
def artifact(lookup):
    return gold_artifact.InProcessArtifact(gold_artifact._respond_fn(lookup))


def _respond(artifact, query_text):
    return _legacy_respond(artifact, ArtifactAgentQuery(text=query_text))


def test_every_sample_returns_the_deck_as_a_file_with_bytes(lookup, artifact):
    for query_text in lookup:
        out = _respond(artifact, query_text)
        pptx = [f for f in out.files if f.file_type == "pptx"]
        assert pptx, "gold response carried no .pptx File (a path string is invisible to eval)"
        assert pptx[0].file_data, "gold .pptx File carried no bytes"


def test_every_sample_scores_a_perfect_functional_pass(lookup, artifact):
    # The deterministic objectives are what a gold replay must max out; the judge-backed ones need a
    # live model and are out of scope here.
    for query_text in lookup:
        out = _respond(artifact, query_text)
        conv = [ConversationTurn(query=ArtifactAgentQuery(text=query_text), response=out)]
        score, meta = sg_eval.functional_success(data_point_id="gold", conversation=conv)
        assert score == 1.0, f"gold deck failed functional_success: {meta}"
        score, meta = sg_eval.output_format_compliance(data_point_id="gold", conversation=conv)
        assert score == 1.0, f"gold deck failed output_format_compliance: {meta}"


def test_unknown_query_falls_back_without_crashing(lookup, artifact):
    out = _respond(artifact, "a brief that matches no gold sample")
    score, _ = sg_eval.functional_success(data_point_id="gold", conversation=[
        ConversationTurn(query=ArtifactAgentQuery(text="x"), response=out)])
    # The fallback deliberately carries no deck, so functional_success fails -- the point is that the
    # run degrades to a score instead of raising.
    assert score == 0.0


# --- _build_lookup (the disk-reading half) --------------------------------------------------------

def test_build_lookup_reads_gold_pptx_bytes_from_disk():
    # Exercises the real dataset-scanning path. Skips (rather than fails) when the shipped gold.pptx
    # fixtures are Git LFS pointer files -- that's an environment/checkout condition, not a defect in
    # this code, and must not fail CI.
    table = gold_artifact._build_lookup()
    if not table:
        pytest.skip("no slides_generator eval_data samples with a gold.pptx available")
    for entry in table.values():
        with open(entry["pptx_path"], "rb") as f:
            header = f.read(4)
        if header != b"PK\x03\x04":
            pytest.skip(f"{entry['pptx_path']} is a Git LFS pointer file, not real pptx bytes")
        Presentation(entry["pptx_path"])  # must open cleanly as a real deck
        assert json.dumps(entry["presentation"])  # presentation JSON must be serializable
