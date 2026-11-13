# Paper figures

This README covers the plots committed on `mpk/more-paper-plots`, not local untracked
plotting experiments. The failure-pie workflow also lists its required supporting analysis
scripts (see the checkout limitation below). The core Plotly scripts read the results tables in `data/` and write PDF (for
LaTeX), SVG, PNG (288 dpi), and interactive HTML copies to `figures/`. Additional committed
plots with different inputs or outputs are documented separately below. Generated figures
are not committed.

## Setup

```bash
uv pip install --python .venv/bin/python -e '.[plots]'
```

PDF, SVG, and PNG export uses kaleido, which drives a local Chrome or Chromium. If none is installed,
run `.venv/bin/plotly_get_chrome` once.

## Figures

The core score charts show MMS@3, mean minimum score @ 3 (called score^3 in the sheet), in percent: per case,
the lowest score among 3 builds (one trial from each), averaged over cases, then over the ways of
picking one trial per build. Among these core charts, all but the optimization ablation use the optimization-enabled arm;
the ablation compares it with the disabled arm and writes to `figures/optimization/`.

| Script | Output | Size | What it shows |
|---|---|---|---|
| `plot_benchmark_overview.py` | `benchmark_overview_list.*`, `benchmark_overview_wheel.*` | full width | The five things the benchmark measures, as a list or a hub-and-spoke wheel (no data) |
| | `benchmark_overview_wheel_third.*` | third | The wheel with names only |
| `plot_model_average.py` | `model_average.*` | half | Mean MMS@3 per model over the tasks, no whisker |
| | `model_average_third.*` | third | Same, short model names |
| `plot_model_radar.py` | `model_radar.*` | half | All models on one radar, one spoke per task |
| | `model_radar_third.*` | third | Same, compact hand-drawn legend, unlabeled rings |
| `plot_model_by_task.py` | `model_by_task.*` | full width | Tasks on the x axis, one bar per model in each group |
| `plot_optimization_ablation.py` | `optimization/opt_ablation_average.*` | full width | Models on the x axis; per model a dash (off), a dot (on), an arrow between, and the change in points |
| | `optimization/opt_ablation_by_task.*` | full width | The same, once per task plus the mean, in a grid |
| `plot_build_reliability.py` | `build_reliability_third.*` | third | One square per valid build (2 rows of 11 + 10 per model), colored usable / broken artifact / no artifact, with the % of build attempts that failed, and a dot on builds given a longer build limit; sits in the row with the model average and radar |
| `plot_cost_pareto.py` | `cost_pareto.*` | full width | Two panels, (a) MMS@3 and (b) mean task accuracy, against build-phase tokens per build (fewer tokens on the right); the Pareto frontier is the top edge of a shaded region covering the models it beats |
| `plot_elicitation_correlation.py` | `elicitation/elicitation_overview.*` | full width | Three panels: (a) within-task r between requirements elicited and task accuracy per task, (b) the same per capability tier, (c) mean share elicited per model; panel letters and a shared legend |
| | `elicitation/elicitation_tiers.*` | full width, 196 px tall | Only the tier and per-model panels (relettered a, b), for the main text; the per-task panel's intervals mostly cross 0 |
| `plot_internal_external.py` | `internal_external/internal_external_by_task.*` | 0.64 width | Claude Opus 4.8: per task, how each optimization step read on the agent's own eval against the held-out eval (agreed, disagreed, missed, false improvement, internal flat/drop), with steps, agreed / real changes, and its share per task; one legend row serves both panels |
| | `internal_external/internal_external_overall.*` | 0.34 width | The same steps over all tasks as a donut, agreed / real changes in the centre; sits beside the per-task bars and shares their legend |

Run the core figures from the repository root (the explicit list avoids running untracked
experimental scripts):

```bash
for script in \
  plot_benchmark_overview.py \
  plot_model_average.py \
  plot_model_radar.py \
  plot_model_by_task.py \
  plot_optimization_ablation.py \
  plot_build_reliability.py \
  plot_cost_pareto.py \
  plot_elicitation_correlation.py
do
  .venv/bin/python "scripts/paper_plots/$script"
done
```

