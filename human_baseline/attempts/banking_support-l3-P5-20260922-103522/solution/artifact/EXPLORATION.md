# Banking Support Task — Environment Exploration

Results of exploring the running task environment for the `banking_support-l3` task.
Date: 2026-09-22.

Sources: direct inspection of the running Docker containers (filesystem, env, source, live tool
calls) and a product-stakeholder briefing. Every tool fact below was verified against the code or a
live call; §8 lists the few points still open.

---

## 1. Summary

Implement the `respond()` handler in [server.py](server.py) so it acts as an **L3 banking support
agent**: read a running conversation, ground answers in the knowledge base, look up customer records,
take the correct actions through the tool API, and return the assistant's next turn.

The environment is **not** in this repo. It runs as two Docker containers:

| Container | Image | Port | Role |
|---|---|---|---|
| `banking-workspace-1` | `banking-workspace` | 8080 | Task-env FastAPI service (DB + tools + retrieval). The resource the artifact acts against. |
| `banking-shell-1` | `banking-shell` | 8081 | Read-only shell sidecar backing the `shell` retrieval tool. Corpus only, no secrets. |

The task prompt / policy and per-case instructions are **not baked into the container** — they are
supplied at eval time (message history plus a user simulator). The agent is expected to *elicit* the
policy from the knowledge base rather than read it from source. If something is not covered in the
KB, the agent must not guess — it says so and escalates.

---

## 2. The artifact: contract and connection

### 2.1 HTTP contract (what we build)

`server.py` is a stub whose `respond()` returns `"TODO: not yet implemented"`. It must satisfy:

- `GET /health` → `200 {"status": "ok"}` — already implemented.
- `POST /respond` → body is OpenAI-style `{"messages": [...]}`; return
  `{"choices": [{"message": {"role": "assistant", "content": ...}}]}`.

Internal structure of the service is the engineer's choice. Delivery is via the harness: **deploy
with `deploy_artifact`, then call `complete`** (orchestration actions, not routes on the task-env
service).

### 2.2 How the artifact reaches the tools

The artifact reaches **all** tools — retrieval and banking — through the task-env service's tool API,
using two env vars injected into the artifact's container at deploy time (per the stakeholder
briefing):

- Base URL: **`BANKING_TOOLS_URL`**
- Bearer token: **`BANKING_TOOLS_TOKEN`**
- Call shape: `POST {BANKING_TOOLS_URL}/<tool_name>` with body `{"arguments": {...}, "requestor": ...}`,
  response `{"result": ...}`.

  The artifact acts as the **agent**, so it must **not** set `requestor: "user"` — that value routes
  the call to the *customer* toolkit (§3.2). The artifact either omits `requestor` or leaves it at
  its non-`"user"` default; the `"user"` path is driven by the eval-time user simulator, not the
  artifact (§5.4). To hand an action to the customer, the agent uses `give_discoverable_user_tool`
  (§5.2) — it does not impersonate the customer by setting `requestor`.

The KB is reachable **only** through the retrieval tools — there is no raw-corpus endpoint, by
design.

> **Do not hardcode** `localhost:8080` or the `BANKING_ENV_*` names. Those are the task-env
> container's *internal* view (§3.1). The artifact must read `BANKING_TOOLS_URL` /
> `BANKING_TOOLS_TOKEN`. Two details remain unconfirmed against the deployer — see §8.

### 2.3 Per-turn approach for `respond()`

1. Read the conversation from `messages`.
2. Search the KB (`KB_search_bm25` / `KB_search_dense` / `shell`) to elicit the relevant policy —
   verification requirements, the procedure, which specialized tool it names, escalation rules.
3. Look up customer records and take actions via the tool API, respecting the unlock-then-call
   protocol for discoverable tools (§5) and the verification/escalation policy found in the KB.
4. Return the assistant's next turn in the `choices[0].message` shape.

The artifact holds only the tool token, so it **cannot** read the scoring routes (`state_hash`,
`action_trace`, `retrieved_docs`). Correct behavior must come from the KB and tool results, never
from the scoring seams.

---

## 3. The task-env service (`banking-workspace-1`, :8080)

