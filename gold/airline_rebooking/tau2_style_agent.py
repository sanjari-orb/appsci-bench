"""A tau2-style dummy artifact agent for the airline task (in-process LLM test subject).

A thin wrapper over the shared airline agent core (_airline_agent.py): the system prompt is the
airline policy and it runs an LLM tool-calling loop over the 14 airline tools, acting on the
task-environment via the tool-API (AIRLINE_TOOLS_URL/AIRLINE_TOOLS_TOKEN). It does NOT see the oracle.
For a HOSTED (HTTP) version of the same agent, see local_server/server.py + local_artifact_agent.py.

Creds-gated (Claude via appsci_agent's client). Used as
`--applied-scientist gold/airline_rebooking/tau2_style_agent.py`.
"""
from __future__ import annotations

import logging

from appsci_bench.eval.artifact import ArtifactQuery, ArtifactResponse, InProcessArtifact

from . import _airline_agent as core

_log = logging.getLogger("airline_artifact")


def _respond_fn(client, schemas):
    def respond(query: ArtifactQuery) -> ArtifactResponse:
        text = core.respond_to_task(client, query.intended_task, list(query.history), schemas)
        return ArtifactResponse(output={"response": text},
                                messages=[{"role": "assistant", "content": text}])
    return respond


class Tau2StyleAppliedScientist:
    def build(self, context):
        client = core.build_client()
        _log.info("tau2-style agent: model=%s base_url=%s (Claude by default; no OpenAI)",
                  getattr(client, "model", "?"), getattr(client.config, "base_url", None))
        return InProcessArtifact(_respond_fn(client, core.tool_schemas()))


def build() -> Tau2StyleAppliedScientist:
    return Tau2StyleAppliedScientist()
