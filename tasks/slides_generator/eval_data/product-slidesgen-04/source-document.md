# LLM-Based Automated QA: Results of an 8-Week Pilot

*Applied Research — Internal technical report. Status: complete. Classification: Internal — do not distribute externally. Intended readership for this write-up: technical (Applied Research, the QA Platform team, and Data Science). All figures in this document are synthetic and exist only for testing.*

---

## 0. Abstract

Contact-center quality assurance is, today, a manual and low-coverage process. We ran a controlled 8-week pilot to test whether an LLM-based **Automated QA system (Auto-QA)** can score every interaction against a team's existing scorecard and agree closely enough with trained human reviewers to be trusted. Across **three contact-center teams** and **14,000 scored calls**, Auto-QA achieved **100% coverage** (versus roughly **3%** under manual sampling), **88% exact agreement** with humans at the individual scorecard-item level, **93% agreement within one point** on the overall 100-point call score, and **96% compliance recall** against human-identified breaches, while reducing analyst scoring time by about **70%**. Agreement was not uniform: it was weakest on subjective soft-skills items and on calls with poor audio. Crucially, analyst-to-analyst agreement was itself only about **85%**, which bounds how high agreement against a single human label can appear. This is an accuracy study only; it did not measure any downstream business outcome. On this evidence we recommend a **controlled expansion to two more teams in shadow mode**, with humans still in the loop — not making Auto-QA the system of record for agent scoring.

---

## 1. Background and motivation

Quality assurance in the contact center exists to answer a deceptively simple question: are agents handling interactions correctly, compliantly, and well? In most operations the answer is produced by supervisors who pull a handful of recorded calls per agent per period and hand-score them against a fixed scorecard. Industry practice, and our own internal baseline, puts the share of interactions actually reviewed at roughly **2–3%**.

This manual process has three well-documented problems. It is **slow** — scoring a single call against a detailed scorecard takes a trained analyst several minutes, and the queue rarely clears. It is **inconsistent** — two analysts scoring the same call frequently disagree, particularly on judgment-heavy items, so an agent's score depends partly on who happened to review them. And it is **low-coverage** — because only a small sample is reviewed, the great majority of interactions are never examined at all. For a regulated business this last point is not merely an analytics gap; it is a **compliance blind spot**, because disclosures, consent language, and verification steps go unaudited on 97%+ of calls.

A previous internal attempt to widen coverage used rules-based keyword spotting (flagging calls that did or did not contain specific phrases). It improved the *volume* of calls touched but produced high false-positive rates and could not score the qualitative items on the scorecard at all, so it was retired. The hypothesis behind the present pilot is that a modern LLM, given the same scorecard rubric a human uses, can do what keyword spotting could not: read the whole interaction and produce an item-by-item score that a supervisor would recognize as reasonable.

## 2. Objectives and research questions

The pilot was scoped narrowly and deliberately. We set out to answer four questions:

- **RQ1 — Coverage.** What share of interactions can Auto-QA realistically score, end to end, without human triage?
- **RQ2 — Agreement.** How closely do Auto-QA's scores match those of trained human analysts, both at the level of individual scorecard items and at the level of the overall call score?
- **RQ3 — Compliance recall.** Of the compliance breaches that human reviewers identify, what fraction does Auto-QA also catch?
- **RQ4 — Analyst effort.** How much of an analyst's manual scoring time does Auto-QA displace?

We explicitly did **not** set out to measure business impact. Whether higher-coverage QA improves customer satisfaction, reduces average handle time, lowers compliance incidents, or improves coaching outcomes are all downstream questions this study was not designed to answer, and the reader should not infer answers to them from what follows. This is an *accuracy and agreement* study.

## 3. Method

### 3.1 Pilot design

The pilot ran for **8 weeks** across **three contact-center teams** drawn from a single line of business and region. Over the window, Auto-QA scored a combined **14,000 calls** — every scorable interaction the three teams handled, distributed roughly as: Team A (billing) ~5,200 calls, Team B (technical support) ~5,600 calls, and Team C (retention) ~3,200 calls. There was no sampling on the Auto-QA side; the system scored the full population.

### 3.2 The scorecard

All three teams used the same **12-item QA scorecard**, which the teams were already using before the pilot. The twelve items group into three families: **four compliance items** (required disclosures, identity verification, consent capture, and regulated-language adherence), **four resolution / process items** (correct diagnosis, correct resolution path, system notation, and follow-up commitment), and **four soft-skills / empathy items** (acknowledgement, tone, active listening, and close). Each item is scored on the team's existing scale and rolls up to an overall **100-point** call score. We did not modify the scorecard for the pilot; using the teams' own rubric was a design requirement.

### 3.3 The system under test

