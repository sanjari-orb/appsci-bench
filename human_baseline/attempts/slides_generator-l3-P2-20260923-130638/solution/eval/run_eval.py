#!/usr/bin/env python3
"""Eval for the Slides Generator artifact against the single provided seed example.

What it does, end to end:
  1. Loads the seed request (PROMPT.md + attachments) from a sample directory.
  2. POSTs it to the artifact's /respond endpoint (files sent inline as base64, so the artifact does
     not depend on a shared filesystem for its inputs).
  3. Collects BOTH deliverables: the presentation JSON from the response body, and the generated
     files (deck.pptx / deck.json) from the artifact's output directory.
  4. Runs deterministic contract checks (valid JSON, hard-rule schema, .pptx opens, pptx<->json
     mirror, source-figure presence).
  5. Renders the .pptx to slide images (LibreOffice, if available) and runs an LLM-as-judge
     (Claude Opus 4.8, vision) that scores the output against the quality rubric.
  6. Prints a scorecard and writes a JSON report.

This is an INDEPENDENT eval: it does not import the artifact's own code, so it checks the contract
from the outside.

Requirements to run:
  - A running artifact reachable at --url (default http://localhost:8000).
  - ANTHROPIC_API_KEY in your environment (for the judge). `pip install anthropic`.
  - Optional for structural + visual checks: `pip install python-pptx PyMuPDF` and LibreOffice
    (`soffice`) on PATH. Without them the eval still scores content dimensions and degrades gracefully.

Typical usage:
  # inside the artifact's environment (sample + /work/out both reachable):
  python run_eval.py --url http://localhost:8000

  # pointing at explicit locations:
  python run_eval.py --url http://HOST:PORT \
      --sample-dir ./slidesgen-example-01 --out-dir /work/out --save-renders ./renders
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

MIME = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "csv": "text/csv",
}
JUDGE_MODEL_DEFAULT = "claude-opus-4-8"
CONTENT_EXT = set(MIME) - {"pptx"} | {"pptx"}  # all allowed types


# --------------------------------------------------------------------------- sample loading


def find_sample_dir(explicit: str | None) -> str:
    if explicit:
        if not os.path.isdir(explicit):
            sys.exit(f"--sample-dir not found: {explicit}")
        return explicit
    for cand in ("/data/samples/slidesgen-example-01", "./slidesgen-example-01", "./sample"):
        if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "PROMPT.md")):
            return cand
    sys.exit(
        "Could not locate the sample dir (looked for ./slidesgen-example-01, ./sample, "
        "/data/samples/slidesgen-example-01).\n"
        "The seed example lives inside the workspace container. Copy it to the host first:\n"
        "  docker cp slides-workspace-1:/data/samples/slidesgen-example-01 ./slidesgen-example-01\n"
        "then re-run, or pass --sample-dir <path> (the dir must contain PROMPT.md)."
    )


def load_sample(sample_dir: str):
    with open(os.path.join(sample_dir, "PROMPT.md"), encoding="utf-8") as fh:
        prompt = fh.read()
    files = []
    for fn in sorted(os.listdir(sample_dir)):
        ext = os.path.splitext(fn)[1].lower().lstrip(".")
        if ext in MIME:
            with open(os.path.join(sample_dir, fn), "rb") as fh:
                files.append({"name": fn, "bytes": fh.read(), "mime": MIME[ext]})
    return prompt, files


# --------------------------------------------------------------------------- call the artifact


def call_artifact(url: str, prompt: str, files: list, timeout: int) -> dict:
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "files": [
            {"name": f["name"], "mime": f["mime"],
             "data": base64.b64encode(f["bytes"]).decode("ascii")}
            for f in files
        ],
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/respond",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- source text (grounding)


def _docx_text(path: str) -> str:
    try:
        z = zipfile.ZipFile(path)
        xml = z.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", " ", xml)
    except Exception:
        return ""


def source_text(sample_dir: str) -> str:
    parts = []
    for fn in sorted(os.listdir(sample_dir)):
        p = os.path.join(sample_dir, fn)
        ext = os.path.splitext(fn)[1].lower().lstrip(".")
        if ext == "csv":
            with open(p, encoding="utf-8-sig", errors="replace") as fh:
                parts.append(f"[{fn}]\n{fh.read()}")
        elif ext == "docx":
            parts.append(f"[{fn}]\n{_docx_text(p)}")
    return "\n\n".join(parts)


def csv_figures(sample_dir: str) -> list[str]:
    figs = []
    for fn in os.listdir(sample_dir):
        if fn.lower().endswith(".csv"):
            with open(os.path.join(sample_dir, fn), encoding="utf-8-sig", errors="replace") as fh:
                for row in csv.reader(fh):
                    for cell in row:
                        c = cell.strip()
                        if re.fullmatch(r"\d[\d,]*\.?\d*", c):
                            figs.append(c)
    return sorted(set(figs), key=lambda x: -len(x))


# --------------------------------------------------------------------------- deterministic checks


def validate_schema(doc: dict, valid_layouts: set | None) -> list[str]:
    errs = []
    pres = doc.get("presentation")
    if not isinstance(pres, dict) or not pres:
        return ["presentation object missing or empty"]
    for k in ("title", "theme", "aspectRatio", "metadata", "slides"):
        if k not in pres:
            errs.append(f"presentation missing '{k}'")
    md = pres.get("metadata", {})
    if not isinstance(md, dict) or any(k not in md for k in ("author", "version", "date")):
        errs.append("metadata must have author/version/date")
    slides = pres.get("slides")
    if not isinstance(slides, list) or not slides:
        return errs + ["slides must be a non-empty list"]
    for i, s in enumerate(slides, 1):
        if s.get("slideNumber") != i:
            errs.append(f"slide {i}: slideNumber mismatch ({s.get('slideNumber')})")
        if not s.get("layout"):
            errs.append(f"slide {i}: missing layout")
        elif valid_layouts and s["layout"] not in valid_layouts:
            errs.append(f"slide {i}: layout {s['layout']!r} not a template layout")
        els = s.get("elements") or []
        if not els or not any(_has_content(e) for e in els):
            errs.append(f"slide {i}: no element with real content")
    return errs


def _has_content(e: dict) -> bool:
    t = e.get("type")
    if t == "text":
        return bool(str(e.get("content", "")).strip())
    if t == "bullet_list":
        return any(str(x).strip() for x in e.get("items", []))
    if t == "table":
        return any(any(str(c).strip() for c in r) for r in e.get("rows", []))
    if t == "image":
        return bool(str(e.get("sourceUrl", "")).strip())
    return False


def pptx_checks(pptx_path: str, json_slide_count: int):
    """Returns (ok, info, template_layout_names_or_None)."""
    out = {"exists": os.path.exists(pptx_path)}
    if not out["exists"]:
        return out
    with open(pptx_path, "rb") as fh:
        out["ooxml_magic"] = fh.read(4) == b"PK\x03\x04"
    try:
        from pptx import Presentation
    except Exception:
        out["note"] = "python-pptx not installed; skipped structural check"
        return out
    try:
        prs = Presentation(pptx_path)
        out["slide_count"] = len(list(prs.slides))
        out["mirror_ok"] = out["slide_count"] == json_slide_count
        out["layout_names"] = sorted({lo.name for lo in prs.slide_layouts})
    except Exception as exc:
        out["error"] = f"could not open pptx: {exc}"
    return out


def figure_presence(doc: dict, figures: list[str]):
    # Search only the presentation content, not any base64 pptx blob in the response.
    blob = json.dumps(doc.get("presentation", doc), ensure_ascii=False)
    norm = blob.replace(",", "")
    present, missing = [], []
    for f in figures:
        if f in blob or f.replace(",", "") in norm:
            present.append(f)
        else:
            missing.append(f)
    return present, missing


# --------------------------------------------------------------------------- render for VLM judge


def render_pptx_to_pngs(pptx_path: str, dpi: int = 110) -> list[bytes]:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp,
                            pptx_path], check=True, capture_output=True, timeout=180)
            pdfs = [f for f in os.listdir(tmp) if f.endswith(".pdf")]
            if not pdfs:
                return []
            import fitz
            d = fitz.open(os.path.join(tmp, pdfs[0]))
            imgs = [d.load_page(p).get_pixmap(dpi=dpi).tobytes("png") for p in range(d.page_count)]
            d.close()
            return imgs
    except Exception as exc:
        print(f"  [render skipped: {exc}]")
        return []


# --------------------------------------------------------------------------- LLM-as-judge


JUDGE_SYSTEM = """You are a demanding evaluator of AI-generated slide decks, judging to the strictest
professional standard (assume the customer will only present decks that are truly ready).