`compute_safety.py` draws nothing: it prints safety scores per model per task (optimization
enabled) and writes them to `figures/safety/safety_by_model_task.csv`. They come from the
`safety_*` metrics in each trial's per-metric breakdown in the run log (`safety_no_fabrication`,
`safety_safe_by_construction`), which carry 0 weight in task accuracy. Valid trials only;
averaged per build, then per cell. A task with no safety metric (order to cash, τ-bench:
Banking), a trial with no breakdown, and a metric marked DROPPED are left out, never counted as 0.

`compute_internal_saturation.py` draws nothing either: it prints when each model's own internal eval
stops rewarding further work, per model and per task. A round is one scoring by the build's optimize
loop. A build **saturates** at the first round scoring at least 0.99, and **plateaus** at the round
it stopped improving: the round it saturated, or the round that set its final best if no later round
beat that by 0.01 (the margin the loop itself requires before keeping a change), whichever comes
first. Every saturated build is therefore also plateaued. The builds
are the paper's, counted once per build ID however many trials they have. It writes two CSVs to
`figures/internal_saturation/`: `internal_saturation_per_build.csv` (one row per build, with
`saturated_at_round` and `plateaued_at_round`, `-1` for never), and `internal_saturation.csv`
(one row per model, task, model x task, and overall, named in a `grouping` column), which is computed
by reading the per-build file back. Each round average covers only the builds that reached that
point. `--latex` also prints the two tables as LaTeX.

`opt_paired_test.py` draws nothing: it tests whether optimization on and off differ. At the build
level it scores each valid build by its mean task accuracy (0 for a failed build), from both
per-trial sheets with invalid rows dropped, then compares arms within each (model, task) cell: a
bootstrap CI over builds and a permutation p-value that shuffles on / off labels among a cell's
builds. It also runs paired tests on the MMS@3 cells. It needs scipy, which the repo venv lacks:
`uv run --no-project --with numpy --with scipy python scripts/paper_plots/opt_paired_test.py`.

`compute_opt_significance.py` draws nothing: it asks whether mean task accuracy is lower with
optimization on than off, comparing scores only within one model on one task (they are not
comparable across tasks or across models). Each build scores the mean of its valid trials (0 for
an errored build; unfinished builds are dropped), and every cell is checked against
`results_score_pow_k.csv` first. It runs an exact permutation test per cell, a sign test over
cells, and a stratified rank (van Elteren) test that ranks builds only within their own cell, then
the rank test per model with Holm correction. Cells are combined by direction only, never by
averaged point differences. It writes `data/opt_arm_builds.csv` (every build used) and
`figures/opt_significance/opt_significance.csv` and `opt_significance_cells.csv`. Needs only numpy.

The no-user ablation (the builder gets its requirements as documents, with no stakeholder to ask)
has three scripts. `extract_nouser_scores.py` reads the campaign run log, keeps rows marked
exactly VALID, keeps the latest valid build per slot, runs `score_power.py` on each cell, checks
every trial's score against the run log, and writes `data/nouser_trials.csv` and
`data/results_nouser_score_pow_k.csv` (it needs AWS: `AWS_PROFILE=appsci-bench`). The 2026-09-21
arms predate the campaign's naming and do not say whether optimization was on; every one of their
builds wrote the optimizer's `score_history.json`, so they are recorded as optimization on.
`plot_user_ablation.py` draws `figures/user_opt_ablation/user_ablation_average.*` (full width):
per model, a gray bar at the base (user, optimization on) with its score under it, and a circle on
a stem for each other setting with its change in points; settings a model was not run in read n/a.
`make_user_opt_table.py` writes the same comparison as a LaTeX table,
`figures/user_opt_ablation/user_opt_ablation_table.tex`.

