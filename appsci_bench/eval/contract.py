"""Load the canonical artifact-agent session-API contract from its versioned registry.

The contract is data, not code: each version is a self-contained file at
`schema/contracts/session_api.v<N>.json` (validated by `schema/contracts/session_api.schema.json`).
This module just loads them. `session_api_contract()` returns the latest version -- the structured
contract the build phase hands to the agent, so the agent knows
the exact endpoints + payload shapes to implement.

Why a registry rather than a pinned dict: changing the contract becomes "add a file", every prior
version stays loadable (`session_api_contract(version=N)`) so the evaluator and an artifact can
negotiate a shared version, and the shape lives in exactly one place (the file) instead of being
pinned by Python signatures + a parallel dict.
"""
from __future__ import annotations

import json
import pathlib

# repo_root/schema/contracts -- parents[2] from appsci_bench/eval/contract.py (mirrors core.spec).
CONTRACTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "schema" / "contracts"
_META_SCHEMA = CONTRACTS_DIR / "session_api.schema.json"
_PREFIX, _SUFFIX = "session_api.v", ".json"


class ContractError(RuntimeError):
    """An unknown contract version was requested, or a contract file is malformed."""


def _version_files() -> dict[int, pathlib.Path]:
    """Map version number -> file path for every session_api.v<N>.json in the registry."""
    out: dict[int, pathlib.Path] = {}
    for p in CONTRACTS_DIR.glob(f"{_PREFIX}*{_SUFFIX}"):
        try:
            out[int(p.name[len(_PREFIX):-len(_SUFFIX)])] = p
        except ValueError:
            continue  # ignore non-numeric names (the meta-schema is session_api.schema.json)
    return out


def available_versions() -> list[int]:
    """All contract versions present in the registry, ascending."""
    return sorted(_version_files())


def latest_version() -> int:
    """The newest contract version on disk."""
    versions = available_versions()
    if not versions:
        raise ContractError(f"no contract files found in {CONTRACTS_DIR}")
    return versions[-1]


def _validate(doc: dict, *, path: pathlib.Path) -> None:
    """Validate a loaded contract document against the registry meta-schema (best-effort)."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return  # validation is a dev-time guard; absence of jsonschema must not break loading
    schema = json.loads(_META_SCHEMA.read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        raise ContractError(f"contract {path.name} violates session_api.schema.json: {detail}")


def session_api_contract(version: int | None = None) -> dict:
    """Load a contract version (default: latest). Returns a fresh dict, safe to embed / mutate.

    Pass an explicit `version` to load an older contract (e.g. to negotiate compatibility with an
    artifact built against it). Raises ContractError for an unknown version.
    """
    v = latest_version() if version is None else version
    files = _version_files()
    if v not in files:
        raise ContractError(f"unknown contract version {v}; available: {sorted(files) or 'none'}")
    doc = json.loads(files[v].read_text())
    _validate(doc, path=files[v])
    return doc
