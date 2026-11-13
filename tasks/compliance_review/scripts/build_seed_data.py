#!/usr/bin/env python3
"""Build compliance_review's build-time seed data from CUAD.

Curates a build-time-safe subset of CUAD contracts (disjoint from a contributor's frozen eval holdout list,
ground-truth/compliance_v1/eval_contracts.txt on a contributor/oracle-data-tooling, vendored here as
seed_data/eval_holdout_contracts.txt) for the seeded DB, plus a small labeled calibration set
(seed_data/labeled_calibration_examples.jsonl -- held out, not mounted to the agent; see the H2
migration note in KNOWN_ISSUES.md for why). Reimplements the same trivial must_have/must_not_have/needs_review
policy evaluation as derive_oracle.py -- that branch is unmerged, so this doesn't import it -- applied
ONLY to the labeled examples; the DB fixture itself carries no clause-presence/violation data (that's
the oracle a contributor's eval scores against, and must never be build-time-visible).

Usage:
    python3 build_seed_data.py --cuad .ai-work/datasets/cuad/CUAD_v1/CUAD_v1.json
"""
import argparse
import json
import re
import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK_DIR = HERE.parent
DEFAULT_CUAD = TASK_DIR.parents[1] / ".ai-work/datasets/cuad/CUAD_v1/CUAD_v1.json"
EVAL_HOLDOUT = TASK_DIR / "seed_data/eval_holdout_contracts.txt"

# The 4 policies compliance_v1 evaluates against (mirrors a contributor's policies.yaml -- the policy is an
# input the artifact must handle, not something to reinvent).
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

N_BUILD_CONTRACTS = 50
N_LABELED_EXAMPLES = 5


def load_presence(cuad_path: Path) -> dict:
    """title -> {category: {"present": bool, "spans": [text, ...]}, "text": full_text}"""
    data = json.loads(cuad_path.read_text())["data"]
    presence = {}
    for doc in data:
        para = doc["paragraphs"][0]
        cats = {}
        for qa in para["qas"]:
            cat = qa["id"].split("__")[-1]
            present = not qa["is_impossible"]
            cats[cat] = {
                "present": present,
                "spans": [a["text"] for a in qa["answers"]] if present else [],
            }
        presence[doc["title"]] = {"categories": cats, "text": para["context"]}
    return presence


def evaluate(categories: dict, policy: dict) -> dict:
    """Apply one policy's rules to one contract's clause presence -> gold verdict."""
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
    """Stable, collision-free id: truncated readable slug + a short hash of the full title (title
    truncation alone isn't guaranteed unique -- two CUAD titles collided at 60 chars in practice)."""
    base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50]
    digest = hashlib.sha1(title.encode()).hexdigest()[:8]
    return f"{base}_{digest}"


def select_build_contracts(presence: dict, holdout: set, n: int) -> list:
    """Deterministic pick from the build-time-safe pool, biased for category coverage across the
    4 policies (round-robins through categories that don't yet have a present+absent example)."""
    safe_titles = sorted(t for t in presence if t not in holdout)
    all_categories = sorted({r["category"] for p in POLICIES for r in p["rules"]})
    present_covered, absent_covered = set(), set()
    picked, remaining = [], list(safe_titles)

    def score(title):
        cats = presence[title]["categories"]
        gain = 0
        for cat in all_categories:
            info = cats.get(cat)
            if info is None:
                continue
            if info["present"] and cat not in present_covered:
                gain += 1
            if not info["present"] and cat not in absent_covered:
                gain += 1
        return gain

    while len(picked) < n and remaining:
        remaining.sort(key=lambda t: (-score(t), t))
        best = remaining.pop(0)
        picked.append(best)
        for cat in all_categories:
            info = presence[best]["categories"].get(cat)
            if info is None:
                continue
            (present_covered if info["present"] else absent_covered).add(cat)

    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cuad", type=Path, default=DEFAULT_CUAD)
    ap.add_argument("--n-build", type=int, default=N_BUILD_CONTRACTS)
    ap.add_argument("--n-examples", type=int, default=N_LABELED_EXAMPLES)
    args = ap.parse_args()

    presence = load_presence(args.cuad)
    holdout = {t.strip() for t in EVAL_HOLDOUT.read_text().splitlines() if t.strip()}
    missing = holdout - set(presence)
    if missing:
        raise SystemExit(f"{len(missing)} eval holdout title(s) not found in CUAD source: {sorted(missing)[:3]}")

    build_titles = select_build_contracts(presence, holdout, args.n_build)
    overlap = set(build_titles) & holdout
    if overlap:
        raise SystemExit(f"build-time selection overlaps eval holdout: {overlap}")

    # DB fixture: raw text only, no labels.
    fixture_path = TASK_DIR / "seed_data/fixture/contracts.jsonl"
    with fixture_path.open("w") as f:
        for title in build_titles:
            f.write(json.dumps({"contract_id": slug(title), "title": title, "full_text": presence[title]["text"]}) + "\n")
    print(f"wrote {len(build_titles)} contracts -> {fixture_path}")

    # Labeled calibration examples: a handful of (contract, policy) pairs with worked violations, held
    # out from the agent -- computed here, once, and baked into a seed_data/ file (never a DB column,
    # never mounted). Feeding the agent input->verdict pairs would teach it the decision rule directly
    # instead of requiring it to elicit/reason about the policy -- see the H2 migration note.
    example_titles = build_titles[: args.n_examples]
    examples_path = TASK_DIR / "seed_data/labeled_calibration_examples.jsonl"
    examples_path.parent.mkdir(parents=True, exist_ok=True)
    with examples_path.open("w") as f:
        for title, policy in zip(example_titles, POLICIES * (len(example_titles) // len(POLICIES) + 1)):
            result = evaluate(presence[title]["categories"], policy)
            f.write(json.dumps({
                "contract_id": slug(title),
                "title": title,
                "policy_id": policy["id"],
                "excerpt": presence[title]["text"][:2000],
                **result,
            }) + "\n")
    print(f"wrote {len(example_titles)} labeled examples -> {examples_path}")

    # Category coverage check -- fail fast rather than silently shipping degraded seed data (same
    # style as the holdout-overlap checks above); a future run with a different --n-build could
    # regress this without anyone noticing otherwise.
    all_categories = sorted({r["category"] for p in POLICIES for r in p["rules"]})
    present_any = {c for c in all_categories if any(presence[t]["categories"][c]["present"] for t in build_titles)}
    absent_any = {c for c in all_categories if any(not presence[t]["categories"][c]["present"] for t in build_titles)}
    uncovered = [c for c in all_categories if c not in present_any or c not in absent_any]
    print(f"category coverage: {len(present_any & absent_any)}/{len(all_categories)} categories have both a present and an absent example")
    if uncovered:
        raise SystemExit(f"category coverage incomplete (present+absent both required): {uncovered}")


if __name__ == "__main__":
    main()