FastAPI app defined in `tasks/banking_support/environment/service.py`. All routes are bearer-token
gated.

### 3.1 Authentication — two tokens

Two tokens exist by design, both present in the task-env container's environment as `BANKING_ENV_*`:

- **Tool token** (`BANKING_ENV_TOKEN`) — gates `/health` and `POST /tools/{name}`. This is the token
  the artifact uses (delivered to the artifact as `BANKING_TOOLS_TOKEN`; see §2.2 and §8).
- **Admin token** (`BANKING_ENV_ADMIN_TOKEN`) — gates all `/admin/*` routes. Held only by the setup
  script and the evaluator. The artifact can neither load data nor read scoring state.

### 3.2 Routes

| Method | Route | Token | Purpose |
|---|---|---|---|
| GET | `/health` | tool | Liveness — answers even when the store is empty. |
| POST | `/tools/{name}` | tool | Execute a tool. Body `{arguments, requestor}`. `requestor:"user"` routes to the customer toolkit; anything else to the agent toolkit. Returns `{"result": ...}` or `400`. |
| POST | `/admin/reset` | admin | Reset per-case DB + retrieved-doc log + action trace. |
| POST | `/admin/seed` | admin | Seed per-case DB (`initial_state`, `read_log_allowlist`, `golden_actions`). |
| POST | `/admin/load` | admin | Load a phase's store from a path in the container (setup-only). |
| GET | `/admin/state_hash` | admin | DB-state fingerprint for scoring. |
| GET | `/admin/retrieved_docs` | admin | Doc ids returned by KB searches this case (retrieval-recall scoring). |
| GET | `/admin/action_trace` | admin | Successful tool calls this case, in order (action-basis scoring). |
| GET/POST | `/admin/simulation_steps` | admin | Shared eval step budget. |

The `/admin/*` routes are also hidden from `/openapi.json` (`include_in_schema=False`) — a second,
independent control so the scoring design isn't disclosed via schema discovery.

### 3.3 Environment variables (observed in the task-env container)

- `BANKING_ENV_TOKEN`, `BANKING_ENV_ADMIN_TOKEN` — the two bearer tokens (required).
- `BANKING_DOCS_PATH=/app/banking_docs` — the knowledge corpus (baked in, shared by both phases).
- `BANKING_EMBEDDINGS_CACHE_DIR=/app/banking_embeddings_cache` — dense-embeddings cache.
- `OPENAI_API_KEY` / `BANKING_EMBED_API_KEY` — the keys `KB_search_dense` would use. **Both are
  unset/empty in this container (re-checked 2026-09-22)**, so dense degrades gracefully and returns no
  results; bm25/shell need no key. See §7 and §8 item 4.
- `BANKING_SHELL_URL=http://shell:8081`, `BANKING_SHELL_TOKEN` — the shell sidecar.
- `BANKING_DB_PATH` — **unset in Docker**, so the service boots with **no transactional records**.

Note these are the `BANKING_ENV_*` names *inside the task-env container*, distinct from the
`BANKING_TOOLS_*` names the artifact reads (§2.2).

---

## 4. Data storage

### 4.1 The transactional DB

An **in-memory `TransactionalDB`** (`data_model.py`, 23 collections: users, bank/checking/savings
accounts, credit/debit cards, transaction histories, disputes, referrals, per-case task config,
etc.). Queried via `db_query.py`.

- The service **boots empty**. A phase's records load once, after the container starts, via admin
  `POST /admin/load` from a bundled `db.json`; the raw fixture directory is then deleted — so the
  tool API is the *only* path to the records.
- `app.state.base` holds the pristine store; `app.state.env` is the live per-case copy.
  `/admin/reset` restores the pristine copy between cases.
- **Current live state (re-checked 2026-09-22): the store is loaded, not empty.** The build share
  has been loaded since the earlier exploration: `get_user_information_by_id` returns real records for
  the build fixtures — e.g. `123` (Mia Smith) and `126` (Alex Riviera) both return full user records.
  Each user record carries `user_id`, `name`, `date_of_birth`, `email`, `phone_number`, `address`
  (the four verification fields plus name/id). `get_current_time` returns `2025-11-14 03:40:00 EST`.
  (The earlier "store is empty — read tools return 'No records found'" observation was true at first
  exploration but is now stale; the setup script has since loaded the build share.)