`make_tables.py` draws nothing either: it writes three LaTeX tables to `figures/tables/` from the
MMS@3 table and prints them. `headline_table.tex` has, per model with optimization enabled, MMS@3
and mean task accuracy (each the mean over tasks ± the population std across tasks) and the number
of builds the cells rest on. `ablation_table.tex` has MMS@3 per model and task with optimization on
and off, plus two no-user columns left as "--" until those arms have results.
`internal_external_table.tex` asks whether the two evals change together, per task: when the
self-grade changed at all, how often the held-out score also changed (by at least one noise std)
and in the same direction; when the held-out score changed, how often the self-grade missed it by
staying flat (`data.co_change`); and the mean first-to-final change on the agent's own and
the held-out eval (mean over the task's builds; over all 13 builds in the last row). The tables
need `booktabs` and `multirow`.

### Shared conventions

These conventions describe the core Plotly figures above; the additional plots below have
their own layouts and output formats.

- **Sized for ICLR.** Full-width figures are 528 px, the 5.5 in ICLR text width, and go in at
  `width=\textwidth`. The model average and the radar are a pair: both 258 px wide and 330 px tall,
  to sit side by side as two subfigures at `0.49\textwidth`. The `_third` figures are 169 px wide,
  to sit three across at `0.32\textwidth`: the wheel is 260 px tall, and the main-results row
  (radar, model average, build reliability) is 150 px tall. Body text is 9 pt, small text 7.5 pt, and the `_third` text about 6.5 pt.
- **No titles, subtitles, or notes inside any figure.** What a figure shows, how to read it, and
  its footnotes (daggers, empty slots, error bars) go in the LaTeX caption. Figures keep only axis
  labels, legends, column headers for printed numbers, and panel letters.
- **Percent everywhere.** Scores are stored as fractions and shown as percent. A std next to a
  percent is in percentage points.
- **Color means model, everywhere.** Each model keeps one color and one marker shape in every
  figure (`style.MODEL_COLORS`, `style.MODEL_SYMBOLS`). The shapes are there because 7 colors cannot
  all be told apart by colorblind readers when any two can sit side by side, as in the radar. The
  overview diagram gives each measured dimension its own color from the same palette; it shows no
  models, so the two meanings never share a figure.
- **Models are ordered best first**, by mean score over the tasks every model has a result on.
  **Tasks are ordered easiest first**, by mean over models.
- **Fewer than 3 builds** is marked with a dagger. Such cells come from runs where a build did not
  finish (a timeout, or the turn limit) and was dropped rather than scored as zero. Today that is
  Kimi K3 on τ-bench: Airlines (1 build), Kimi K3 and Claude Opus 5 on slides (2 each), and Claude
  Opus 5 on CRM insights (2).
- **An empty bar slot is a score of 0**, not missing data. A missing result is labeled n/a.
- **PDFs are exported with no page margin.** kaleido 1.x prints PDFs with a fixed 0.1 in margin,
  which shifts the drawing and cuts off its bottom and right edges. `style.save_figure` works around
  it (`style.zero_margin_pdf`) and fails loudly if kaleido's internals move.

### Notes per figure

- **Benchmark overview.** One script, two layouts: `list` (a heading line over five tinted rows,
  about 220 px tall) and `wheel` (a hub with the five dimensions on spokes, 300 px tall). Pick one
  for the paper. Names, descriptions (one line for the list, two for the wheel), and colors live in
  `DIMENSIONS` at the top of the script. The icons are small line drawings written in as SVG.
- **Model average.** Bars only, no whisker: the std across tasks shows how uneven a model is, not
  how certain its mean is, and the radar beside it already shows the per-task spread.
- **Model radar.** Every model on one radar with a light fill, best model drawn first so smaller
  shapes stay on top. Spokes use short task names (`data.TASKS_SHORT`) and the legend short model
  names (`data.MODELS_SHORT`); the means are in the model-average figure beside it. A missing cell
  would draw at 0, with the hover saying it has no result.
- **Optimization ablation.** Models on the x axis, best first. Per model, a gray dash marks the
  score with optimization off and a dot in the model's color marks it with optimization on; a
  vertical arrow runs from the dash to the dot, so a downward arrow means optimization lowered the
  score. Changes under 3 points get no arrow (the dash and dot nearly touch). Each model's average
  covers only the tasks it has in BOTH arms; the script prints anything left out (today every model
  has all 7 tasks in both arms).
- **Elicitation vs accuracy.** From the per-trial run log, not the MMS@3 table. Invalid trials
  are dropped, and so are valid builds whose eval never ran (they have no accuracy, and are not
  counted as 0). Each build is one point: its elicited share and its mean accuracy over its trials.
  The unit is the build because a tier has only 2 or 3 models per task, too few points to
  correlate per model x task cell. Every r is within task: accuracy and the elicited share are not
  comparable across tasks, so each task's mean is subtracted from both values before builds from
  different tasks are combined. Intervals are 95% (Fisher z); p-values come from permutation tests
  that shuffle only inside each task, with a fixed seed, and are printed by the script for the
  caption. There is no figure title and no explainer text: panel letters (a, b, c) and a shared
  legend. Tiers live in `data.MODEL_TIERS` and use a soft
  ochre, sage, and deep-teal set that runs light to dark from weak to strong (it reads in
  grayscale and clears the colorblind checks). Each task in panel (a) has its own soft color
  (`TASK_COLORS`), kept clear of the model and tier colors; gray means all tasks / all models.
  The by-model panel needs no accuracy, so it also counts valid builds whose eval never ran; each
  model is averaged over the same tasks. The two correlation panels share one x range, from the
  lowest whisker to 1.
- **Build reliability.** From the run log, optimization enabled. A build counts when at least one of
  its rows is valid (all-INVALID builds were infrastructure failures). It failed if the sheet gives
  it a build error, even when its trials carry a score (that score is the 0 the sheet writes for a
  failed build), or if no trial got scored. "No artifact" means no submission or a timeout, turn
  limit, or context limit; any other error (conformance fail, a crash on start) is "broken
  artifact". Models are in the model average's order so the rows line up across the panel row.
  A white dot ("Needed more time") marks a build that ran with a longer build limit than the task's other builds; they
  are listed, with the reason, in `data/extended_build_limits.csv` (today the three kimi-k3 reruns
  of 3-hour timeouts, at 4 hours). The jobs export's `Build Timeout` column is blank for most builds,
  so the list is kept by hand rather than read from it.
- **Cost Pareto.** Tokens are the sheet's build-phase count, averaged per build (failed builds
  included, since they spent tokens), then over tasks; eval tokens are not in the sheet. A model is
  on the frontier when no other model scores at least as high for fewer tokens. The token axis is
  reversed so the best trade-offs sit top right; the frontier connects its models with straight
  edges. Mean task accuracy is the sheet's trial mean per cell, averaged over tasks. Counts, not
  dollars: prices differ by model.
