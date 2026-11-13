# Slides Generator — eval

`run_eval.py` runs the single provided seed example (`slidesgen-example-01`) end to end against a
running artifact and scores the result. It is **independent** of the artifact code — it checks the
contract from the outside.

## What it does

1. Loads the seed request (`PROMPT.md` + attachments) from a sample directory.
2. POSTs it to the artifact's `POST /respond` (attachments sent inline as base64).
3. Collects **both** deliverables: the presentation JSON from the response body, and the generated
   `deck.pptx` / `deck.json` from the artifact's output directory.
4. Runs deterministic contract checks: valid non-empty JSON, hard-rule schema, `.pptx` opens (valid
   OOXML), pptx↔json slide-count mirror, and source-figure presence (do the CSV numbers appear?).
5. Renders the `.pptx` to slide images (LibreOffice, if available) and runs an **LLM-as-judge**
   (Claude Opus 4.8, vision) scoring the rubric: contract compliance, content completeness,
   grounding fidelity (no invented/rounded figures), narrative flow, ready-to-present, and — from the
   images — on-brand look and no overflow/overlap.
6. Prints a scorecard and writes `eval_report.json`.

## Prerequisites

- A **running artifact** (e.g. `make dev-deploy`, which prints its URL/port).
- `ANTHROPIC_API_KEY` in your environment (for the judge): `export ANTHROPIC_API_KEY=...`
- `pip install anthropic` (required). Optional for full checks: `pip install python-pptx PyMuPDF`
  and LibreOffice (`soffice`) on PATH. Without the optional pieces it still scores content dimensions
  and degrades gracefully.
- The sample files. Inside the task container they live at
  `/data/samples/slidesgen-example-01` (auto-detected). On the host, copy them out first:
  `docker cp slides-workspace-1:/data/samples/slidesgen-example-01 ./slidesgen-example-01`

## Usage

```bash
# Simplest: run where the sample and the artifact's /work/out are reachable
python run_eval.py --url http://localhost:8000

# Against a dev-deployed container — pull deck.pptx/json straight out of it for the visual judge:
python run_eval.py --url http://HOST:PORT \
    --sample-dir ./slidesgen-example-01 \
    --from-container appsci-artifact-slides_generator-dev \
    --save-renders ./renders

# Content-only (no rendering / no visual judge), or skip the judge entirely:
python run_eval.py --url http://HOST:PORT --no-render
python run_eval.py --url http://HOST:PORT --no-judge
```

### Key flags
- `--url` artifact base URL (default `http://localhost:8000`).
- `--sample-dir` seed example dir (auto-detects `/data/samples/slidesgen-example-01` or `./sample`).
- `--out-dir` where the artifact wrote the files (default `/work/out`).
- `--from-container NAME` `docker cp` `/work/out` out of a running container (overrides `--out-dir`).
- `--save-renders DIR` save the rendered slide PNGs for your own inspection.
- `--judge-model` (default `claude-opus-4-8`), `--report` (default `eval_report.json`),
  `--no-render`, `--no-judge`, `--timeout`.

## Notes

- The judge is an **LLM opinion**, not ground truth (there is no reference answer for this task).
  Treat the scores as a directional signal; the deterministic checks (schema, mirror, figure
  presence) are the hard gates.
- Every judge run spends API tokens (Opus 4.8, vision over the rendered slides). One run is cheap; be
  mindful if you loop it.
