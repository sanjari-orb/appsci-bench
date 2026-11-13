#!/usr/bin/env python3
"""Paper Pareto frontiers: MMS@3 versus cache-adjusted dollars and build wall time.

Run from the repository root (requires the project's [plots] extra and Chrome):
    .venv/bin/python scripts/paper_plots/plot_pareto_cost_and_time.py

No preparatory scripts are needed. Reads the optimization-enabled per-trial CSV
and score_power.py's results_score_pow_k.csv. Reuses local run logs and downloads
missing files automatically from s3://<your-bucket>/runs/<build_id>/ using the
AWS credential chain; --profile selects an explicit profile. Downloaded call logs
are gzip-compressed locally, without dropping records. --offline forbids network
access; --refresh replaces cached files from S3. --html-only avoids Chrome.

One observation per unique VALID/VALID* build, including valid failed builds.
Costs and durations are averaged over builds within each task, then equally over
tasks. Both panels use the same builds/tasks and the score_pow_k_mean column,
never task accuracy. Cells with fewer than three valid builds retain their
exported MMS@k and are flagged; --require-three-builds excludes their tasks for
every model instead. Build counts must agree with the score table.

Cost = (cache_reads * cached_rate + (input - cache_reads) * input_rate
        + output * output_rate) / 1e6, for AppSci + mini-SWE only.
Recorded prompt tokens include cache reads. Missing cache counts use the standard
input rate and are flagged; calls without input/output usage are ignored. Cache
creation uses the ordinary input rate, without a cache-write premium. Rates are
the frozen September 23, 2026 rate card used in the preceding cost analysis.

Time is build.complete.payload.duration_ms, from events-orchestrator.jsonl:
agent build start through completion, including optimization and API waits,
excluding container setup, teardown, queueing, and final evaluation. If the
duration counter is absent, the matching build.start/build.complete timestamps
provide the same interval. Missing/inconsistent lifecycle records fail loudly;
LLM call durations and spreadsheet display strings are never timing substitutes.

Outputs: pareto_cost_and_time.{pdf,svg,png,html}, per-source/build/task/model CSVs,
caption.txt, and manifest.json with input hashes, policies, and pricing sources.
Uses only checked-in data/style helpers, not the earlier cost/token scripts.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from statistics import fmean

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.paper_plots import data, style  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_SHEET = HERE / "data/results_per_trial_sheet_optimization_enabled_runs.csv"
DEFAULT_SCORES = HERE / "data/results_score_pow_k.csv"
DEFAULT_CACHE = REPO / "scripts/_out/pareto_cost_and_time/cache"
DEFAULT_OUT = HERE / "figures/pareto_cost_and_time"
LOCAL_RUNS = [REPO / "runs", REPO / "runs/failure-analysis-cache/runs",
              REPO / "runs/build-failure-audit-cache/runs"]
SOURCES = ("appsci_agent", "mini_swe_agent")
SHEET_TASKS = {"Airline": "airline_rebooking", "O2C": "order_to_cash",
               "Customer-Call Insights": "customer_call_insights", "Compliance": "compliance_review",
               "Slides": "slides_generator", "CRM Insights": "crm_insights", "Banking": "banking_support"}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_creation_input_tokens",
                "cache_known_input_tokens", "cache_unknown_input_tokens", "records", "usage_records",
                "records_without_usage", "cache_known_records", "cache_unknown_records")
PRICING_DATE = "2026-09-23"
ANTHROPIC_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
# USD per million: input, output, cached input, official source. Open models use Fireworks.
PRICES = {
    "claude-opus-5": (5.0, 25.0, 0.50, ANTHROPIC_URL),
    "claude-opus-4-8": (5.0, 25.0, 0.50, ANTHROPIC_URL),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, ANTHROPIC_URL),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, ANTHROPIC_URL),
    "gpt-5.5": (5.0, 30.0, 0.50, "https://developers.openai.com/api/docs/models/gpt-5.5"),
    "gpt-oss-120b": (0.15, 0.60, 0.015, "https://fireworks.ai/models/fireworks/gpt-oss-120b"),
    "kimi-k3": (3.0, 15.0, 0.30, "https://fireworks.ai/models/fireworks/kimi-k3"),
}
COST_FIELDS = ("cached_input_cost_usd", "standard_input_cost_usd", "output_cost_usd", "cost_usd")


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        for number, raw in enumerate(reader, 2):
            yield number, {k.strip(): (v or "").strip() for k, v in raw.items() if k}


def select_builds(path):
    """Count a build once even when the sheet contains several evaluation trials."""
    selected = {}
    for number, row in read_csv(path):
        if row.get("Valid/Invalid", "").upper() not in {"VALID", "VALID*"}:
            continue
        bid = row.get("Build ID", "")
        task = SHEET_TASKS.get(row.get("Task"))
        model = row.get("Model", "").rsplit("/", 1)[-1]
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", bid)
                or task not in data.TASKS or model not in PRICES
                or not bid.startswith(f"{task}_{model}_")):
            raise ValueError(f"{path}:{number}: invalid build/task/model identity")
        item = {"build_id": bid, "task": task, "model": model}
        if bid in selected and selected[bid] != item:
            raise ValueError(f"Conflicting identity for {bid}")
        selected[bid] = item
    if not selected:
        raise ValueError(f"No valid builds in {path}")
    return sorted(selected.values(), key=lambda r: (r["task"], r["model"], r["build_id"]))


def load_scores(path, require_three=False):
    cells = {}
    for number, row in read_csv(path):
        if row.get("opt_enabled", "").lower() != "true":
            continue
        key = row.get("model"), row.get("task")
        if key[0] not in PRICES or key[1] not in data.TASKS or key in cells:
            raise ValueError(f"{path}:{number}: unknown or duplicate score cell {key}")
        score = float(row["score_pow_k_mean"]) if row.get("score_pow_k_mean") else None
        k = int(row["k_builds"]) if row.get("k_builds") else None
        if score is not None and (not math.isfinite(score) or not 0 <= score <= 1
                                  or k not in (1, 2, 3)):
            raise ValueError(f"{path}:{number}: invalid MMS score or build count")
        cells[key] = {"mms_at_k": score, "k_builds": k, "score_note": row.get("note", "")}
    missing = [(m, t) for m in PRICES for t in data.TASKS if (m, t) not in cells]
    if missing:
        raise ValueError(f"Missing optimization-enabled score cells: {missing}")
    tasks = [t for t in data.TASKS if all(
        cells[m, t]["mms_at_k"] is not None and
        (not require_three or cells[m, t]["k_builds"] == 3) for m in PRICES)]
    if not tasks:
        raise ValueError("No tasks have comparable MMS scores for every model")
    return cells, tasks


class Archive:
    """Read local source files, automatically and atomically downloading gaps."""

    def __init__(self, root, runs_dirs=(), *, bucket="<your-bucket>", prefix="runs",
                 profile=None, region="us-east-2", offline=False, refresh=False, client=None):
        self.root, self.runs_dirs = Path(root), [Path(p) for p in runs_dirs]
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.profile, self.region = profile, region
        self.offline, self.refresh = offline, refresh
        self.client, self.lock = client, threading.Lock()

    def s3(self):
        with self.lock:
            if self.client is None:
                import boto3
                from botocore.config import Config

                self.client = boto3.Session(profile_name=self.profile, region_name=self.region).client(
                    "s3", config=Config(connect_timeout=15, read_timeout=120,
                                        retries={"max_attempts": 2}, max_pool_connections=16))
        return self.client

    def uri(self, bid, name):
        return f"s3://{self.bucket}/{self.prefix}/{bid}/{name}"

    def ensure(self, bid, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", bid) or name not in (
                "events-orchestrator.jsonl", *(f"model_calls-{s}.jsonl" for s in SOURCES)):
            raise ValueError("Invalid archive file identity")
        compressed = name.startswith("model_calls-")
        target = self.root / bid / (name + ".gz" if compressed else name)
        if not self.refresh:
            if target.is_file():
                return target
            for root in [self.root, *self.runs_dirs]:
                for suffix in ("", ".gz"):
                    path = root / bid / (name + suffix)
                    if path.is_file():
                        return path
        if self.offline:
            raise ValueError(f"Missing {self.uri(bid, name)} locally; rerun without --offline")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            response = self.s3().get_object(Bucket=self.bucket, Key=f"{self.prefix}/{bid}/{name}")
            body = response["Body"]
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    if compressed:
                        with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as zipped:
                            count = self.copy_body(body, zipped)
                    else:
                        count = self.copy_body(body, stream)
                if count != response["ContentLength"]:
                    raise ValueError(f"Incomplete S3 download: {count}/{response['ContentLength']} bytes")
                os.replace(temporary, target)
            finally:
                body.close()
        except Exception as exc:
            raise ValueError(f"Cannot download {self.uri(bid, name)}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target

    @staticmethod
    def copy_body(body, stream):
        size = 0
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            stream.write(chunk)
            size += len(chunk)
        return size


def records(path, digest):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        for number, raw in enumerate(stream, 1):
            digest.update(raw)
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f"Malformed JSON at {path}:{number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Expected an object at {path}:{number}")
            yield record


def token_count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if math.isfinite(value) and value >= 0 and int(value) == value else None


def first_count(*values):
    return next((n for v in values if (n := token_count(v)) is not None), None)


def read_usage(path, model):
    totals = dict.fromkeys(TOKEN_FIELDS, 0)
    digest = hashlib.sha256()
    for call in records(path, digest):
        totals["records"] += 1
        usage = call.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        inp, out = token_count(usage.get("prompt_tokens")), token_count(usage.get("completion_tokens"))
        if inp is None or out is None:
            totals["records_without_usage"] += 1
            continue
        if inp + out and str(call.get("model", "")).rsplit("/", 1)[-1] != model:
            raise ValueError(f"{path}: unexpected billed model {call.get('model')!r}, expected {model}")
        details = usage.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        cached = first_count(usage.get("cache_read_input_tokens"), details.get("cached_tokens"))
        created = first_count(usage.get("cache_creation_input_tokens"),
                              details.get("cache_creation_tokens"), details.get("cache_write_tokens"))
        if (cached or 0) + (created or 0) > inp:
            raise ValueError(f"{path}: cached reads/writes exceed recorded input")
        totals["usage_records"] += 1
        totals["input_tokens"] += inp
        totals["output_tokens"] += out
        totals["cached_input_tokens"] += cached or 0
        totals["cache_creation_input_tokens"] += created or 0
        bucket = "cache_unknown" if cached is None else "cache_known"
        totals[f"{bucket}_records"] += 1
        totals[f"{bucket}_input_tokens"] += inp
    return totals, digest.hexdigest()


def read_build_time(path, build):
    lifecycle = defaultdict(list)
    digest = hashlib.sha256()
    for event in records(path, digest):
        if event.get("event_type") in {"build.start", "build.complete"}:
            if event.get("trace_id") != build["build_id"]:
                raise ValueError(f"{path}: lifecycle belongs to another build")
            lifecycle[event["event_type"]].append(event)
    if len(lifecycle["build.start"]) != 1 or len(lifecycle["build.complete"]) != 1:
        raise ValueError(f"{path}: expected exactly one build.start and one build.complete")
    start, end = lifecycle["build.start"][0], lifecycle["build.complete"][0]
    config = start["payload"]
    if config.get("optimization_enabled") is not True:
        raise ValueError(f"{path}: build metadata does not confirm optimization enabled")
    if config.get("harness", "appsci_agent") not in {"appsci_agent", "internal_appsci_agent"}:
        raise ValueError(f"{path}: unexpected external agent harness")
    if (config.get("task_id") != build["task"]
            or str(config.get("agent_model", "")).rsplit("/", 1)[-1] != build["model"]):
        raise ValueError(f"{path}: build metadata task/model disagrees with sheet")
    started, finished = (datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                         for e in (start, end))
    if started.tzinfo is None or finished.tzinfo is None:
        raise ValueError(f"{path}: build timestamps must include a timezone")
    span = (finished - started).total_seconds()
    duration = end["payload"].get("duration_ms")
    if duration is None:
        seconds, method = span, "build.start/build.complete timestamps"
    else:
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError(f"{path}: invalid build duration_ms")
        seconds, method = duration / 1000, "build.complete.payload.duration_ms"
    if not math.isfinite(seconds) or seconds < 0 or span < 0 or abs(seconds - span) > 1:
        raise ValueError(f"{path}: invalid or inconsistent build duration ({seconds}s vs {span}s)")
    return {"build_seconds": seconds, "build_minutes": seconds / 60,
            "build_started_at": start["timestamp"], "build_completed_at": end["timestamp"],
            "build_status": str(end["payload"].get("status", "")), "time_method": method,
            "optimization_enabled": True, "events_sha256": digest.hexdigest()}


def price_usage(model, usage):
    inp, out, cached, _ = PRICES[model]
    costs = {"cached_input_cost_usd": usage["cached_input_tokens"] * cached / 1e6,
             "standard_input_cost_usd": (usage["input_tokens"] - usage["cached_input_tokens"]) * inp / 1e6,
             "output_cost_usd": usage["output_tokens"] * out / 1e6}
    return {**costs, "cost_usd": sum(costs.values())}


def extract_build(build, archive):
    sources = []
    for source in SOURCES:
        name = f"model_calls-{source}.jsonl"
        path = archive.ensure(build["build_id"], name)
        usage, digest = read_usage(path, build["model"])
        sources.append({**build, "source": source, **usage, **price_usage(build["model"], usage),
                        "file": str(path.resolve()), "s3_uri": archive.uri(build["build_id"], name),
                        "uncompressed_sha256": digest})
    events = archive.ensure(build["build_id"], "events-orchestrator.jsonl")
    timing = read_build_time(events, build)
    totals = {field: sum(row[field] for row in sources) for field in (*TOKEN_FIELDS, *COST_FIELDS)}
    return ({**build, **totals, **timing, "events_file": str(events.resolve()),
             "events_s3_uri": archive.uri(build["build_id"], "events-orchestrator.jsonl")}, sources)


def frontier_models(rows, x):
    """Minimize cost/time, maximize MMS; retain exact ties as nondominated."""
    return {p["model"] for p in rows if not any(
        q[x] <= p[x] and q["mean_mms_at_k"] >= p["mean_mms_at_k"]
        and (q[x] < p[x] or q["mean_mms_at_k"] > p["mean_mms_at_k"]) for q in rows)}


def add_coverage(row, prefix=""):
    inp = row[f"{prefix}input_tokens"]
    row["cache_reporting_coverage_pct"] = (
        100 * row[f"{prefix}cache_known_input_tokens"] / inp if inp else None)
    row["recorded_cached_input_pct"] = (
        100 * row[f"{prefix}cached_input_tokens"] / inp if inp else None)
    row["cache_count_missing"] = row[f"{prefix}cache_unknown_records"] > 0
    return row


def aggregate(builds, cells, tasks):
    grouped, seen = defaultdict(list), set()
    for row in builds:
        if row["build_id"] in seen:
            raise ValueError(f"Duplicate build {row['build_id']}")
        seen.add(row["build_id"])
        grouped[row["model"], row["task"]].append(row)
    numeric = (*TOKEN_FIELDS, *COST_FIELDS, "build_seconds", "build_minutes")
    by_task, by_model = [], []
    for model in PRICES:
        model_tasks = []
        for task in tasks:
            group, cell = grouped[model, task], cells[model, task]
            if not group or len(group) != cell["k_builds"]:
                raise ValueError(f"{model}/{task}: {len(group)} valid builds but score uses k={cell['k_builds']}")
            row = {"model": model, "task": task, "builds": len(group), **cell,
                   **{f"mean_{key}": fmean(r[key] for r in group) for key in numeric}}
            model_tasks.append(add_coverage(row, "mean_"))
        by_task.extend(model_tasks)
        row = {"model": model, "tasks": len(tasks), "builds": sum(r["builds"] for r in model_tasks),
               "tasks_with_fewer_than_3_builds": sum(r["builds"] < 3 for r in model_tasks),
               "mean_mms_at_k": fmean(r["mms_at_k"] for r in model_tasks),
               **{f"mean_{key}": fmean(r[f"mean_{key}"] for r in model_tasks) for key in numeric}}
        by_model.append(add_coverage(row, "mean_"))
    for key, flag in [("mean_cost_usd", "cost_frontier"), ("mean_build_minutes", "time_frontier")]:
        front = frontier_models(by_model, key)
        for row in by_model:
            row[flag] = row["model"] in front
    return by_task, by_model


def make_figure(rows):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.16)
    ymax = min(1.05, math.ceil((max(r["mean_mms_at_k"] for r in rows) + 0.08) * 10) / 10)
    for col, (x, flag, label) in enumerate([
            ("mean_cost_usd", "cost_frontier", "Mean build cost (USD; lower →)"),
            ("mean_build_minutes", "time_frontier", "Mean build time (min; faster →)")], 1):
        hi = max(r[x] for r in rows) * 1.23 or 1
        lo = -hi * (0.02 if col == 1 else 0.10)
        front = sorted((r for r in rows if r[flag]), key=lambda r: r[x], reverse=True)
        fig.add_trace(go.Scatter(
            x=[hi] + [r[x] for r in front] + [front[-1][x], hi, hi],
            y=[front[0]["mean_mms_at_k"]] + [r["mean_mms_at_k"] for r in front]
              + [0, 0, front[0]["mean_mms_at_k"]],
            mode="lines", fill="toself", fillcolor=style.hex_to_rgba("#2E9C88", 0.16),
            line={"color": style.hex_to_rgba("#2E9C88", 0.55), "width": 1},
            name="Beaten by the Pareto frontier", hoverinfo="skip", showlegend=col == 1,
        ), row=1, col=col)
        for row in rows:
            model = row["model"]
            name = data.MODELS_SHORT[model]
            if row["tasks_with_fewer_than_3_builds"]:
                name += "†"
            if col == 1 and row["cache_count_missing"]:
                name += "*"
            positions = {"kimi-k3": "middle left", "claude-haiku-4-5": "bottom center",
                         "gpt-oss-120b": "top left"}
            if col == 2:
                positions.update({"kimi-k3": "middle right", "gpt-oss-120b": "top center"})
            fig.add_trace(go.Scatter(
                x=[row[x]], y=[row["mean_mms_at_k"]], mode="markers+text", showlegend=False,
                text=[f"<b>{name}</b>" if row[flag] else name],
                textposition=positions.get(model, "top center"), cliponaxis=False,
                textfont={"size": 8},
                marker={"color": style.MODEL_COLORS[model], "symbol": style.MODEL_SYMBOLS[model],
                        "size": 8, "line": {"color": "white", "width": 0.7}},
                hovertemplate=(f"{data.MODELS[model]}<br>Mean MMS@k: %{{y:.2%}}"
                               f"<br>Mean cost: ${row['mean_cost_usd']:.3f}/build"
                               f"<br>Mean build time: {row['mean_build_minutes']:.2f} min"
                               f"<br>{row['tasks']} tasks; {row['builds']} valid builds"
                               f"<br>On frontier: {row[flag]}<extra></extra>"),
            ), row=1, col=col)
        fig.update_xaxes(range=[hi, lo], tickprefix="$" if col == 1 else "", tickfont={"size": 9},
                         title={"text": label, "font": {"size": 9}, "standoff": 4},
                         showgrid=True, gridcolor=style.GRID, layer="below traces", row=1, col=col)
        fig.update_yaxes(range=[0, ymax], dtick=0.1 if ymax <= 0.6 else 0.2,
                         tickformat=".0%", tickfont={"size": 9},
                         title={"text": f"MMS@3, mean of {rows[0]['tasks']} tasks",
                                "font": {"size": 9}, "standoff": 4},
                         showgrid=True, gridcolor=style.GRID, layer="below traces", row=1, col=col)
        suffix = "" if col == 1 else str(col)
        fig.add_annotation(x=0, y=1.03, xref=f"x{suffix} domain", yref=f"y{suffix} domain",
                           text=f"<b>{'ab'[col - 1]}</b>", showarrow=False, xanchor="left",
                           yanchor="bottom", font={"size": 11})
    style.apply_paper_layout(fig, width=style.FULL_WIDTH_PX, height=285)
    fig.update_layout(margin={"l": 38, "r": 12, "t": 35, "b": 36}, showlegend=True,
                      legend={"x": 1, "xanchor": "right", "y": 1.10, "yanchor": "bottom",
                              "orientation": "h", "font": {"size": 8}})
    return fig


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sheet", type=Path, default=DEFAULT_SHEET)
    parser.add_argument("--scores", type=Path, default=DEFAULT_SCORES)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--runs-dir", type=Path, action="append", default=[],
                        help="Additional local directory containing <build_id>/<log>; repeatable")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--profile", help="AWS profile; by default use the AWS credential chain")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--bucket", default="<your-bucket>")
    parser.add_argument("--prefix", default="runs")
    parser.add_argument("--workers", type=int, default=8)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--refresh", action="store_true", help="Redownload every selected source file")
    parser.add_argument("--require-three-builds", action="store_true")
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be 1..16")
    cells, tasks = load_scores(args.scores, args.require_three_builds)
    selected = [r for r in select_builds(args.sheet) if r["task"] in tasks]
    # Validate the cohort before any download.
    for model in PRICES:
        for task in tasks:
            n = sum(r["model"] == model and r["task"] == task for r in selected)
            if n != cells[model, task]["k_builds"]:
                raise ValueError(f"{model}/{task}: sheet has {n} builds, score has {cells[model, task]['k_builds']}")
    archive = Archive(args.cache_dir, [*args.runs_dir, *LOCAL_RUNS], profile=args.profile,
                      region=args.region, bucket=args.bucket, prefix=args.prefix,
                      offline=args.offline, refresh=args.refresh)
    print(f"Reading {len(selected)} builds across {len(tasks)} tasks; missing files download automatically.", flush=True)
    builds, sources = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, (build, source_rows) in enumerate(pool.map(lambda b: extract_build(b, archive), selected), 1):
            builds.append(add_coverage(build))
            sources.extend(add_coverage(r) for r in source_rows)
            if n % 10 == 0 or n == len(selected):
                print(f"  {n}/{len(selected)} builds read", flush=True)
    by_task, by_model = aggregate(builds, cells, tasks)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in [("by_source", sources), ("by_build", builds),
                       ("by_task_model", by_task), ("by_model", by_model)]:
        write_csv(args.out_dir / f"{name}.csv", rows)
    incomplete = [{"model": m, "task": t, **cells[m, t]}
                  for m in PRICES for t in tasks if cells[m, t]["k_builds"] < 3]
    manifest = {
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "sheet": str(args.sheet.resolve()), "sheet_sha256": hashlib.sha256(args.sheet.read_bytes()).hexdigest(),
        "scores": str(args.scores.resolve()), "scores_sha256": hashlib.sha256(args.scores.read_bytes()).hexdigest(),
        "optimization_enabled": True, "score_column": "score_pow_k_mean", "tasks": tasks,
        "excluded_tasks": [t for t in data.TASKS if t not in tasks], "builds": len(builds),
        "incomplete_k_cells": incomplete, "sources": list(SOURCES),
        "weighting": "Unique valid builds averaged within task, then equal task weights",
        "time_policy": "Build start to completion; includes optimization/API waits; excludes setup, queue, teardown, final evaluation",
        "cache_policy": "Cached reads replace standard input charges; missing cache counts use standard rate; missing usage excluded",
        "cost_formula": "(cached_input * cached_rate + (input - cached_input) * input_rate + output * output_rate) / 1e6",
        "pricing_date": PRICING_DATE,
        "prices": {m: dict(zip(("input", "output", "cached_input", "source"), rates, strict=True)) for m, rates in PRICES.items()},
        "pricing_units": "USD per million tokens; cache-write premiums and service-tier adjustments excluded",
        "time_methods": sorted({r["time_method"] for r in builds}),
        "records_without_usage": sum(r["records_without_usage"] for r in builds),
        "raw_file_hashes": "SHA-256 of uncompressed bytes in by_source.csv and by_build.csv",
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.out_dir / "caption.txt").write_text(
        f"Mean MMS@3 across {len(tasks)} tasks versus (a) cache-adjusted dollar cost and (b) build "
        "wall-clock minutes, optimization enabled. Both panels use the same unique VALID/VALID* "
        "builds, including valid failed builds; builds are averaged per task, then tasks equally. "
        "Lower cost/time is to the right; green regions lie below frontiers, with straight lines "
        "joining frontier models. Bold labels identify nondominated models. "
        "Build time includes optimization and API waits, excluding setup, queueing, teardown, and "
        "final evaluation. Dollar cost includes AppSci and mini-SWE usage only. "
        "* Missing cache counts are priced at standard input rates; unrecorded usage is ignored. "
        "Cache-write premiums are excluded. † Includes tasks with fewer than three valid builds; "
        "their existing MMS@k scores are retained.\n"
    )
    fig = make_figure(by_model)
    if args.html_only:
        fig.write_html(args.out_dir / "pareto_cost_and_time.html", include_plotlyjs=True)
    else:
        style.save_figure(fig, "pareto_cost_and_time", args.out_dir)
    for row in by_model:
        print(f"{data.MODELS_SHORT[row['model']]:<15} MMS {row['mean_mms_at_k']:.2%}  "
              f"${row['mean_cost_usd']:.4f}/build  {row['mean_build_minutes']:.2f} min/build")
    print(f"Saved plot, CSVs, and provenance to {args.out_dir}")


if __name__ == "__main__":
    main()
