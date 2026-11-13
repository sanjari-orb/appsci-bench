# Poking at Rho-Bank by hand

How to bring the `banking_support` task environment up on its own — no build agent, no scoring run —
and interrogate it directly. **Every command below was run against a live container.**

The task simulates a retail bank's customer-service channel. The environment is a single container
serving a tool API over HTTP: retrieval across a knowledge base, plus read and write tools over a
transactional store. Standing it up alone takes about two minutes and tells you more than reading the
code does.

| | |
|---|---|
| **Knowledge base** | 698 JSON documents, 2.9 MB, baked into the image |
| **Store** | 39 users, 38 cards, 338 transactions — in memory, no DB server |
| **Tools** | 24 total: 13 read, the rest writes and the discoverable mechanism |

---

## Bring it up

A real sequence — each step depends on the one before.

### 1. Get on the branch

The task lives on `mpk/banking_task`, not on main. Use a **separate checkout** if you might switch
branches later — a branch switch deletes the task files out from under anything running.

```bash
# a worktree keeps this checkout pinned no matter what you do elsewhere
git worktree add ~/appsci-bench-banking mpk/banking_task
cd ~/appsci-bench-banking
```

### 2. Start the environment

Compose builds the image and starts one service. The project name matters: it fixes the container
name the rest of the harness expects.

```bash
docker compose -p banking \
  -f tasks/banking_support/environment/docker-compose.yml up -d --build
```

First build pulls dependencies and takes a few minutes. You should end up with a container called
`banking-workspace-1`.

### 3. Confirm it answers

```bash
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-tool-token-local" http://localhost:8080/health'
# {"status":"ok"}
```

---

## The two things that will trip you up

**Port 8080 is not published to your host.** Curling from your own shell will fail. Everything runs
through `docker exec`, because the artifact reaches this service over the Docker network by container
name, not via localhost.

**Every route needs a bearer token, and there are two of them.**

| Token | Held by | Opens |
|---|---|---|
| `banking-tool-token-local` | the agent and the simulated customer | `/health`, `/documents`, `/tools/*` |
| `banking-admin-token-local` | the evaluator only | the `/admin/*` scoring routes |

---

## What's in the knowledge base

```bash
# all 698, id and title only (document text is not in the listing)
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-tool-token-local" http://localhost:8080/documents' \
  | python3 -m json.tool | head -40

# one document, with its content
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-tool-token-local" \
   "http://localhost:8080/documents/doc_bank_accounts_bank_accounts_(general)_001"'

# or pull the whole corpus out and read it locally — easier than working through exec
docker cp banking-workspace-1:/app/banking_docs ./banking_docs
ls banking_docs | wc -l   # 698
```

The corpus is lopsided, which matters if you are looking at retrieval scores: six product families
hold most of it and the tail is nearly empty.

| Category | Docs | Category | Docs |
|---|---|---|---|
| Credit cards | 110 | Everyone-pay | 51 |
| Checking accounts | 110 | Buy-now | 48 |
| Savings accounts | 89 | Bank accounts | 47 |
| Business savings | 82 | Personal subscriptions | 2 |
| Business credit | 79 | Customer support | 1 |
| Business checking | 79 | | |

---

## Searching it

Three retrieval tools are provided to the agent. Two work with no credentials.

```bash
# keyword search — works out of the box, returns ranked hits with content
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-tool-token-local" -H "Content-Type: application/json" \
   -X POST -d "{\"arguments\":{\"query\":\"annual fee waiver subscription\",\"k\":3},\"requestor\":\"assistant\"}" \
   http://localhost:8080/tools/KB_search_bm25'

# regex over the corpus — note the parameter is `pattern`, NOT `query`
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-tool-token-local" -H "Content-Type: application/json" \
   -X POST -d "{\"arguments\":{\"pattern\":\"cash back dispute\"},\"requestor\":\"assistant\"}" \
   http://localhost:8080/tools/grep'
```

> **Embedding search is off unless you give it a key.** `KB_search_dense` returns a plain explanation
> rather than results: no embeddings key is configured. That is deliberate degradation, not a fault —
> keyword search and grep need no key. Set `BANKING_EMBED_API_KEY` (or `OPENAI_API_KEY`) on the host
> before `up` if you want all three paths live. Worth knowing before you read any retrieval numbers:
> by default the artifact has two retrieval tools, not three.

---

## Reading the bank's records

Thirteen read tools. There is no SQL and no connection string — records come through tools or not at
all.

```bash
# one customer
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-tool-token-local" -H "Content-Type: application/json" \
   -X POST -d "{\"arguments\":{\"user_id\":\"890389b165\"},\"requestor\":\"assistant\"}" \
   http://localhost:8080/tools/get_user_information_by_id'

# their cards
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-tool-token-local" -H "Content-Type: application/json" \
   -X POST -d "{\"arguments\":{\"user_id\":\"890389b165\"},\"requestor\":\"assistant\"}" \
   http://localhost:8080/tools/get_credit_card_accounts_by_user'
```

