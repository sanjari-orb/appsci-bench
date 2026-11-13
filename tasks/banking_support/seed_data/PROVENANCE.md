# tau2-bench banking_knowledge — vendored seed (provenance)

These files are the **upstream source** the banking eval cases will be converted from. Conversion is
OFFLINE (see `scripts/convert_tau2_banking.py`, added in a later phase); this directory records
exactly what was converted so the result is auditable and reproducible.

## Source
- Repo: https://github.com/sierra-research/tau2-bench
- Paths: `data/tau2/domains/banking_knowledge/` and `src/tau2/domains/banking_knowledge/`
- Commit: see `TAU2_COMMIT.txt` (`3b6a28ed8206dc31fc8b2dfdb497b8600821b463`).
- Note: banking_knowledge is a **newer** upstream domain than airline, so it is pinned to a
  different (later) commit than `tasks/airline_rebooking/seed_data/TAU2_COMMIT.txt`
  (`5ebebbe…`). Each domain records the commit it was actually taken from.

## What is vendored here
| File / dir | What it is |
|------------|------------|
| `../../../../provenance/banking_support/tasks.json` | The **97** tau2 banking tasks (verbatim from upstream `data/tau2/domains/banking_knowledge/tasks.json`), the offline conversion source. Byte-equal to upstream (verified). Lives under top-level `provenance/banking_support/` (reference only; not read at run time). |
| `prompts/` | The upstream **per-variant policy prompts** (`classic_rag.md`, `agentic_search.md`, `full_kb.md`, `no_knowledge.md`, …) + `components/` (`policy_header.md`, `shell_instructions.md`, `additional_instructions.md`). The **core domain policy** is `components/policy_header.md`; the variants differ only in retrieval-tool instructions. We adopt the retrieval **tools** themselves 1:1 (see “Retrieval adopted 1:1”) and keep `policy_header.md` as the domain policy. |
| `upstream_src/` | The upstream domain source (`tools.py`, `retrieval_toolkits.py`, `retrieval.py`, `db_query.py`, `data_model.py`, `environment.py`, `retrieval_mixins.py`, `utils.py`, `DATABASE_GUIDE.md`) — kept for **action-space / DB-schema provenance** and as the reference for the vendored retrieval runtime (`tasks/banking_support/environment/_runtime/`). The curated `tools_reference.json` lists the base agent/customer tools **plus the three provided retrieval tools** (`KB_search_bm25`, `KB_search_dense`, `shell`) — `grep` is not among them, matching the exposed surface. It is COPIED to `user_sim_docs/common/`, which is where a run actually reads it: the tool names and argument lists are sim-only material the agent must elicit. The two copies must stay identical (guarded by test). Deliberately lists only the BASE surface — the ~48 specialist tools are absent because the knowledge base is the only route to their names. |
| `TAU2_COMMIT.txt` | The upstream git commit the above were taken from. |

The **knowledge corpus** (698 documents) and the **transactional DB** (`db.json`) are not duplicated
here; they are materialized as the agent-facing task-environment fixture under
`tasks/banking_support/environment/documents/` and `…/environment/eval/data/db.json` (byte-equal to
upstream, verified). All cast tasks are used as held-out **test** cases — we do not split into
train/base, so the upstream `split_tasks.json`/`tasks_voice.json` are not vendored.

---

## ⚠️ Confirmed task shape (this corrected the initial high-level plan)

Reading the actual tasks overturned the pure-QA framing the first-pass plan assumed. The real
banking_knowledge domain is a **knowledge-grounded *transactional* customer-service agent**
(“Rho-Bank”): the agent searches a large knowledge base to give **accurate, grounded** advice and
then the correct **transactional outcome** is what is scored — not answer text.

