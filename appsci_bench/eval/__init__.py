"""Eval-time scoring: dataset + objectives + per-data-point eval functions -> weighted aggregation.

Orchestration (this module's surface), eval-function authoring helpers (`helpers/`), and the
eval-time artifact agent user sim (`artifact_agent_user_sim.py`) are all feature-complete and wired
into `runner.py`'s `_score()`; see `eval/README.md`'s "Eval-function contract" section.
This module intentionally does NOT re-export individual helper names into its own `__all__` --
`helpers` is a distinct namespace for task designers, kept separate from this module's
orchestration surface; import from `appsci_bench.eval.helpers` directly.

See docs/evaluator_system_design.md for the design.
"""
from . import helpers
from .datatypes import (
    ALLOWED_FILE_TYPES, DEFAULT_STOP_CRITERIA, ArtifactAgentOutput, ArtifactAgentQuery,
    ConversationTurn, Dataset, DataPointMetricResult, ElicitationResult, EvalReport, EvalSample,
    EvalTypeError, File, GenerationRecord, ItemResult, MultiTurnInput, Objective,
    OptimizationCall, OptimizationResult,
)
from .dataset import load_dataset, parse_dataset
from .objectives import load_objectives, parse_objectives, resolve_eval_function
from .artifact_client import (
    ArtifactAgentClient, ArtifactClientError, HttpArtifactClient, InProcessArtifactClient,
    McpArtifactClient, UnknownSessionError,
)
from .artifact_agent_user_sim import (
    ArtifactAgentUserSim, ArtifactAgentUserSimulator, ScriptedArtifactAgentUserSim,
)
from .generation import generate
from .store import TranscriptStore
from .scoring import check_compatibility, score_records
from .aggregate import aggregate
from .report import render_json, render_text
from .eval_system import artifact_to_client, run_evaluation
from .conformance import ConformanceError, ConformanceReport, check_conformance
from .contract import ContractError, available_versions, latest_version, session_api_contract

__all__ = [
    # eval-function authoring helpers (Part 2) -- a namespace, not re-exported names; see above
    "helpers",
    # types
    "ALLOWED_FILE_TYPES", "DEFAULT_STOP_CRITERIA", "ArtifactAgentOutput", "ArtifactAgentQuery",
    "ConversationTurn", "Dataset", "DataPointMetricResult", "EvalReport", "EvalSample",
    "EvalTypeError", "File", "GenerationRecord", "MultiTurnInput", "Objective",
    "ElicitationResult", "ItemResult", "OptimizationResult", "OptimizationCall",
    # loaders
    "load_dataset", "parse_dataset", "load_objectives", "parse_objectives", "resolve_eval_function",
    # artifact client
    "ArtifactAgentClient", "ArtifactClientError", "UnknownSessionError", "InProcessArtifactClient",
    "McpArtifactClient", "HttpArtifactClient",
    # artifact agent user sim
    "ArtifactAgentUserSimulator", "ArtifactAgentUserSim", "ScriptedArtifactAgentUserSim",
    # generation + store
    "generate", "TranscriptStore",
    # scoring
    "score_records", "check_compatibility",
    # aggregation + report
    "aggregate", "render_text", "render_json",
    # eval_system (run loop)
    "run_evaluation", "artifact_to_client",
    # contract + conformance
    "session_api_contract", "available_versions", "latest_version", "ContractError",
    "check_conformance", "ConformanceReport", "ConformanceError",
]
