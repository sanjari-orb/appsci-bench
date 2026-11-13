#!/usr/bin/env python3
"""Build a TEMPORARY compliance_review eval_data/ set, pending a contributor's PR #34 merging.

Selects contracts disjoint from BOTH a contributor's frozen eval holdout (seed_data/eval_holdout_contracts.txt)
AND our own build-time corpus (seed_data/fixture/contracts.jsonl) -- i.e. genuinely unseen by the
agent during build, but NOT the real reserved eval set either. This exists purely so the build-time
e2e pipeline can exercise a real eval phase (schema/dataset.schema.json + tasks/compliance_review/
eval.py) instead of crashing on a missing eval_data/ dir -- scores produced against this set are NOT
a trustworthy measure of task performance (that's a contributor's job) and this whole directory should be
deleted and replaced once his real eval_data/ + eval.py publish.

Reimplements the same must_have/must_not_have/needs_review policy evaluation as build_seed_data.py
(and a contributor's derive_oracle.py) -- deliberately duplicated, not imported, since his branch is
unmerged and this script predates the module it will eventually be replaced by.

Usage:
    python3 tasks/compliance_review/scripts/build_temp_eval_data.py --cuad .ai-work/datasets/cuad/CUAD_v1/CUAD_v1.json
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CUAD = TASK_DIR.parents[1] / ".ai-work/datasets/cuad/CUAD_v1/CUAD_v1.json"
EVAL_HOLDOUT = TASK_DIR / "seed_data/eval_holdout_contracts.txt"
BUILD_FIXTURE = TASK_DIR / "seed_data/fixture/contracts.jsonl"

POLICIES = [
    {
        "id": "vendor_standard",
        "name": "Vendor Contract Standard",
        "rules": [
            {"category": "Cap On Liability", "type": "must_have"},
            {"category": "Governing Law", "type": "must_have"},
            {"category": "Uncapped Liability", "type": "must_not_have"},
            {"category": "Most Favored Nation", "type": "must_not_have"},
            {"category": "Change Of Control", "type": "needs_review"},
        ],
    },
    {
        "id": "ip_protective",
        "name": "IP-Protective Standard",
        "rules": [
            {"category": "License Grant", "type": "must_have"},
            {"category": "Ip Ownership Assignment", "type": "must_have"},
            {"category": "Joint Ip Ownership", "type": "must_not_have"},
            {"category": "Irrevocable Or Perpetual License", "type": "must_not_have"},
            {"category": "Source Code Escrow", "type": "needs_review"},
        ],
    },
    {
        "id": "risk_averse",
        "name": "Risk-Averse Standard",
        "rules": [
            {"category": "Cap On Liability", "type": "must_have"},
            {"category": "Audit Rights", "type": "must_have"},
            {"category": "Insurance", "type": "must_have"},
            {"category": "Uncapped Liability", "type": "must_not_have"},
            {"category": "Liquidated Damages", "type": "must_not_have"},
            {"category": "Covenant Not To Sue", "type": "needs_review"},
        ],
    },
    {
        "id": "procurement_clean",
        "name": "Procurement Clean Standard",
        "rules": [
            {"category": "Audit Rights", "type": "must_have"},
            {"category": "Most Favored Nation", "type": "must_not_have"},
            {"category": "Non-Compete", "type": "must_not_have"},
            {"category": "Exclusivity", "type": "must_not_have"},
            {"category": "Minimum Commitment", "type": "must_not_have"},
            {"category": "Volume Restriction", "type": "needs_review"},
        ],
    },
]

N_EVAL_CONTRACTS = 50


def load_presence(cuad_path: Path) -> dict:
    data = json.loads(cuad_path.read_text())["data"]
    presence = {}
    for doc in data:
        para = doc["paragraphs"][0]
        cats = {}
        for qa in para["qas"]:
            cat = qa["id"].split("__")[-1]
            present = not qa["is_impossible"]
            cats[cat] = {"present": present, "spans": [a["text"] for a in qa["answers"]] if present else []}
        presence[doc["title"]] = {"categories": cats, "text": para["context"]}
    return presence


def evaluate(categories: dict, policy: dict) -> dict:
    violations, needs_review = [], []
    for rule in policy["rules"]:
        cat, rtype = rule["category"], rule["type"]
        info = categories[cat]
        if rtype == "must_have" and not info["present"]:
            violations.append({"category": cat, "rule": "must_have", "present": False, "evidence_spans": []})
        elif rtype == "must_not_have" and info["present"]:
            violations.append({"category": cat, "rule": "must_not_have", "present": True, "evidence_spans": info["spans"]})
        elif rtype == "needs_review" and info["present"]:
            needs_review.append({"category": cat, "evidence_spans": info["spans"]})
    return {"compliant": not violations, "violations": violations, "needs_review": needs_review}


def slug(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50]
    digest = hashlib.sha1(title.encode()).hexdigest()[:8]
    return f"{base}_{digest}"


def make_sample(title: str, text: str, policy: dict, verdict: dict) -> dict:
    """Attaches the contract's full text as a file rather than referencing it by title only --
    eval-time contracts are, by design, disjoint from contracts_db (the build-time fixture), so an
    artifact whose only text-access path is `SELECT full_text FROM contracts WHERE title = ?` would
    find nothing for these titles. The schema's single_turn_input.files exists for exactly this
    (see tasks/_TEMPLATE/eval_data/s1/sample.json) -- the query carries the document itself instead
    of a lookup key into a store the artifact never had eval-time access to."""
    pid = policy["id"]
    instruction = (
        f"Review the attached contract against the '{policy['name']}' policy "
        f"({pid}). Report every violation (the broken rule, the offending text span when the "
        "clause is present, and a short rationale), list any clauses needing human review, and "
        "give an overall compliant verdict."
    )
    gold = {"policy_id": pid, **verdict}
    data_point_id = f"compliance_review_temp__{slug(title)}__{pid}"
    return {
        "data_point_id": data_point_id,
        "single_turn_input": {
            "text": instruction,
            "files": [{"file_type": "txt", "file_name": "contract.txt", "file_path": "contract.txt"}],
        },
        "expected_output": {"json": json.dumps(gold)},
    }, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cuad", type=Path, default=DEFAULT_CUAD)
    ap.add_argument("--n", type=int, default=N_EVAL_CONTRACTS)
    ap.add_argument("--out", default=str(TASK_DIR / "eval_data"))
    args = ap.parse_args()

    presence = load_presence(args.cuad)
    holdout = {t.strip() for t in EVAL_HOLDOUT.read_text().splitlines() if t.strip()}
    build_seen = {json.loads(l)["title"] for l in BUILD_FIXTURE.read_text().splitlines() if l.strip()}

    safe_pool = sorted(t for t in presence if t not in holdout and t not in build_seen)
    if len(safe_pool) < args.n:
        raise SystemExit(f"only {len(safe_pool)} contracts free of both holdout and build corpus")
    chosen = safe_pool[: args.n]

    overlap_holdout = set(chosen) & holdout
    overlap_build = set(chosen) & build_seen
    if overlap_holdout or overlap_build:
        raise SystemExit(f"selection overlap: holdout={overlap_holdout} build={overlap_build}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for i, title in enumerate(chosen):
        policy = POLICIES[i % len(POLICIES)]
        sample, text = make_sample(title, presence[title]["text"], policy, evaluate(presence[title]["categories"], policy))
        sample_dir = out_dir / sample["data_point_id"]
        sample_dir.mkdir(parents=True, exist_ok=True)
        (sample_dir / "sample.json").write_text(json.dumps(sample, indent=2) + "\n")
        (sample_dir / "contract.txt").write_text(text)
        n += 1

    print(f"wrote {n} TEMPORARY eval samples -> {out_dir} "
          f"(disjoint from {len(holdout)} holdout + {len(build_seen)} build-time contracts)")


if __name__ == "__main__":
    main()
