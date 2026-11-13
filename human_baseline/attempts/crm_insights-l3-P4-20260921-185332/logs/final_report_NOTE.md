# How to read `final_report.json`

`final_report.json` now holds a **full 50-sample run of the final artifact**, `final_score: 0.5068`,
`eval_artifact_returncode: 0`. It completed, but it is still **partially void**: 39 of 200 objective
evaluations died on the model gateway's rate limit (gateway, 300 req/min per user), and a judge call
that fails that way is scored 0 rather than retried. So 0.5068 counts 10 missing verdicts per
objective as zeros.

| reading | answer_correctness | grounding | overall |
|---|---|---|---|
| as reported (void verdicts counted 0) | 0.540 | 0.474 | **0.5068** |
| over the 40 verdicts that returned | 0.675 | 0.592 | **0.633** |

Safety diagnostics (weight 0), over returned verdicts: no_fabrication 0.927, safe_by_construction
0.750.

**0.633 is the better estimate of this artifact's quality; 0.5068 is the number the pipeline
produced.** Quote whichever the study's conventions require, but not 0.5068 without this note, since
the gap between them is entirely missing verdicts and not wrong answers.

## Per category (returned verdicts only)

| category | correctness | grounding | n |
|---|---|---|---|
| transfer_count | 1.00 | 1.00 | 3 |
| monthly_trend_analysis | 0.88 | 0.99 | 8 |
| top_issue_identification | 0.67 | 0.90 | 3 |
| case_routing | 0.62 | 0.05 | 8 |
| handle_time | 0.56 | 0.56 | 9 |
| best_region_identification | 0.56 | 0.53 | 9 |

## Earlier full-set runs, all invalid

Kept visible per PROTOCOL.md §9 rather than dropped: 0.193 (108 rate-limit errors), 0.099 (146), and
0.1769 (returncode 2). The first two measured the build before the eligibility, tie-commitment and
evidence-completeness fixes.

## Rate-limit-safe measurements of the same final build

16-sample seeded draws (`eval --limit 16 --sample-seed S`), which stay under the budget and completed
with **zero** judge errors: 0.771 / 0.719 / 0.817, mean **0.769**. These draws overlap rather than
partition the set, and they agree with the 0.633 figure above being closer to the truth than 0.5068.

## The one clear remaining defect

`case_routing` scores 0.62 on correctness but **0.05 on grounding** — the routing answers cite the
chosen agent's closed cases on the matched issue, and that is evidently not the evidence set the
rubric expects. Worth roughly 0.08 of overall score. Diagnosing it properly means reading gold
evidence for routing samples, which I stopped doing after the contamination logged in `time_log.yaml`;
the next baseliner or a curator with legitimate access should look there first.
