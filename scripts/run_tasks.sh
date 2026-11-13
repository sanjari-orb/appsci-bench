#!/usr/bin/env bash
# =============================================================================
# appsci-bench — run a BATCH of tasks end-to-end (BUILD -> EVAL) from a YAML
# config: which tasks, which persona level(s) each, how many eval data points,
# the timeout, and optionally which model the agent under test runs.
# Runs sequentially with the live observability dashboard on.
#
#   ./scripts/run_tasks.sh <config.yaml>
#
# See scripts/run_tasks_yamls/run_tasks.sample.yaml for the schema (all 3 tasks x all personas).
# Credentials must already be loaded — run `source the setup steps in README.md` first, plus
# `source scripts/setup_local_with_fireworks.sh` for a Fireworks-served agent under test.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ $# -lt 1 ]; then
    echo "usage: ./scripts/run_tasks.sh <config.yaml>" >&2
    echo "       e.g. ./scripts/run_tasks.sh scripts/run_tasks_yamls/run_tasks.sample.yaml" >&2
    exit 2
fi
CONFIG="$1"
[ -f "$CONFIG" ] || { echo "ERROR: config not found: $CONFIG" >&2; exit 2; }

PY="${REPO_ROOT}/.venv/bin/python"; [ -x "$PY" ] || PY="python"

# ---- preflight: credentials (gateway gateway OR a first-party key) -----------
if [ -z "${MODEL_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "ERROR: no model credentials. Run:  source the setup steps in README.md" >&2
    exit 1
fi
# The eval-time artifact agent user sim runs the tau-bench parity model (an OpenAI id) and does not
# inherit MODEL_* credentials -- it needs OPENAI_API_KEY (setup_local_with_gateway.sh maps the gateway's
# OpenAI endpoint onto it). Warn, don't fail: single_turn tasks never call it.
# Gate on CREDENTIAL presence, not on ARTIFACT_USERSIM_MODEL: that var is a model id and says
# nothing about whether creds for it are reachable, so keying off it would go quiet in exactly the
# case creds are most likely missing (var uncommented in .env, no key, no live gateway session).
# ANTHROPIC_* counts only when the override names an anthropic/ model, hence the two-branch check.
_sim_model="${ARTIFACT_USERSIM_MODEL:-openai/gpt-5.2}"
case "$_sim_model" in
    anthropic/*) _sim_creds="${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ;;
    *)           _sim_creds="${OPENAI_API_KEY:-}" ;;
esac
if [ -z "$_sim_creds" ]; then
    echo "[run_tasks] WARNING: no credentials reachable for the eval-time artifact agent user sim" >&2
    echo "[run_tasks]          (model $_sim_model) — multi_turn tasks such as airline_rebooking will" >&2
    echo "[run_tasks]          fail during eval. Run:  source the setup steps in README.md" >&2
fi
unset _sim_model _sim_creds

# ---- expand the config into rows: task persona limit timeout observability agent_model ---
# Fields are separated by US (0x1f), NOT tab: tab is an IFS whitespace character, so `read`
# collapses consecutive tabs and an empty `limit` ("all data points") would shift every later
# field left -- timeout then received "true" and argparse rejected it. Keep in sync with
# scripts/_expand_runs.py.
ROWS="$("$PY" scripts/_expand_runs.py "$CONFIG")" || exit 1
if [ -z "$ROWS" ]; then echo "ERROR: config produced no runs" >&2; exit 1; fi
total="$(printf '%s\n' "$ROWS" | grep -c .)"
echo "[run_tasks] $total run(s) from $CONFIG (sequential)"

# ---- preflight: sim-only document declarations (issue #76) -------------------
# Validate every task in this batch BEFORE the first run starts, so an authoring mistake surfaces now
# rather than 40 minutes in, after earlier rows have already produced results. A bad declaration is a
# PER-TASK problem: its rows are skipped and every other row still runs (the script exits 0 for that
# reason; exit 2 means the preflight itself could not run, which is worth aborting on).
BAD_DOC_TASKS=""
_batch_tasks="$(printf '%s\n' "$ROWS" | cut -d"$(printf '\x1f')" -f1 | sort -u | tr '\n' ' ')"
if [ -n "$_batch_tasks" ]; then
    # shellcheck disable=SC2086
    if ! PREFLIGHT_OUT="$("$PY" scripts/preflight_usersim_docs.py $_batch_tasks)"; then
        echo "ERROR: user-sim docs preflight could not run (see above)" >&2
        exit 3
    fi
    printf '%s\n' "$PREFLIGHT_OUT" | grep -E '^(WARN|FAIL)' >&2 || true
    BAD_DOC_TASKS="$(printf '%s\n' "$PREFLIGHT_OUT" | awk '/^FAIL /{print $2}' | tr -d ':' | sort -u | tr '\n' ' ')"
    [ -n "$BAD_DOC_TASKS" ] && echo "[run_tasks] skipping rows for task(s) with invalid user-sim docs: $BAD_DOC_TASKS" >&2
fi

# ---- loop -------------------------------------------------------------------
i=0; failures=0; summary=""

# slides_generator needs its LFS-vendored eval data MATERIALIZED on disk. A
# checkout without `git lfs pull` leaves tiny pointer stubs in place of the
# .pptx files; those pass a bare existence check but make EVAL fail mid-run when
# it opens the pointer text as a .pptx. Reject both "absent" and "pointer stub".
_slides_data_ready() {
    local files f first
    files=(tasks/slides_generator/eval_data/product-slidesgen-*/template.pptx)
    [ -e "${files[0]}" ] || return 1          # glob matched nothing -> absent
    for f in "${files[@]}"; do
        [ -f "$f" ] || return 1
        IFS= read -r first < "$f" || true
        # LFS pointer stubs start with this header; a real .pptx is a zip ("PK").
        [ "${first#version https://git-lfs.github.com/spec/v1}" != "$first" ] && return 1
    done
    return 0
}

