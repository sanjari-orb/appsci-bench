"""Select eval + calibration samples from the real CRMArena query set and write sample.json /
labeled_calibration_examples.jsonl using oracle.py's best-effort evidence derivation.

Usage: python3 tasks/crm_insights/scripts/build_eval_data.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK_DIR = HERE.parent
sys.path.insert(0, str(TASK_DIR / "seed_data"))
import oracle  # noqa: E402

QUERIES_PATH = TASK_DIR.parents[1] / ".ai-work/datasets/crmarena/analyst_manager_queries.jsonl"
DB_PATH = TASK_DIR.parents[1] / ".ai-work/datasets/crmarena/crmarena_data.db"
CATEGORIES = [
    "case_routing", "handle_time", "transfer_count",
    "top_issue_identification", "monthly_trend_analysis", "best_region_identification",
]
N_EVAL_PER_CAT = 5
N_EXAMPLE_PER_CAT = 1


def _query_text(row: dict) -> str:
    """The bare CRMArena query, plus its own per-query anchor date when the question uses a
    relative time window ("past/last N quarters/months") -- without this, a relative window is
    genuinely unanswerable (there's no fixed 'today' shared across questions). The stable policy
    definitions (case routing / handle time / transfer count) live in schema_reference.md instead,
    since -- unlike the date -- they're identical across every row in a category (verified: 1
    distinct policy text per category across all 130 rows)."""
    m = re.search(r"Today's date:\s*(\d{4}-\d{2}-\d{2})", row["metadata"].get("required", "") or "")
    if not m:
        return row["query"]
    return f"{row['query']}\n\n(Today's date: {m.group(1)})"


def main():
    con = sqlite3.connect(DB_PATH)
    rows = [json.loads(l) for l in QUERIES_PATH.read_text().splitlines()]
    by_cat = {c: [r for r in rows if r["task"] == c] for c in CATEGORIES}

    eval_data_dir = TASK_DIR / "eval_data"
    eval_data_dir.mkdir(exist_ok=True)
    examples_path = TASK_DIR / "seed_data/labeled_calibration_examples.jsonl"
    examples_path.parent.mkdir(parents=True, exist_ok=True)

    n_eval_written, n_examples_written = 0, 0
    with examples_path.open("w") as ex_f:
        for cat in CATEGORIES:
            picked, ev_cache = [], {}
            for r in sorted(by_cat[cat], key=lambda r: r["idx"]):
                ev = oracle.evidence_for(r, con)
                if not ev:
                    continue
                picked.append(r)
                ev_cache[r["idx"]] = ev
                if len(picked) >= N_EVAL_PER_CAT + N_EXAMPLE_PER_CAT:
                    break

            if len(picked) < N_EVAL_PER_CAT + N_EXAMPLE_PER_CAT:
                raise SystemExit(f"{cat}: only found {len(picked)} samples with derivable evidence, "
                                  f"need {N_EVAL_PER_CAT + N_EXAMPLE_PER_CAT}")

            eval_rows, example_rows = picked[:N_EVAL_PER_CAT], picked[N_EVAL_PER_CAT:]

            for r in eval_rows:
                data_point_id = f"crm_insights__{cat}__{r['idx']}"
                sample = {
                    "data_point_id": data_point_id,
                    "single_turn_input": {"text": _query_text(r)},
                    "expected_output": {"json": json.dumps({
                        "answer": r["answer"],
                        "evidence_case_ids": ev_cache[r["idx"]],
                        "task_category": cat,
                        "reward_metric": r["reward_metric"],
                    })},
                }
                d = eval_data_dir / data_point_id
                d.mkdir(exist_ok=True)
                (d / "sample.json").write_text(json.dumps(sample, indent=2) + "\n")
                n_eval_written += 1

            for r in example_rows:
                ex_f.write(json.dumps({
                    "task_category": cat,
                    "query": _query_text(r),
                    "answer": r["answer"],
                    "evidence_case_ids": ev_cache[r["idx"]],
                }) + "\n")
                n_examples_written += 1

    print(f"wrote {n_eval_written} eval samples -> {eval_data_dir}")
    print(f"wrote {n_examples_written} calibration examples -> {examples_path}")


if __name__ == "__main__":
    main()
