#!/usr/bin/env python
"""Manual end-to-end demo of the evaluator orchestration (eval Part 1).

    python scripts/evaluator_system_e2e.py              # fully mocked, no creds
    python scripts/evaluator_system_e2e.py --real-judge  # real LLM for one judge if creds exist
    python scripts/evaluator_system_e2e.py --out-dir /some/path

A human-in-the-loop check (NOT part of the automated suite): wires the whole
generate -> score -> aggregate loop over fully mocked dependencies so you can confirm the setup runs
end to end and eyeball the reports. It prints each report as TEXT and as JSON, and writes the reports
+ transcripts to a fresh, randomly-named run folder under the gitignored scripts/_out/ (override with
--out-dir) for easy inspection.

Mocks: an in-process artifact client with canned outputs; a single_turn task and a multi_turn task;
deterministic eval functions (one positive=False); a ScriptedArtifactAgentUserSim for the multi_turn
case. The eval functions are defined here and passed to run_evaluation as PRE-RESOLVED objectives
(so the script needs no package/path import gymnastics).
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

# allow running as `python scripts/evaluator_system_e2e.py` from anywhere
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_OUT_ROOT = _REPO_ROOT / "scripts" / "_out"   # gitignored; each run gets a fresh subfolder

from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.eval_system import run_evaluation
from appsci_bench.eval.report import render_json, render_text
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, Objective
from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim

_USE_REAL_JUDGE = False


# --- mock eval functions ----------------------------------------------------------------------
# Keyword-only contract: (*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs).
def classification_correctness(*, data_point_id, conversation, expected_output=None, eval_rubric=None,
                               **kwargs):
    got = (conversation[-1].response.text or "").strip() if conversation else ""
    exp = (expected_output.text or "").strip() if expected_output is not None else ""
    return (1.0 if got == exp else 0.0), {"got": got, "expected": exp}


def latency_cost(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    """A positive=False metric (lower is better): a fixed mock 'normalized cost'."""
    return 0.3, {"note": "mock normalized cost; lower is better"}


def _overlap(a: str, b: str) -> float:
    ta, tb = set((a or "").lower().split()), set((b or "").lower().split())
    return (len(ta & tb) / len(ta | tb)) if (ta | tb) else 0.0


def conversation_quality(*, data_point_id, conversation, expected_output=None, eval_rubric=None,
                         **kwargs):
    replies = " ".join((t.response.text or "") for t in conversation)
    if _USE_REAL_JUDGE:
        real = _try_real_judge(replies, eval_rubric)
        if real is not None:
            return real, {"judge": "real-llm"}
    return (_overlap(replies, eval_rubric) if eval_rubric else 1.0), {"judge": "deterministic"}


def task_resolution(*, data_point_id, conversation, expected_output=None, eval_rubric=None, **kwargs):
    resolved = any("done" in (t.response.text or "").lower() for t in conversation)
    return (1.0 if resolved else 0.0), {"resolved": resolved}


def _try_real_judge(replies: str, rubric):
    """Optional: a real LLM grade via the shared model-config transport (the seam Part 2 will use)."""
    try:
        from appsci_bench.common.model_config import ModelConfig, complete
        cfg = ModelConfig.from_env()
        msg = [{"role": "system", "content": "Score 0..1 how well TEXT meets RUBRIC. Reply with only the number."},
               {"role": "user", "content": f"RUBRIC:\n{rubric}\n\nTEXT:\n{replies}"}]
        return max(0.0, min(1.0, float(complete(msg, cfg).strip().split()[0])))
    except Exception as e:  # noqa: BLE001
        print(f"  [--real-judge unavailable, using deterministic judge: {e!r}]")
        return None


# --- mock endpoints + specs -------------------------------------------------------------------
def _single_turn_endpoint(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
    # canned: always "approved" -> correct for case A (expected approved), wrong for case B (denied)
    return ArtifactAgentOutput(text="approved", json='{"decision": "approved"}')


def _multi_turn_endpoint(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
    if "finalize" in (q.text or "").lower():
        return ArtifactAgentOutput(text="All set, the plan is done.")
    return ArtifactAgentOutput(text="Here is a draft plan with milestones and owners.")


def _single_turn_spec():
    return {"task_id": "demo_single_turn", "evaluation_data": {
        "type": "single_turn", "repeat": 1,
        "data": [
            {"data_point_id": "case_a", "single_turn_input": {"text": "Decide case A"},
             "expected_output": {"text": "approved"}},
            {"data_point_id": "case_b", "single_turn_input": {"text": "Decide case B"},
             "expected_output": {"text": "denied"}},
        ]}}


def _single_turn_objectives():
    return [
        Objective("classification_correctness", "(local)", True, 2.0, eval_function=classification_correctness),
        Objective("latency_cost", "(local)", False, 1.0, eval_function=latency_cost),
    ]


def _multi_turn_spec():
    return {"task_id": "demo_multi_turn", "evaluation_data": {
        "type": "multi_turn", "max_turns": 4,
        "data": [{"data_point_id": "plan_1",
                  "multi_turn_input": {"user_task": "Build a project plan", "user_persona": "PM",
                                       "user_stop_criteria": "a finalized plan exists"},
                  "eval_rubric": "draft plan with milestones and owners, then finalize"}]}}


def _multi_turn_objectives():
    return [
        Objective("conversation_quality", "(local)", True, 1.0, eval_function=conversation_quality),
        Objective("task_resolution", "(local)", True, 1.0, eval_function=task_resolution),
    ]


def _emit(report, out_dir: pathlib.Path) -> None:
    """Print the report as text + JSON and write both to out_dir."""
    text, js = render_text(report), render_json(report)
    print(text)
    print("\n--- JSON ---")
    print(js)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.txt").write_text(text)
    (out_dir / "report.json").write_text(js)


def main(argv=None) -> int:
    global _USE_REAL_JUDGE
    parser = argparse.ArgumentParser(description="End-to-end demo of the evaluator orchestration (mocked).")
    parser.add_argument("--real-judge", action="store_true",
                        help="use a real LLM for conversation_quality if creds exist (else deterministic)")
    parser.add_argument("--out-dir", default=None,
                        help="where to write reports + transcripts (gitignored). "
                             "Default: a fresh random folder under scripts/_out/ each run.")
    args = parser.parse_args(argv)
    _USE_REAL_JUDGE = args.real_judge
    if args.out_dir:
        out = pathlib.Path(args.out_dir)
    else:
        _OUT_ROOT.mkdir(parents=True, exist_ok=True)
        out = pathlib.Path(tempfile.mkdtemp(prefix="run_", dir=str(_OUT_ROOT)))

    try:
        print("Running SINGLE-TURN demo evaluation ...\n")
        st = run_evaluation(_single_turn_spec(), InProcessArtifactClient(_single_turn_endpoint),
                            objectives=_single_turn_objectives(), workdir=str(out / "single_turn"))
        _emit(st, out / "single_turn")

        print("\n" + "=" * 70 + "\n")
        print("Running MULTI-TURN demo evaluation ...\n")
        mt = run_evaluation(_multi_turn_spec(), InProcessArtifactClient(_multi_turn_endpoint),
                            user_sim=ScriptedArtifactAgentUserSim(["Start the plan", "Now finalize it"]),
                            objectives=_multi_turn_objectives(), workdir=str(out / "multi_turn"))
        _emit(mt, out / "multi_turn")

        print(f"\nReports + transcripts written under: {out}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"DEMO FAILED: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
