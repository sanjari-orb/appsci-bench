#!/usr/bin/env python
"""Run slides_generator's real gold reference artifact through the real eval loop.

Unlike `scripts/run_eval_slides_generator.py` (which builds its own canned replay straight from
each sample's `expected_output`), this drives this package's own `reference_artifact.py` --
`ReferenceAppliedScientist.build()`, the actual gold_solution.path entrypoint declared in the task
spec. That artifact builds its own {query_text: presentation+pptx} lookup by scanning
eval_data/*/sample.json + sibling gold.pptx on disk, then answers each query from the lookup.

The gold artifact implements the legacy `.respond(ArtifactQuery)` contract (`appsci_bench.eval.artifact`),
not the modern `/query_session` client. This script wraps it in an `InProcessArtifactClient` itself,
calling `_legacy_respond` through a logging shim rather than letting `artifact_to_client()` do the
bridging, so every query and response is logged as it goes; that client plugs into the standard
`run_evaluation()` loop unmodified.

Usage:
    python gold/slides_generator/eval_gold.py
    python gold/slides_generator/eval_gold.py --limit 1 --out-dir /tmp/x
    python gold/slides_generator/eval_gold.py -v          # DEBUG-level logging
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import tempfile

# allow running as `python gold/slides_generator/eval_gold.py` from anywhere
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_OUT_ROOT = _REPO_ROOT / "scripts" / "_out"  # gitignored; each run gets a fresh subfolder

from appsci_bench.core.spec import load_task_spec
from appsci_bench.eval.datatypes import ArtifactAgentQuery
from appsci_bench.eval.eval_system import _legacy_respond, run_evaluation
from appsci_bench.eval.report import render_json, render_text
from gold.slides_generator.reference_artifact import ReferenceAppliedScientist, _build_lookup

TASK_ID = "slides_generator"

log = logging.getLogger("eval_gold_slides_generator")


def _logging_respond(artifact, lookup: dict, query: ArtifactAgentQuery):
    """Wrap `_legacy_respond` to log each dispatch: which sample the query text matched (or missed)
    in the gold lookup, and whether the response carried a deck."""
    text_preview = (query.text or query.json or "")[:80]
    hit = query.text in lookup if query.text else False
    log.info("query dispatched: text=%r -> %s", text_preview, "HIT (gold sample found)" if hit else "MISS (fallback deck)")
    output = _legacy_respond(artifact, query)
    pptx_files = [f for f in output.files if f.file_type == "pptx"]
    if pptx_files:
        log.info("  -> response carries %d pptx file(s), %d bytes",
                  len(pptx_files), len(pptx_files[0].file_data or b""))
    else:
        log.warning("  -> response carries NO pptx file (objectives needing a deck will score 0/None)")
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=None,
                        help="where to write reports + transcripts (gitignored). "
                             "Default: a fresh random folder under scripts/_out/ each run.")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of data points (debug)")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.out_dir:
        out = pathlib.Path(args.out_dir)
    else:
        _OUT_ROOT.mkdir(parents=True, exist_ok=True)
        out = pathlib.Path(tempfile.mkdtemp(prefix="gold_run_", dir=str(_OUT_ROOT)))
    log.info("output dir: %s", out)

    log.info("loading task spec: %s", TASK_ID)
    spec = load_task_spec(TASK_ID)

    log.info("building gold reference artifact (ReferenceAppliedScientist.build) ...")
    scientist = ReferenceAppliedScientist()
    artifact = scientist.build(context=None)
    lookup = _build_lookup()
    log.info("gold lookup built: %d sample(s) with a presentation + gold.pptx on disk", len(lookup))
    if not lookup:
        log.error("no replayable gold samples found (missing gold.pptx or expected_output.json?) -- aborting")
        return 1
    for query_text in sorted(lookup):
        log.debug("  lookup entry: %r", query_text[:80])

    from appsci_bench.eval.artifact_client import InProcessArtifactClient
    client = InProcessArtifactClient(lambda q: _logging_respond(artifact, lookup, q))

    log.info("starting evaluation (limit=%s) ...", args.limit)
    report = run_evaluation(spec, client, workdir=str(out), limit=args.limit)
    log.info("evaluation complete: final_score=%s", report.final_score)

    text_out, json_out = render_text(report), render_json(report)
    print(text_out)
    (out / "report.txt").write_text(text_out)
    (out / "report.json").write_text(json_out)
    log.info("reports + transcripts written under: %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
