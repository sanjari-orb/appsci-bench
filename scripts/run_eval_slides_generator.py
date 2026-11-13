#!/usr/bin/env python
"""Replay slides_generator's own gold decks through the real eval loop as a canned "agent," to
smoke-test generate -> score -> aggregate end to end without a real agent.

Each sample is replayed from its own `expected_output`: the gold presentation JSON is already
inline in `expected_output.json` (no file to read), and the gold deck bytes come from the
`gold.pptx` entry in `expected_output.files` via the normal `File.materialize()` path. Both are an
AUTHORING/sanity aid (see spec.yaml), not a slide-to-slide gold reference -- a high score here means
"the pipeline plumbing works," not "a real agent is good." Samples missing either are skipped.

    python scripts/run_eval_slides_generator.py
    python scripts/run_eval_slides_generator.py --limit 1 --out-dir /tmp/x
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

# allow running as `python scripts/run_eval_slides_generator.py` from anywhere
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_OUT_ROOT = _REPO_ROOT / "scripts" / "_out"   # gitignored; each run gets a fresh subfolder

from appsci_bench.core.spec import load_task_spec
from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.dataset import load_dataset
from appsci_bench.eval.datatypes import ArtifactAgentOutput, File
from appsci_bench.eval.eval_system import run_evaluation
from appsci_bench.eval.report import render_json, render_text

TASK_ID = "slides_generator"


def _build_replay(dataset_samples) -> tuple[dict[str, ArtifactAgentOutput], dict[str, str]]:
    """data_point_id -> canned ArtifactAgentOutput built from the sample's own `expected_output`
    (gold.json content is already inline in `expected_output.json`; gold.pptx bytes come from the
    `gold.pptx` entry in `expected_output.files` via `File.materialize()`); and {id: skip_reason}
    for anything missing either."""
    replay: dict[str, ArtifactAgentOutput] = {}
    skipped: dict[str, str] = {}
    for sample in dataset_samples:
        expected = sample.expected_output
        if expected is None or not expected.json:
            skipped[sample.data_point_id] = "sample has no expected_output.json to replay"
            continue
        gold_pptx_file = next((f for f in expected.files if f.file_name == "gold.pptx"), None)
        if gold_pptx_file is None:
            skipped[sample.data_point_id] = "sample has no gold.pptx in expected_output.files to replay"
            continue
        try:
            json.loads(expected.json)
        except (TypeError, ValueError) as e:
            skipped[sample.data_point_id] = f"expected_output.json invalid: {e!r}"
            continue
        gold_pptx_file.materialize()
        replay[sample.data_point_id] = ArtifactAgentOutput(
            json=expected.json,
            files=[File(file_type="pptx", file_name="gold.pptx",
                        file_data=gold_pptx_file.file_data)],
        )
    return replay, skipped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=None,
                        help="where to write reports + transcripts (gitignored). "
                             "Default: a fresh random folder under scripts/_out/ each run.")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of data points (debug)")
    args = parser.parse_args(argv)

    if args.out_dir:
        out = pathlib.Path(args.out_dir)
    else:
        _OUT_ROOT.mkdir(parents=True, exist_ok=True)
        out = pathlib.Path(tempfile.mkdtemp(prefix="run_", dir=str(_OUT_ROOT)))

    spec = load_task_spec(TASK_ID)

    dataset = load_dataset(spec)
    replay, skipped = _build_replay(dataset.data)
    for sid, reason in sorted(skipped.items()):
        print(f"SKIP {sid}: {reason}")
    if not replay:
        print("No replayable samples found.", file=sys.stderr)
        return 1

    # run_evaluation() re-loads the dataset from spec internally, so pass a spec-shaped dict whose
    # dataset is pre-filtered to replayable samples only -- skipped samples have no canned output and
    # would otherwise hit fn() and fail every objective.
    replayable = [s for s in dataset.data if s.data_point_id in replay]
    if args.limit is not None:
        replayable = replayable[:args.limit]
    filtered_evaluation_data = dict(spec.evaluation_data)
    filtered_evaluation_data["data"] = [s.to_dict() for s in replayable]
    filtered_evaluation_data.pop("dataset", None)
    spec_dict = {"task_id": spec.task_id, "evaluation_data": filtered_evaluation_data}

    # fn(query) below is only ever handed the query (text + files) -- same as a real artifact
    # agent's /query_session call, which never learns which data point it's answering (that would
    # leak which sample it's being graded against). So query.text is the only thing available to
    # map a query back to its canned replay; fail loud rather than silently misroute if two samples
    # ever share identical brief text -- including two samples that both have NO text (None/""),
    # since `fn` would be equally unable to tell those two apart. A single such sample is fine: it
    # keys on None/"" like any other value and is found again the same way.
    text_to_id: dict[str, str] = {}
    for s in replayable:
        if not s.single_turn_input:
            continue
        text = s.single_turn_input.text
        if text in text_to_id:
            raise ValueError(
                f"duplicate single_turn_input.text between {text_to_id[text]!r} and "
                f"{s.data_point_id!r} -- this script can't disambiguate replay by text alone")
        text_to_id[text] = s.data_point_id

    def fn(query):
        data_point_id = text_to_id.get(query.text)
        if data_point_id is None:
            raise KeyError(f"no canned replay for this query (unmatched sample), text={(query.text or '')[:80]!r}")
        return replay[data_point_id]

    client = InProcessArtifactClient(fn)
    report = run_evaluation(spec_dict, client, workdir=str(out))

    text_out, json_out = render_text(report), render_json(report)
    print(text_out)
    (out / "report.txt").write_text(text_out)
    (out / "report.json").write_text(json_out)
    print(f"\nReplayed {len(replay)} sample(s): {sorted(replay)}")
    if skipped:
        print(f"Skipped {len(skipped)} sample(s): {sorted(skipped)}")
    print(f"Reports + transcripts written under: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
