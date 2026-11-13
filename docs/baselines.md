# Baselines for appsci-bench

What we compare the AppSci agent against, and what each comparison isolates. Survey done 2026-08-11;
every repo fact below (stars, license, last push, shipped task adapters) was checked against the
GitHub API on that date. Leaderboard numbers are quoted with their source and their caveats.

This doc is about **runnable** baselines. The design-oriented literature review lives in
`.ai-work/sessions/research-2026-05-01-appsci-bench-literature/literature-review.md`; the novelty
argument is in `.ai-work/notes/project_novelty_gaps.md`. Nothing here repeats those.

## The headline finding

**No existing system is a drop-in substitute for the AppSci agent.** Every candidate fails on one of
two things:

1. **The requirement is not given as a spec.** Every agent-design and workflow-optimization system in
   the literature (ADAS, AFlow, AgentSquare, MaAS, DSPy, GEPA, TextGrad, Trace, …) takes a *metric
   plus a labelled trainset* as its input. appsci-bench deliberately withholds both — the agent has to
   elicit them from a user sim. These systems can only substitute for the AppSci agent's
   **optimization layer**, not for the whole pipeline.
2. **The deliverable is not a served agent.** General coding agents (OpenHands, SWE-agent, MetaGPT)
   can in principle write and serve an artifact, but none of them are built to interview a user, and
   none implement our session API.
3. **The artifact it designs is static, not agentic.** Most agent-design search methods emit a fixed
   call graph evaluated once per query, not a stateful tool-using agent. See the Tier B filter below —
   of that whole family, only AgentSquare searches over agentic artifacts, and it has no license.

That is the same gap the novelty map calls Gap 1 and Gap 3, now confirmed against code rather than
papers. The practical consequence: **external Tier-A baselines will score near the floor on
elicitation, and that is the expected, reportable result** — not a sign the harness is misconfigured.
Budget for wiring each of them to `appsci_agent_user_sim` and to the session API; that shim is ours to
write either way.

## Three tiers

| Tier | What it substitutes for | What the number isolates | Where it plugs in |
|---|---|---|---|
| **A** | The whole AppSci agent | Value of the applied-science *process* (elicit → design → build → validate) | `--applied-scientist` |
| **B** | The AppSci agent's optimization layer (A.9) only | Value of *our* technique-selection + internal-eval loop, holding the rest fixed | Inside our agent, replacing the optimizer |
| **C** | The produced artifact | Difficulty of the underlying task; ceiling the built artifact is chasing | Directly as an artifact in `--phase eval` |

Tier C matters most for interpreting Tier A: if a hand-written policy handed to a frontier model
already scores what the AppSci agent's artifact scores, the process added nothing measurable and we
need to know that before publishing.

---

## Tier A — AppSci-agent substitutes

All must accept `prompts/applied_scientist_protocol.md`, talk HTTP to the build-time user sim, and
end with an endpoint serving `create_session` / `query_session` / `delete_session`
(`docs/artifact_agent_api.md`).

| System | Stars | License | Last push | What it is | Cost to us |
|---|---|---|---|---|---|
| **OpenHands** (`OpenHands/OpenHands`) | 83.7k | MIT | 2026-08-11 | General dev agent, sandboxed shell + browser + editor | **Medium.** Give it the protocol as the task prompt; it can already write a server and curl the user sim. Needs a conformance-probe shim and budget caps. |
| **SWE-agent** (`SWE-agent/SWE-agent`) | 20.0k | MIT | 2026-08-10 | Constrained agent-computer interface over a repo | **Low-medium.** Cheapest external Tier A. Narrower ACI than OpenHands, so likelier to fail the "serve an endpoint" step. |
| **RD-Agent** (`microsoft/RD-Agent`) | 14.2k | MIT | 2026-08-04 | R&D loop: hypothesis → implement → evaluate → feedback. Scenarios: `data_science`, `kaggle`, `qlib`, `general_model`, `finetune`, `rl` | **High, highest conceptual value.** Closest published analog of an applied scientist. Needs a new scenario; all shipped scenarios assume a given metric and a tabular/ML task. |
| **AutoAgent** (`HKUDS/AutoAgent`) | 9.7k | MIT | 2025-10-16 | "Zero-code" framework that builds agents from natural-language instructions | **Medium**, but **stale ~10 months.** Literally the requirement→agent shape; the closest thing to a published Tier-A peer. Expect dependency rot. |
| **MetaGPT** (`FoundationAgents/MetaGPT`) | 69.8k | MIT | 2026-01-21 | SOP-driven multi-agent "software company"; requirements → code | **Medium.** The canonical requirements→software baseline. Fixed SOP, so it will not adapt its process to ambiguity — which is exactly the contrast we want to show. |
| **AIDE** (`WecoAI/aideml`) | 1.5k | MIT | 2026-08-09 | ML-engineering agent; the scaffold behind the reported MLE-bench results | **Medium.** Solution-tree search over code. Optimizes against a given metric, so it is Tier A only in a degraded, spec-is-handed-to-it mode. |
| **DGM** (`jennyzzt/dgm`) | 2.2k | Apache-2.0 | 2025-08-13 | Darwin Gödel Machine — agent that edits its own code | **High**, stale. Interesting only if we want a self-improvement axis; needs a scored benchmark loop it can climb. |

