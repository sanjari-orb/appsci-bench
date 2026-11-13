# Discrepancy detector -- the human baseliner's submitted system artifact for order_to_cash.
#
# I/O contract (matches appsci_bench/eval's ArtifactAgentClient contract -- the types below are
# vendored locally in datatypes.py so the standalone workspace needs no appsci_bench package; see
# that file and appsci_bench/eval/artifact_client.py for the general shape;
# tasks/order_to_cash/README.md's "System Artifact I/O Contract" section still describes the OLD
# detect(invoice_id) contract and is stale relative to spec.yaml's objectives: migration):
#
#     def respond(query: ArtifactAgentQuery) -> ArtifactAgentOutput | dict | str
#
# `query.text` is a natural-language review instruction (from eval_data/samples.jsonl's
# single_turn_input.text), e.g. "Review invoice INV-3350-DUP (supplier Supplier_3, PO PO-000996)
# against its PO line and the supplier contract; decide whether to flag it..." -- NOT a bare
# invoice id. The return value must carry a `json` field (or "json" dict key) that is a JSON-encoded
# {"flagged": bool, "reason": str} object -- this is what
# tasks/order_to_cash/evaluators/deterministic/utils.py:extract_flag_and_reason reads.
#
# Queries the OLTP database via DATABASE_URL (set by docker-compose in the workspace container --
# see ../run_setup.sh) to look up the invoice/PO/contract referenced in query.text. Left blank on
# purpose: writing this function IS the timed task, using the requirements elicited via elicit.py --
# it should not be pre-written in the pilot scaffold.
#
# Below is a dummy stub satisfying the contract (always answers "not flagged") so the pipeline can
# be smoke-tested end to end via scoring/runner.py before the real logic is written.

from __future__ import annotations

import json
import re
import time

# The artifact I/O types resolve differently by context, all correct:
#   - repo / eval run (scoring/runner.py, once the eval PR lands): the REAL appsci_bench types, so
#     the evaluator's InProcessArtifactClient._coerce_output isinstance-check passes;
#   - standalone workspace (cd /work/solution): the vendored datatypes.py copy (no appsci_bench there).
try:
    from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery  # repo / eval run
except ImportError:
    try:
        from .datatypes import ArtifactAgentOutput, ArtifactAgentQuery   # package import w/o appsci_bench
    except ImportError:
        from datatypes import ArtifactAgentOutput, ArtifactAgentQuery    # flat: cd /work/solution

# The pipeline lives in the agent package; resolve it the same two ways as datatypes:
#   - package import (python -m solution.eval.run_eval, pytest): the relative form;
#   - flat (cd /work/solution): the top-level form.
try:
    from .agent.pipeline import review_invoice          # package import
except ImportError:
    from agent.pipeline import review_invoice           # flat: cd /work/solution

# Invoice numbers look like INV-3046 or INV-3046-DUP; the -DUP suffix is part of
# the primary key and must be preserved.
_INVOICE_RE = re.compile(r"INV-[A-Za-z0-9-]+")


def _parse_invoice_number(text: str | None) -> str | None:
    """Extract the first invoice number (``INV-...`` token) from the instruction.

    ``query.text`` is a natural-language review instruction, e.g. "Review invoice
    INV-3046-DUP (supplier ...)". Returns the matched token verbatim (keeping any
    ``-DUP`` suffix), or ``None`` if no invoice number is present.
    """
    if not text:
        return None
    match = _INVOICE_RE.search(text)
    return match.group(0) if match else None


def respond(query: ArtifactAgentQuery) -> ArtifactAgentOutput:
    """Review the invoice named in ``query.text`` and return a flag decision.

    Thin adapter: parse the invoice number out of the natural-language instruction,
    run the agent pipeline, and pack the result into the JSON contract the evaluator
    reads -- ``{"flagged": bool, "fault_type": str | None, "reason": str}``. ``category``
    (one of the six ``FAULT_TYPES`` in ``tasks/order_to_cash/eval.py``, or ``None`` when
    not flagged) is surfaced as ``fault_type`` -- migration fix: the pilot's rules already
    compute this value, this line only stops the JSON payload from discarding it.

    If no invoice number can be parsed, the invoice is flagged for manual review
    (we cannot verify what we cannot identify).

    Latency is measured and printed on every run: the ~2s/invoice budget is a hard
    requirement, so each call reports its wall-clock time to make regressions visible.
    """
    start = time.perf_counter()
    invoice_number = _parse_invoice_number(query.text)
    if invoice_number is None:
        payload = {
            "flagged": True,
            "fault_type": None,
            "reason": "Could not identify an invoice number in the request; flagged for manual review.",
        }
        output = ArtifactAgentOutput(json=json.dumps(payload))
    else:
        result = review_invoice(invoice_number)
        payload = {"flagged": result.flagged, "fault_type": result.category, "reason": result.reason}
        output = ArtifactAgentOutput(json=json.dumps(payload))

    elapsed = time.perf_counter() - start
    print(f"respond() took {elapsed:.3f}s (invoice {invoice_number or 'UNPARSEABLE'})")
    return output


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(f"usage: python {sys.argv[0]} '<review instruction text>'", file=sys.stderr)
        sys.exit(1)

    query = ArtifactAgentQuery(text=" ".join(sys.argv[1:]))
    output = respond(query)
    print(output.json)