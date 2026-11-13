"""CLI: (re)generate the self-contained HTML report from one or more persisted eval runs.

    python -m appsci_bench.eval.visualize <run_dir | report.json | parent_dir> [more...] [-o out.html]

Each eval run drops `report.json` + `report.html` into its transcript store (see
`eval_system._write_report_artifacts`). This rebuilds the HTML from `report.json` + conversations --
handy after a template change, or to combine several tasks into ONE tabbed report:
  - a run dir (has report.json)            -> that run
  - a parent dir of run dirs (*/report.json) -> one tab per task
  - multiple paths                          -> one tab per task
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from .datatypes import EvalReport
from .report_html import render_html, render_html_multi


def _report_jsons(path: pathlib.Path) -> list[pathlib.Path]:
    """Resolve a CLI path to the report.json file(s) it denotes."""
    if path.is_file():
        return [path]
    if (path / "report.json").is_file():          # a single run dir
        return [path / "report.json"]
    return sorted(path.glob("*/report.json"))      # a parent dir of run dirs


def _load(report_json: pathlib.Path) -> EvalReport:
    report = EvalReport.from_dict(json.loads(report_json.read_text()))
    # The recorded store_root may be stale/foreign; if its conversations aren't there, fall back to
    # the report.json's own directory (conversations live alongside it).
    if not (pathlib.Path(report.store_root or "") / "conversations").is_dir():
        report.store_root = str(report_json.parent)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="appsci_bench.eval.visualize",
        description="Render a single-file HTML report (one tab per task) from eval run(s).")
    parser.add_argument("paths", nargs="+",
                        help="run dir(s) with report.json, a parent dir of runs, or report.json file(s)")
    parser.add_argument("-o", "--out", default=None, help="output HTML path")
    args = parser.parse_args(argv)

    report_jsons: list[pathlib.Path] = []
    for p in args.paths:
        found = _report_jsons(pathlib.Path(p))
        if not found:
            print(f"warning: no report.json under {p}", file=sys.stderr)
        report_jsons.extend(found)
    if not report_jsons:
        print("error: no report.json found in the given path(s)", file=sys.stderr)
        return 1

    reports = [_load(rj) for rj in report_jsons]
    html = render_html(reports[0]) if len(reports) == 1 else render_html_multi([(r, None) for r in reports])

    if args.out:
        out = pathlib.Path(args.out)
    elif len(report_jsons) == 1:
        out = report_jsons[0].parent / "report.html"
    else:
        out = pathlib.Path(args.paths[0]) / "report.html"
    out.write_text(html)
    print(f"wrote {out} ({len(reports)} task(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