**Reward basis (from `evaluation_criteria.reward_basis`, all 97 tasks):**
| reward_basis | count | meaning |
|---|---|---|
| `DB` | 88 | correct database end-state (same machinery as airline's DB half) |
| `ACTION` | 9 | golden action **trajectory** must be matched (tau2 `RewardType.ACTION`) |
| `COMMUNICATE` | 0 | **empty in every task** — tau2's COMMUNICATE half is vacuous here |

**Other structural facts that shape later phases:**
- **`communicate_info` is empty for all 97 tasks** → there is no “required strings” check; do not
  build the eval around COMMUNICATE.
- **75/97 tasks have a non-null `initial_state`** → the DB must be **seeded per sample**, so
  `evaluation_data.reset_env_per_sample: true` **is required** (contrary to the first-pass plan).
- **The customer holds the write tools** (`user_scenario.user_tools`): `apply_for_credit_card` (50),
  `call_discoverable_user_tool` (57), `request_human_agent_transfer` (19), `submit_referral` (6),
  `submit_transaction` (1). The agent advises; the **user executes**, and the resulting DB state is
  scored. The eval-time user sim therefore needs to be able to **call tools** (new vs. airline).
- **`required_documents`** on each task lists the KB docs the correct answer depends on — the
  ground-truth grounding set (the groundedness / retrieval-recall objectives compare against it).
- **Discoverable-tools mechanic**: golden actions include `unlock_discoverable_agent_tool` (274),
  `call_discoverable_agent_tool` (456), `give_discoverable_user_tool` (21),
  `call_discoverable_user_tool` (62). Agents dynamically unlock/call tools and hand tools to the
  user. This is banking-specific and is the main environment-complexity item for P2.
- **Identity verification**: `log_verification` appears in 81 tasks — verifying the customer is a
  required policy step before acting (a compliance dimension).
- **No-fabrication is an explicit policy directive** (`policy_header.md` guideline 1: “Do not make
  up policies… If you cannot find relevant information, let the user know”) → groundedness /
  `no_fabrication` remain meaningful **secondary** dimensions, but the **primary reward is DB/ACTION**.

## Which upstream VARIANT our numbers correspond to

Upstream does not run banking as one configuration. It runs the same 97 tasks under a dozen retrieval
**variants** (`prompts/`), and each published figure belongs to exactly one of them:

| Upstream variant | Retrieval the agent gets |
|---|---|
| `classic_rag_bm25` / `_openai` / `_qwen` (+ `_no_grep`) | one `KB_search` embedder, with or without `grep` |
| `all_tools` | `KB_search_bm25` + `KB_search_dense` + `shell` |
| `agentic_search` | the corpus as files in a writable workspace, via `shell` |
| `full_kb` | the whole corpus inlined into the prompt -- no retrieval at all |
| `grep_only` / `required_docs` / `no_knowledge` | as named |

**We expose `KB_search_bm25` + `KB_search_dense` + `shell` -- exactly `all_tools`, with nothing added.**
`grep` used to be exposed alongside them, which made the surface a superset of `all_tools` by one
read-only tool and meant every comparison against a published number needed a caveat instead of matching.
It was dropped for that reason. The capability is not lost to the agent, only relocated to where upstream
has it: `shell` runs real GNU grep over the corpus as files. The grep PIPELINE is still built, so trying a
grep-based variant stays a wiring change rather than a rewrite.

Recorded because a comparability claim nobody can check is worth nothing. It is also what makes the
removal below correct rather than merely tidy.

**`shell` was DECLARED BUT DEAD until the sidecar landed, so the sentence above did not describe any run
before it.** Upstream's shell executes through Anthropic's sandbox-runtime, which needs bubblewrap and so
user namespaces; in a stock task container it fails at construction (`bwrap: No permissions to create a
new namespace`), the failure was swallowed, and the tool answered "Sandbox not initialized" every time.
The effective surface in every earlier run was therefore `KB_search_bm25` + `KB_search_dense` + `grep` --
NARROWER than `all_tools`, not a superset of it, and the leaderboard reports `all_tools` for 22 of its 27
rows. Any banking number produced before this change should be read as a no-shell number and is not
comparable either to a published figure or to a post-sidecar run of our own.

Commands now execute in `environment/shell_sidecar/`, a container holding the exported corpus and nothing
else -- no task source, so no way to read the policy this task expects the agent to elicit. The upstream
runtime is deliberately NOT revived: making it work needs SYS_ADMIN or relaxed seccomp, i.e. weakening the
container in order to sandbox inside it. The observable contract is reproduced instead (see
`environment/shell_client.py`), and the exported file tree is verified byte-identical to upstream's own
exporter by test.

**`grep` was dropped when the sidecar landed**, in the same change, so the surface went from
"bm25 + dense + grep, shell dead" straight to "bm25 + dense + shell" -- `all_tools`. There is no revision
of this task whose exposed surface was `all_tools` plus grep in any run.

**No raw-corpus endpoint.** The environment used to serve the corpus directly as well -- a document index
and a fetch-by-id -- from the era when retrieval was withheld and the agent built its own search over a
corpus we handed it. Adopting upstream's tools 1:1 reversed that design, and the routes survived by
oversight. They were removed because they cost the two things this section exists to protect: a
configuration no upstream variant has (index browsing plus fetch-on-demand, strictly easier than
`all_tools`), and a hole in `retrieval_recall`, which counts only what a SEARCH returned -- so an
artifact could ground entirely by fetching by id and score zero on the objective meant to measure it.

**Retrieval adopted 1:1 (revised design decision):** upstream ships managed retrieval toolkits
(BM25 / dense embeddings / grep / sandboxed shell, in `upstream_src/retrieval_toolkits.py`). We
**adopt these**, vendored into `tasks/banking_support/environment/_runtime/` and exposed as the agent
tools `KB_search_bm25`, `KB_search_dense`, `grep`, and `shell` (all **read-only** over the corpus, so
they never touch the DB or the DB/ACTION reward). The corpus is reachable ONLY through those tools -- see
"No raw-corpus endpoint" above -- and the Applied Scientist no longer builds its own retrieval; the graded skill is
**wiring the provided tools** into a grounded, identity-verified, correct transactional agent.
(BM25/grep/shell need no API key; `KB_search_dense` uses OpenAI embeddings and degrades gracefully
without a key.)

## How each tau2 field will map to our `eval_sample` (schema/dataset.schema.json `$defs/eval_sample`)
| tau2 field | eval_sample field |
|------------|-------------------|
| `id` | `data_point_id` (prefixed `tau2_banking_<id>`) |
| `user_scenario.instructions` (persona + behavior) | `multi_turn_input.user_task` (composed; must not leak golden actions / required_documents) |
| (fixed pointer-back string) | `multi_turn_input.user_persona` |
| `initial_state` | per-sample DB seed applied via `reset_env_per_sample` hooks |
| `evaluation_criteria.{actions,communicate_info,reward_basis}` + `required_documents` + `user_tools` | `expected_output.json` (full struct, JSON-encoded) |
| `evaluation_criteria` + `required_documents` | `eval_rubric` (NL, read by the grounding/compliance judges) |

The per-data-point eval functions (`tasks/banking_support/eval.py`) are added in a later phase;
the **primary** objective replays golden `actions` to a fresh DB and hash-matches (DB reward,
reusing the airline pattern), with groundedness / no-fabrication / verification as secondary axes.
