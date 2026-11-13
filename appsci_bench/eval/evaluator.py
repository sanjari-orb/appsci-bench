"""Evaluator wrapper for the benchmark orchestrator (`appsci_bench/orchestrator/run.py`).

A thin pass-through to `eval_system.run_evaluation`: adapts the orchestrator's artifact into a
session-API client via `artifact_to_client` (a plain HttpArtifactClient/ArtifactAgentClient passes
through unchanged; only a foreign `.respond`-only artifact, not currently produced in production,
would hit the legacy bridge), then runs the standard generate-score-aggregate loop.
"""
from __future__ import annotations

from typing import Any, Callable

from .datatypes import EvalReport, EvalSample
from .eval_system import artifact_to_client, run_evaluation
from .lanes import LanePool
from .artifact_agent_user_sim import ArtifactAgentUserSimulator


class Evaluator:
    """Evaluator wrapper for `BenchmarkOrchestrator`'s `.evaluate(spec, artifact)` interface."""

    def evaluate(
        self,
        spec: Any,
        artifact: Any,
        *,
        user_sim: ArtifactAgentUserSimulator | None = None,
        setup_state: Callable[[EvalSample], None] | None = None,
        read_final_state: Callable[[EvalSample], dict] | None = None,
        lanes: LanePool | None = None,
        query_timeout_s: float | None = None,
        eval_timeout_s: float | None = None,
        limit: int | None = None,
        sample_seed: int | None = None,
        sample_ids: list[str] | None = None,
        rerun_source: str | None = None,
        rerun_source_sha256: str | None = None,
        allow_config_change: bool = False,
        workdir: str | None = None,
        build_transcript: Any = None,
        build_run_dir: Any = None,
    ) -> EvalReport:
        client = artifact_to_client(artifact)
        # `build_transcript` (the run directory holding the build-time user-sim event log) is what the
        # elicitation diagnostic scores. Forwarded unconditionally, including None -- which the
        # diagnostic reports as unmeasurable rather than guessing where the conversation might be.
        # `build_run_dir` is the same directory on a benchmark run and is what the optimization
        # diagnostic reads (the agent's event log), forwarded on the same terms.
        kwargs: dict[str, Any] = {"user_sim": user_sim, "limit": limit,
                                  "sample_seed": sample_seed,
                                  "setup_state": setup_state, "read_final_state": read_final_state,
                                  "lanes": lanes,
                                  "build_transcript": build_transcript,
                                  "build_run_dir": build_run_dir}
        # When given, the transcripts + report are written straight into this durable dir instead of a
        # throwaway tempfile store -- so the logged report path is the real one, not /var/folders/...
        if sample_ids is not None or rerun_source is not None:
            kwargs.update(sample_ids=sample_ids, rerun_source=rerun_source,
                          rerun_source_sha256=rerun_source_sha256)
            if allow_config_change:
                kwargs["allow_config_change"] = True
        if workdir is not None:
            kwargs["workdir"] = workdir
        if query_timeout_s is not None:
            kwargs["query_timeout_s"] = query_timeout_s
        if eval_timeout_s is not None:
            kwargs["eval_timeout_s"] = eval_timeout_s
        return run_evaluation(spec, client, **kwargs)
