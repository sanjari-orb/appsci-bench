# Known Issues — Customer-Call Insights

**Status: runnable end to end on a 50-sample held-out set.** The task validates, its personas load,
its environment builds, and it scores.


## The dataset

`eval_data/` holds 50 hand-authored synthetic calls (`cci-0056` … `cci-0105`). Sentiment runs 24
`NEUTRAL` / 16 `POSITIVE` / 10 `NEGATIVE`; 13 samples have all three lists empty. Tone and content are
decorrelated on purpose — there are calls with a warm customer and a real warning sign, and calls with
an annoyed customer whose account is in no danger — so an artifact cannot infer one field from
another. **An artifact that returns empty lists everywhere and the most common sentiment every time
scores 0.450.**

Two limits worth stating plainly:

- `risks` is empty on 32 of 50 samples, so an always-empty answer still scores well on that field
  alone. This is deliberate: most account calls contain no warning sign, and forcing the share down
  means inventing signals, which trains exactly the fabrication the task exists to catch. The
  discrimination comes from the other three objectives and from the precision half of the risks score.
- The two cells that break the tone/content correlation hold 3 and 2 samples. They are the
  highest-value cases in the set and the thinnest; a later batch should raise them where the scenarios
  support it honestly.

## The matcher threshold question is settled, but the matcher is not calibrated

Item matching for the three list labels goes through a model (see `eval.py`'s module docstring for why a
lexical ratio was measured and rejected). The prompt has not been checked for agreement against a human
matcher on this data. Spot-check a sample of its matching decisions before trusting the per-label numbers.

## There is no grounding judge, and re-adding one is not a one-liner

Fabrication is scored by F1's precision half: an invented item is not in the oracle, so it cannot be
matched, so it lands in the denominator as an extra. That is the intended design (see `TASK_SOP.md`) and
a separate grounding objective would score one mistake twice. The measured baselines below confirm it
works — padding already scores *worse* than returning nothing, with no judge involved.

Recording the trap, because a future author will reach for one anyway: **`make_transcript_judge` is blind
to file content by construction** — it renders an attachment as `[a file was attached: transcript.txt
(txt)]` and nothing more. An earlier revision here shipped a transcript-judge grounding objective whose
prompt said to "judge against the TRANSCRIPT" while handing it that placeholder, so the only call-shaped
thing in its context was the oracle; it was scoring plausibility.

**That is a limitation of the transcript judge, not of this task.** `helpers/files.py`'s
`make_source_grounding_judge` reads the query's attached files for real (the query rides on the
ConversationTurn, and the bytes are materialized by the time scoring runs) and hard-bails on token
overflow. So a genuine source-anchored check *is* available — it is declined on the double-counting
argument above, not because it is impossible. Use that helper if one is ever wanted; do not re-point a
transcript judge at a file.

## The metrics were validated by measurement

Baselines from `tests/tasks/customer_call_insights/test_eval_wiring.py`, which runs the real `spec.yaml`
and the real `eval_data/` through `run_evaluation()` against in-process fake artifacts — no Docker, no
credentials, seconds:

| Artifact strategy | Final score |
|---|---|
| Gold (returns the oracle) | 1.000 |
| Always-empty | 0.450 |
| Always-pad (plausible items everywhere) | 0.439 |
| Correct answer wrapped in prose | 0.000 |

**Padding scoring below emptiness is the load-bearing result** — it is what symmetric F1 is for, and it
is why no separate grounding metric is needed. It still holds, but read the margin: 0.011, where it used
to be wide. Padding now actually *beats* silence on next steps and on promises, because most calls do
have items in those two; the entire ordering rests on warning signs, empty on 32 of 50 calls, where
invention costs 0.27 against 0.64 for staying quiet.

That makes the risks distribution load-bearing for more than its own objective. Lowering the share of
calls with no warning sign — by adding risk-bearing calls, or by "fixing" the emptiness as if it were an
imbalance — narrows this margin and can invert it. An inversion would not mean symmetric F1 broke; it
would mean the set stopped rewarding restraint. Do not compensate by changing the metric.

One trap that test guards: `aggregate.py` **excludes** a `None`-scored objective rather than scoring it 0,
and never pre-normalizes weights, so an objective that drops out silently rescales the final score away
from what `spec.yaml` declares. These metrics return `None` on a malformed oracle, so the path is live.

## Not authored yet

- **Adversarial persona variants** (`user_personas/*_adversarial.yaml`). `order_to_cash` has three, each
  the base persona plus one honestly-voiced mistaken belief and an `adversarial_pressures` list. The
  natural plants here: a stakeholder who is sure "at risk" is one of the sentiment values, or who
  believes the reps' existing notes are usable as labeled training data.
- **`eval_safety.py`.** `order_to_cash` carries diagnostic safety objectives at weight 0.0; this task
  has no equivalent.

## Agent-facing surface (`/sop` and the calls table)

`environment/sop/` holds `kpis.md` and nothing else — the orchestrator mounts it read-only at `/sop`,
hardcoding the path rather than going through `docker-compose.yml`. The recorded calls live in Postgres
(`calls` table), loaded from the bundled fixture by `environment/db/setup.sh`, which then deletes
`/fixture` so the author's label record is unreadable inside the container. There is no `/data` mount.

Everything the agent can reach is agent-visible, so nothing that gives away the field set, the
sentiment vocabulary, the risk taxonomy, or the output shape may go there. The same applies to
`spec.description` and `spec.interface_design`, which survive redaction verbatim into the agent's
system prompt. All of it reaches the agent only by asking the stakeholders. See `TASK_SOP.md`'s
"Information boundary" section for the authoritative list and the per-level split.


## Definition layer was rebuilt; two things to know before editing it

The task definition was reworked so that a passing artifact requires **elicitation**, not just reading.
Two properties are load-bearing and easy to break by accident:

- **The persona gradient is hand-authored.** L3 paraphrases what the team wants captured in business
  words and defers on every specific; L1 owns the judgement rules; L2 owns the wire format. No single
  persona is sufficient. `task_sop.sections[].audience` does **not** enforce any of this — nothing in
  the codebase reads that field, and the user-sim never receives `task_sop` at all. If you add
  material, put it in the right persona's `domain_knowledge`/`known_docs` yourself.
- **No file may claim either failure direction is weighted above the other.** Inventing an item and
  dropping a real one are **equally wrong** by design: a fabricated entry corrodes trust in the whole
  record, and a lost commitment can put the customer relationship on the fence. Symmetric F1 at 0.25 per
  field is therefore the *intended* scoring, not a gap. Earlier revisions of `kpis.md` and the L1 persona
  asserted that fabrication costs more than omission — a false requirement that would have led an agent
  to bias toward empty lists and lose recall for it. Those claims were removed. Do not reintroduce them,
  and do not "fix" the symmetry in the eval layer on the assumption that the docs wanted an asymmetry.
