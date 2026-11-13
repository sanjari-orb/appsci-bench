# Artifact plan — slides_generator L3 (P1)
# Attempt: slides_generator-l3-P1-20260909-173926
#
# Source chain (protocol order):
#   1. logs/transcript.md  (L3 user-sim chat)
#   2. requirements.md     (extracted from transcript — authoritative product requirements)
#   3. this plan           (implementation approach to satisfy requirements.md)
#   4. standalone artifact/ (code that fulfills the requirements)
#   5. make checkpoint / make final-score (Oracle scores the artifact)
#
# Do not invent product requirements here; if the plan conflicts with requirements.md, requirements win.
# Agent-facing sample (/data/samples) + /sop/kpis.md are for grounding/implementation, not new requirements.

## Goal
HTTP service (`GET /health`, `POST /respond`) that turns brief + attachments + `template.pptx`
into **both**: editable `.pptx` + structured `presentation` JSON (same turn).

## /respond packaging (v1 pick) — matches appsci session API

Request (harness sends):
```json
{
  "messages": [{"role": "user", "content": "..."}],
  "files": [{"file_type": "csv", "file_name": "metrics.csv", "file_data": "<base64>"}]
}
```

Response (**do not** stuff the pptx into `content` alone). Use the three channels:

```json
{
  "choices": [{"message": {"role": "assistant", "content": "Deck ready. warnings: [...]"}}],
  "json": "{\"presentation\": { ... L3 shape ... }}",
  "files": [{"file_type": "pptx", "file_name": "deck.pptx", "file_data": "<base64>"}]
}
```

Rules:
- `json` must be a **JSON-encoded string** (not a nested object), containing the `presentation` object.
- Produced `.pptx` goes only in top-level `files[]` (`file_type` / `file_name` / `file_data` base64).
- `choices[].message.content` can be a short status + warnings (attachment failures / gaps). Prefer failing loudly over a silent wrong deck.
- On hard failure: return an error payload / empty files rather than a fake polished deck.

This matches `docs/artifact_agent_api.md` (agent/harness contract).

## Pipeline (build time vs request time)

**Build/design (now):** inspect `/data/samples/slidesgen-example-01/` (PROMPT + docs + template).
Implement against that shape.

**Per `/respond` request:**
1. Parse `messages` → latest user text + any attached files in the request body (however the harness sends them — usually text + file metadata/base64; mirror sample structure).
2. Identify `template.pptx` vs content attachments (template = look only).
3. Extract text/tables from content files; **surface failures** in `warnings` / error.
4. Plan outline (audience/tone + slide-count default by deck type).
5. Fill **template layouts/placeholders** (python-pptx) — do not invent free-floating boxes that ignore theme.
6. Build matching `presentation` JSON (same order as slides).
7. Return both.

Prefer an LLM call inside the artifact for outline+copy **grounded only on extracted sources**, with explicit “flag gaps / do not invent numbers” instructions. If no API key in container, fall back to deterministic extract→outline for MVP then add LLM.

## File layout under `artifact/`

```
artifact/
  Dockerfile
  requirements.txt      # fastapi, uvicorn, python-pptx, pillow, pydantic, httpx, optional: openai/anthropic, pypdf, python-docx, openpyxl
  server.py             # /health, /respond orchestration
  pipeline/
    parse_request.py    # messages → PromptRequest
    extract.py          # per-format extractors + failure report
    plan.py             # outline + slide count + audience heuristic
    render_pptx.py      # template → filled pptx
    to_json.py          # presentation object matching pptx order
    respond_format.py   # pack choices + json(string) + files[] per session API
  tests/
    test_smoke_sample.py  # optional local smoke using copied sample bytes
```

## MVP vs stretch (remaining ~3h work budget)

**MVP (must ship for scoring):**
- Parse text prompt + attachments (at least: txt/md/csv + pptx template; docx/pdf best-effort).
- Fail loudly on unreadable attaches.
- Render editable pptx from template placeholders.
- Return presentation JSON + pptx together.
- Content fidelity over polish; no invented figures.

**Stretch if time:**
- Stronger LLM planning; better layout mapping; tables/images; richer warnings taxonomy.

## Timeboxes (from resume — ~3h13 left of soft cap)

| Block | ~min | Work |
|---|---|---|
| A | 20 | Inspect sample + template layouts; finalize I/O packing |
| B | 50 | extract + plan + render_pptx skeleton; `/respond` returns dual output on sample |
| — | — | **`make checkpoint MARK=1h`** when ~1h total work elapsed (~13–15 min into this session if continuous from resume… adjust: total work so far was ~47m, so 1h mark is ~13m from resume) |
| C | 50 | Harden fidelity; csv/docx; warnings; smoke via `make dev-deploy` |
| — | — | **`make checkpoint MARK=2h`** (~1h13 from resume) |
| D | 50 | Polish template adherence; gap flags; fix scoring failures from checkpoints |
| — | — | **`make checkpoint MARK=3h30`** |
| E | 20 | Freeze artifact; optional `MARK=end` |
| After soft cap | — | **`make final-score`** (eval time; out of solve clock), reflection, `ended_at`, `make down` |

Adjust marks if you take more breaks — log them.

## Scoring (yes — you score the artifact)

Two layers:

1. **During solve:** `make checkpoint MARK=1h|2h|3h30`  
   - Deploys current `artifact/` and scores a **small sample** (`LIMIT=5` default).  
   - Appends to `logs/score_checkpoints.jsonl`.  
   - Use to steer the remaining time.

2. **After you stop building:** `make final-score`  
   - Full held-out eval set — **the number that counts** vs agents.  
   - Writes `logs/final_report.json` (+ html).  
   - Protocol: scoring logistics are outside the 4h solve focus, but still do it once frozen.

Commands (from `human_baseline/template`):
```bash
make dev-deploy          # while coding
make checkpoint MARK=1h
make checkpoint MARK=2h
make checkpoint MARK=3h30
make final-score
make down
```

## Immediate next actions
1. Env is already **up** (build phase) after today’s `make up`.
2. Open Claude Code at standalone dir; implement MVP under `artifact/`.
3. `make dev-deploy` → curl `/respond` with sample prompt.
4. Hit checkpoints on schedule; then final-score.