### 4.2 Two stores, by design (host-side, not in this container)

A two-phase benchmark:

- **Eval store** — the full ~768-record store the 97 eval cases are drawn from and scored against.
- **Build share** — the ~8 records left after carving out everything the eval cases touch, plus ~100
  authored synthetic records for the build agent to explore.

Both are *referenced* in code (`EVAL_STORE_PATH`, `BUILD_STORE_PATH`) but the files live on the host
(scoring/replay side), not in the container.

### 4.3 The knowledge base (corpus)

The bank's official documentation — the authoritative source the agent grounds answers in. Holds
product terms, fee schedules, eligibility rules, and situation-handling procedures; **each procedure
names the specific action tool to use.**

- **698 JSON documents** in `/app/banking_docs`, shape `{id, title, content}` (markdown content).
- Categories: bank accounts (47), credit cards (25), everyone-pay (15), savings/gold (14), checking
  accounts (blue/green/purple/light-green), business credit cards, etc.
- **Not carved** — both phases share all 698 docs. Roughly 46 of ~48 specialized tools appear to be
  named only by documents the eval cases require, so withholding docs would make the task unsolvable.
  These specialist-tool counts (~48 total; cf. the ~44 discoverable agent-side + 4 discoverable
  customer-side in §5) are **estimates**, not exact figures — the specialist set is deliberately
  non-enumerable (P3), and the small differences between these numbers are a **non-blocking**
  bookkeeping matter, not something to build against.
- Reached **only** through the retrieval tools; no raw-corpus fetch route.

### 4.4 Document pattern (from a sampled read)

Reading ~8 documents across categories shows two clearly distinct doc types, split by title
convention:

**A. Internal procedure docs** (title starts with `Internal:`) — agent-facing, and the most
important type. This is where the policy lives. Consistent structure:

