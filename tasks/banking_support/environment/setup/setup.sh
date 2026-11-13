#!/usr/bin/env bash
# Banking-support task-env setup, run by TaskRunner inside the container AFTER the phase's bundle is
# extracted. ONE script, shared by both phases: it loads whatever that phase put at the fixed fixture
# path, so which records a phase holds is a data decision and this file never learns which phase it is
# running in. The same shape airline and order_to_cash use.
#
# WHY A LOAD STEP EXISTS AT ALL. The service boots holding NO transactional records. Both phases run
# from one image, and the harness copies a phase's data in only after the container is running, so a
# service that read its store at start-up could serve only what was baked into the image -- and a baked
# store is present in the BUILD container no matter what that phase bundled, which is how a carve
# becomes decorative. The knowledge base is the exception and stays baked in: it is shared whole with
# both phases, so there is nothing to carve and nothing to load.
#
# THE ASSERTION AT THE END IS THE POINT, not decoration. /health answers while the store is empty, by
# design, so container readiness proves nothing about data. Without a check here, an absent or partial
# load reads as success and the run comes back a sheet of zeros with nothing to explain them. A failing
# setup script aborts the run, which is the right outcome when the records are missing: there is
# nothing worth measuring against a store that did not arrive.
set -euo pipefail

BASE="http://localhost:8080"
FIXTURE_DIR="${BANKING_FIXTURE_DIR:-/fixture}"
STORE="${FIXTURE_DIR}/db.json"
MANIFEST="${FIXTURE_DIR}/expected_counts.json"
TOOL_TOKEN="${BANKING_ENV_TOKEN:-}"
# The load route is ADMIN-gated: WHICH RECORDS EXIST is the whole difference between the two phases, so
# the artifact must be able neither to load them nor to count them. Only this script and the evaluator
# hold this token.
ADMIN_TOKEN="${BANKING_ENV_ADMIN_TOKEN:-}"

if [ -z "$ADMIN_TOKEN" ]; then
  echo "BANKING_ENV_ADMIN_TOKEN is not set; cannot load this phase's store" >&2
  exit 1
fi

# 1. Wait for the process, not for data.
for _ in $(seq 1 30); do
  if curl -fsS -H "Authorization: Bearer ${TOOL_TOKEN}" "${BASE}/health" >/dev/null 2>&1; then
    echo "banking task-env process healthy"
    break
  fi
  sleep 1
done
if ! curl -fsS -H "Authorization: Bearer ${TOOL_TOKEN}" "${BASE}/health" >/dev/null 2>&1; then
  echo "banking task-env did not become healthy within 30s" >&2
  exit 1
fi

# 2. Refuse a phase that bundled nothing. Silence here is the failure mode worth being loud about: the
#    service is healthy and holds no records, so without this the phase runs to completion against an
#    empty store and reports itself fine.
if [ ! -f "$STORE" ]; then
  echo "no store at ${STORE} -- this phase bundled no data, so nothing would be loaded" >&2
  exit 1
fi
if [ ! -f "$MANIFEST" ]; then
  echo "no manifest at ${MANIFEST} -- a load with nothing to assert against is a load nobody checked" >&2
  exit 1
fi

# 3. Load this phase's store. The route takes a PATH: the file is already here, bundled by the phase.
#
#    The body is kept and the status code checked separately rather than leaning on curl's -f, which
#    fails the request but discards the body. The load route answers a bad file with a 400 that NAMES
#    what was wrong with it ("no such store", "unreadable store: ..."), and that sentence is the whole
#    difference between an abort someone can act on and one that says only that setup failed.
echo "loading ${STORE} ..."
BODY=/tmp/banking_load_response.json
CODE=$(curl -sS -o "$BODY" -w "%{http_code}" -X POST \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"path\": \"${STORE}\"}" \
  "${BASE}/admin/load")
if [ "$CODE" != "200" ]; then
  echo "loading ${STORE} failed with HTTP ${CODE}: $(cat "$BODY")" >&2
  exit 1
fi
RESPONSE=$(cat "$BODY")
rm -f "$BODY"

# 4. Assert the loaded counts against the manifest bundled beside the store, EXACTLY and per collection.
#
#    A manifest rather than the carve spec's "every collection loaded a non-zero count", because that
#    check cannot be written for this task: the build share is a strict carve-out of a densely
#    cross-referenced store, so 21 of the 23 collections this manifest lists are legitimately empty --
#    15 of the 17 the store file itself carries, plus six the data model declares and this store never
#    uses -- and a non-zero check would fail on a correct build share. A total-only check has the opposite defect -- it passes a
#    badly truncated store as long as the arithmetic happens to land. Exact per-collection counts are
#    the only form that is sharp on both sides of a split whose totals differ by two orders of
#    magnitude, and it catches the three failures that matter: a partial load, a truncated file, and a
#    build share somebody regenerated without re-checking.
#
#    The manifest is task-owner material -- it states how many records a phase holds -- so it lives in
#    the phase's own data directory and dies with it at step 5.
python3 - "$RESPONSE" "$MANIFEST" <<'PY'
import json, sys

counts = (json.loads(sys.argv[1]) or {}).get("counts") or {}
expected = json.loads(open(sys.argv[2]).read())

problems = []
if set(counts) != set(expected):
    problems.append(f"collections differ: loaded {sorted(set(counts) - set(expected))!r} unexpectedly, "
                    f"missing {sorted(set(expected) - set(counts))!r}")
for name in sorted(set(counts) & set(expected)):
    if counts[name] != expected[name]:
        problems.append(f"{name}: loaded {counts[name]} records, manifest expects {expected[name]}")
if problems:
    print("FAILED: the loaded store does not match its manifest:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    raise SystemExit(1)

held = {k: v for k, v in sorted(counts.items()) if v}
print(f"loaded {sum(counts.values())} records across {len(held)} non-empty collection(s): "
      + ", ".join(f"{k}={v}" for k, v in held.items()))
PY

# 5. Remove the raw fixture now the store is live, as airline and order_to_cash do. The agent reaches
#    these records through the tool API; leaving a readable copy of them on the container filesystem is
#    a second, unaudited path to the same data -- and the manifest beside them states how many there
#    are, which is exactly the phase difference the split exists to withhold.
rm -rf "$FIXTURE_DIR"

echo "banking task-env setup complete"
