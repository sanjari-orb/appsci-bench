# Banking Customer-Service Agent — environment resources

Where the customer's resources live inside the `appsci-bench/banking` task environment (agent-facing;
distinct from the hidden held-out eval set in `eval_data/`).

This task is cast from the **tau2-bench banking_knowledge domain**; the environment reuses that
domain's transactional DB + tool logic. See `seed_data/PROVENANCE.md`.

| Resource | Type | Location | Notes |
|----------|------|----------|-------|
| Banking DB | database | `environment/{build,eval}/data/db.json` | In-memory JSON-backed transactional store, **one per run phase** (users, accounts, cards, applications, referrals, disputes, verification history, ...), **not** a Postgres/SQL server — see below |
| Knowledge base | docs | `environment/documents/` | ~700 official documents (product terms, fees, eligibility, procedures) — reachable only by searching with the provided retrieval tools — see below |

> **There are no production traces.** A `prod_traces` resource was declared in `spec.yaml` and never
> materialized — no traces file exists in this repo for any task, nothing is bundled into either run
> phase, and no endpoint serves them. The entry is gone from `spec.yaml` and from the L1/L2
> `known_resources` (plain and adversarial). Do not re-add it without a fixture the agent can open.

> **The tool interface is no longer a declared resource either.** `banking_tools` was a resource row
> whose `location` was never a location, restating what `interface_design` in `spec.yaml` already
> carries — that is the field holding the tool contract, so the hint lives there and the row is gone
> (from the L1/L2 `known_resources` too). The surface itself is unchanged and is still described under
> "Banking tools" below; the names and argument lists remain the L2 stakeholder's material, to be
> elicited.

> **Neither the policy nor the KPIs is listed here, because neither is published to the agent.** The
> table above mirrors the two resources in `spec.yaml`, and that spec deliberately declares no
> `banking_policy` and no `kpis` entry: this task mounts no `/sop`, so an entry for either would have
> named a document the agent cannot reach, and each entry's own description restated the material it was
> hiding. The policy is gated to L1 and the KPIs to L3, both carried as stakeholder knowledge to be
> elicited. The policy's authoring copy stays at `seed_data/prompts/components/policy_header.md`; the
> KPIs are sim-only material the L3 stakeholder holds, mounted into the user-sim container and nowhere
> else, so this doc deliberately does not name the file. `../TASK_SOP.md` is the single source of truth
> for where each lives — they used to be
> merged into a jinja-rendered SOP, until #107 removed the renderer and the SOP became a plain document
> that links instead. This doc stays about *where resources live*.

## Banking DB — in-memory, JSON-backed (not Postgres)
The DB is the tau2 banking `TransactionalDB`: a set of JSON collections in a `db.json`. There are now
**two of them, one per run phase** — `environment/build/data/db.json` holds the carved 8-record build
share the agent explores, and `environment/eval/data/db.json` holds the full 768-record store the
artifact is scored against (see `environment/build/README.md` for the carve and what it costs). The
service boots holding **no** records and the phase's setup script loads its own store, so nothing is
baked into the image; the environment package's default resolves to the **eval** copy, which is what the
scoring replay reads.

At task-run time the environment **loads that store into an in-memory object** the tools read and
mutate; it is not a networked database server and there is no `DATABASE_URL`. Each eval case starts from
a fresh copy, then the per-case `initial_state` is applied (the DB-state oracle is replayed against the
same seeded fixture).

