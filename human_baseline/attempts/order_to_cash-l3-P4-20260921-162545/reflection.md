# Reflection

Filled in by the baseliner after the attempt ends (submitted or abandoned). Per PROTOCOL.md
section 7: a ~5-sentence note plus a quantitative difficulty score.

## Note (~5 sentences)

* Build environment only — I never opened the eval fixtures, labels or scoring code.
* The spec came from asking the simulator, so I did almost no guessing.
* I checked the rules by rewriting them in SQL and replaying all 662 build invoices, plus unit tests for the four fault types the build data lacks.
* Oracle score 0.9904 across 60/60.
* Claude Code the whole way, well inside the 4h cap; times in `logs/time_log.yaml`.

## Difficulty score (1-5)

2

## Feedback on task / setup / user simulator

* The simulator was genuinely useful here — ask it flatly for the output contract and it gives exact
  key names, the closed fault set and the tie-break order.
* The build data only holds two of the six fault types, so plan on unit tests with made-up records for
  the other four. Easy to misread the absence as "my rules never fire".
* A sequential-only test suite passes this task while shipping a broken artifact; querying the
  artifact concurrently would separate attempts.
* The environment vanished mid-attempt, cause unknown — no time lost, but scoring is blocked until
  it's back up.
* The latency KPI doesn't discriminate: ~2 ms against a 2 s target.
