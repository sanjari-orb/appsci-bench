# Slides Generator — environment resources

Where the customer's resources live inside the `appsci-bench/slides_generator` container (agent-facing; distinct from the hidden held-out eval set in `eval_data/`).

| Resource | Type | Location in container | Notes |
|----------|------|-----------------------|-------|
| Sample requests | sample_data | `/data/samples/*` | Synthetic request examples for the slide generator; each is a text prompt plus attached files (grounding documents in .md/.csv/.pdf/.pptx and a `template.pptx` provided directly in the sample folder). Purpose-built, non-eval-derived, **inputs only** — no reference answer ships with them. See `environment/README.md`. |
| Success criteria | kpi | `/sop/kpis.md` | Business framing: the pain, what the team needs from a generated deck, and the human-review boundary. States plainly that what counts as a *good* deck is not settled and has to be agreed with the customer — so it names no criterion, no slide count, no accepted file type, and nothing about how the result comes back. |


## Access

- `/work` is the writable scratch directory. Artifact outputs go in `/work/out`.
- `/data` and `/sop` are **inputs only** — treat them as read-only. They are delivered into the
  container at the start of the build phase and nothing reads them back, so a write there changes
  nothing and is lost.
- No database is present; the prompt text plus attached files arrive at query time.


## Held-out eval data (not agent-facing)

The scored eval set lives in `eval_data/` and is **vendored in the repo via Git LFS** — it is read
on the **host** at eval time and is **never mounted to the agent**. A normal clone with Git LFS has
it; if you see only pointer files, run `git lfs install && git lfs pull`.