**Also needed, and ours to write: the null baseline (A0).** A single-shot agent that reads only the
task's short description and the environment, skips the interview entirely, and wraps a frontier model
with the domain tools. This is the floor that makes every Tier-A number interpretable. Cheap — it is
an ablation of `appsci_agent`, not a new integration.

## Tier B — optimization-layer substitutes

### First filter: does it produce an *agentic* artifact?

The acceptance criterion is the artifact's **runtime shape**, not what the system optimizes. Our
artifact must hold a session, call tools against a live environment, and survive multiple turns
(`docs/artifact_agent_api.md`). Most of the agent-design literature produces a *static* artifact —
a fixed call graph evaluated once per query. Sorting the candidates this way:

| Group | Systems | Artifact at runtime |
|---|---|---|
| **1 — builds an agentic artifact** | OpenHands, SWE-agent, mini-swe-agent, MetaGPT, AutoAgent, DGM (RD-Agent partially — pipeline-shaped) | Writes code for a served, stateful, tool-using agent |
| **2 — optimizes an agentic artifact you own** | GEPA, DSPy, TextGrad, Trace | Shape-agnostic: they tune text components of whatever system you define, including a tool loop. GEPA's `langchain_adapter` explicitly covers tool-using agents and LangGraph graphs |
| **3 — produces a static call graph** ⚠ | ADAS, AFlow, MaAS, GPTSwarm, ScoreFlow, MAS-GPT (Cognify / syftr / Optimas likely too — unverified) | One stateless pass per query. ADAS is the verified case: `forward(self, taskInfo)` called once per example |
| **4 — agentic, but blocked** | AgentSquare | Verified agentic: `ToolUseBase.__call__(task_description, tool_instruction, feedback_of_previous_tools)` is an iterative tool loop, and `MemoryBase` subclasses (DILU / Generative / TP / Voyager) carry state across steps. **No license file** |

**Consequence: of the published agent-design-search methods, exactly one searches over agentic
artifacts — and it is the unlicensed one.** Everything else in that family searches over static
graphs. Group 2 survives the filter precisely because it does not dictate artifact shape, which is why
GEPA and DSPy are the recommended A.9 substitutes and the ADAS/AFlow family is not.

These replace the A.9 layer (technique selection, internal eval synthesis + judge, prompt/model/
topology search). They need a scalar metric and a trainset; in our setting those come from the
*agent's own* internal eval, which is the thing being compared.