while IFS=$'\x1f' read -r task persona limit timeout obs agent_model; do
    [ -z "$task" ] && continue
    i=$((i + 1))
    label="$task :: $(basename "$persona" .yaml)"

    # a bad sim-only document declaration is a per-task authoring error: skip this row, keep the batch
    if [ -n "$BAD_DOC_TASKS" ] && printf '%s' " $BAD_DOC_TASKS " | grep -q " $task "; then
        echo "[$i/$total] SKIP $label — invalid user-sim doc declaration (see preflight above)"
        summary="$summary"$'\n'"  SKIP  $label (bad user_sim_docs)"
        failures=$((failures + 1))
        continue
    fi

    # slides needs its (LFS-vendored) eval data materialized on disk, else EVAL fails mid-run
    if [ "$task" = "slides_generator" ] && ! _slides_data_ready; then
        echo "[$i/$total] SKIP $label — slides eval data not materialized (run: git lfs install && git lfs pull)"
        summary="$summary"$'\n'"  SKIP  $label (no eval data)"
        failures=$((failures + 1))
        continue
    fi

    args=(--task "$task" --persona "$persona" --timeout "$timeout")
    [ "$obs" = "true" ] && args+=(--enable-observability)
    [ -n "$limit" ] && args+=(--limit "$limit")
    # Empty => don't pass the flag, so the orchestrator's own default decides (and the run id it
    # builds still matches the model that actually ran).
    [ -n "$agent_model" ] && args+=(--agent-model "$agent_model")

    echo ""
    echo "========== [$i/$total] $label  (limit=${limit:-all}, timeout=${timeout}s, model=${agent_model:-${AGENT_MODEL:-default}}) =========="
    # Captured (not just streamed) so the leakage check below can pull this row's trace_id out of
    # the "Trace ID: <id>" line the orchestrator logs on startup -- `tee` under `set -o pipefail`
    # still reports the orchestrator's own exit code, not tee's.
    RUN_LOG="$(mktemp)"
    row_failed=false
    if "$PY" -m appsci_bench.orchestrator.run "${args[@]}" 2>&1 | tee "$RUN_LOG"; then
        summary_line="  OK    $label"
    else
        rc=$?
        # exit 3 = environment blocker, not a per-task failure: the observability port range
        # (default 8080+) was exhausted, or the agent model's credentials are missing/misrouted.
        # Either will hit every remaining row too, so abort the batch now instead of burning
        # through the rest of the list on the same cause.
        if [ "$rc" -eq 3 ]; then
            echo "[$i/$total] ABORT ($rc): $label — environment blocker, see error above" >&2
            summary="$summary"$'\n'"  ABORT $label (exit $rc)"
            echo ""
            echo "========== summary ($((i - 1))/$total attempted, aborted early) =========="
            printf '%s\n' "$summary"
            rm -f "$RUN_LOG"
            exit 3
        fi
        echo "[$i/$total] FAILED ($rc): $label" >&2
        summary_line="  FAIL  $label (exit $rc)"
        row_failed=true
    fi

    # Auto-detect task leakage (#77) against THIS row's trace, regardless of build outcome -- a
    # leak in a trace that also happened to time out/crash is still worth flagging, not just on a
    # clean success. Only possible under --enable-observability (that's what produces a trace at
    # all); a detector-script error (bad install, missing spec) only warns, so an infra hiccup in
    # the checker itself can't fail an otherwise-good run.
    if [ "$obs" = "true" ]; then
        # `|| true` guards this bare assignment against `set -e`: BenchmarkOrchestrator.run() can
        # fail/return before it ever logs "Trace ID: ..." (e.g. user-sim doc validation runs before
        # trace creation), so `grep` finds nothing, exits 1, and -- being a bare command substitution
        # assignment outside any if/while condition -- that WOULD abort the whole batch right here,
        # before this row's summary is ever appended or $RUN_LOG removed. The `-n "$trace_id"` check
        # right below already handles the resulting empty string correctly.
        trace_id="$(grep -o 'Trace ID: [A-Za-z0-9_]*' "$RUN_LOG" | tail -1 | sed 's/^Trace ID: //')" || true
        if [ -n "$trace_id" ] && [ -d "runs/$trace_id" ]; then
            # `leak_out=$(cmd)` as a bare statement WOULD trip `set -e` the instant a leak is found
            # (the assignment's exit status is cmd's, and that's not exempt from errexit outside a
            # conditional) -- so the capture must itself be the if-condition, same as the non-negated
            # pattern above; `if ! cmd; then rc=$?` would again capture the NEGATED status.
            if leak_out="$("$PY" scripts/detect_task_leakage.py "runs/$trace_id" 2>&1)"; then
                leak_rc=0
            else
                leak_rc=$?
            fi
            # Always print the findings table (clean or not) -- it previously only went into
            # $RUN_LOG, which is deleted at the end of this iteration, so a CLEAN run left no
            # visible sign the check ever ran at all.
            printf '%s\n' "$leak_out"
            if [ "$leak_rc" -eq 1 ]; then
                echo "[$i/$total] LEAK detected for $label — see runs/$trace_id/leakage_report.json" >&2
                # A successful build's summary_line is still exactly "  OK    $label" at this point
                # (nothing has appended to it yet) -- swap that misleading OK prefix for LEAK rather
                # than just tacking a [LEAK: ...] note onto an "OK" line. A build that already FAILED
                # keeps its FAIL prefix (still correct -- row_failed was already true) and just gets
                # the same note appended, so the leak reason doesn't get lost behind the exit code.
                if [ "$summary_line" = "  OK    $label" ]; then
                    summary_line="  LEAK  $label"
                fi
                summary_line="$summary_line  [LEAK: runs/$trace_id/leakage_report.json]"
                row_failed=true
            elif [ "$leak_rc" -ne 0 ]; then
                echo "[$i/$total] WARNING: leakage check itself failed ($leak_rc) for $label (see output above)" >&2
            fi
        fi
    fi
    rm -f "$RUN_LOG"

    summary="$summary"$'\n'"$summary_line"
    [ "$row_failed" = true ] && failures=$((failures + 1))
done <<< "$ROWS"

echo ""
echo "========== summary ($((total - failures))/$total ok) =========="
printf '%s\n' "$summary"
[ "$failures" -eq 0 ] || { echo "$failures run(s) failed/skipped" >&2; exit 1; }