## Knowledge base — retrieval is provided (tau2's tools, 1:1)
The rules that make an answer correct — product terms, fee schedules, eligibility, procedures, and
even **the names of the specialized (discoverable) tools** — live across ~700 documents in
`environment/documents/` (each `{id, title, content}`). Retrieval over that corpus is **provided out
of the box** as agent tools (tau2's, adopted 1:1), so the artifact does **not** spin up a vector DB
or write its own search code:
- **`KB_search_bm25(query, k)`** — BM25 sparse keyword search. No API key.
- **`KB_search_dense(query, k)`** — dense embedding search (OpenAI `text-embedding-3-large`). Needs
  an embeddings key (`BANKING_EMBED_API_KEY`, else `OPENAI_API_KEY`); **degrades gracefully** (returns
  a guidance message) when no key is set, so bm25/shell still cover the task creds-free.
- **`shell(command)`** — read-only Unix commands (`ls`/`cat`/`grep`/`head`/`find`/`awk`) over the corpus
  as files. Commands execute in a separate container (`environment/shell_sidecar/`) holding the exported
  corpus and nothing else; upstream's sandbox runtime is deliberately not used, since it needs namespace
  privileges a task container does not have.

`grep` used to be a fourth tool here and is **no longer exposed**: upstream's `all_tools` variant — the
configuration the published leaderboard reports — is exactly bm25 + dense + shell, and offering `grep`
alongside made our surface a superset that no published number could be read against. The capability is
not lost, only relocated to where upstream has it: real GNU grep, inside `shell`.

All three are **READ-only over the corpus** — they never touch the transactional DB, so
`GET /admin/state_hash` and the DB/ACTION reward are unaffected by any search. The env logs the doc
ids each `KB_search_*` call returns and exposes them at `GET /admin/retrieved_docs` (admin token;
cleared on `/admin/reset`) so the evaluator can measure retrieval recall. Note `shell` output is
unstructured and therefore NOT id-logged, so an artifact that reads the corpus as files scores low on
that metric while succeeding at the task. The raw corpus is
**not served raw**: there is no endpoint that lists the documents or returns one by id, and that is
deliberate. Two such routes existed while the design was "retrieval withheld, build your own search",
and survived the reversal to "retrieval provided 1:1" by oversight. They put the task in a
configuration no upstream variant has, which breaks the comparability that adopting the tools 1:1 exists
to buy, and they bypassed `retrieval_recall`, which counts only what a search returned. Searching is the
only way in. The graded skill is **wiring these provided tools** into a grounded agent — the corpus is
far too large to stuff into context.

## Banking tools — base surface + discoverable mechanism
The agent acts through a **tool interface**, not raw DB writes:
- **Base agent tools** (always available): identity lookups (`get_user_information_by_*`),
  `log_verification`, account reads (`get_credit_card_accounts_by_user`, ...), `get_current_time`,
  `transfer_to_human_agents`, and the discoverable-tool meta-tools.
- **Customer tools** (executed by the customer, not the agent): `apply_for_credit_card`,
  `submit_referral`, `submit_transaction`, `request_human_agent_transfer`, `call_discoverable_user_tool`.
- **Discoverable tools**: specialized actions that are **not** listed by default. The **knowledge
  base tells the agent the tool's name**; the agent unlocks it (`unlock_discoverable_agent_tool`)
  then calls it (`call_discoverable_agent_tool`), or hands a customer-side tool over
  (`give_discoverable_user_tool`, which the customer then calls). This is why retrieval matters:
  you cannot act correctly without first finding the right doc.

Base per-tool argument lists are held by the **L2 stakeholder** as sim-only material and must be
elicited. They live under `user_sim_docs/`, which is mounted into the user-sim container and into no
other, so the agent cannot open them at any point. `interface_design` in `spec.yaml` tells the agent the
retrieval and discoverable-tool *mechanism* but not the tool names or their arguments — those come from
asking. The `dataset_files` entry that once named an action-space reference was removed for pointing at a
file the agent could never reach; rather than delete the reference, it was given to the simulator, so the
stakeholder can now answer a question about a tool's arguments exactly instead of approximately.

## Important: policy is NOT enforced by the tools
The write tools apply changes blindly — they do **not** validate eligibility, grounding, or identity.
The artifact must **ground its actions in the knowledge base**, **verify the customer's identity**
before account actions, and only take actions the KB sanctions. The environment's reference "now" is
fixed at **2025-11-14 03:40:00 EST** (use `get_current_time`; time-relative rules evaluate against it).

## Access
- DB/tool/corpus/policy detail is gated to the L1/L2 personas; L3 speaks only to business KPIs
  (anti-leakage).
- The agent under test is never shown the held-out eval set or the per-case oracle.

## Expected artifact architecture (interim)
Until the 3-tier sandbox lands, the artifact the agent builds should be a small HTTP service:
- `GET /health` → `{"status":"ok"}`; `POST /respond` with `{"messages":[...]}` →
  OpenAI-style `{"choices":[{"message":{"role":"assistant","content":...}}]}`.
- It reaches the tools as a **tool-API** at `BANKING_TOOLS_URL` (bearer `BANKING_TOOLS_TOKEN`):
  `POST {BANKING_TOOLS_URL}/<tool_name>` with `{"arguments": {...}}` → `{"result": ...}`.
- The knowledge base is reached through the retrieval tools on that same tool-API; there is no
  raw-corpus URL.
- **This guidance will be replaced by 3-tier-architecture-compliant instructions** so the AppSci
  agent learns to architect its own artifact container.
