"""Render an EvalReport as human-readable text or JSON."""
from __future__ import annotations

import json

from .datatypes import EvalReport


def render_text(report: EvalReport) -> str:
    """A compact, human-readable summary: final score, per-metric, per-data-point, and errors."""
    head = f"== appsci-bench eval :: {report.task_id} =="
    if report.run_id:
        head += f"  (run {report.run_id})"
    lines = [head, ""]
    lines.append(f"FINAL SCORE: {report.final_score:.4f}" if report.final_score is not None
                 else "FINAL SCORE: n/a")
    if report.store_root:
        lines.append(f"transcripts: {report.store_root}")

    per_metric = report.views.get("per_metric", [])
    if per_metric:
        lines += ["", "per metric (avg across data points, higher is better):"]
        for m in per_metric:
            sign = "+" if m["positive"] else "-"
            score_str = f"{m['score']:.4f}" if m["score"] is not None else "n/a (no applicable data)"
            lines.append(f"  {m['objective']:28s} {score_str}  w={m['weight']:.2f} ({sign})")

    per_dp = report.views.get("per_data_point", [])
    if per_dp:
        lines += ["", "per data point (weighted avg of metrics):"]
        for d in per_dp:
            score_str = f"{d['score']:.4f}" if d["score"] is not None else "n/a (no applicable metric)"
            lines.append(f"  {d['data_point_id']:28s} {score_str}")

    if report.warnings:
        lines += ["", f"warnings: {len(report.warnings)}"]
        for w in report.warnings:
            lines.append(f"  - {w}")

    errored = [r for r in report.results if r.error]
    if errored:
        lines += ["", f"errors: {len(errored)} of {len(report.results)} result(s)"]
        for r in errored[:5]:
            lines.append(f"  {r.data_point_id} / {r.objective} [r{r.repeat_index}]: {r.error}")
        if len(errored) > 5:
            lines.append(f"  ... and {len(errored) - 5} more")
    return "\n".join(lines)


def render_json(report: EvalReport) -> str:
    """The full report as indented JSON (the machine-readable artifact)."""
    return json.dumps(report.to_dict(), indent=2)