| System | Stars | License | Last push | Optimizes | Shipped task adapters | Cost to us |
|---|---|---|---|---|---|---|
| **GEPA** (`gepa-ai/gepa`) | 6.1k | MIT | 2026-08-11 | Prompts + code, by reflective evolution | `mcp_adapter`, `optimize_anything_adapter`, `langchain_adapter`, `generic_rag_adapter`, `dspy_adapter`, `terminal_bench_adapter`, … | **Lowest.** `mcp_adapter` + `optimize_anything_adapter` fit a black-box served artifact directly. Top pick. Note the adapters README documents only 4 of the 10 dirs in the tree — read the tree, not the README. |
| **DSPy** (`stanfordnlp/dspy`) | 37.1k | MIT | 2026-08-11 | Prompts/demos in a declarative program (MIPROv2, SIMBA) | — (library, not benchmarks) | **Medium.** Requires the artifact expressed as a DSPy program, or a custom module wrapping our session API. Canonical, so worth the cost. |
| **AgentSquare** (`tsinghua-fib-lab/AgentSquare`) | 229 | ⚠ **none** | 2025-11-04 | Modular design space: Planning / Reasoning / Tool-use / Memory | `alfworld`, `webshop`, `sciworld`, `travelplanner`, `m3tooleval` | **Medium.** The only search space here that already targets *interactive* tasks, and the closest match to "choose the topology". **No license file — cannot vendor or ship against.** Contact authors or reimplement the search. |
| **AFlow** (`FoundationAgents/MetaGPT`, `metagpt/ext/aflow`) | — | MIT | — | Workflow structure (code-level operators), MCTS search | `gsm8k`, `math`, `humaneval`, `mbpp`, `hotpotqa`, `drop` | **Medium-high.** All shipped benchmarks are single-turn QA/code; needs a new benchmark class for a multi-turn served artifact. |
| **ADAS** (`ShengranHu/ADAS`) | 1.6k | Apache-2.0 | 2025-01-28 | Full agent code, via a meta-agent that programs agents | `_arc`, `_drop`, `_gpqa`, `_mgsm`, `_mmlu`, `_transfer_math` | **High — higher than the README implies.** See the ADAS note below. Best conceptual fit (searches topology, not just prompts); worst structural fit. |
| **TextGrad** (`zou-group/textgrad`) | 3.7k | MIT | 2025-07-25 | Text "gradients" over prompts/variables | — | Medium. Alternative paradigm; include only if we want a second optimizer family. |
| **Trace** (`microsoft/Trace`) | 753 | MIT | 2026-06-17 | End-to-end generative optimization of agent code | — | Medium. |
| **MaAS** (`bingreeky/MaAS`) | 280 | ⚠ **none** | 2025-11-13 | Agentic supernet → per-query architecture (ICML'25 oral) | `gsm8k`, `math`, `humaneval` | **No license — same blocker as AgentSquare.** |
| **EvoAgentX** (`EvoAgentX/EvoAgentX`) | 3.2k | MIT (file is MIT; GitHub reads NOASSERTION due to curly quotes) | 2026-07-07 | Self-evolving agent workflows | `gsm8k`, `hotpotqa`, `humaneval`, `mbpp`, `math`, `bbh`, `livecodebench`, `nq`, `WorfBench`, `real_mm_rag` | Medium. Broadest shipped benchmark set of the evolution frameworks. |
| Also: **Cognify** (280, Apache-2.0), **syftr** (344, MIT), **Optimas** (82, Apache-2.0), **ScoreFlow** (97), **GPTSwarm** (1.0k, MIT), **ShinkaEvolve** (1.3k, Apache-2.0), **OpenEvolve** (6.9k) | | | | Workflow/param autotuning and program evolution | mostly QA/code | Second wave — only if the first two prove insufficient. |

For the taxonomy behind this table, the IBM survey *From Static Templates to Dynamic Runtime Graphs*
(arXiv 2603.22386, `IBM/awesome-agentic-workflow-optimization`) enumerates ~45 methods split by
*when* workflow structure is fixed (offline template search / node-level / joint / runtime-adaptive).
It links papers only, no code — hence the repo-by-repo check above.

## Tier C — artifact-level reference points

Our `airline_rebooking` task is converted from tau2-bench airline (50 tasks, 14 tools, real FlightDB),
so the τ-bench line of work is the natural external anchor.

| Reference | What it gives us | Status |
|---|---|---|
| **tau2 default `LLMAgent` + frontier model, scored by *our* eval** | The apples-to-apples artifact ceiling: the domain policy handed over directly, zero applied-science process | **The single most important number to produce.** Implement `HalfDuplexAgent` behind our session API — ~a day. |
| **`gold/airline_rebooking/`** | Our own reference artifact | Already in repo |
| **`human_baseline/`** | Human applied scientist on the same task | Already in repo (pilot done) |
| **tau2-bench submission JSONs** (`web/leaderboard/public/submissions/`) | External calibration: per-domain `pass_1..pass_4` + config for 65 submissions. Primary source — prefer over taubench.com's rendered view | See caveats below |
| **`amazon-agi/tau2-bench-verified`** (45★, MIT, 2026-04) | Corrected τ² tasks — cross-check our 50 converted cases for known-bad items | Worth a one-off diff |

### Published numbers — use the airline column, not the headline

**Source of record: the submission JSONs committed in the tau2-bench repo**, at
`web/leaderboard/public/submissions/<model>_<org>_<date>/submission.json` (65 submissions as of
2026-08-11). Each carries per-domain `pass_1..pass_4`, `user_simulator`, `tau2_bench_version`, and a
`methodology.notes` field. Prefer these over the rendered site: they are versioned, greppable, and
state the configuration behind each number.

**Every one of those rows ran `gpt-5.2` as the user simulator.** Read from the submissions themselves: all 8 text airline rows at `tau2_bench_version` 1.0.1 declare `user_simulator: "gpt-5.2"` (it is even in the trajectory filenames, e.g. `claude-opus-4-5_high_airline_gpt-5.2_4trials.json`). Rows declaring `gpt-4.1-2025-04-14` are all on v0.1.3/0.1.3; rows declaring `v1.0` are the voice board, where the field carries a sim *version* rather than a model. Note tau2's code default `DEFAULT_LLM_USER` is STILL `gpt-4.1-2025-04-14` at v1.0.1 — the published runs override it, so reading the config file gives the wrong answer about what these numbers were measured against. Our own pin now follows the declared field (`appsci_bench/eval/artifact_agent_user_sim.py`).

The site's headline τ²-bench figure is the **mean of the three text domains**. That mean is dominated
by telecom, which every frontier model nearly saturates — so it is the wrong anchor for us. The
airline column, which is the one we care about, has 8 Sierra-run rows (all submitted by Sierra itself,
so no vendor self-reporting):

| airline `pass_1` | Model | Reasoning |
|---|---|---|
| 84.0 | Claude Opus 4.5 | high |
| 83.0 | GPT-5.2 | high |
| 82.5 | GLM-5 / Gemini 3 Flash | enabled / high |
| 81.5 | Qwen3.5-397B-A17B | enabled |
| 80.5 | Gemini 3 Pro | high |
| 72.0 | Claude Sonnet 4.5 | enabled |
| **52.5** | **GPT-5.2** | **none** |

**The airline ceiling is 80–84 for a reasoning-enabled frontier model.** The 52.5 outlier is the same
GPT-5.2 as the 83.0 row with reasoning turned off — **30.5 points from one config flag.** Check
reasoning effort before attributing any gap to agent design.

For context, the 3-domain means the site headlines: Qwen3.5-397B-A17B 87.9 (airline 81.5, retail
84.43, telecom 97.81), Gemini 3.0 Pro 85.4 (73.0 / 85.3 / 98.0), Claude Opus 4.5 85.3
(84.0 / 79.61 / 92.32).

### `final_score` is now exactly `task_success` — and both moved

**Changed as of the tau2-parity pass; figures below this line from runs before it are not
reproducible.** Two things changed in how `airline_rebooking` is scored:

1. **`policy_compliance` is now `weight: 0`.** It has no tau2 counterpart — every one of the 50 cases
   sets `reward_basis` to `["DB", "COMMUNICATE"]`, so upstream carries `nl_assertions` *without*
   scoring them into reward (their NL evaluator is marked experimental and exposed as a preview-only
   evaluation mode). It is still computed and still reported under `per_metric`, so the diagnostic
   signal is intact; it simply no longer moves the headline. `final_score == task_success` now.
2. **`task_success` gained upstream's premature-termination gate.** A conversation cut off by the
   `max_turns` ceiling scores 0 before DB or COMMUNICATE is evaluated, exactly as
   `src/tau2/evaluator/evaluator.py` returns 0 for any run not ending in `AGENT_STOP`/`USER_STOP`.
   Paired with it: `max_turns` 10 → 20, and every sample now carries a stop criterion covering a
   human handoff or a dead end, so the ceiling rarely binds and the gate is fair rather than merely
   harsh.

- **`task_success` = DB × COMMUNICATE — this *is* tau2's airline reward**, binary per case, and the
  only number comparable to a published `pass^1`.
- **`policy_compliance` is a graded LLM-judge transcript score** (continuous: we see 0.1, 0.55, 0.95,
  1.0 in run `airline_rebooking_20260731_155934`). tau2 has no such component.

**Pre-gate figures, retained for history only.** The 20260731 smoke run predates all of the above: it
reported `final_score` 0.76 = mean(`task_success` 0.80, `policy_compliance` 0.72) under equal weights
and with no termination gate. Two of its five cases ran to the 10-turn ceiling *after* the agent had
already transferred to a human, and one of those scored `task_success` 1.0 on a truncated trace
because its case requires no writes and no communications. **Do not quote 0.80 against the
leaderboard** — re-run before quoting anything.

### Current airline measurement: `task_success` 0.70 (35/50)

**`claude-opus-4-8`, 2026-08-23, upstream v1.0.1 configuration (`gpt-5.2` user simulator), `repeat: 1`.**
`policy_compliance` 0.778 at `weight: 0`, so `final_score` equals `task_success` as intended.

The run is internally clean: all 50 cases ended on a user stop, so the termination gate never fired and
zeroed nothing; no case reached the 20-turn ceiling (mean depth 3.10, max 10), so neither the gate nor
the raised ceiling distorted the result; `task_success` is binary per case as upstream scores it; and
**zero cases hit a gateway or artifact error** — the serialized-tool-call workaround held for all 50.

**Switching the user simulator to the one the published rows use moved the score, and only upward.**
Same artifact, same 50 cases, sim as the only variable:

| Sim | Score | Improved | Regressed |
|---|---|---|---|
| `gpt-4.1-2025-04-14` (v0.1.3-era) | 29/50 = 0.580 | — | — |
| **`gpt-5.2` (v1.0.1 submissions)** | **35/50 = 0.700** | **6** | **0** |

Six discordant pairs, all one direction (cases 8, 12, 17, 18, 30, 37) — exact binomial sign test
p ≈ 0.031, so a directional effect rather than noise. A more capable simulated customer supplies
information more competently and follows its scenario more faithfully, so the agent gets a fairer run at
cases it had been failing partly because of the simulator. Nothing regressed, which is what you expect
if the old sim was the limiting factor rather than a differently-shaped adversary. Note an 8-case smoke
of this same change showed **zero** movement — it happened to contain none of the six cases that moved.
Do not read a subset as an early signal for this metric.

**Against the leaderboard: 0.70 vs the 80–84 airline band** (Opus 4.5 at 84.0). Three things govern how
much of that 14-point gap is real:

- **Our artifact serializes its tool calls** to survive the gateway's parallel-tool-use defect (below).
  tau2's agent parallelizes. A real deviation from upstream, expected small on this domain but unmeasured.
- **Single trial.** This estimates the same quantity as their `pass^1` (which averages 4 trials), just
  noisier — not a different quantity. Trials are PR #110.
- **The artifact predates the elicitation change (#100).** It was built when the agent was handed the
  policy and tool reference; both are now withheld and must be elicited. So this number reflects an
  easier build task than a current build faces.

With the simulator corrected, the remaining gap is a genuine agent-quality gap rather than an artifact of
the wrong measurement configuration.

#### Superseded, kept for the record: 0.58 (29/50), 2026-08-17

Ran on the old user-simulator pin, so it is not comparable to the 80–84 column it was measured against. Retained because its diagnostics are what first validated the termination gate, and because the paired comparison above needs it as the baseline:
`policy_compliance` came in at 0.731 and, at `weight: 0`, left `final_score` equal to `task_success`
as intended. The run is internally clean: all 50 cases ended on a user stop, so the gate never fired
and zeroed nothing; no case reached the 20-turn ceiling (mean depth 2.9, max 11), so neither the gate
nor the raised ceiling distorted the number; and `task_success` came out binary per case, as upstream
scores it.

Two gateway faults shaped that run, both still relevant:

- **The artifact was patched to serialize its tool calls.** The gateway's OpenAI-compatible route
  cannot translate parallel tool use to Bedrock — an assistant turn with N>1 `toolUse` blocks gets one
  `toolResult` back, and the next request dies with `400 Each 'toolUse' block must be accompanied with
  a matching 'toolResult' block`. Reproduced standalone (1 call passes, 2 fail); `parallel_tool_calls=False`
  is accepted and silently ignored. Unpatched, the agent lost 34 of 50 conversations at ~2 turns and
  the run scored 0.48 — measuring the gateway, not the agent. tau2's own agent *does* execute every
  tool call in a turn, so serializing is a real deviation from upstream, expected to be small on this
  domain (its policy already asks for one call at a time, and observed depth is shallow) but unmeasured.
- **One case was lost to a second, undiagnosed gateway fault** (`400 required field 'input' is
  zero value`), scoring 0 — so 0.58 was a floor, 0.60 its ceiling. That fault did NOT recur in
  the 2026-08-23 run, which had zero errored cases; it is intermittent, not fixed.

`repeat` is still 1, so this is not `pass^1` either way. A note on the failure mode that hid the first
three attempts: the artifact reported these gateway errors as an apology string inside an HTTP 200, so
they were invisible to the harness, the termination gate and the trace at once, and the runs looked
like honest low scores. Our own model calls now log at WARNING and emit a `model.error` event carrying
the exception class and provider status code; an artifact's internal calls remain out of reach by
construction.

Two more things to confirm on any run before quoting it against the leaderboard: that the DB half was
actually measured (`task_success` falls back to COMMUNICATE-only and flags
`db_component: "not_measurable_yet"` when the per-sample env-reset seam is inactive — a *weaker*
criterion than tau2's, so it inflates the number), and the trial count (leaderboard rows use 4 trials;
`repeat` is still 1, so a default run is not `pass^1`).

Four things to hold onto:

- **The leaderboard has a Standard/Custom split, and scaffolds live in Custom.** `standard` means a
  general-purpose LLM on the default τ-bench scaffold with no modifications; `custom` covers modified
  scaffolds, extra tools, model routers, or training on τ-bench domains, and must carry methodology
  notes plus an implementation link. The default site view shows Standard only, and the homepage shows
  just the top 3 per track — so "the leaderboard is all bare models" is an artifact of the default
  view, not the truth.
- **On airline, no custom scaffold beats a standard frontier model** (see next section). That is the
  real basis for "a better model, not a better harness" — not an absence of attempts.
- **Aggregator sites disagree with the source and with each other.** `leaderboard.steel.dev` (updated
  2026-04-16) lists Qwen3.5-397B-A17B at 68.4% and Claude Opus 4.5 at 70.2% — different benchmark
  generation, different user-sim model, mixed self-reported and independent rows. Cite the submission
  JSONs or our own runs. Do not cite the aggregators.
- **`task_success` now matches tau2's reward component for component — with three residual
  differences.** Verified against the upstream source: COMMUNICATE is the same per-message,
  case-insensitive, comma-stripped substring rule (`evaluator_communicate.py`); DB is the same
  golden-action replay compared by state hash (`evaluator_env.py`); the two multiply exactly as
  `reward_basis` dictates (`evaluator.py`); and the premature-termination gate now matches too. What
  still differs: (1) the *agent-side* DB state — upstream rebuilds it by replaying the trajectory's
  tool calls onto a fresh env, we read the live task-env's post-run hash (same comparison, different
  route, arguably ours is the truer read); (2) our stop signal is one LLM-judged free-text criterion
  where upstream has three discrete tokens; (3) banking now uses a shared 200-step simulation ceiling, matching the upstream CLI default.
  Customer/agent text costs one step and each environment tool request/result pair costs two.
  Our artifact interface hides model-level batches, so parallel tool requests count individually;
  upstream counts one tool-call message and one environment step for an entire batch. Historical
  banking runs used a 20-exchange ceiling and a separate 16-consecutive-customer-tool guard. Leaderboard rows also
  differ among themselves in `tau2_bench_version` (Distyl's is `0.2.1-dev`), so even within the
  leaderboard rows are not strictly comparable. The meaningful anchor is a C1 run (below), not a
  leaderboard row.
  The 200-step default is verified at our banking seed commit:
  [upstream config](https://github.com/sierra-research/tau2-bench/blob/3b6a28ed8206dc31fc8b2dfdb497b8600821b463/src/tau2/config.py).
- **`pass^1` vs `pass^k`.** The submission JSONs carry `pass_1` through `pass_4`, and the drop is
  steep — Distyl ButtonAgent goes 31.19 → 13.4 from `pass_1` to `pass_4`. Report `pass^k`; single-trial
  numbers overstate a stochastic artifact.

### ADAS specifically — why the port is more than a new domain folder

The README says adding a domain means a new `_{domain}/search.py` with your own
`evaluate_forward_fn()`. `_mmlu/search.py:279` shows four assumptions that our tasks break:

- **A candidate is one stateless function.** `def forward(self, taskInfo) -> Union[Info, str]`, called
  once per example via `executor.map`. Our artifact is a session with state across turns. Using ADAS
  means redefining "one example" as "one full episode" so `forward` becomes the episode driver.
- **Ground truth is exact-match on a letter** (`LETTER_TO_INDEX[example['Answer']]`) read from a CSV.
  Ours is DB-hash × COMMUNICATE plus a graded judge. `evaluate_forward_fn` is a rewrite, not an edit.
- **The seed archive is all single-turn QA patterns** — Chain-of-Thought, Self-Consistency CoT,
  Self-Refine (Reflexion), LLM Debate, Step-back Abstraction, Quality-Diversity, Dynamic Role
  Assignment. None call a tool or hold state. The meta-agent proposes designs by mutating this
  archive, so a QA archive yields QA architectures; you would have to author a tool-loop archive
  yourself, and then the discovered designs trace back to your seeds rather than to ADAS.
- **It assumes cheap parallel evals.** Our per-sample env reset serializes to `workers=1`, and each
  eval becomes a multi-turn conversation plus a judge call. At ~30 generations × candidates ×
  `valid_size` × `n_repeat`, cost binds before code does.

Also `exec()`s model-written code (their own warning) and is 19 months stale.

**Prefer AgentSquare for the topology comparison if its license clears.** Its space is modular
(Planning / Reasoning / Tool-use / Memory) and it ships interactive tasks, so episodes and tool loops
are already handled. The trade is clean: ADAS is Apache-2.0 and architecturally wrong; AgentSquare is
architecturally right and unlicensed. Ask the authors before porting ADAS.

### The four custom submissions worth reading

Nine of the 65 submissions are `custom`; five of those differ only in the user-simulator model. The
four that change the *system* are the closest thing to a borrowable τ-bench architecture — and three
have public code:

| Submission | Score (`pass_1`) | What it does | Code |
|---|---|---|---|
| **grok-voice-think-fast-1.0 + tool-mentor** (pickle.com) | airline **70.0**, retail 76.3, telecom 78.1 | A second LLM (gemini-3.5-flash) reviews the agent's tool calls at runtime: pre-execution gates on state-mutating and escalation calls, 10s/15s deadlines, fail-open | `github.com/pickle-com/tau2-bench/tree/tool-mentor-v1.0` |
| **Nemotron-Orchestrator-8B** (NVIDIA, ToolOrchestra) | airline **56.0**, retail 84.2, telecom 88.6 | Orchestrator routes between a strong and a weak model by task complexity | `NVlabs/ToolOrchestra`, arXiv 2511.21689 |
| **RAFT-30B-A3B** | retail **82.46** | Qwen3-30B-A3B fine-tuned on synthetic retail data with entities disjoint from the eval DB; error-driven RL | `raft.hailab.io` (blog) |
| **Distyl ButtonAgent** (Distyl AI) | banking **31.19** (vs 55.2 standard best) | GPT-5.4 + custom agentic retrieval over the 698 banking docs (Mixedbread `mxbai-wholembed-v3`) | none — trajectories on Google Drive |

Read against the standard rows: **tool-mentor's airline 70.0 and Nemotron's 56.0 both sit below
Gemini 3.0 Pro's 73.0 and Claude Opus 4.5's 84.0**, and ButtonAgent loses to the standard best on
banking by 24 points. Scaffolding τ-bench has not paid off publicly so far.

Two of these are directly relevant to us anyway. **tool-mentor is a pre-execution gate on
state-mutating calls** — the same concern our airline task scores via the DB-replay oracle, so it is a
plausible design an AppSci agent *should* discover; worth keeping as a reference implementation of a
good answer. **RAFT is fine-tuning on synthetic in-domain data**, which is one of the techniques our
A.9 layer chooses among, with the same train/eval disjointness discipline we require.

### τ-bench has moved to τ³ — and our seed is current

Upstream is now **τ³-bench 1.0.0**: adds `banking_knowledge` (RAG over ~700 docs) and
`telecom`, adds full-duplex voice evaluation, a Gymnasium-compatible `gym` module, and **75+ task
corrections** informed by the SABER critique (Cuadron et al., ICLR 2026 workshop). Upstream also warns
that results from tau2-bench < 1.0.1 are not comparable with ≥ 1.0.1.

Our seed is now pinned to **v1.0.1** (commit `fc0055dc`, 2026-07-16). Verified against upstream rather than assumed:

- `provenance/airline/tasks.json` is **byte-identical** to upstream's airline `tasks.json` at that release. That file has had two commits ever — the 2025 release and the τ³ task-quality pass `01e812d1` (2026-03-18) — so **the τ³ corrections are included**.
- v1.0.1's grading fixes are banking-only; its changelog says *"Other domains are unaffected."* Its sole evaluator/environment change is an additive `strict_replay` flag that defaults to existing behaviour, and lenient mode exists only for re-grading historical trajectories.
- τ³'s new `auth_classifier` / `hallucination_reviewer` / `review_llm_judge_user_only` modules are imported only by `reviewer.py` and a standalone review script — offline tooling, outside the reward path. They are not graders.
- The 50 converted samples carry upstream's reward inputs unchanged, asserted by `tests/tasks/airline_rebooking/test_sample_fidelity.py`.

**The earlier "no action needed" conclusion was right about the vendored content and wrong about the run configuration.** Adopting a release is not only a matter of matching files: the user simulator the published runs used is part of the measurement, and ours was two generations behind. Re-check both halves when adopting another domain.

## Integration points, concretely

**Tier A shim (write once, reuse per baseline).** Each external agent needs: the protocol prompt as
its task, the user-sim base URL (`/health`, `/ask`), the Docker env mounted, a budget cap, and a
final "here is my endpoint" hand-back. Everything after the hand-back is already built.

**Tier C shim — tau2 agent contract.** Implement `HalfDuplexAgent` from `tau2.agent.base`:
`get_init_state(message_history)` and `generate_next_message(message, state)`, constructor
`(tools, domain_policy)`, plus `LLMConfigMixin` for `llm` / `llm_args`. Register a factory via
`registry.register_agent_factory(create_my_agent, "my_agent")` in `src/tau2/registry.py`, select with
`--agent`. Wrapping that class behind our three session endpoints is the whole Tier-C baseline.

**Tier B against tau2 domains directly (optional).** `tau2.gym` exposes `AgentGymEnv` /
`UserGymEnv` / `GymAgent` with string observations (`"role: content"` transcript), string actions
(JSON tool call, functional call, or prose), and `step() -> obs, reward, terminated, truncated, info`
where `info` carries `tools` and `policy`. That is a usable optimization target for GEPA or DSPy
without going through our harness — useful for debugging the optimizer in isolation. Caveat: the gym
README documents no train/test split, and its orchestrator runs as a daemon thread, which bites on
long optimization runs.

## Spec-given mode (elicitation skipped)

A useful control arm: hand the agent the task's SOP instead of making it interview the user sim. The
delta between the two arms *is* the cost of elicitation, which generalizes C1 from airline to every
ready task.

**Plumbing note.** This arm needs a single merged SOP string to hand the agent. There is no longer a
renderer for one: `appsci_bench/core/task_sop.py` was removed as dead code, so spec-given mode has to
assemble the text itself from the task's own documents (`TASK_SOP.md`, plus the policy / KPI docs under
`user_sim_docs/`). Personas are not needed in this arm.

**Task readiness gates this, not baseline choice.** A task needs SOP + `eval.py` + `eval_data/` +
`environment/`. Ready today: `airline_rebooking`, `order_to_cash`, `slides_generator`. The other seven
have only the spec shape — no SOP, no `eval.py`, no eval data.

| Baseline | Covers all ready tasks? | Notes |
|---|---|---|
| `appsci_agent` spec-given ablation | Yes | Cheapest. Build path already proven via `appsci_agent/tools/mini_swe_agent.py` |
| mini-swe-agent standalone | Yes | Already vendored; the "just a coding agent with the spec" floor |
| OpenHands / SWE-agent | Yes | Same shape, stronger, medium integration |
| MetaGPT / AutoAgent | Yes | Only *fair* in this arm — both are designed for a complete requirement. AutoAgent last pushed 2025-10 |
| tau2 `LLMAgent` | **No** | Only where the task is expressible as tau2 tools + policy (airline; retail/telecom if built). No equivalent for `order_to_cash` or `slides_generator` |
| Tier B optimizers, RD-Agent, AIDE | **No** | Handing over the SOP does not supply a metric or a trainset, which is what they actually require |

Spec-given mode removes Gap 1, so it measures a conventional spec→agent build task rather than this
benchmark's claim. Right as a control and a ceiling; wrong as the headline number.

## Recommended set

**P0 — needed before any external comparison means anything.** A0 null baseline (ablation of
`appsci_agent`); C1 tau2 `LLMAgent` + two frontier models behind our session API; report `pass^1` and
`pass^k` on `airline_rebooking`. Sanity check: C1's `task_success` should land near the published
airline band (80–84 `pass_1` with reasoning enabled). Landing far outside it means our eval, not the
model, is the variable — investigate before drawing any conclusion about the AppSci agent.

**Why C1 is the whole ballgame.** Every airline leaderboard row gets the domain policy passed
straight into the agent constructor (`HalfDuplexAgent.__init__(tools, domain_policy)`). That makes
80–84 the **oracle-elicitation ceiling**: the score when the SOP is handed over for free. Our AppSci
agent has to reconstruct that policy from a user sim first. (An elicitation miss used to be charged
twice — once in `task_success`, again in `policy_compliance` against the same SOP — but with
`policy_compliance` at `weight: 0` the headline is charged once.) C1 holds
the policy, the model, the metric, and the 50 cases fixed, so the only remaining difference is
elicitation. **`AppSci artifact task_success / C1 task_success` is the fraction of the oracle ceiling
recovered from a vague requirement** — the headline number this benchmark exists to produce. Without
C1, a low score is unattributable: it could be elicitation, our harness, or a reasoning-effort flag.

**P1 — the two comparisons that carry the paper.** Tier A: OpenHands given the protocol. Tier B: GEPA
via `mcp_adapter` / `optimize_anything_adapter`, swapped in for our A.9 layer, holding elicitation and
build fixed.

**P2 — breadth, in this order.** MetaGPT (fixed-SOP contrast) and RD-Agent (closest peer) for Tier A;
DSPy MIPROv2 for Tier B; AgentSquare *only* if the license clears, since it is the only interactive
design-space search available.

**Deliberately excluded.** MLE-bench / RE-Bench scaffolds as Tier A (their input is a spec plus a
metric — no elicitation stage to compare). The 2026 "agent harness evolution" preprint cluster
(HarnessForge, HarnessBank, HarnessCompass, EvolveNet, SHE, Bayesian-Agent, and a dozen more): the
closest thing in the literature to what appsci-bench measures, but a GitHub sweep found no released
code for any of them, and most have zero citations. Track for related work, not for baselines.