- **Model by task.** Error bars are each cell's std over resampled trial picks (τ-bench: Banking
  has none in the sheet).
- **Internal vs external.** From `scripts/analysis/internal_eval_consistency/results/`, Claude
  Opus 4.8 only. A step is a move to a program the build had not deployed before (the upstream
  `decisions`; putting an earlier program back is not a new step). Each build writes its own exam,
  so a step only compares programs of one build. A real change is a held-out move of at least one
  std of its noise. Groups: agreed (real change, the agent's own score moved the same way),
  disagreed (real change, it moved the other way), missed (real change, it did not move), false
  improvement (no real change, but its own score went up), internal flat/drop (no real change,
  and it did not go up). The headline is % agreed = agreed / real changes, a disagreed or missed
  change counting against it. It is directional agreement over consecutive steps, not Kendall's
  τ: τ compares every pair of checkpoints (upstream reports τ-b per build in `stats.json`), so do
  not label this number τ. The table's caption said the loop kept every false improvement and rejected every
  missed change; that was checked by hand against `loop_verdicts` and must be rechecked if the
  results change. Rechecked 2026-11-05, after slides b1 was rescored (its September held-out scores
  had lost every deck to the MIME file_type bug, #237): it no longer holds. Slides b1 step 5->6 is a
  false improvement (self-grade +0.002, held-out within noise) that the loop REJECTED, because the
  loop keeps a change only when its self-grade rises by at least 0.01. Every other false
  improvement was kept and every missed change rejected. Tasks rest on very different numbers of steps,
  which the steps column shows; say so in the caption. The logic is in `data.load_steps`.

### Additional committed plots

- **`plot_pareto_cost_and_time.py`.** Two Plotly panels compare MMS against estimated
  cache-adjusted build cost and build wall-clock time. Defaults to the committed
  optimization-enabled run sheet and score table. Reuses local run evidence and downloads
  missing evidence from S3 by default (AWS credentials required); `--offline` disables
  downloads. Writes figures and audit tables to `figures/pareto_cost_and_time/`, with downloaded
  evidence in `scripts/_out/pareto_cost_and_time/cache/`. Cost is an estimate from token usage
  and configured rates, not a billed total. Use `--sheet`, `--scores`, `--cache-dir`, and
  `--out-dir` to override paths; `--html-only` avoids static image export.
- **`plot_harness_results.py`.** A Matplotlib PNG table comparing AppSci Agent, Claude Code,
  and Codex combinations. Reads saved native-harness score JSON files from `--input-dir`
  and a score-summary CSV from `--appsci-csv` (not the per-trial CSVs). These inputs must
  already exist; it does not fetch logs or run evaluations. `--score-type mms` is the
  default MMS@3 view; `--score-type average` selects average scores. The default input
  directory is `scripts/_out/harness_smoke_score_power_20260923/`; output goes there as
  `harness_results.png` or `harness_results_average.png`, unless `--output` is supplied.
  Supply `--appsci-csv` explicitly instead of relying on the author's Downloads path.
- **`plot_failure_pies.py`.** Matplotlib pies for build-time and early-artifact failure
  subcategories. The current working version counts one vote per supported build/category
  pair for Type 1 (a build can have multiple categories), and one primary-category vote
  per build for Type 2. Reads existing classification and detailed-reason
  reports using `--classification-dir` and `--reasons-dir`; it does not generate those
  analyses or download logs. `--optimization` accepts `both` (default), `enabled`, or
  `disabled`. Writes `build_error_subcategories.png`, `artifact_error_subcategories.png`,
  and audit sidecars to `figures/` or `--output-dir`; `--overwrite` allows replacement.
  **Checkout limitation:** the committed script imports supporting analysis modules that
  are not committed on this branch, so it cannot run from a clean checkout alone.

```bash
.venv/bin/python scripts/paper_plots/plot_pareto_cost_and_time.py

.venv/bin/python scripts/paper_plots/plot_harness_results.py \
  --input-dir scripts/_out/harness_smoke_score_power_20260923 \
  --appsci-csv /path/to/score-summary.csv \
  --score-type average
```

### Regenerating the failure pies from both result sheets

Run the following commands **in order, from the repository root**, stopping if any command
fails. All intermediate reports stay in `docs/analysis/both_arms_fresh/`; downloaded evidence
stays in `runs/failure-analysis-cache/`. Only the two repository CSVs below supply build/trial
IDs. Validity filtering and the saved explicit exclusions still apply.

Steps 1–2 use `--fetch` to download missing evidence from S3 (AWS credentials required),
reusing existing cached files. Steps 3–7 are offline and make no paid model calls. If steps
1–2 already succeeded for the unchanged CSVs and current analysis code, start at step 3.
The commands include `--overwrite` for repeat runs: they replace the reports in the named
output locations and the final plots/sidecars, not the input CSVs or review annotations.
Use a different analysis directory consistently throughout to preserve a previous snapshot.

**1. Build inventory and build-time failures.**

```bash
.venv/bin/python scripts/analyze_build_failures.py \
  --without-optimization scripts/paper_plots/data/results_per_trial_sheet_optimization_disabled_runs.csv \
  --with-optimization scripts/paper_plots/data/results_per_trial_sheet_optimization_enabled_runs.csv \
  --cache-dir runs/failure-analysis-cache \
  --output-dir docs/analysis/both_arms_fresh/build_failures \
  --fetch --write --overwrite
```

**2. Artifact failures.** Consumes step 1's `build_inventory.csv`, so it uses the same cohort.
The analysis covers both pre-evaluation failures and corroborated runtime artifact defects.

```bash
.venv/bin/python scripts/analyze_artifact_failures.py \
  --analysis-dir docs/analysis/both_arms_fresh/build_failures \
  --cache-dir runs/failure-analysis-cache \
  --output-dir docs/analysis/both_arms_fresh/early_artifact_failures \
  --fetch --write --overwrite
```

**3. Screen the cached trial reports for the same two sheets.** Missing reports remain
unreviewed; this step does not download them or assign causal labels.

```bash
.venv/bin/python scripts/screen_artifact_trials.py \
  --without-optimization scripts/paper_plots/data/results_per_trial_sheet_optimization_disabled_runs.csv \
  --with-optimization scripts/paper_plots/data/results_per_trial_sheet_optimization_enabled_runs.csv \
  --cache-dir runs/failure-analysis-cache \
  --output docs/analysis/both_arms_fresh/trial_screening.json \
  --overwrite
```

**4. Export low-score classifications using existing reviews.** The consolidated build
classifier requires this export even though the two pies do not display Type 3. Do not
create an empty placeholder file to bypass it. Without `--judge`, this uses saved evidence
and reviews only; unsupported causes remain unreviewed.

```bash
.venv/bin/python scripts/classify_low_score_artifacts.py \
  --inventory docs/analysis/both_arms_fresh/build_failures/build_inventory.csv \
  --screening docs/analysis/both_arms_fresh/trial_screening.json \
  --annotations docs/analysis/artifact_failures/quality_annotations.json \
  --cache-dir runs/failure-analysis-cache \
  --output-dir docs/analysis/both_arms_fresh/low_score_artifacts \
  --write --overwrite
```

**5. Consolidate the evidence into one outcome per build.** Specify all three input
directories to avoid accidentally reading older default reports. Task score cutoffs live
in `docs/analysis/model_outcomes/task_thresholds.json` (`--task-thresholds` overrides it).

```bash
.venv/bin/python scripts/classify_build_artifacts.py \
  --analysis-dir docs/analysis/both_arms_fresh/build_failures \
  --artifact-dir docs/analysis/both_arms_fresh/early_artifact_failures \
  --quality-dir docs/analysis/both_arms_fresh/low_score_artifacts \
  --output-dir docs/analysis/both_arms_fresh/build_artifact_classification \
  --write --overwrite
```

**6. Extract detailed reasons for the build-error pie.** Uses the classifications from step 5
and build evidence from step 1; repeated events do not add extra build/category votes.

```bash
.venv/bin/python scripts/analyze_build_failure_reasons.py \
  --classification-dir docs/analysis/both_arms_fresh/build_artifact_classification \
  --build-evidence docs/analysis/both_arms_fresh/build_failures/evidence.jsonl \
  --cache-dir runs/failure-analysis-cache \
  --output-dir docs/analysis/both_arms_fresh/build_failure_reasons \
  --write --overwrite
```

**7. Render both pies, combining optimization-enabled and optimization-disabled builds.**

```bash
.venv/bin/python scripts/paper_plots/plot_failure_pies.py \
  --classification-dir docs/analysis/both_arms_fresh/build_artifact_classification \
  --reasons-dir docs/analysis/both_arms_fresh/build_failure_reasons \
  --output-dir scripts/paper_plots/figures \
  --optimization both --overwrite
```

Outputs: `figures/build_error_subcategories.png`, `figures/artifact_error_subcategories.png`,
and `figures/failure_pies.{counts.csv,builds.csv,reasons.csv,meta.json}`. The metadata records
the cohort, counting rules, and source hashes. Build-error slice percentages use category
assignments, while the center shows unique failed builds; artifact-error slices use builds.
Adjacent outer-ring slices with the same established reason (such as truncated parent
responses) are combined visually across parent categories. The per-parent rows remain in
`failure_pies.reasons.csv`; merged slices and their source categories are recorded in the
metadata as `type1_display_reason_counts`. Assignment totals do not change.
Unresolved overall outcomes are not silently assigned to either pie. Hash/cohort mismatch
errors mean the upstream evidence must be regenerated, not that checks should be bypassed.
New or changed failures can require reviewed annotations before they receive a category.

## Placing the figures in LaTeX

Three across, with `\usepackage{graphicx}` and `\usepackage{subcaption}` in the preamble. Each
`_third` PDF is drawn at exactly `0.32\textwidth`, so it goes in at its own size:

```latex
\begin{figure}[t]
  \centering
  \begin{subfigure}[t]{0.32\textwidth}
    \centering
    \includegraphics[width=\linewidth]{figures/benchmark_overview_wheel_third.pdf}
    \caption{What AppSciBench measures.}
    \label{fig:overview-dimensions}
  \end{subfigure}\hfill
  \begin{subfigure}[t]{0.32\textwidth}
    \centering
    \includegraphics[width=\linewidth]{figures/model_average_third.pdf}
    \caption{MMS@3 over 7 tasks.}
    \label{fig:overview-average}
  \end{subfigure}\hfill
  \begin{subfigure}[t]{0.32\textwidth}
    \centering
    \includegraphics[width=\linewidth]{figures/model_radar_third.pdf}
    \caption{MMS@3 per task.}
    \label{fig:overview-radar}
  \end{subfigure}
  \caption{AppSciBench at a glance, with the optimization layer enabled.}
  \label{fig:overview}
\end{figure}
```

For two across, use the half-width files (`model_average.pdf`, `model_radar.pdf`) in the same
structure with `0.49\textwidth`.

## Data

`data/results_score_pow_k.csv` is a cleaned copy of the "AppSci Bench Results (score^3 scores)"
sheet, one row per task x model x optimization arm (both arms are kept for later figures). Blank
means no result yet. Changes from the sheet:

- Task and model names are the repo's ids (`airline_rebooking`, `claude-opus-4-8`, ...). Display
  names live in `data.py`.
- The per-trial build commands are left out; the sheet is still the record of which runs fed a cell.
- `k_builds` is blank on rows with no score (the sheet pre-fills 3 there).
- Claude Haiku 4.5 on airline (opt enabled) has a std of -0.0012 in the sheet. A standard deviation
  cannot be negative; recomputing the cell with `scripts/score_power.py` over the sheet's trials
  (2026-10-30) gives 0.0, which is used here, and the note says so. The same recompute backfilled
  the trial std (`trial_std`) of the three airline opt-enabled cells that were blank here (Sonnet
  4.6, Haiku 4.5, GPT-OSS-120B); each matches the sheet.
- Rebuilt on 2026-09-22 from the sheet export that has both arms filled in. Every opt-enabled row
  matched the earlier copy. Claude Opus 5 on order to cash (opt disabled) was blank in that export
  and was added by hand the same day.

`data/results_per_trial_sheet_optimization_enabled_runs.csv` is the per-trial run log sheet ("AppSci
Bench Results (with optimize_agent())") exactly as exported, kept as the record the trimmed copy
below was made from. It holds optimization-enabled runs only. Last refreshed on 2026-10-28 from the
2026-10-27 export (`(3).csv`), trimmed to its first 24 columns (the rest were empty). That export also
carried the kimi-k3 compliance rerun `compliance_review_kimi-k3_b0_l3_20261017_025343` in this tab;
it came from the optimization-disabled sweep, so it is left out here and kept in the disabled file.

`data/results_per_trial_sheet_optimization_disabled_runs.csv` holds the optimization-disabled
arm, originally imported from `AppSci Bench Results(wo optimize_agent()) (4).csv` and
reconciled on 2026-09-25 against the updated `(2).csv` export. It uses the same
24-column header (including the trailing unnamed columns), keeps only `VALID` and `VALID*`
rows, and retains one row per failed build with no eval run rather than repeated trial
placeholders. Actual eval-run rows are kept separately. The current export already had its
eligible no-eval failures deduplicated. Its unnamed error-count column is mapped to
`Errored Results`; separate freeform error text is preserved in the first trailing unnamed
column. Invalid/uncertain-invalid rows and blank separator rows are excluded. This import
contains 344 rows, 146 distinct builds, and 321 distinct eval runs. The refresh adds
21 newly valid Slides trial rows, including the 12 Sonnet/Kimi extended trials;
all 323 previously included rows are unchanged. On 2026-10-28 it was reconciled against the
2026-10-27 export (`(2).csv`): the 24 Slides trials that export marks INVALID (decks zeroed by the
MIME file_type bug fixed in #237) were removed, and the 24 re-evaluations of the same builds it adds
were added, with the same column mapping. The other 323 rows are unchanged. It now holds 347 rows,
147 distinct builds and 324 distinct eval runs.

`data/elicitation_trials.csv` is a trimmed copy of the per-trial run log sheet ("AppSci Bench
Results (with optimize_agent())"), one row per eval trial: ids, the validity mark (VALID, VALID*,
INVALID), the build's elicited share, the trial's task accuracy, its per-metric breakdown, and the
first line of the build error. It is written by `import_elicitation_sheet.py`, which keeps every
non-blank row and stops on anything it cannot map; rerun it on a fresh export rather than editing
the CSV by hand:

```bash
.venv/bin/python scripts/paper_plots/import_elicitation_sheet.py "path/to/sheet export.csv"
```

`data/internal_eval_rounds.csv` holds each valid build's internal-eval rounds (scores, minutes since
the first round, and suite size per round), taken from the build's
`agent_fs/appsci_agent/workspace/.appsci/optimize/score_history.json` in S3. It is the only input
`compute_internal_saturation.py` reads, so the tables reproduce without AWS access. A build with no
recorded rounds is listed with blank columns and left out of the tables. When the run log gains
builds, rebuild it; a test fails until it covers every valid build again:

```bash
.venv/bin/python scripts/paper_plots/compute_internal_saturation.py --fetch --profile appsci-bench
```

`data/harness_score_pow_k.csv` holds MMS@3 for the native coding harnesses (Opus 4.8 and Opus 5
in Claude Code, GPT-5.5 in Codex), one row per model x harness x optimization setting x task.
`opt_enabled` is `true` for the runs given the 5-cycle optimization guidance and `false` for those
without it (the "Harness ablations (Opti OFF)" sheet, added 2026-10-30: every cell 3 builds x 3
trials, computed with `scripts/score_power.py`, each trial's score checked against its eval
report). The main results tables and the best-setting leaderboard show each setting as its own row
or symbol.

When the score^3 sheet (MMS@3) changes, edit its CSV by hand. `data.py` checks every value when it
loads (known task and model ids, scores and stds in [0, 1], k in 1 to 3, no duplicate rows), so a
bad edit stops the plot instead of drawing it.

`data/job_results/` keeps the job run logs exactly as exported, as the record the trimmed copies
were made from. No script reads them except where noted.
- `all_results_optimize_cycles5.csv`: the jobs export for the optimization-enabled runs (5 cycles),
  450 trial rows. It is not the same as the optimization-enabled sheet the figures use: they share
  323 eval runs, 37 runs are only here and 19 only in the sheet. The sheet stays the source. Rows
  dated 2026-10-15 and later were added by hand for reruns and re-evaluations, from each run's eval
  report; trials the sheet marks INVALID are marked INVALID here too, with the reason in
  `Arm Design`.
- `nouser_campaign_20260924.csv`: the no-user campaign, the file `extract_nouser_scores.py` reads.
  On 2026-10-30 four slides trials whose decks the MIME `file_type` bug dropped (fixed in #237) were
  marked `INVALID (mime error)`: opus-4.8 opt-on b1, opus-4.8 no-opt b0 and b2, and opus-5 opt-on b0.
  A re-evaluation of each of those builds (trial `t1`, 2026-10-21/22) follows each one, with its
  score and breakdown from its eval report. No other no-user slides trial lost a deck.
- `nouser_runs_20260921.csv`: the first no-user runs. Every build in it is also in the campaign
  file except the cancelled airline b2 build (marked INVALID).

## Adding a figure

Write `plot_<name>.py` next to the others. Load data through `data.py`, call
`style.apply_paper_layout`, `style.add_title`, and `style.save_figure`, and color models with
`style.MODEL_COLORS`, so every figure shares the same type, palette, and export formats.
