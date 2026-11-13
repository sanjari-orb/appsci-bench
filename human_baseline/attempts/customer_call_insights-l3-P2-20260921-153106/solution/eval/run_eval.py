#!/usr/bin/env python
"""Run eval_samples.jsonl against the deployed Customer-Call Insights artifact.

Each sample is a transcript. The artifact must produce the same correct record no matter HOW the
transcript is packaged in the request, so this harness can wrap every sample in one or more wire
formats and score each format separately. If the scores hold across formats, the agent's
shape-agnostic input handling is working.

Formats (see artifact/insights.py extract_transcript, cases A-H):
  A  plain string in the last user message            (the default / baseline)
  B  content as a list of text blocks
  C  a system message plus the transcript user message
  D  the transcript wrapped in framing prose
  F  a JSON payload (serialized row) with a transcript field, as the message content
  G  a base64 text data-URI attachment block
  H  the transcript at the top level of the body (no messages array)
(Case E -- a bare call_id with no transcript -- carries no transcript to score, so it is not included.)

Scoring, per format:
  - sentiment      : exact enum match
  - field presence : for next_steps / risks / commitments, non-empty exactly when the GT is (catches
                     misses AND over-extraction). List content stays judgment-based (printed for review).

Usage:
  python run_eval.py                     # baseline format A only
  FORMATS=all python run_eval.py         # every format, side-by-side
  FORMATS=A,H,G python run_eval.py       # a chosen subset
  ARTIFACT_URL=http://localhost:PORT FORMATS=all python run_eval.py   # pin the endpoint
The artifact's host port changes on every redeploy, so it is auto-discovered from `docker ps` unless
ARTIFACT_URL is set.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "eval_samples.jsonl"
REPORT = HERE / "eval_results.md"
FIELDS = ("next_steps", "risks", "commitments")


def _b64_datauri(text: str) -> str:
    return "data:text/plain;base64," + base64.b64encode(text.encode()).decode()


# code -> (human label, builder(transcript) -> request body dict)
FORMATS = {
    "A": ("plain string user message", lambda t: {"messages": [{"role": "user", "content": t}]}),
    "B": ("content as text blocks",
          lambda t: {"messages": [{"role": "user", "content": [{"type": "text", "text": t}]}]}),
    "C": ("system + user message",
          lambda t: {"messages": [
              {"role": "system", "content": "You extract CRM insights from a customer call transcript."},
              {"role": "user", "content": t}]}),
    "D": ("framing prose around transcript",
          lambda t: {"messages": [{"role": "user",
                                    "content": "Here is the transcript of a customer call. "
                                               "Please analyze it and produce the record:\n\n" + t}]}),
    "F": ("JSON payload with transcript field",
          lambda t: {"messages": [{"role": "user",
                                    "content": json.dumps({"call_id": "eval", "account_name": "Test",
                                                           "transcript": t})}]}),
    "G": ("base64 text data-URI attachment",
          lambda t: {"messages": [{"role": "user",
                                    "content": [{"type": "input_file", "file_url": _b64_datauri(t)}]}]}),
    "H": ("top-level body transcript field", lambda t: {"transcript": t}),
}


def artifact_base_url() -> str:
    override = os.environ.get("ARTIFACT_URL")
    if override:
        return override.rstrip("/")
    try:
        out = subprocess.check_output(["docker", "ps", "--format", "{{.Ports}}\t{{.Names}}"], text=True)
    except Exception:  # noqa: BLE001
        out = ""
    for line in out.splitlines():
        if "artifact" in line and "8080/tcp" in line:
            m = re.search(r"(?:0\.0\.0\.0|127\.0\.0\.1|\[::\]):(\d+)->8080", line)
            if m:
                return f"http://localhost:{m.group(1)}"
    sys.exit("Could not find the artifact container port. Set ARTIFACT_URL and retry.")


def call_artifact(base: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}/respond", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        outer = json.loads(resp.read())
    return json.loads(outer["choices"][0]["message"]["content"])


def score_one(gt: dict, pred: dict) -> tuple[bool, dict]:
    s_ok = pred.get("sentiment") == gt["sentiment"]
    flags = {f: bool(pred.get(f)) == bool(gt.get(f)) for f in FIELDS}
    return s_ok, flags


def run_format(base: str, fmt: str, rows: list, report: list) -> dict:
    label, builder = FORMATS[fmt]
    n = len(rows)
    sent = pres = 0
    fld = {f: 0 for f in FIELDS}
    report.append(f"\n## Format {fmt} — {label}\n")
    for i, row in enumerate(rows, 1):
        gt = row["ground_truth"]
        try:
            pred = call_artifact(base, builder(row["sample"]))
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            print(f"  [{fmt}] #{i:02d} REQUEST FAILED: {exc}")
            report.append(f"- #{i:02d} REQUEST FAILED: {exc}")
            continue
        s_ok, flags = score_one(gt, pred)
        sent += s_ok
        for f in FIELDS:
            fld[f] += flags[f]
        ap = all(flags.values())
        pres += ap
        if not (s_ok and ap):
            report.append(f"- #{i:02d} sentiment {gt['sentiment']}->{pred.get('sentiment')} "
                          f"({'ok' if s_ok else 'MISMATCH'}); presence "
                          + " ".join(f"{f.split('_')[0]}={'ok' if flags[f] else 'X'}" for f in FIELDS))
            for f in FIELDS:
                if not flags[f]:
                    report.append(f"    - {f}: gt={gt.get(f)}  pred={pred.get(f)}")
    return {"sent": sent, "pres": pres, **fld, "n": n}


def main() -> None:
    base = artifact_base_url()
    raw = os.environ.get("FORMATS", "A").strip()
    selected = list(FORMATS) if raw.lower() == "all" else [x.strip().upper() for x in raw.split(",") if x.strip()]
    unknown = [f for f in selected if f not in FORMATS]
    if unknown:
        sys.exit(f"unknown format(s) {unknown}; choose from {list(FORMATS)} or 'all'")

    rows = [json.loads(l) for l in SAMPLES.read_text().splitlines() if l.strip()]
    print(f"artifact: {base}\nformats : {', '.join(selected)}\nsamples : {len(rows)}\n")

    report = ["# Eval results — input-format robustness", "",
              f"Artifact: `{base}`  |  Samples: {len(rows)}  |  Formats: {', '.join(selected)}", ""]
    results = {}
    for fmt in selected:
        print(f"running format {fmt} ({FORMATS[fmt][0]}) ...")
        results[fmt] = run_format(base, fmt, rows, report)

    n = len(rows)
    header = f"{'fmt':<4}{'description':<34}{'sent':>7}{'commit':>8}{'next':>7}{'risks':>7}{'all':>7}"
    table = [header, "-" * len(header)]
    for fmt in selected:
        r = results[fmt]
        table.append(f"{fmt:<4}{FORMATS[fmt][0]:<34}{r['sent']:>5}/{n}{r['commitments']:>6}/{n}"
                     f"{r['next_steps']:>5}/{n}{r['risks']:>5}/{n}{r['pres']:>5}/{n}")
    summary = "\n".join(table)
    print("\n" + "=" * len(header) + "\n" + summary)
    report[4:4] = ["## Summary", "", "```", summary, "```", ""]
    REPORT.write_text("\n".join(report))
    print(f"\nDetail (per-format mismatches) written to {REPORT}")


if __name__ == "__main__":
    main()
