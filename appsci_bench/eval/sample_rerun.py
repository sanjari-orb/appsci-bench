"""Validate and assemble a revision of an existing eval, without editing its source trace."""
from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .datatypes import Dataset, EvalReport


def validate_sample_ids(sample_ids: list[str]) -> list[str]:
    if (not isinstance(sample_ids, list) or not sample_ids
            or any(not isinstance(s, str) or not s.strip() for s in sample_ids)):
        raise ValueError("sample_ids must be a non-empty list of sample ID strings")
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("sample_ids contains duplicates")
    return sample_ids


def read_source(source: Path, sample_ids: list[str], digest: str | None = None) -> EvalReport:
    validate_sample_ids(sample_ids)
    raw = (source / "report.json").read_bytes()
    if digest and hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("source eval report changed after the sample rerun was queued")
    report = EvalReport.from_dict(json.loads(raw))
    if (report.partial or not report.results or report.cases_total is None
            or report.cases_scored != report.cases_total):
        raise ValueError("sample reruns require a complete eval report")
    unknown = set(sample_ids) - {r.data_point_id for r in report.results}
    if unknown:
        raise ValueError(f"sample IDs absent from the original eval: {', '.join(sorted(unknown))}")
    return report


def result_keys(results):
    return Counter((r.data_point_id, r.objective, r.repeat_index) for r in results)


def report_fingerprint(report: EvalReport) -> str:
    if not report.run_id or not report.run_id.startswith("run_"):
        raise ValueError("source eval has no configuration fingerprint")
    return "cfg_" + report.run_id.split("_", 2)[1]


def prepare(source: Path, sample_ids: list[str], dataset: Dataset, objectives,
            task_id: str, reward_objective: str | None, destination: Path,
            digest: str | None = None, *, allow_config_change: bool = False):
    """Validate the source grid and aggregation, and require an opt-in for config changes."""
    from .eval_system import _config_fingerprint

    if source.resolve() == destination.resolve():
        raise ValueError("a sample rerun must write into a new trace directory")
    report = read_source(source, sample_ids, digest)
    original_ids = {r.data_point_id for r in report.results}
    full = replace(dataset, data=[s for s in dataset.data if s.data_point_id in original_ids])
    fingerprint = _config_fingerprint(task_id, full, objectives)
    if report.task_id != task_id:
        raise ValueError("task differs from the source eval")
    if report_fingerprint(report) != fingerprint and not allow_config_change:
        raise ValueError("dataset, run limits, repeats, or objective definitions differ from the source eval; "
                         "use --allow-config-change to rerun with the current configuration")
    expected = Counter((s.data_point_id, o.name, i) for s in full.data
                       for o in objectives for i in range(full.repeat))
    if result_keys(report.results) != expected or report.cases_total != len(full.data) * full.repeat:
        raise ValueError("source eval does not contain the complete original sample/objective grid")
    if (report.views.get("reward") or {}).get("objective") != reward_objective:
        raise ValueError("reward objective differs from the source eval")
    if allow_config_change:
        # Changing the run conditions is intentional; reinterpreting untouched scores with new
        # weights or directions would silently alter samples the operator did not select.
        old_objectives = {o["objective"]: (o.get("weight"), o.get("positive"))
                          for o in report.views.get("per_metric", [])}
        if old_objectives != {o.name: (o.weight, o.positive) for o in objectives}:
            raise ValueError("objective weights or directions differ from the source eval")
    # Validate transcript availability before spending any model tokens. These files are needed
    # for the revised trace to remain a complete, self-contained suite.
    units = {(s.data_point_id, i) for s in full.data for i in range(full.repeat)}
    for folder in ("conversations", "results"):
        found = set()
        for path in (source / folder).glob("*.json"):
            doc = json.loads(path.read_text())
            found.add((doc["data_point_id"], doc["repeat_index"]))
        if found != units:
            raise ValueError(f"source eval has missing or unexpected {folder}")
    selected = replace(full, data=[s for s in full.data if s.data_point_id in sample_ids])
    return report, selected, fingerprint


def config_provenance(original: EvalReport, dataset: Dataset, sample_ids: list[str],
                      fingerprint: str, *, merged: bool) -> dict:
    """Carry each retained sample's configuration across successive report revisions."""
    previous = original.sample_rerun or {}
    source_fingerprint = report_fingerprint(original)
    old_mapping = previous.get("sample_config_fingerprints") or {}
    mapping = ({r.data_point_id: old_mapping.get(r.data_point_id, source_fingerprint)
                for r in original.results} if merged else {})
    mapping.update({sample_id: fingerprint for sample_id in sample_ids})
    configurations = dict(previous.get("configurations") or {})
    # Older reports only record the fingerprint. Do not invent their run limits.
    configurations[fingerprint] = {"type": dataset.type, "repeat": dataset.repeat,
                                    "max_turns": dataset.max_turns, "max_steps": dataset.max_steps}
    return {"source_config_fingerprint": source_fingerprint,
            "current_config_fingerprint": fingerprint,
            "config_changed": source_fingerprint != fingerprint,
            "mixed_configurations": len(set(mapping.values())) > 1,
            "sample_config_fingerprints": mapping,
            "configurations": {key: configurations.get(key) for key in sorted(set(mapping.values()))}}


def merge_results(source: Path, destination: Path, original: EvalReport,
                  sample_ids: list[str], replacements):
    old_selected = [r for r in original.results if r.data_point_id in sample_ids]
    if result_keys(old_selected) != result_keys(replacements):
        raise ValueError("sample rerun did not finish every selected sample/objective/repeat")
    # Copy only untouched units. Their relative artifact references keep working in the new trace.
    for folder in ("conversations", "results"):
        for path in (source / folder).glob("*.json"):
            if json.loads(path.read_text())["data_point_id"] in sample_ids:
                continue
            target = destination / folder / path.name
            if target.exists():
                raise ValueError(f"rerun output collides with an unchanged sample: {path.name}")
            shutil.copy2(path, target)
            if folder == "conversations":
                artifacts = source / "artifacts" / path.stem
                if artifacts.is_dir():
                    shutil.copytree(artifacts, destination / "artifacts" / path.stem)
    return [r for r in original.results if r.data_point_id not in sample_ids] + replacements
