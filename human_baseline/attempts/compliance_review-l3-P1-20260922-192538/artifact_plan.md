# Artifact plan — compliance_review L3

Attempt: `compliance_review-l3-P1-20260922-192538`  
Standalone: `~/Documents/baseline-work/compliance_review-l3-P1-20260922-192538/`  
Soft cap: 4h from `started_at` (2026-09-22T19:25:39+02:00). Elicit done (~19:35–19:52 local); remaining budget for build + checkpoints.

Requirements source: L3 transcript → `requirements.md` (dialogue only; no TASK_SOP / eval gold).

## Goal

Deployable FastAPI artifact: one customer-facing document (+ applicable policy) → single JSON object with `compliant`, `violations`, `needs_review` per `requirements.md`.

## Layout

```
artifact/
  Dockerfile
  requirements.txt   # fastapi, uvicorn, (+ LLM client deps as needed)
  server.py          # /health + /respond
```

## Approach (recommended)

**LLM (or hybrid) policy check** against the applicable standard’s required / prohibited / needs_review categories — not brittle regex-only for every clause.

Needs language judgment (uncapped liability vs a real cap, MFN phrasing, etc.) plus exact span quotes for `must_not_have`.

### Prompt / logic strategy

- Load the **applicable** policy rule set (four standards in requirements).
- For each rule: `must_have` → violate if absent; `must_not_have` → violate if present **with exact spans**; needs_review categories → `needs_review` only if present.
- Emit **only** the three top-level keys; empty lists when clean.
- `compliant = (violations == [])` — ignore needs_review for the boolean.
- Never invent categories outside the active policy; never report must_not_have without spans.

### Input path (to discover in build env — L3 punted formats)

1. Prefer eval-style file attachment if present (`files[]` / base64), else message body / title / id lookup if the env provides a contracts store.
2. Resolve **which policy** applies (request field, filename/title heuristic, or env metadata — TBD once we inspect the running task-env; do not invent).
3. Missing document → still return valid schema (`compliant: true`, empty lists) or a clear schema-safe empty result so the queue doesn’t choke.

## Phases (remaining)

| Phase | Timebox | Work |
|-------|---------|------|
| A | 25–40m | Implement `/respond`: resolve doc + policy, run check, JSON validate |
| B | 15–20m | `make dev-deploy`; smoke on 1–2 build-env docs |
| C | — | **`make checkpoint MARK=1h`** (after switching to eval env + `--attach`) |
| D | 40–60m | Harden spans, must_have absences, needs_review vs violations |
| E | — | **`make checkpoint MARK=2h`** |
| F | as budget | Borderline language / policy selection; optional sim example review |
| G | — | **`make checkpoint MARK=3h30`** if still iterating |
| H | after stop | **`make final-score`** (or `eval --attach` full set), reflection, `ended_at`, `make down` |

## Risks

- Policy-selection ambiguity (called out by L3) — wrong policy ⇒ wrong flags.
- Over-flagging style/noise vs under-flagging real must_haves.
- LIMIT=5 checkpoints can mislead (lesson from CCI) — trust full held-out.

## Non-goals this pass

- Auto-approve / auto-reject.
- Editorial style nitpicks.
- Inventing a fifth standard (sim named four lists despite saying “five”).
