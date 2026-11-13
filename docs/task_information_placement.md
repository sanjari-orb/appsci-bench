# Task information placement: handed vs. elicited

> **Status: DRAFT — for discussion.** Not normative. [`tasks/README.md`](../tasks/README.md) remains
> the authoritative authoring guide until this is agreed.

The benchmark scores the process of building an agent from a vague requirement: clarify → design →
build → validate. That only works if the requirements are genuinely out of the agent's reach. If they
sit in a file it can open, the run measures file access instead.

This document is the taxonomy for distributing a task's information across the available paths. It
proposes a default tier per information class, and lists the questions the defaults do not settle.

## Vocabulary

| Term | Meaning |
|---|---|
| **Handed requirement** | The agent can read it for itself — it is in a file mounted into its container. |
| **Elicited requirement** | Carried only in the user-sim's context. The agent's only route is asking. |
| **Agent-facing** | Reaches the agent under test. |
| **Sim-only** | Reaches the user-sim, never the agent. |
| **Held out** | Reaches neither at runtime — evaluator only. Scoring reads it, so it must never be inferable from an agent mount. |
| **Authoring only** | Reaches no running component. Documents *for the humans* building or reviewing the task. |

Held out and authoring-only are both invisible at runtime, but they are worth separating: held-out
material is what scoring compares against, so a leak there corrupts the measurement. Authoring-only
material has no scoring role, so it needs care only where it quotes something held out.

## Where information can live

What belongs in each path, and what must not. **Tier** is who can ever see it. **Maybe** is the column
that carries the judgement — those are the calls the taxonomy below is meant to settle.

| Path | Tier | Expect | Never | Maybe |
|---|---|---|---|---|
| `spec.yaml` — the file on disk | held out | the complete task definition: objectives and weights, resource host `location`s, the eval dataset pointer | — | — |
| `spec.yaml` → `/spec` — the **redacted** copy | agent-facing | nine allowlisted fields only: `task_id`, `name`, `description`, `business_goals`, `dataset_files`, `technical_docs`, `interface_design`, `artifacts`, `budgets`; plus each resource's `name` / `type` / `description` and any `access_control` | requirements restated in the free-text fields — they survive redaction verbatim | how much of the interface contract to state; volumes and latency targets |
| `environment/data/` → `/data` | agent-facing | source data, schemas, unlabelled inputs, templates, reference material the agent must work *from* | labels, answer keys, anything byte-identical to a held-out file, examples of the scored output | one unlabelled worked input; a sample whose class balance is representative |
| `environment/build/` — loaded into the **build** environment | agent-facing | roughly 20% of the records, chosen for category coverage and referentially closed | **any record an eval case touches** — the agent reads this store through the task's own tool or database interface, so a record here is a record it can enumerate | how far below 20% closure forces it; whether a near-duplicate of an eval record is itself a leak |
| `environment/eval/` — loaded into the **eval** environment | held out | the task's full store, the build share included — one unambiguous whole, since it is what a state-based oracle replays against | being reachable during the build phase — the environment holding it does not exist until the build one is torn down | — |
| `environment/sop/` → `/sop` | agent-facing | domain background, vocabulary, system and tool documentation, process context | decision rules, thresholds, KPI or success-metric documents, label taxonomies, rubric restatements | qualitative stubs — *"there is a per-bag fee"* without the number |
| `user_sim_docs/<file>` | sim-only | the elicited requirements themselves: thresholds, rules, taxonomies, tie-breaks, output contract | anything also present under `environment/` (duplication defeats the test); filenames the agent could be told | material that is arguably derivable — cheap to hide, but adds turns |
| `user_sim_docs/common/` | sim-only | only the doability floor: what no persona may withhold at any level | anything intended to differentiate L1/L2/L3 — this path is unconditional, so material here cannot be level-gated | what the floor has to include for L1 to stay playable (Q4) |
| `user_personas/*.yaml` | sim-only | `domain_knowledge` as a closed fact set, priorities and tone, planted wrong beliefs for adversarial variants | agent-reachable paths in prose; requirements stated so loosely they survive paraphrase into something wrong | whether levels differ in *what* they know or only in *how* they answer (Q4) |
| `eval_data/<id>/` | held out | eval inputs, `expected_output`, gold artifacts, per-case expectations | anything reachable from an agent mount, by path or by content hash | — |
| `TASK_SOP.md` *(found by convention, not named in the spec)* | held out | the single ground-truth definition everything else is authored *from* | being mounted, or quoted verbatim into the spec | how much of it to restate in `description`, which does reach the agent |
| `provenance/<domain>/` *(repo root, not under `tasks/`)* | held out | vendored upstream source material and the field mapping / commit pin that make a conversion auditable | being reachable via `task_runner.bundle` | — |
| `seed_data/` | held out | upstream source material and provenance | being reachable via `task_runner.bundle` if it contains labels | unlabelled fixtures the environment legitimately loads |
| `RESOURCES.md` | authoring only | where resources live, for a human building or reviewing the task | being treated as the agent's map — it is not mounted, and no spec field points at it | — |
| `README.md`, `KNOWN_ISSUES.md` | authoring only | design intent, open issues, provenance | — | — |

