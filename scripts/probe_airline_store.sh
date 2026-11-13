#!/usr/bin/env bash
# Which store is loaded in the live airline task-env: the build share, or the eval store?
#
# WHY THIS EXISTS. Both phases come up under the same container name and network alias, and /health
# answers while the store is empty, so nothing about the container tells you which phase's data it
# holds. The record counts are admin-gated (deliberately -- the artifact must not be able to count
# them) and the setup script deletes the fixture file once loaded, so there is nothing on the
# filesystem to read either.
#
# What is left is that the two stores' reservation ids are DISJOINT: the build share is
# TRN001..TRN010, the eval store uses upstream codes like 00GMVN. One read-only tool call therefore
# names the phase, using only the tool token an artifact already has.
#
# Used to verify #136 (an eval trial must score against the eval store, not the build share) against
# real Docker, and useful for any later change to the handover or the trial path. Run it while a run
# is live; it needs nothing but a running airline-workspace-1.

set -o pipefail
C=airline-workspace-1
TOK=airline-tool-token-local

look() {
    docker exec "$C" curl -s -o /dev/null -w '%{http_code}' -X POST \
        -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
        -d "{\"arguments\": {\"reservation_id\": \"$1\"}}" \
        http://localhost:8080/tools/get_reservation_details 2>/dev/null
}

while true; do
    now=$(date '+%H:%M:%S')
    if [ -n "$(docker ps -q --filter name=airline-workspace-1)" ]; then
        b=$(look TRN001)
        e=$(look 00GMVN)
        if [ "$e" = "200" ] && [ "$b" != "200" ]; then
            echo "$now  EVAL store live"
        elif [ "$b" = "200" ] && [ "$e" != "200" ]; then
            echo "$now  BUILD share live"
        else
            echo "$now  container up, store quiet (TRN001=$b 00GMVN=$e)"
        fi
    else
        echo "$now  no task-env container"
    fi
    sleep 5
done
