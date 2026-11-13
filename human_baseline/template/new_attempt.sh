#!/bin/bash
# Bootstrap a new human-baseline attempt: creates human_baseline/attempts/<attempt_id>/ with a
# config.yaml (single source of truth for this attempt's identity fields, same schema as the
# legacy human_baseline/pilot/config.yaml) plus an empty logs/ dir and reflection.md.
#
# Run from the repo root (or anywhere -- it resolves paths off this script's own location).
#
# Usage:
#   ./new_attempt.sh <task_id> <ambiguity_level> <baseliner> [attempt_id]
#   ./new_attempt.sh order_to_cash L3 P5
#   ./new_attempt.sh airline_rebooking L1 P2 air-l1-P2-01
set -euo pipefail

if [ $# -lt 3 ]; then
  echo "Usage: $0 <task_id> <ambiguity_level> <baseliner> [attempt_id]" >&2
  exit 1
fi

TASK_ID="$1"
AMBIGUITY_LEVEL="$2"
BASELINER="$3"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
ATTEMPT_ID="${4:-${TASK_ID}-$(echo "$AMBIGUITY_LEVEL" | tr '[:upper:]' '[:lower:]')-${BASELINER}-${TIMESTAMP}}"

TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATTEMPTS_DIR="$(cd "$TEMPLATE_DIR/.." && pwd)/attempts"
ATTEMPT_DIR="$ATTEMPTS_DIR/$ATTEMPT_ID"

if [ -e "$ATTEMPT_DIR" ]; then
  echo "Refusing to overwrite existing $ATTEMPT_DIR" >&2
  exit 1
fi

mkdir -p "$ATTEMPT_DIR/logs"

cat > "$ATTEMPT_DIR/config.yaml" <<EOF
task_id: $TASK_ID
ambiguity_level: $AMBIGUITY_LEVEL
collaboration_style: collaborative
baseliner: $BASELINER
attempt_id: $ATTEMPT_ID
tooling: "human + Claude Code"
soft_cap_hours: 4
# Set once the standalone workspace exists (setup_standalone.py --dir ...).
standalone_dir: ""
EOF

cp "$TEMPLATE_DIR/reflection.md.template" "$ATTEMPT_DIR/reflection.md"

touch "$ATTEMPT_DIR/logs/transcript.md" "$ATTEMPT_DIR/logs/score_checkpoints.jsonl"
cat > "$ATTEMPT_DIR/logs/time_log.yaml" <<EOF
started_at: null
ended_at: null
breaks: []
technical_issues: []
EOF

STANDALONE_DIR="$HOME/Documents/baseline-work/$ATTEMPT_ID"
ARTIFACT_DIR="$STANDALONE_DIR/artifact"

echo "=== Created attempt: $ATTEMPT_DIR ==="
echo "Next steps (see $TEMPLATE_DIR/README.md for the full walkthrough):"
echo
echo "  1. Build the standalone workspace (outside the repo -- artifact/ scaffold + sop/):"
echo "       python $TEMPLATE_DIR/setup_standalone.py --task $TASK_ID --dir $STANDALONE_DIR"
echo "     Then set standalone_dir: \"$STANDALONE_DIR\" in $ATTEMPT_DIR/config.yaml"
echo
echo "  2. Bring the task environment up (DB, tool API, etc. -- same as a real agent gets; from the repo):"
echo "       python -m appsci_bench.orchestrator.eval_artifact up --phase build --task $TASK_ID"
echo
echo "  3. Open Claude Code / your editor rooted at $STANDALONE_DIR"
echo "     Author artifact/Dockerfile + artifact/server.py (see tasks/$TASK_ID/spec.yaml's interface_design)."
echo "     Log the start time in $ATTEMPT_DIR/logs/time_log.yaml by hand."
echo
echo "  4. While coding, test your CURRENT artifact against the live task-env (repeatable,"
echo "     rebuilds + redeploys each time -- this is your edit/test loop, not scoring; from the repo):"
echo "       python $TEMPLATE_DIR/dev_deploy.py --task $TASK_ID --artifact-dir $ARTIFACT_DIR"
echo
echo "  5. Elicit requirements whenever needed (talks to the live user-sim, not answer-key data):"
echo "       python $TEMPLATE_DIR/elicit.py --task $TASK_ID --persona <l1|l2|l3> --attempt $ATTEMPT_ID"
echo
echo "  6. Checkpoints at 1h/2h/3h30/end -- real scores against a few samples, reusing the"
echo "     task-env from step 2 (--attach):"
echo "       python $TEMPLATE_DIR/checkpoint.py --task $TASK_ID --attempt $ATTEMPT_ID \\"
echo "           --artifact-dir $ARTIFACT_DIR --mark 1h --limit 5 --attach"
echo
echo "  7. Final score -- scores the FULL held-out eval set (no --limit):"
echo "       python $TEMPLATE_DIR/score_final.py --task $TASK_ID --attempt $ATTEMPT_ID \\"
echo "           --artifact-dir $ARTIFACT_DIR"
echo
echo "  8. Reflect: fill in $ATTEMPT_DIR/reflection.md (~5 sentences + 1-5 difficulty score)."
echo
echo "  9. Tear down when fully done:"
echo "       python -m appsci_bench.orchestrator.eval_artifact down --task $TASK_ID"
echo "       python $TEMPLATE_DIR/dev_deploy.py --task $TASK_ID --artifact-dir $ARTIFACT_DIR --stop"
echo "     Delete $STANDALONE_DIR by hand once you're sure you're done."
