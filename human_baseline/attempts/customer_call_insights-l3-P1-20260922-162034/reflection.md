# Reflection

Filled in by the baseliner after the attempt ends (submitted or abandoned). Per PROTOCOL.md
section 7: a ~5-sentence note plus a quantitative difficulty score.

## Note (~5 sentences)

Talked to the L3 sim, wrote requirements down, then built a FastAPI `/respond` that pulls next_steps / risks / commitments / sentiment out of one transcript (Claude Code / Cursor helping). Scoring stops at that first artifact — no score-harden loop. The 1h checkpoint was limit=5 and came back at 0.833 with sentiment at 1.0; that is not the official number. Full held-out of the same artifact is **0.741** (50/50): next_steps 0.815, risks 0.739, commitments 0.710, sentiment 0.70. The small checkpoint overstated sentiment. Stopped inside the 4h soft cap.

## Difficulty score (1-5)

3 — schema is straightforward; the 5-sample checkpoint made sentiment look solved.

## Feedback on task / setup / user simulator

* L3 chat was fine once I asked concrete questions about the four fields.
* `--limit 5` is only a checkpoint. The comparable number is the full held-out set.
* `make checkpoint` without `--attach` refuses if the build env is still up; score against the eval env after tearing the build env down.