The system under test was **Auto-QA v0.9**. It operates on a transcript of the call produced by our standard ASR pipeline (word error rate ~**12%** on clean audio, with speaker diarization), and scores each transcript against the scorecard by retrieving the relevant rubric text for each item and prompting an internal instruction-tuned LLM to assign and justify a per-item score. Generation was run at low temperature (0.2) for determinism, with the full scorecard rubric supplied in-context. Mean end-to-end scoring latency was approximately 8 seconds per call. Full configuration is in Appendix B; the specific model build, prompt templates, and retrieval parameters are implementation detail and are expected to change.

### 3.4 Human ground truth

To establish ground truth, human QA analysts independently scored a **stratified sample of 1,200 calls** (roughly 450 / 450 / 300 across Teams A / B / C), drawn to mirror the call-type mix of each team. These 1,200 human scores — *not* the Auto-QA scores — are the reference against which agreement is measured.

A methodological point that bears directly on how the headline agreement numbers should be read: a subset of the sample was **double-scored** by two analysts to estimate human-to-human reliability, and the two analysts gave the *same* item-level score only about **85%** of the time (Cohen's κ ≈ 0.70). In other words, trained humans using this very scorecard agree with each other roughly 85% of the time. This figure is not a side note; it is effectively a **ceiling** on the agreement any automated system — or any *second human* — can demonstrate against a single human label. An automated system that matched a human 100% of the time would be matching one analyst's idiosyncrasies, not "the truth."

### 3.5 Metrics and statistical treatment

Agreement is reported two ways. **Exact item-level agreement** is the share of individual scorecard items on which Auto-QA and the human assigned the identical score. **Within-one-point agreement** is the share of calls on which Auto-QA's overall 100-point score lands within one point of the human's overall score. **Compliance recall** is the share of human-identified compliance breaches that Auto-QA also flagged. **Analyst time** was measured as supervisor minutes spent scoring, before versus during the pilot, on the teams in scope.

Point estimates are reported with bootstrap 95% confidence intervals (1,000 resamples); intervals were narrow (typically ±1–2 points) given the sample sizes and are omitted from the body for readability. Item-level agreement is also summarized with Cohen's κ where a chance-corrected statistic is useful. None of this statistical apparatus is necessary to understand the findings; it is included for completeness and review.

## 4. Results

### 4.1 Coverage (RQ1)

Under the existing manual process, human QA reviewed roughly **3%** of calls. Auto-QA scored **100%** — every call in the population, every item on the scorecard. This is the single largest change the pilot demonstrates: the move from a small hand-picked sample to the entire interaction volume.

### 4.2 Agreement (RQ2)

Across the 1,200-call human-scored sample, Auto-QA reached **88% exact agreement** with human scores at the individual scorecard-item level, and **93% of calls** fell within one point of the human overall score on the 100-point scale. Per-team item-level agreement was 90% (Team A), 88% (Team B), and 85% (Team C) — the spread tracks call complexity, with the retention team's longer, more negotiation-heavy calls hardest to score. Chance-corrected, overall item-level κ was approximately 0.71. Read against the ~85% human-to-human ceiling from §3.4, an 88% match with a single human label is about as high as one should expect a *well-performing* scorer — human or machine — to reach.

### 4.3 Compliance recall (RQ3)

Of the compliance breaches that human reviewers identified in the sample, Auto-QA independently flagged **96%**. The small miss rate concentrated in cases where the required disclosure was spoken but garbled in transcription, or where consent was given non-verbally and noted by the human from context the transcript did not capture. Compliance items were, in general, where Auto-QA was strongest, because they are the most concretely specified items on the scorecard.

### 4.4 Analyst effort (RQ4)

Supervisor time spent manually scoring calls on the pilot teams fell by approximately **70%** over the window. The residual time was spent reviewing Auto-QA's flagged calls and disputed scores rather than scoring from scratch. Supervisors reported reallocating the freed time toward agent coaching, though we did **not** instrument or measure any coaching or performance outcome — see §6.

## 5. Error analysis

Disagreements between Auto-QA and humans were **not** evenly distributed across items or call types. They concentrated in three places.

**Soft-skills and empathy items.** These are the most subjective items on the scorecard, and they are where both humans and the machine struggled most. Item-level agreement on the soft-skills family was about **78%** — markedly below the 88% overall — and it was also where the double-scored human analysts disagreed with *each other* most. In other words, much of the "error" on these items is really irreducible subjectivity in the rubric, not a clean machine failure.

**Poor audio and cross-talk.** On the subset of calls with heavy background noise, overlapping speakers, or low recording quality, ASR word error rate roughly doubled (to ~24%), and scoring agreement dropped in step. When the transcript is wrong, the score built on it is wrong; this is a transcription-quality problem propagating into QA, not a reasoning failure per se.

**Multi-intent calls.** On the roughly 6% of calls that contained several distinct issues (e.g., a billing dispute that became a retention conversation), Auto-QA sometimes scored the wrong segment of the conversation — applying, say, resolution criteria to the wrong intent. These are comparatively rare but produce confusing, hard-to-trust scores when they occur.

A smaller, time-localized effect: in week 7 one team's agreement dipped before recovering, which we traced to a transient change in call mix rather than to the model. We note it for completeness; it did not move the aggregate figures materially.

## 6. Threats to validity and limitations

Several constraints bound how far these results generalize. They are stated plainly because a reader skimming only the results would otherwise over-read them.

- **English-only, single region.** Every call in the pilot was in English from one region. Multilingual and cross-region performance is **untested**, and there is no basis in this study for assuming it transfers.
- **One scorecard, three similar teams.** All three teams shared a single scorecard and a broadly similar call mix. Scorecards are organization-specific; agreement on a *different* scorecard, vertical, or business line may differ and was not measured.
- **No downstream impact measured.** This is the most important limitation to keep in view. The study measured *agreement and effort*, not outcomes. We did **not** measure any effect on CSAT, average handle time, compliance-incident rates, agent performance, or coaching effectiveness. Any claim that Auto-QA improves those is unsupported by this work.
- **Eight-week window.** The pilot may not capture seasonal shifts in call mix, staffing changes, or longer-term model or process drift.

## 7. Discussion

The headline reading is straightforward: on this scorecard, with these teams, Auto-QA produced scores a supervisor would mostly recognize as reasonable, at full coverage, for a fraction of the analyst time. The places it struggled — subjective items, bad audio, tangled multi-intent calls — are intuitive and, for the subjective items, are places humans struggle too.

The headline reading is also easy to over-extend, and we want to head off two specific over-extensions.

First, several stakeholders who saw early numbers asked whether the ~70% analyst-time reduction could be converted into a **cost or headcount saving**. We deliberately did **not** model cost, savings, or headcount, and the ~70% figure should not be read as a business case. It is a measurement of analyst effort on the scored teams during the pilot; turning it into a dollar figure would require a dedicated cost study with assumptions this work did not make.

Second, the agreement numbers invite the conclusion that Auto-QA could simply **replace** the manual QA function or become the **authoritative agent score**. The ~85% human ceiling alone should temper that: a system at 88% agreement is operating near the reliability of the humans it would replace, not comfortably above them, and it does so unevenly across item types. Making it the system of record for agent scoring today would put a known-imperfect, partly-subjective score directly into agent evaluation, with no human check on the disputed cases where it is least reliable.

## 8. Recommendation

The results justify a **controlled expansion to two additional teams, run in shadow mode** — Auto-QA scores are produced and reviewed, but are **not** yet authoritative and do not feed agent evaluation. Before Auto-QA could be considered as a system of record, we recommend three concrete steps:

1. **Calibrate the soft-skills items with QA leads**, since that family is both the lowest-agreement and the most subjective, for humans and machine alike.
2. **Run a multilingual evaluation** before any rollout beyond the English, single-region setting tested here.
3. **Keep human QA in the loop**, especially for disputed scores and the call types (poor audio, multi-intent) where the system is least reliable.

We explicitly do **not** recommend making Auto-QA the authoritative agent score at this stage, and we do not recommend retiring manual QA.

---

## Appendix A — Per-item agreement (sample)

| Scorecard family | Items | Item-level agreement |
|---|---|---|
| Compliance | 4 | ~95% |
| Resolution / process | 4 | ~90% |
| Soft-skills / empathy | 4 | ~78% |
| **All items (overall)** | **12** | **88%** |

## Appendix B — System configuration

Auto-QA v0.9; internal instruction-tuned LLM (build pinned for the pilot window); scorecard-rubric retrieval per item; generation temperature 0.2; full rubric supplied in-context (~32k token budget); ASR pipeline WER ~12% on clean audio with diarization; mean scoring latency ~8s/call. Prompt templates, retrieval parameters, and the model build are implementation detail subject to change and are not load-bearing for the findings.

## Appendix C — Glossary

- **Exact item-level agreement** — share of individual scorecard items where Auto-QA and the human assigned the identical score.
- **Within-one-point agreement** — share of calls where the overall 100-point Auto-QA score is within one point of the human's.
- **Compliance recall** — share of human-identified compliance breaches that Auto-QA also flagged.
- **Coverage** — share of interactions actually scored.
- **Cohen's κ** — chance-corrected agreement statistic; 1.0 is perfect, 0 is chance.
