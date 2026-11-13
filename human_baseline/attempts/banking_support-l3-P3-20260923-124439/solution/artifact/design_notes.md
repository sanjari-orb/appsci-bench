# Rho-Bank agent — design notes

Records what the artifact's agent core is, the interfaces it depends on, and the current state. As of
the channel-operators' BASE-interface specification (received 2026-09-23), the previously-missing
interface is confirmed and the transactional agent is fully wired.

## Architecture

- `server.py` — stable HTTP surface. `GET /health`, `POST /respond` (OpenAI-style envelope), binds
  `0.0.0.0:$PORT`. Loads the agent core lazily via `import agent; agent.run_agent`. On any core
  failure it returns an honest error envelope (503 if the core is absent, 500 if it raises) — never a
  fabricated assistant answer.
- `config.py` — runtime env accessors (banking + Anthropic), model id, redaction. No secrets logged.
- `tools.py` — `ToolClient.call(name, arguments) -> ToolResult` over the confirmed transport
  `POST {BANKING_TOOLS_URL}/<name> {"arguments":{...}} -> {"result":<string>}` (bearer). Faithful
  failure classes; token only in the Authorization header.
- `llm_client.py` — `AnthropicMessagesClient`: confirmed Anthropic Messages call, text + `tool_use`
  parsing, redacted `LLMError`s. `from_config()` builds from runtime env.
- `agent_core.py` — `AgentLoop`: schema-agnostic tool-use loop. Advertises a list of `ToolSpec`s to
  the model, dispatches tool calls through `ToolClient`, feeds results back, returns one grounded
  assistant turn (or an honest not-completed message on step exhaustion). Hard-codes no tool. Treats
  an `Error:`-prefixed string inside a 200 response as a failure (`is_error`), not success.
- `tool_specs.py` — the confirmed ToolSpec catalog: retrieval (`KB_search_bm25`, `shell`), BASE read,
  and BASE action/discovery tools. `AGENT_TOOLS` is the full advertised set. `KB_search_dense` is
  defined but excluded (unavailable in the build env). No specialist action names are hard-coded.
- `agent.py` — `run_agent(messages) -> str`: wires `AnthropicMessagesClient.from_config()` +
  `ToolClient(require_banking_tools())` + `prompts.SYSTEM_PROMPT` + `tool_specs.AGENT_TOOLS` into an
  `AgentLoop`. Construction is lazy (inside `run_agent`), so `import agent` is side-effect-free and a
  missing runtime env surfaces as an honest per-request error, not an import failure.
- `prompts.py` — policy system prompt (grounding, discovery sequence, verification, requested-only
  actions, disputes, resolution, escalation, honesty on failure). Names no specific tools/fees; tool
  mechanics live in the ToolSpec descriptions.

## Confirmed interfaces

- **LLM:** `POST {ANTHROPIC_BASE_URL}/v1/messages`, model `claude-opus-4-8`, `Authorization: Bearer
  {ANTHROPIC_AUTH_TOKEN}`, `anthropic-version: 2023-06-01`, `APPSCI_AGENT_CUSTOM_HEADERS` merged in.
- **Tool transport:** `POST {BANKING_TOOLS_URL}/<name> {"arguments":{...}} -> {"result":<string>}`,
  bearer `BANKING_TOOLS_TOKEN`.
- **Retrieval tools:** `KB_search_bm25(query)`, `KB_search_dense(query)`, `shell(command)` — read-only.
- **BASE interface (channel-operators' spec, 2026-09-23):**
  - Read: `get_current_time()`, `get_user_information_by_id(user_id)`,
    `get_user_information_by_name(customer_name)`, `get_user_information_by_email(email)`,
    `get_referrals_by_user(user_id)`, `get_credit_card_transactions_by_user(user_id)`,
    `get_credit_card_accounts_by_user(user_id)`, `list_discoverable_agent_tools()`.
  - Action: `change_user_email(user_id, new_email)`;
    `log_verification(name, user_id, address, email, phone_number, date_of_birth, time_verified)` —
    the GENERAL verification-recording tool (compare two identity fields against the record first;
    `time_verified` is the `get_current_time` timestamp); `transfer_to_human_agents(summary,
    reason="other")` (19 valid reasons enumerated in `tool_specs._TRANSFER_REASONS`);
    `give_discoverable_user_tool(discoverable_tool_name, arguments="{}")`;
    `unlock_discoverable_agent_tool(agent_tool_name)` (returns text incl. the unlocked action's
    parameters); `call_discoverable_agent_tool(agent_tool_name, arguments="{}")`.
  - Notes honored: parameters shown without a default are required; in give/call, `arguments` is a
    JSON-ENCODED STRING (not a nested object) and is optional; some failures arrive as an
    `Error:`-prefixed string inside a 200 response and must not be treated as success.

## Runtime discovery flow (schema-agnostic; no specialist names hard-coded)

Search KB (or `list_discoverable_agent_tools`) → read the exact action name from the procedure →
`unlock_discoverable_agent_tool(name)` → read the returned parameter list → build the JSON-string
`arguments` and `call_discoverable_agent_tool(name, arguments)` → confirm the outcome in the records.
The `AgentLoop` handles this generically: the model reads unlock's returned parameter description and
constructs the call; nothing about specific specialist actions is baked into the code.

## Tests

140 passing. New for this phase: `tests/test_tool_specs.py` (the confirmed catalog: exact tool set,
seven required `log_verification` fields, 19 escalation reasons, empty schemas for no-arg tools, the
optional JSON-string `arguments` on give/call, no specialist names), `tests/test_agent.py` (wiring:
returns the answer, advertises the full confirmed set, closes both clients, import-safe, honest error
without runtime env), plus two `agent_core` tests for the `Error:`-prefix handling. `test_server.py`
updated: with the core now wired, the honest-error-not-fake-answer invariant is checked via the
missing-runtime-config path.

## Not done here (P3 owns)

Build-environment validation via `dev_deploy`, and any scoring, remain with P3. This work does not
deploy, score, or call `complete`, and modifies only `artifact/`.
