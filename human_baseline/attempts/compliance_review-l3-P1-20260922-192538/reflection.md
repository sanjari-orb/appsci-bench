# Reflection

Filled in by the baseliner after the attempt ends (submitted or abandoned). Per PROTOCOL.md
section 7: a ~5-sentence note plus a quantitative difficulty score.

## Note (~5 sentences)

Talked to the L3 sim, wrote requirements down, then built a FastAPI `/respond` that checks a contract against one of four policies and dumps `compliant` / `violations` / `needs_review` (Claude Code / Cursor helping). Took an overnight break after elicit and picked it back up the next day. First full held-out score after the artifact existed was **0.657** (violation_set_f1 0.58, rationale 0.65, evidence 0.79, verdict 0.70). Main miss was over-flagging Cap On Liability. Later harden passes are not part of this baseline — scoring stops at that first result.

## Difficulty score (1-5)

3 — schema is clear once L3 walks it; the first score already shows Cap On Liability is easy to over-flag.

## Feedback on task / setup / user simulator

* L3 chat was fine once I asked for the JSON shape and walked the standards one by one.
* Sim said "five standards" but only walked four — left that hanging.
* Policy-selection mapping is still the annoying open question L3 flagged.
* `score_final` without `--attach` refuses if the eval env is already up; keep build/`dev-deploy` down first.