1. **Eligibility / pre-conditions** — a checklist the agent must satisfy first (e.g. "customer is
   verified", account is OPEN, transaction ≥ $1.00 and < 60 days old, tier-specific dispute limits).
2. **The action tool, named explicitly** — e.g. *"Use `open_bank_account_4821` to open the
   account."*, *"Tool: `file_debit_card_transaction_dispute_6281`"*, *"Use `close_bank_account_7392`
   to close the account."*
3. **Argument spec with valid enum values** — e.g. `credit_type` ∈ {`rebate_credit`, `fee_refund`};
   `account_class` must be the full official name ("Blue Account"); and cross-references to *other*
   tools for gathering inputs (*"Use `get_bank_account_transactions_9173` to find the
   transaction_id"*).

These docs also carry the domain rules the agent must apply: tier tables (Entry/Mid/Premium/Elite
with distinct fees, notice periods, and dispute caps), Regulation E liability windows, closure-fee
logic, etc.

**B. Customer-facing docs** (all other titles, e.g. "How to Dispute a Credit Card Transaction",
"World Blue: How to Avoid Monthly Fees") — friendly prose for end customers. Explain *when* and
*why*, list product terms and fee thresholds, but **do not name internal tools**.

**Implications for `respond()`**

- The agent finds the right **procedure doc**, checks its eligibility list, then calls the exact tool
  the doc names with the enum values the doc specifies — matching the validation the tools enforce
  (§7). A procedure often chains a *read* tool (to fetch ids) before the *action* tool.
- Retrieval must surface the `Internal:` docs, not just the customer-facing ones — searching by the
  customer's phrasing may return the prose doc; the tool name lives in the internal doc.
- **Data caveat:** some customer-facing docs contain `$[value not available]` placeholders (seen in
  the BNPL late-fee docs). The agent should not quote those as real figures; authoritative numbers
  come from the internal/tier docs.

---

## 5. Tool inventory

Verified by runtime introspection of the registered toolkits (`toolkit.tools` + the
`DISCOVERABLE_ATTR` flag) and one live call per tool (§7). **71 callable tools total: 61 agent-side,
10 customer-side.** The totals and the *base* subsets are exact; every count of **discoverable
(specialist) tools below is an estimate**, because — by design — the specialist actions are not an
enumerable set the stakeholder or the agent holds up front (§4.3, P3 in REQUIREMENTS.md). Estimates:
~44 discoverable agent-side (61 − 17 base) and 4 discoverable customer-side (§5.4).

Tools split by how they are reached:

- **Base (always available):** callable directly (non-discoverable in the code).
- **Discoverable:** must be **unlocked then called** — agent-side via `unlock_discoverable_agent_tool`
  then `call_discoverable_agent_tool`; customer-side handed over by the agent via
  `give_discoverable_user_tool`. The **~44 (estimate)** specialized `*_NNNN` action tools are
  discoverable, each named only by the KB document(s) a case requires.

### 5.1 Retrieval tools (base, read-only over the corpus)

Read-only: they never touch the DB, so they don't affect the state hash. Returned doc ids are logged
per case for retrieval-recall scoring.

| Tool | Purpose | Arguments | Requirement |
|---|---|---|---|
| `KB_search_bm25` | Keyword search; returns ranked `{id, score, content}` | `query` (req), `k` (opt, default 10) | None. |
| `KB_search_dense` | Embedding search | `query` (req), `k` (opt, default 10) | Needs an embeddings key (`BANKING_EMBED_API_KEY` or `OPENAI_API_KEY`). Without one it **degrades gracefully** — guidance message, no results — fall back to bm25 or `shell`/grep. |
| `shell` | Read-only shell (`grep`, `cat`, `ls`, …) over the corpus exported as files | `command` (req) | Only when the sandbox runtime is present. |

### 5.2 Base agent tools (14) — always available, no unlock

Confirmed against the stakeholder briefing: **all 14 exist, are non-discoverable, and their argument
lists match exactly.**

| Tool | Arguments (`?` = optional) | Mutates |
|---|---|---|
| `get_user_information_by_id` | `user_id` | no |
| `get_user_information_by_email` | `email` | no |
| `get_user_information_by_name` | `customer_name` (case sensitive) | no |
| `get_credit_card_accounts_by_user` | `user_id` | no |
| `get_credit_card_transactions_by_user` | `user_id` | no |
| `get_referrals_by_user` | `user_id` | no |
| `get_current_time` | *(none)* | no |
| `log_verification` | `name`, `user_id`, `address`, `email`, `phone_number`, `date_of_birth`, `time_verified` | yes |
| `change_user_email` | `user_id`, `new_email` | yes |
| `transfer_to_human_agents` | `summary`, `reason?` (closed enum: fraud_or_security_concern, account_closure_request, deceased_account_holder, legal_or_regulatory_matter, … , other) | no |
| `unlock_discoverable_agent_tool` | `agent_tool_name` | yes |
| `call_discoverable_agent_tool` | `agent_tool_name`, `arguments?` | yes |
| `list_discoverable_agent_tools` | *(none)* | no |
| `give_discoverable_user_tool` | `discoverable_tool_name`, `arguments?` | yes |

The three retrieval tools (§5.1) are also always-available, making **17** base agent tools in total.

### 5.3 Discoverable agent tools (~44, estimate) — unlock then call

Specialized action tools, all suffixed with a numeric id. Examples: `open_bank_account_4821`,
`close_bank_account_7392`, `close_credit_card_account_7834`,
`transfer_funds_between_bank_accounts_7291`, `pay_credit_card_from_checking_9182`,
`file_credit_card_transaction_dispute_4829`, `file_debit_card_transaction_dispute_6281`,
`freeze_debit_card_3892` / `unfreeze_debit_card_3893`, `order_debit_card_5739`,
`order_replacement_credit_card_7291`, `approve_credit_limit_increase_5847` /
`deny_credit_limit_increase_5848`, the various `get_*_NNNN` reads, `activate_debit_card_829x`. Called
without unlocking, they return *"…has not been unlocked. You must first use
`unlock_discoverable_agent_tool`…"*.

### 5.4 Customer-side tools (10) — requestor `"user"`

Driven by the eval-time user simulator, not the artifact, but they mutate the same shared DB.

- **Base:** `apply_for_credit_card`, `submit_transaction`, `submit_referral`,
  `request_human_agent_transfer`, `list_discoverable_user_tools`, `call_discoverable_user_tool`.
- **Discoverable** (agent must hand over first via `give_discoverable_user_tool`):
  `deposit_check_3847`, `get_card_last_4_digits`, `get_referral_link`, `submit_cash_back_dispute_0589`.

---

## 6. Scoring seams (how the task is graded)

Three independent mechanisms, all read via admin routes (not reachable by the artifact):

1. **DB state hash** — the evaluator replays each case's golden actions against a fresh, identically
   seeded DB and compares `state_hash()`. This is the tau2 DB check.
2. **Action trace** (`/admin/action_trace`) — for the 9 action-basis cases whose reward matches the
   golden action trajectory rather than an end state.
3. **Retrieval recall** (`/admin/retrieved_docs`) — doc ids returned by KB searches vs. the case's
   `required_documents`.

A per-case **read-log allowlist**, derived from the golden trajectory, decides which read calls are
logged into the hashed state — so an agent that reaches the right end state without performing the
required reads still scores 0.

### The shell sidecar (`banking-shell-1`, :8081)

- Its only process is a command executor (`/srv/server.py`) that runs a shell command over HTTP, with
  the exported corpus (`/kb/knowledge_base`) as the working directory.
- **Separate container on purpose:** the task-env container holds material the task withholds from
  the agent (notably `tools.py`, which encodes the verification rule, the unlock-then-call protocol,
  the discoverable-tool catalogue, and escalation rules). A shell there could just `cat` the policy
  the agent is meant to elicit. This image is built with the corpus and nothing else — no task
  source, no store, no admin token, no evaluator code.
- Confinement is by Docker (unprivileged user, read-only root FS, dropped capabilities,
  internal-only network); the process is deliberately "dumb" and only executes and reports.
  Escape-pattern blocking and output sanitisation live on the workspace side in `shell_client.py`.

---

## 7. Live one-call-per-tool test

Every registered tool was called once through the real HTTP API (`POST /tools/{name}` with the tool
token), then the DB was restored.

**Method.** State hash snapshotted before; each of the 71 tools called once with best-effort valid
arguments; `POST /admin/reset` afterward. State hash returned to the identical pre-test value
(`44e051da…0769d0`) — the store was left clean.

**Result: all 71 tools responded HTTP 200 with a `result` payload** — the service, tool-token auth,
and every tool endpoint are wired and reachable. Because the store was empty **at the time of this
test** (§4.1 — the build share has since been loaded, so id-based reads now return real records),
responses fell into four informative buckets rather than acting on real data:

| Bucket | What it shows | Examples |
|---|---|---|
| Real output | Tool ran fully | `KB_search_bm25` (ranked docs), `get_current_time`, `shell` (listed corpus files), `log_verification`, `open_bank_account_4821` (after unlock), `submit_referral` |
| "Not found" | Logic reached, no record for the id | `change_user_email`, `freeze_debit_card_3892`, `transfer_funds_between_bank_accounts_7291` |
| Validation error | Tool enforced its arg/enum rules (self-documenting valid values) | `close_debit_card_4721`, `apply_statement_credit_8472`, `transfer_to_human_agents` |
| Protocol gate | Unlock / give protocol enforced | `call_discoverable_agent_tool`, user-side `get_referral_link`, `submit_cash_back_dispute_0589` |

**Confirmations**

- `KB_search_dense` **degraded gracefully** as briefed — *"unavailable … no embeddings API key
  configured"*, which is correct: neither `BANKING_EMBED_API_KEY` nor `OPENAI_API_KEY` is set in this
  container (§3.3), so the dense backend is not active here. `KB_search_bm25` and `shell` work and
  need no key.
- The **unlock-then-call** and **give-to-user** protocols are enforced exactly as designed.
- Several discoverable transfer tools return **policy/steering text** rather than data (e.g.
  `initial_transfer_to_human_agent_1822`: *"Do not explicitly tell the user … try to help first"*).

---

## 8. Open questions and discrepancies

The stakeholder briefing agreed with direct observation on almost everything: the KB's nature and
contents, the three retrieval tools and their arguments, read-only retrieval, dense degrading
gracefully when no embeddings key is set, the `/health` + `/respond` shapes, the
`{"arguments"}` → `{"result"}` convention, no raw-corpus endpoint, escalate-when-uncovered, and all
14 base tools with exact signatures. Three items remain genuinely open (items 1–2 and the policy
source below); items 3–4 are recorded as resolved for the record:

1. **Tool-API env var names — confirm with the deployer.** The briefing says the artifact reads
   `BANKING_TOOLS_URL` / `BANKING_TOOLS_TOKEN`, but the task-env container exposes only
   `BANKING_ENV_TOKEN` / `BANKING_ENV_ADMIN_TOKEN` (no `BANKING_TOOLS_*` anywhere in it). Most likely
   the `BANKING_TOOLS_*` vars are injected into the *artifact's own* container at deploy time and
   point at the task-env service. The artifact should read them and must not assume `localhost:8080`
   or the `BANKING_ENV_*` names — but confirm this against the deployer rather than assume it.

2. **Tool-call URL path shape.** Briefing: `POST {BANKING_TOOLS_URL}/<tool_name>`. Observed route:
   `POST /tools/{name}` (with the `/tools/` prefix). Either `BANKING_TOOLS_URL` already includes
   `/tools`, or the deployer front-ends the service without it. Bodies match; only the base path is
   uncertain. Confirm the exact value of `BANKING_TOOLS_URL` before hardcoding a path.

3. **`query_database` is not a callable tool — verified in source, not a build-env quirk.** The
   briefing mentioned it as a customer tool, but `POST /tools/query_database` returns
   `{"detail":"Tool 'query_database' not found."}` — both as agent and with `requestor:"user"`
   (re-checked 2026-09-22). Root cause confirmed in the image's own source: `query_database` exists as
   a method on the `KnowledgeUserTools` toolkit (`tools.py:4508`) but is the **one method there with
   no `@is_tool(...)` decorator** (the methods immediately above and below it, e.g.
   `call_discoverable_user_tool`, both carry one). The toolkit metaclass (`_runtime/toolkit.py`)
   registers **only** `@is_tool`-marked methods into `_func_tools`/`tools`, and the `/tools/{name}`
   dispatch resolves names only from that set — with no name-based allowlist that could re-add it. So
   the method is genuinely unreachable over the tool API by construction.
   - **Not a hallucination and not build-vs-eval:** the method really is in the code (so the briefing
     wasn't inventing it), and the same vendored `tools.py` is baked into the image both phases share,
     so this holds in the **real eval env too**, not just the build env. Do not design any flow that
     assumes the customer (or agent) can call `query_database`; the customer reaches data through the
     other registered user tools. Worth flagging to the stakeholder as a likely missing decorator, but
     it does **not** change what the artifact can rely on.

4. **`KB_search_dense` is inactive here — resolved: expected by-design degradation, not a bug.**
   Confirmed with the stakeholder and against the container: **neither `BANKING_EMBED_API_KEY` nor
   `OPENAI_API_KEY` is set** (§3.3, re-checked 2026-09-22), so dense returns its guidance message and
   no results — exactly the documented graceful-degradation path. (An earlier draft of this doc said
   `OPENAI_API_KEY` was "set"; that was a mistaken observation — both keys are empty — so there is no
   contradiction to resolve.) The stakeholder confirms all three retrieval tools hit the **same KB**,
   so no coverage is lost: **build so the agent never depends on dense being live** — use
   `KB_search_bm25` or `shell`/grep as the primary path, and treat dense as a bonus that activates
   only if a key is ever present. No action needed beyond that; the key source is not something the
   artifact should chase.

Also unconfirmed: the artifact's **system prompt / policy source**. It is supplied at eval time, not
baked into the container; confirm where it comes from before hardcoding any behavior.