| Tool | Takes | Gives |
|---|---|---|
| `get_user_information_by_id` | `user_id` | one customer record |
| `get_user_information_by_email` | `email` | same, by email |
| `get_user_information_by_name` | `name` | same, by name |
| `get_credit_card_accounts_by_user` | `user_id` | their cards |
| `get_credit_card_transactions_by_user` | `user_id` | their transactions |
| `get_referrals_by_user` | `user_id` | their referrals |
| `list_discoverable_agent_tools` | — | specialist tools unlocked so far |

The remaining eleven are writes plus the discoverable-tool mechanism, which is the heart of the task:
the knowledge base names a specialist tool, the agent unlocks it, then calls it. Full signatures are in
`seed_data/tools_reference.json` (the curated copy, for authoring) and in
`user_sim_docs/common/tools_reference.json` (the copy a run reads — sim-only, so the agent under test
elicits these names rather than reading them).

---

## Seeing what the scorer sees

The admin token opens three routes that exist purely for measurement. These are the most useful thing
here for understanding how the task is graded.

```bash
# a fingerprint of the entire store — the DB half of the score compares this
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-admin-token-local" http://localhost:8080/admin/state_hash'

# which documents retrieval touched — retrieval recall is scored from this
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-admin-token-local" http://localhost:8080/admin/retrieved_docs'

# every successful tool call this case, in order — the ACTION half of the score
docker exec banking-workspace-1 sh -lc \
  'curl -fsS -H "Authorization: Bearer banking-admin-token-local" http://localhost:8080/admin/action_trace'
```

> **They are hidden, not absent.** You will not find these in `/openapi.json` — the schema lists only
> the four routes the agent is meant to see. They are withheld deliberately: the published schema used
> to describe the seed route and its golden-actions field, which told an agent under test how it was
> about to be graded. The token was never the whole guard, because the schema needs no token.

### The loop worth running yourself

This is the measurement chain in four commands. Take a hash, change something, watch it move, reset,
watch it come back.

```bash
ADM='Authorization: Bearer banking-admin-token-local'
TOK='Authorization: Bearer banking-tool-token-local'

docker exec banking-workspace-1 sh -lc "curl -fsS -H '$ADM' http://localhost:8080/admin/state_hash"
# c1cb8797f7c0829b...

docker exec banking-workspace-1 sh -lc "curl -fsS -H '$TOK' -H 'Content-Type: application/json' \
  -X POST -d '{\"arguments\":{\"card_type\":\"Gold Rewards Card\",\"customer_name\":\"Test Person\",\
\"annual_income\":90000,\"rho_bank_subscription\":true},\"requestor\":\"user\"}' \
  http://localhost:8080/tools/apply_for_credit_card"

docker exec banking-workspace-1 sh -lc "curl -fsS -H '$ADM' http://localhost:8080/admin/state_hash"
# 4f953776dcb77ae9...  the write moved it

docker exec banking-workspace-1 sh -lc "curl -fsS -H '$ADM' -X POST http://localhost:8080/admin/reset"
docker exec banking-workspace-1 sh -lc "curl -fsS -H '$ADM' http://localhost:8080/admin/state_hash"
# c1cb8797f7c0829b...  back to where it started
```

Note `"requestor":"user"` above. The same route serves both sides: the agent's tools and the
customer's own. Roughly half the eval cases hinge on an action only the customer can take, so that
field decides which toolkit handles the call.

---

## Things that will confuse you

**Argument mistakes now explain themselves.** Call a tool with the wrong arguments and you get a 400
naming the offender — *"arguments did not match tool 'grep': got an unexpected keyword argument
'query'"*. Trust it: until recently the same mistake returned a bare 500, indistinguishable from the
environment being broken.

**There is no database.** No Postgres, no SQLite, nothing listening but the tool service. `db.json` is
loaded into an in-memory store inside the same process that serves the tools. That is why per-case
reset replaces the whole store rather than rolling back, and why cases must run one at a time — they
share one mutable object.

**The knowledge base is inside the image; the records are not.** `/app/banking_docs/` is copied in at
build time, because the corpus is shared whole with both run phases and there is nothing to carve. The
transactional store is bundled per phase to `/fixture/` instead, loaded through the admin-gated load
route by `setup.sh`, and then deleted — so a container of this image holds no records at all until its
phase loads them, and none on its filesystem afterwards. Verify both halves yourself:

```bash
docker exec banking-workspace-1 sh -lc 'ls /app/banking_docs | wc -l; ls /fixture 2>&1; ls /app/banking_db 2>&1'
```

The corpus count answers; the other two say the path does not exist. If either of them lists a
`db.json`, the carve is decorative and the agent is reading records it should never see.

**`| head` will lie to you.** A directory listing piped through `head` shows nine files and looks like
the corpus is tiny. Count instead — `ls /app/banking_docs | wc -l` — or read the `total` line, which
reports the real size.

---

## Tear it down

```bash
docker compose -p banking -f tasks/banking_support/environment/docker-compose.yml down
```

If you have moved out of the checkout and compose cannot find its file, `docker rm -f
banking-workspace-1` does the same job.

---

The tokens shown here are the local development values from the compose file. They are not secrets and
are the same on every machine.