You are given: the USER'S REQUEST, the SOURCE MATERIALS the deck had to be built from, the deck's
STRUCTURED JSON, and (if available) RENDERED IMAGES of the slides. Score the deck on this rubric,
each 1-5 (5 = excellent, 1 = unacceptable):

- contract_compliance: valid non-empty presentation JSON; every slide has slideNumber, a layout, and
  at least one real-content element; JSON order mirrors the deck.
- content_completeness: says everything the request asked for; nothing important dropped; not padded
  with material the sources didn't provide.
- grounding_fidelity: every fact/figure traces to the sources; numbers/dates/currency are EXACT (not
  rounded, invented, or recomputed wrongly). List any invented or incorrect figures explicitly.
- narrative_flow: reads like a coherent story, in a sensible order, for the stated audience.
- ready_to_present: a person could open and present it as-is, no cleanup pass.
- visual_on_brand: (images only) uses the template's colours/fonts/feel; looks native, not generic.
- visual_no_overflow: (images only) nothing overflows, overlaps, or spills off the slide edge.

Set visual_* to null if you were given no images. Be specific and critical; cite concrete slides."""

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dimension_scores", "overall_score_100", "verdict", "rationale"],
    "properties": {
        "dimension_scores": {
            "type": "object",
            "additionalProperties": False,
            "required": ["contract_compliance", "content_completeness", "grounding_fidelity",
                         "narrative_flow", "ready_to_present", "visual_on_brand",
                         "visual_no_overflow"],
            "properties": {
                "contract_compliance": {"type": ["integer", "null"]},
                "content_completeness": {"type": ["integer", "null"]},
                "grounding_fidelity": {"type": ["integer", "null"]},
                "narrative_flow": {"type": ["integer", "null"]},
                "ready_to_present": {"type": ["integer", "null"]},
                "visual_on_brand": {"type": ["integer", "null"]},
                "visual_no_overflow": {"type": ["integer", "null"]},
            },
        },
        "invented_or_wrong_figures": {"type": "array", "items": {"type": "string"}},
        "dropped_points": {"type": "array", "items": {"type": "string"}},
        "visual_issues": {"type": "array", "items": {"type": "string"}},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "overall_score_100": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["pass", "borderline", "fail"]},
        "rationale": {"type": "string"},
    },
}


def run_judge(model: str, request: str, sources: str, doc: dict, images: list[bytes]) -> dict:
    from anthropic import Anthropic

    client = Anthropic(max_retries=3, timeout=600.0)
    content = [
        {"type": "text", "text": f"USER'S REQUEST:\n{request}"},
        {"type": "text", "text": f"SOURCE MATERIALS:\n{sources}"},
        {"type": "text", "text": "DECK STRUCTURED JSON:\n" + json.dumps(doc, ensure_ascii=False,
                                                                        indent=2)},
    ]
    if images:
        content.append({"type": "text", "text": f"RENDERED SLIDES ({len(images)}), in order:"})
        for i, img in enumerate(images, 1):
            content.append({"type": "text", "text": f"Slide {i}:"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(img).decode()}})
    else:
        content.append({"type": "text", "text": "(No rendered images provided; set visual_* to null.)"})
    content.append({"type": "text", "text": "Call the `score` tool with your evaluation."})

    tool = {"name": "score", "description": "Return the rubric evaluation.",
            "input_schema": JUDGE_SCHEMA}
    kwargs = dict(model=model, max_tokens=8000, system=JUDGE_SYSTEM,
                  tools=[tool], tool_choice={"type": "auto"},
                  messages=[{"role": "user", "content": content}])
    try:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "high"}
        with client.messages.stream(**kwargs) as st:
            msg = st.get_final_message()
    except TypeError:
        kwargs.pop("thinking", None); kwargs.pop("output_config", None)
        with client.messages.stream(**kwargs) as st:
            msg = st.get_final_message()
    for b in msg.content:
        if getattr(b, "type", None) == "tool_use" and b.name == "score":
            return dict(b.input)
    raise RuntimeError("judge did not return a score")


# --------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="Eval the slides generator on the seed example.")
    ap.add_argument("--url", default="http://localhost:8000", help="artifact base URL")
    ap.add_argument("--sample-dir", default=None, help="dir with PROMPT.md + attachments")
    ap.add_argument("--out-dir", default="/work/out", help="where the artifact wrote deck.pptx/json")
    ap.add_argument("--from-container", default=None,
                    help="docker container name to copy /work/out from (overrides --out-dir)")
    ap.add_argument("--judge-model", default=JUDGE_MODEL_DEFAULT)
    ap.add_argument("--report", default="eval_report.json")
    ap.add_argument("--save-renders", default=None, help="dir to save rendered slide PNGs")
    ap.add_argument("--no-render", action="store_true", help="skip rendering + visual judging")
    ap.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    sample_dir = find_sample_dir(args.sample_dir)
    prompt, files = load_sample(sample_dir)
    print(f"Sample: {sample_dir}  ({len(files)} attachment(s): {[f['name'] for f in files]})")

    print(f"Calling artifact at {args.url}/respond ...")
    try:
        resp = call_artifact(args.url, prompt, files, args.timeout)
    except Exception as exc:
        sys.exit(f"Could not reach the artifact at {args.url}: {exc}\n"
                 f"Is it running? (dev-deploy prints the URL/port.)")
    content = resp["choices"][0]["message"]["content"]

    # Optionally pull the output files out of a running container.
    out_dir = args.out_dir
    if args.from_container:
        out_dir = tempfile.mkdtemp(prefix="slidesgen_out_")
        try:
            subprocess.run(["docker", "cp", f"{args.from_container}:/work/out/.", out_dir],
                           check=True, capture_output=True)
            print(f"Copied /work/out from container {args.from_container} -> {out_dir}")
        except Exception as exc:
            print(f"  [could not docker cp from {args.from_container}: {exc}]")

    report = {"sample_dir": sample_dir, "url": args.url, "report_path": args.report, "checks": {}}

    # Parse the response body (should be the presentation JSON).
    try:
        doc = json.loads(content)
    except Exception as exc:
        sys.exit(f"FAIL: response content is not valid JSON: {exc}\n---\n{content[:500]}")
    if doc.get("error") or not doc.get("presentation"):
        print("Artifact returned an error / empty presentation:")
        print(json.dumps(doc, indent=2)[:1000])
        report["checks"]["artifact_error"] = doc.get("error") or "empty presentation"
        _write(report, args.report)
        sys.exit("FAIL: no deck produced.")

    slides = doc["presentation"]["slides"]
    json_n = len(slides)
    print(f"Response OK: presentation JSON with {json_n} slide(s).")

    # Files on disk. If the .pptx isn't reachable on disk but the response carried pptx_base64,
    # decode it to a temp file so the structural + visual checks can still run.
    pptx_path = os.path.join(out_dir, "deck.pptx")
    json_path = os.path.join(out_dir, "deck.json")
    if not os.path.exists(pptx_path) and doc.get("pptx_base64"):
        try:
            tmp_pptx = os.path.join(tempfile.mkdtemp(prefix="slidesgen_pptx_"),
                                    doc.get("pptx_filename", "deck.pptx"))
            with open(tmp_pptx, "wb") as fh:
                fh.write(base64.b64decode(doc["pptx_base64"]))
            pptx_path = tmp_pptx
            report.setdefault("checks", {})["pptx_source"] = "response pptx_base64 (disk file absent)"
            print(f"  deck.pptx taken from response pptx_base64 -> {pptx_path}")
        except Exception as exc:
            print(f"  [could not decode pptx_base64: {exc}]")
    pcheck = pptx_checks(pptx_path, json_n)
    report["checks"]["files"] = {
        "deck_pptx": pcheck,
        "deck_json_exists": os.path.exists(json_path),
        "out_dir": out_dir,
    }
    if not pcheck.get("exists"):
        print(f"  [note: deck.pptx not found in {out_dir}; visual checks limited. "
              f"Use --from-container <name> or --out-dir to point at the artifact's /work/out.]")
    valid_layouts = set(pcheck.get("layout_names") or []) or None

    # Contract / schema.
    schema_errors = validate_schema(doc, valid_layouts)
    report["checks"]["schema_errors"] = schema_errors

    # Grounding: source figure presence.
    figs = csv_figures(sample_dir)
    present, missing = figure_presence(doc, figs)
    report["checks"]["figures"] = {"total": len(figs), "present": present, "missing": missing}

    both_delivered = bool(doc.get("presentation")) and pcheck.get("exists", False)
    report["checks"]["both_deliverables_present"] = both_delivered

    # Render + judge.
    images = []
    if not args.no_render and pcheck.get("exists"):
        print("Rendering slides for the visual judge ...")
        images = render_pptx_to_pngs(pptx_path)
        print(f"  rendered {len(images)} image(s)")
        if args.save_renders and images:
            os.makedirs(args.save_renders, exist_ok=True)
            for i, im in enumerate(images, 1):
                with open(os.path.join(args.save_renders, f"slide{i}.png"), "wb") as fh:
                    fh.write(im)

    judge = None
    if not args.no_judge:
        print(f"Running LLM-as-judge ({args.judge_model}) ...")
        judge_doc = {k: v for k, v in doc.items() if k not in ("pptx_base64", "pptx_filename")}
        try:
            judge = run_judge(args.judge_model, prompt, source_text(sample_dir), judge_doc, images)
        except Exception as exc:
            print(f"  judge failed: {exc}")
            report["checks"]["judge_error"] = str(exc)
    report["judge"] = judge

    _write(report, args.report)
    print_scorecard(report)


def _write(report: dict, path: str):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


def print_scorecard(report: dict):
    print("\n" + "=" * 60)
    print("SCORECARD")
    print("=" * 60)
    c = report["checks"]
    pptx = c.get("files", {}).get("deck_pptx", {})
    print(f"deck.pptx present : {pptx.get('exists')}  (valid OOXML: {pptx.get('ooxml_magic')})")
    print(f"deck.json present : {c.get('files', {}).get('deck_json_exists')}")
    print(f"both deliverables : {c.get('both_deliverables_present')}")
    if "slide_count" in pptx:
        print(f"pptx<->json mirror: {pptx.get('mirror_ok')} "
              f"(pptx={pptx.get('slide_count')})")
    se = c.get("schema_errors", [])
    print(f"schema hard-rules : {'PASS' if not se else 'FAIL -> ' + '; '.join(se)}")
    fg = c.get("figures", {})
    print(f"source figures    : {len(fg.get('present', []))}/{fg.get('total')} present"
          + (f"; MISSING {fg['missing']}" if fg.get("missing") else ""))
    j = report.get("judge")
    if j:
        print("-" * 60)
        print(f"JUDGE verdict     : {j.get('verdict','?').upper()}   "
              f"overall {j.get('overall_score_100','?')}/100")
        for k, v in (j.get("dimension_scores") or {}).items():
            print(f"  {k:22s}: {v if v is not None else 'n/a'}")
        if j.get("invented_or_wrong_figures"):
            print(f"  ! invented/wrong figures: {j['invented_or_wrong_figures']}")
        if j.get("dropped_points"):
            print(f"  ! dropped points: {j['dropped_points']}")
        if j.get("visual_issues"):
            print(f"  ! visual issues: {j['visual_issues']}")
        print(f"  rationale: {j.get('rationale','')[:600]}")
    print("=" * 60)
    print(f"Full report written to: {report.get('report_path', 'eval_report.json')}")


if __name__ == "__main__":
    main()
