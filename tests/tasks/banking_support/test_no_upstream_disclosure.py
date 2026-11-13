"""The agent under test must not learn WHICH benchmark this task is cast from, or HOW it is scored.

Two distinct leaks, one guard, because they travel the same channels and were found in one line.

WHY THE BENCHMARK NAME MATTERS HERE MORE THAN ELSEWHERE. banking_support is offline-cast from a public
agentic benchmark. A frontier model may hold that benchmark's policy, product catalogue and tool
signatures in training data (KNOWN_ISSUES.md section 1). This task's premise is that correct handling is
gated on RETRIEVING the right knowledge-base document -- the document is what names the specialised tool
to unlock -- so an agent that recalls the catalogue instead of searching it can reach a correct outcome
while measuring nothing about grounding. Naming the source benchmark is a direct invitation to do that.

WHY THE MECHANISM MATTERS. 81178b3 already closed this leak through another channel: a real run had the
agent fetch the environment's OpenAPI schema, so its context came to hold the admin routes and the fact
that a database state hash is compared against a replayed trajectory. The principle recorded there is
that withholding the ORACLE is not enough if the MECHANISM is published. A persona is the worse channel,
because the simulator volunteers what it knows the moment the agent asks how it will be judged.

WHAT THIS DOES NOT TOUCH. Provenance is required and stays: the raw spec's `source` block, the vendored
upstream seed under `sop/`, `provenance/`, the converter, the eval-case ids and the docs all name the
upstream benchmark on purpose. They are benchmark-side. The redacted agent-facing spec drops the fields
that carry them, which is what this test pins rather than assumes.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

from appsci_bench.core.spec import build_agent_spec

TASK = pathlib.Path(__file__).resolve().parents[3] / "tasks" / "banking_support"

#: Naming the upstream benchmark, in any spelling an author might reach for.
UPSTREAM = [r"tau[\s._-]?\d", r"tau[\s-]?bench", r"sierra", r"banking_knowledge"]

#: Describing how the reward is computed, rather than what outcome is wanted.
MECHANISM = [r"reward_basis", r"state[\s_-]?hash", r"golden action", r"golden trajectory",
             r"\boracle\b", r"DB/ACTION", r"replay"]


def _hits(blob: str, patterns: list[str]) -> list[str]:
    return [p for p in patterns if re.search(p, blob, re.I)]


def _persona_files() -> list[pathlib.Path]:
    return sorted((TASK / "user_personas").glob("*.yaml"))


def test_the_redacted_spec_names_neither() -> None:
    """The agent is mounted a redacted view, not the raw spec. Pin that the redaction is what removes
    the provenance -- the raw spec legitimately carries `source`, so this passing is a property of the
    redactor, and it would be worth knowing immediately if that ever changed."""
    raw = yaml.safe_load((TASK / "spec.yaml").read_text())
    blob = json.dumps(build_agent_spec(raw))
    assert not _hits(blob, UPSTREAM + MECHANISM)


def test_the_raw_spec_does_still_record_provenance() -> None:
    """The mirror of the test above, so a future author cannot 'fix' a leak by deleting provenance.
    Losing the record of what this task was cast from is a different and worse failure."""
    raw = yaml.safe_load((TASK / "spec.yaml").read_text())
    assert _hits(json.dumps(raw.get("source") or {}), UPSTREAM)


@pytest.mark.parametrize("persona", _persona_files(), ids=lambda p: p.stem)
def test_no_persona_can_disclose_either(persona: pathlib.Path) -> None:
    """Checks the PARSED persona -- what the simulator is actually handed. Comments are excluded by the
    parser and so are excluded here, which is the honest model of the channel: an authoring note
    explaining why a leak was removed is not itself reachable."""
    blob = json.dumps(yaml.safe_load(persona.read_text()))
    assert not _hits(blob, UPSTREAM + MECHANISM)


def test_no_sim_document_can_disclose_either() -> None:
    """Sim-only documents are quoted VERBATIM by the simulator when asked, so a mechanism sentence in
    one reaches the agent as surely as a persona entry -- and more exactly, since the simulator is told
    to state what these say word for word."""
    docs = sorted((TASK / "user_sim_docs").rglob("*.md"))
    assert docs, "expected banking to ship sim-only documents"
    offenders = {d.name: _hits(d.read_text(), UPSTREAM + MECHANISM) for d in docs}
    assert not {k: v for k, v in offenders.items() if v}
