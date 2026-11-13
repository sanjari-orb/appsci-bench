# Artifact plan — customer_call_insights L3

Attempt: `customer_call_insights-l3-P1-20260922-162034`  
Standalone: `~/Documents/baseline-work/customer_call_insights-l3-P1-20260922-162034/`  
Soft cap: 4h from `started_at` (~16:21 local). Elicit done; remaining budget for build + checkpoints.

Requirements source: L3 transcript → `requirements.md` (gateway-assisted extract).

## Goal

Deployable FastAPI artifact that turns **one** call transcript into the four-field CRM JSON in `requirements.md`.

## Layout

```
artifact/
  Dockerfile
  requirements.txt   # fastapi, uvicorn, httpx (+ optional psycopg for DB smoke)
  server.py          # /health + /respond
```

## Approach (recommended)

**LLM extraction** with a strict JSON schema + groundedness instructions (not pure heuristics).

L3 definitions need language judgment: hedges, dual-list promises, risk traps, empty check-ins. Heuristics alone will miss those.

### Prompt strategy

- System: extract only from the transcript; never invent; never drop real promises/risks; quiet calls → empty lists; a promise appears in **both** `next_steps` and `commitments`; sentiment ∈ `POSITIVE|NEUTRAL|NEGATIVE`; JSON only.
- User: raw transcript (+ short trap reminders).
- Post-process: parse JSON; coerce keys/lists; uppercase sentiment; one repair retry on parse failure.

### Input path

1. Prefer `files[0].file_data` (base64) — eval path.
2. Else if message names a `call_id` and `DATABASE_URL` is set, fetch `calls.transcript` — build smoke.
3. Else use message text if it looks like a transcript.
4. Missing input → still return valid four-key JSON with empty lists + `NEUTRAL` (or clear error content only if unavoidable).

## Phases (remaining)

| Phase | Timebox | Work |
|-------|---------|------|
| A | 20–30m | Implement server: file decode, LLM call, JSON validate |
| B | 15–20m | `make dev-deploy`; smoke on 1–2 DB example calls |
| C | — | **`make checkpoint MARK=1h`** |
| D | 40–60m | Harden prompt (dual-list, hedges, empty check-ins) |
| E | — | **`make checkpoint MARK=2h`** |
| F | 30–40m | Edge cases / parsing robustness |
| G | — | **`make checkpoint MARK=3h30`** |
| H | after stop | **`make final-score`**, reflection, `ended_at`, `make down` |

## MVP vs stretch

**MVP:** `files[]` → LLM → valid four-key JSON; dual-list promises; empty-ok; sentiment enum.  
**Stretch:** DB lookup by `call_id`; repair pass; stronger trap reminders in prompt.

## Scoring

- Mid-run: `make checkpoint MARK=1h|2h|3h30`
- Final: `make final-score` after stop building