## Taxonomy by information class

The test to apply per item, before reading the table: **is it derivable from the environment, or is it
a choice someone made?** A table schema, a row count, a value range, domain vocabulary — the agent can
establish these by exploring, so handing them over costs nothing. A threshold, a taxonomy, a key
spelling, a tie-break order, a definition of "material" — no amount of exploration yields these,
because they are not properties of the data. If a choice is scored, it is a candidate for elicitation.

Tiers as defined above, plus **TBD** — meaning the authored tasks are not aligned with each other, so
there is no default to state yet and the open question is named instead.

| Class | Tier | Note |
|---|---|---|
| Thresholds, business constants | **Elicited** | flips only when industry-standard and unmemorable, where hiding it just adds turns |
| Decision rules, tie-breaks | **Elicited** | the core of what the benchmark measures |
| Label taxonomy / enum values | **Elicited** | unless dictated by an external system the agent can inspect |
| Tone, audience, style preference | **Elicited** | scored only loosely |
| Rubric wording, objective weights | **Held out** | never handed; a persona may state priorities qualitatively |
| Data dictionary, source schema | **Handed** | derivable from the data — unless it encodes a customer choice |
| Volumes, latency targets | **Handed** | either channel works; pick one, stating both is drift |
| Tool / action space | **Handed** | tool discovery is not what these tasks measure |
| KPIs, definition of done | **TBD** — Q1 | agent-facing in one task, sim-only in another |
| Output schema, key names | **TBD** — Q2 | sim-only in one task, agent-facing in two |
| Worked / labelled examples | **TBD** — Q3 | held out in one task, agent-facing in another |
| Records an eval case is drawn from | **Held out** | in the eval environment only; the build environment must not contain them ([task_env_split.md](task_env_split.md)) |

## Cross-task picture

Current tier per information class, as authored today. 

| Task | Thresholds / rules | Label taxonomy | Output schema | Worked examples | KPIs | Rubric / weights |
|---|---|---|---|---|---|---|
| `order_to_cash` | sim-only | sim-only, but see below | sim-only | held out | **agent-facing** | held out |
| `slides_generator` | sim-only | n/a | **agent-facing** | **agent-facing** | agent-facing | held out |
| `airline_rebooking` | sim-only | n/a | agent-facing | n/a | sim-only | sim-only |
| `compliance_review` | sim-only | sim-only | sim-only | held out | agent-facing | held out |
| all other tasks | not authored | not authored | not authored | not authored | not authored | not authored |

Bold marks a cell that disagrees with another task's answer for the same class — the three TBD rows in
the taxonomy above.

Three specifics behind those cells, kept short because the resolution is a discussion, not a fact:

- `order_to_cash` mounts a KPI document naming its four scored objectives in order and marking one
  "(Critical)" in line with its weight. The same file also enumerates the fault categories in prose
  (*"price issue, quantity mismatch, off-contract charge, duplicate, etc."*), so the taxonomy is
  sim-only in its authoritative form — the six exact enum values and their spelling — while an informal
  version of it is handed over. Its resource description restates the metrics inside the redacted spec.
- `slides_generator` mounts a KPI document, but written to business concerns only. Its "worked example" cell is the build-time sample under `environment/data/`, which
  carries request **inputs only**; the reference deck (`gold.json` + `gold.pptx`) moved to
  `sample_reference/`, which no container mounts. Everything technical — the PowerPoint format mechanics
  and how a request is packaged — is elicited.
- `airline_rebooking` relocates its policy correctly, but `interface_design` survives redaction and
  still carries the confirmation gate and the eligibility-validation duty — three of the four things
  its policy objective scores. Removing a mount does not close a spec field.

## Open questions

Each question carries a **proposal** — a starting position to argue against, not a decision.

