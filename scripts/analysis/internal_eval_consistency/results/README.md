# Internal vs external eval results — Claude Opus 4.8

One directory per task, seven tasks. Each holds the two views of the same checkpoint sweep:

| View | Question it answers |
|---|---|
| `did_it_improve/` | Does the **real** held-out eval score go up across the build's deploys? |
| `self_grade_vs_real/` | Does the agent's **own** internal eval predict that real score? |

`claude-opus-4-8/SUMMARY.md` and `summary.csv` gather every build's numbers into one table, with the
per-task and cross-task averages quoted in the paper.

Only the latest valid result per task is kept here. Earlier passes over the same tasks were dropped
rather than archived, because a stale bundle that still looks plottable is the easier mistake to make
than a missing one.

## What each file is

`did_it_improve/`
- `curve__<run_id>.png` — one build's real eval score against deploy ordinal
- `all_runs__curve.png` — every build on shared axes
- `checkpoints.csv`, `stats.json`, `REPORT.md` — the scorer's own tabular output

`self_grade_vs_real/`
- `internal_vs_external.png` — both evals per build, with Kendall tau-b, directions agreed, and regret
- `decisions.png` — whether each accept/reject the loop made agreed with the real eval
- `loop_decisions.png` — the verdicts the optimize loop recorded for itself
- `cases__<build>.png` — per-case internal scores, which is where a saturated internal suite shows up
- `points.csv`, `stats.json` — the underlying numbers, including Pearson and Spearman

## Method

Programs are deduplicated by content hash before correlating, so a roll-back that redeploys identical
bytes counts as one candidate rather than inflating the correlation. Each build writes its own internal
exam, so internal levels do not compare across builds; every average is over per-build statistics and
nothing is pooled.

| Tasks | Scored |
|---|---|
| airline_rebooking, compliance_review, customer_call_insights, order_to_cash | 2026-09-16 |
| crm_insights, slides_generator | 2026-09-20 |
| banking_support | 2026-09-22 |
| slides_generator b1 (`b1_l3_20260917_105454`), rescored | 2026-11-05 |

**`slides_generator` b1 was rescored on 2026-11-05.** Its September held-out scores (0.19-0.21 on
every checkpoint) came from an evaluator that dropped every deck the artifact labelled with a MIME
type (fixed in #237). Its 8 captures were rescored at `4a229509`, full block, 3 repeats,
and now read 0.743-0.767. The b0 and b2 records are unchanged.

## Caveats that matter when reading these

**`compliance_review` is scored as the task currently stands.** Its `evidence_span_overlap` metric
returns nothing on all 50 cases, so the headline score for that task rests on the remaining metrics.
The task was deliberately not modified to fix this, so these numbers describe the benchmark as it is
rather than as it ought to be.

**`banking_support` covers two of its three builds.** The two builds shown were both launched as `b0`,
on 09-20 and 09-21, and are labelled `b0-0920` and `b0-0921` to tell them apart. The third build
(`b1_l3_20260921_085532`) was dropped: its programs send the shell tool `grep -r` over the whole
filesystem, which never finishes, and at eight eval lanes those runaway processes saturated the scoring
box until its captures hit the scorer's one-hour limit. Byte-identical code timed out once and scored
once, so those timeouts measured the box rather than the program.

**`banking_support` is one score per checkpoint, scored at eval-lane width 8** over the full 97 cases.
The 09-20 build also has second and third repeats in S3 from an earlier three-repeat sweep. They were
left out here, so both builds are measured the same way. Its external noise comes instead from one
program that was deployed four times, scoring 6, 9, 10 and 7 cases. That spread is wider than the gap
between any two distinct programs, so on banking the external eval cannot tell the loop's candidates
apart.

**Some scores are stamped `scoring_tree_dirty`**, meaning the scoring machine had uncommitted files.
For `banking_support` this was checked: the only differences from the recorded
`scoring_commit` were deleted research notes and untracked scratch files, with no tracked code changed.
The other tasks' dirty stamps were not individually audited; each record's `scoring_commit` names the
code it ran.
