#!/usr/bin/env bash
# slides_generator build-phase setup.
#
# No database, so nothing is "made live" here. What this script does instead is assert the build phase's
# bundle actually landed: /data/samples/* and /sop/kpis.md are the agent's only build-time inputs, and
# container health is no evidence they arrived -- this workspace is `tail -f /dev/null` and reports
# healthy holding nothing at all.
#
# Two failures this catches, both otherwise completely silent:
#
#   1. A dropped bundle mapping. This is the state the task shipped in before the mappings were added:
#      four documents promised the agent /data and /sop while the bundle carried neither, so it explored
#      an empty /data and a missing /sop while /RESOURCES.md insisted both were there. An absent mapping
#      leaves a perfectly valid spec, so nothing upstream complains -- a typo'd path does fail in the
#      bundler, but a missing entry does not.
#
#   2. An unresolved large-file placeholder in place of a real document. template.pptx and
#      migration-brief.docx are stored as large-file assets, and a checkout that has not resolved them
#      leaves a ~130-byte text placeholder on disk. The bundler packages it and tar extracts it without
#      complaint at any layer, and the agent then meets a template it cannot open and concludes the
#      customer's file is corrupt. Resolving them is a host-side step; this script only reports it.
#      See environment/README.md.
#
# A failing setup script aborts the run, which is the right outcome: there is nothing worth building
# against missing inputs. Per-file completeness beyond this is checked in CI, by
# tests/tasks/slides_generator/test_environment_delivery.py -- it does not need repeating here.
set -euo pipefail

echo "=== slides_generator build setup: verifying bundled inputs ==="

fail() { echo "SETUP FAILED: $*" >&2; exit 1; }

[ -s /sop/kpis.md ] || fail "/sop/kpis.md is missing or empty (expected environment/sop/ -> /sop/ in \
the build phase's bundle)"

# Checked before the find: `find` on a missing path exits non-zero, and under `pipefail` that would kill
# the script at the assignment below -- aborting the run with no message, which is the one outcome these
# assertions exist to prevent.
[ -d /data/samples ] || fail "/data/samples does not exist (expected environment/data/ -> /data/ in \
the build phase's bundle)"
samples=$(find /data/samples -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d '[:space:]')
[ "$samples" -ge 1 ] || fail "no sample request directories under /data/samples"

# Every delivered .pptx/.docx must be a real OOXML zip. Checked by magic bytes rather than by size,
# because an unresolved large-file placeholder has a perfectly plausible size and a size check would
# wave it through. python3 is the base image's own interpreter; it has neither `unzip` nor `file`.
while IFS= read -r -d '' f; do
  python3 -c 'import sys; sys.exit(0 if open(sys.argv[1],"rb").read(4)==b"PK\x03\x04" else 1)' "$f" \
    || fail "$f is not a valid OOXML archive. It is most likely an unresolved large-file placeholder \
rather than the real document -- resolve the repository's large-file assets on the host and retry. See \
tasks/slides_generator/environment/README.md."
done < <(find /data -type f \( -name '*.pptx' -o -name '*.docx' \) -print0)

echo "=== slides_generator build setup: OK ($samples sample request(s)) ==="
