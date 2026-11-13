"""Conformance probe for a served artifact agent.

Before scoring, the evaluator checks that the artifact actually implements the session API contract
(docs/artifact_agent_api.md): create_session -> query_session -> delete_session, with the right
shapes (non-empty str id, a well-formed ArtifactAgentOutput, a bool from delete). `check_conformance`
never raises -- it returns a structured ConformanceReport. The orchestrator runs it and aborts the
run (ConformanceError) on failure, rather than scoring garbage.

JSON framing on `ArtifactAgentOutput.json` is intentionally not checked here: every client reaches
this probe through `artifact_client._coerce_output`, whose policy is to drop a malformed `json`
field (warning logged) rather than raise, so a bad framing never fails a turn whose other
deliverables are fine -- same leniency during the probe as during real scoring. See
artifact_client's own tests for that behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .artifact_client import ArtifactAgentClient
from .datatypes import ArtifactAgentOutput, ArtifactAgentQuery


@dataclass
class ConformanceCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ConformanceReport:
    passed: bool
    checks: list[ConformanceCheck] = field(default_factory=list)

    def summary(self) -> str:
        head = "conformance: PASS" if self.passed else "conformance: FAIL"
        lines = [head] + [f"  [{'ok' if c.passed else 'XX'}] {c.name}: {c.detail}" for c in self.checks]
        return "\n".join(lines)


class ConformanceError(RuntimeError):
    """Raised when an artifact does not conform to the session API (carries the report)."""

    def __init__(self, report: ConformanceReport):
        self.report = report
        super().__init__("artifact does not conform to the session API:\n" + report.summary())


def check_conformance(client: ArtifactAgentClient,
                      *, probe: ArtifactAgentQuery | None = None) -> ConformanceReport:
    """Probe the client's session lifecycle and return a structured report (never raises)."""
    probe = probe or ArtifactAgentQuery(text="conformance probe: please respond")
    checks: list[ConformanceCheck] = []

    # create_session -> non-empty str. Probe WITH an empty reserved `context` so artifacts are
    # forced to accept the arg now -- non-accepting artifacts fail here (loud) rather than silently
    # when per-case starting state lands later.
    session_id = None
    try:
        session_id = client.create_session(context={})
        ok = isinstance(session_id, str) and bool(session_id)
        checks.append(ConformanceCheck("create_session", ok,
                                       f"returned {session_id!r}" if ok else
                                       f"must return a non-empty str, got {session_id!r}"))
    except Exception as e:  # noqa: BLE001
        checks.append(ConformanceCheck("create_session", False, f"raised {e!r}"))

    have_session = isinstance(session_id, str) and bool(session_id)

    # query_session -> ArtifactAgentOutput
    if have_session:
        try:
            out = client.query_session(session_id, probe)
            if isinstance(out, ArtifactAgentOutput):
                checks.append(ConformanceCheck("query_session", True, "returned ArtifactAgentOutput"))
            else:
                checks.append(ConformanceCheck("query_session", False,
                                               f"must return ArtifactAgentOutput, got {type(out).__name__}"))
        except Exception as e:  # noqa: BLE001
            checks.append(ConformanceCheck("query_session", False, f"raised {e!r}"))
    else:
        checks.append(ConformanceCheck("query_session", False, "skipped: no session id from create_session"))

    # delete_session -> bool
    if have_session:
        try:
            res = client.delete_session(session_id)
            ok = isinstance(res, bool)
            checks.append(ConformanceCheck("delete_session", ok,
                                           "returned bool" if ok else
                                           f"must return bool, got {type(res).__name__}"))
        except Exception as e:  # noqa: BLE001
            checks.append(ConformanceCheck("delete_session", False, f"raised {e!r}"))
    else:
        checks.append(ConformanceCheck("delete_session", False, "skipped: no session id"))

    return ConformanceReport(passed=all(c.passed for c in checks), checks=checks)