1. **KPIs and metric names — handed or elicited?** Tasks currently disagree, both deliberately. If
   elicited in general, the `kpi` resource type is structurally a trap: its location must be
   agent-reachable while its description restates the metrics.

   **Proposal: keep KPIs handed; rely on thresholds and taxonomies being elicited.** A customer who
   cannot say what "good" means has no project, so a KPI document is realistic framing rather than a
   leak, and it keeps the `kpi` resource type. The load-bearing requirements — where the bar sits, which categories exist — stay elicited, so the clarification conversation still has to happen. This costs something real: a KPI document is one paraphrase away from the rubric, so a handed one must name the *business* concern and never the objective set, its ordering, or its weights. This contradicts current O2C so it requires team discussion. 

2. **The output/response contract — elicited or handed?** Three positions are live. *Always elicited*:
   the response shape is a customer choice like any other. *Handed*: it is interface plumbing, and
   making the agent guess key spellings measures secretarial work rather than requirements gathering.  *Split*: elicit the semantics (taxonomy, thresholds, what counts as a fault), hand the syntax (key spelling, envelope, file
   format). The split is the option nobody has tried.

   **Proposal: optional by task** 

3. **Is a handed worked example ever legitimate?** One task documents one as pedagogy, another removed
   one as fitting. 

   **Proposal: optional by task, but never extracted from the real eval_data**

4. **Should persona level create an information gradient?** It does in one task and not in another. If
   it should, some personas need differentiating; if not, some L1/L2/L3 splits are over-engineered.

   **Proposal: yes — level should vary what is known, not only how it is said.** Otherwise the levels
   are a tone setting and the benchmark has no difficulty axis. The natural reading: L1 knows the
   business outcome but not the mechanism, L2 knows the mechanism, L3 knows the mechanism and the edge
   cases. 

5. **Should `sop/` and `environment/sop/` both exist?** They are different tiers with nearly the same
   name: `environment/sop/` is mounted at `/sop` and handed to the agent, a top-level `sop/` is not
   mounted at all. The mount source is hardcoded (`orchestrator/run.py:746`), so the directory name
   carries no visibility meaning on its own — the `environment/` prefix is the only signal.

   All three layouts are in use today:

   | Task | `environment/sop/` (agent-facing) | `sop/` (held out) |
   |---|---|---|
   | `order_to_cash` | `contract_terms.md`, `kpis.md` | — |
   | `slides_generator` | `kpis.md` | `TASK_SOP.md` |
   | `airline_rebooking` | `kpis.md`, `policy.md`, `tools_reference.json` | `TASK_SOP.md.j2`, `kpis.md`, `seed/` |

   Note `airline_rebooking`: **`kpis.md` exists in both**, one held out and one handed to the agent.
   Same filename, opposite tiers, one task.

   This can create potential issues. A reviewer scanning for "is the policy hidden?" sees a
   plausible-looking `sop/policy.md` and cannot tell the tier from the filename. Moving a file between
   the two silently changes who can read it, with no diff signal beyond the path. The static spec
   guardrail (`core/spec.py`'s `agent_spec_leaks`) still treats the bare token `sop/` as a
   ground-truth marker, so the two directories are not distinguishable to it. The runtime leakage
   scanner (`scripts/detect_task_leakage.py`) no longer carries that marker — it was dropped precisely
   because of this collision, since the agent's own legitimate mount produces indistinguishable
   strings; every real reference to held-out SOP content is repo-root-relative, so the `tasks/` marker
   plus the literal-content layer still cover a genuine leak.

   **Proposal: rename the held-out one to `ground_truth/`.** The tier becomes legible from the path, so
   a reviewer and the static guardrail both stop having to infer it from a prefix, and that guardrail's
   `sop/` marker stops being ambiguous. Two tasks would migrate (`slides_generator`,
   `airline_rebooking`) and `airline_rebooking`'s duplicate `kpis.md` resolves by construction. **No
   task has migrated yet** — a rename is only worth doing repo-wide, since one task with a different
   name for the same tier is worse for a reviewer than a name that is uniformly ambiguous. The
   alternatives are weaker: folding into `seed_data/` conflates upstream provenance with authored
   ground truth, and merely forbidding duplicate basenames fixes the collision while leaving the
   naming ambiguity in place.

6. **Should `/sop` and `domain_knowledge` be allowed to duplicate?** Deliberate redundancy, or a
   silently defeated elicitation test — currently unfalsifiable either way.

   **Proposal: forbid it for anything scored.**

   **`compliance_review` applies this proposal**: its four policy files under `environment/data/`
   used to duplicate the clause-level rules already stated in every persona's `domain_knowledge`,
   defeating elicitation for that content. Trimmed to name + applies-to only, with the clause rules
   moved to `user_sim_docs/common/` (sim-only) and dropped from `domain_knowledge` (no longer needed
   there either, since the sim renders `common/` content on its own).


# Possible test: 
Run a simulation with a persona that only responds "I don't know" and score it to see what requirements are still fulfilled. 

