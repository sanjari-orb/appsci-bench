# Codex and Claude Code

Both adapters run non-interactively in the benchmark's build container. They receive the public
task/environment bundle and full prompt, use their own coding/shell/optimization capabilities,
and call the existing HTTP endpoints for `talk_to_usersim`, `deploy_artifact`, and `complete`.
There is no extra HTTP service, MCP server, or benchmark client library.

## Install

The normal setup installs both CLIs on the host and builds their shared Docker image:

```bash
source the setup steps in README.md
```

Pins are in [versions.env](versions.env): Codex 0.154.0 and Claude Code 2.1.271. Matching installed
versions are reused; otherwise installation uses the repo-local, ignored `.harness-clis/` directory.
No sudo npm, global CLI replacement, browser login, or user auth/config edits are performed.
Installing missing packages needs Node.js 22+ and npm. Set `APPSCI_SKIP_HARNESS_CLIS=1` to skip
host installation; the Docker image still installs both CLIs using its own Node.js.

For an existing environment, including an EC2 host with direct provider credentials:

```bash
# Optional: host CLIs for manual use (Node.js 22+).
bash scripts/install_harness_clis.sh
export PATH="$PWD/.harness-clis/bin:$PATH"
bash scripts/install_harness_clis.sh --check

# Required for benchmark runs; no host Node.js or CLI login needed.
docker build -f harnesses/Dockerfile -t appsci-bench-harness-clis:latest .
```

The image contains the two CLIs, Python, Node.js, git, curl, ripgrep, and build tools. It runs as
non-root. It does not contain the repo, hidden task data, evaluators, user auth files, or Docker
socket. Package versions are pinned; the base image tag and apt packages are not digest-locked.

## Do I need to sign in?

No personal ChatGPT/Claude subscription or interactive CLI login is required for these benchmark
adapters. A valid API or gateway credential is required, and usage is billed by that provider or
gateway. Interactive subscription login remains an option for manual host CLI use, but these
container adapters deliberately use API credentials, not your cached subscription login.
See [Codex authentication](https://learn.chatgpt.com/docs/auth) and
[Claude Code gateway authentication](https://code.claude.com/docs/en/llm-gateway-connect).

| Harness configuration | Credential and route |
|---|---|
| `codex.yaml` | First-party `OPENAI_API_KEY`; OpenAI Responses API |
| `claude-code.yaml` | First-party `ANTHROPIC_API_KEY`; Anthropic API |
| `claude-code-gateway.yaml` | gateway's `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and optional `ANTHROPIC_CUSTOM_HEADERS` |
| `codex-gateway.yaml` | gateway bearer/header variables plus **explicit** `APPSCI_CODEX_RESPONSES_BASE_URL` |

Run `gateway login` if your organization credential needs refreshing, then source the normal
setup script. This is organization/gateway sign-in, not a personal Claude/OpenAI subscription.
The adapter forwards identity headers such as `X-Gateway-User` without writing their values into
CLI arguments or the public input bundle. Do not paste keys into YAML, prompts, source, or logs.

Claude Code supports this Anthropic gateway configuration, but the gateway must still accept its
Messages API streaming and tool-use traffic and required headers. A successful ordinary model
request alone does not verify an entire agent run.
[Gateway protocol requirements](https://code.claude.com/docs/en/llm-gateway-protocol).

**Codex + gateway remains conditional:** the pinned Codex CLI requires the Responses API, including
streaming/tool calls; it no longer accepts the Chat Completions wire protocol. The repo's eval-time
OpenAI-compatible chat endpoint does not establish Responses compatibility. Confirm support with
the gateway operator before setting `APPSCI_CODEX_RESPONSES_BASE_URL` to its API base (not the full
`/responses` URL). No automatic fallback, endpoint guessing, or translation proxy is supplied.
Otherwise use a first-party OpenAI key.
[Codex custom providers](https://learn.chatgpt.com/docs/config-file/config-advanced).

The direct Codex config rejects a gateway `OPENAI_BASE_URL` or a key equal to the gateway token.
The direct Claude config removes gateway overrides only in its child process. Neither adapter
changes the separate benchmark-side user-sim/judge credentials or the artifact's runtime model
configuration. Harness-only variables are not automatically forwarded to deployed artifacts.

## Run through the orchestrator

Use a model ID available to the selected provider (not a LiteLLM `anthropic/` or `openai/` prefix).
Set `CODEX_MODEL` and/or `CLAUDE_MODEL` to that ID. Both adapters require an explicit model so a
CLI update cannot silently change it. Keep the usual benchmark-side model credentials configured.

```bash
# Codex, first-party API. Load a real OPENAI_API_KEY from your approved secret source first.
# The subshell leaves the parent shell's eval-time gateway routing unchanged.
(
  unset OPENAI_BASE_URL
  python -m appsci_bench.orchestrator.run \
    --task airline_rebooking --persona tasks/airline_rebooking/user_personas/l3.yaml \
    --harness-config harnesses/codex.yaml --agent-model "${CODEX_MODEL:?set a native model ID}" \
    --enable-observability --limit 3
)

# Claude Code with gateway credentials already loaded by setup.
python -m appsci_bench.orchestrator.run \
  --task airline_rebooking --persona tasks/airline_rebooking/user_personas/l3.yaml \
  --harness-config harnesses/claude-code-gateway.yaml \
  --agent-model "${CLAUDE_MODEL:?set a gateway model ID}" --enable-observability --limit 3
```

For first-party Claude use `harnesses/claude-code.yaml` and `ANTHROPIC_API_KEY` instead.
For a verified Responses-capable gateway use `harnesses/codex-gateway.yaml` and the explicit base
URL described above. The adapter never probes credentials by making an extra paid model request.

## Controls and limitations

The adapters fail **before starting task services** for missing auth/model. The reference-agent
controls `agent_temperature`, build turns, total tokens, and USD budgets are ignored for both
native harnesses, whether supplied explicitly or inherited from the task spec. This also applies
to Claude Code's turn/USD flags: these benchmark settings are not passed to either native CLI.
The internal AppSci agent and original task specs are unchanged.

| Control | Codex | Claude Code |
|---|---|---|
| Build timeout | Existing orchestrator timeout | Existing orchestrator timeout |
| Model | Native `--model` | Native `--model` |
| Turns | Ignored | Ignored |
| USD | Ignored | Ignored |
| Total tokens or temperature | Ignored | Ignored |
| Internal evaluation / optimization cycles | Prompt instructions | Prompt instructions |

Submission returns warnings naming ignored controls. Native cell records have
`agent_temperature: null`, so ignored temperature settings do not create separate experiment arms.
The original sweep YAML retains ignored temperatures. Each build's `harness/context.json` records
effective `build_controls` separately from ignored task budgets or direct-orchestrator overrides
in `ignored_build_controls`. The prompt explicitly overrides the
original task's build turn/token/USD limits; they are not soft stopping instructions either.
The orchestrator still enforces the configured wall-clock timeout. Native runs therefore do not
have the reference agent's turn/token/dollar budget guarantees and should not be described as
budget-matched comparisons. Optimization cycle counts remain prompt instructions, not separately
enforced native CLI counters or measured reference-tool optimization cycles.

Tasks with non-null build budgets, including `order_to_cash`, can run with either adapter without
changing their specs. Deploy the updated job server, worker code, and rebuilt native harness image
together; old server/image versions still reject these controls.

Non-interactive native permission-bypass flags are used **only inside the isolated, non-root
benchmark Docker container** so agents can execute their own shell/code tools. The entry point
refuses host/root execution. No host CLI defaults or permissions are changed. Do not add host
directory, credential-store, or Docker-socket mounts to this image.

Native stdout/stderr is captured live to `runs/<run>/harness/native.log`, with known credentials
redacted, timestamps, and a 50 MiB capture limit. The tracer's Build tab shows native messages,
tool calls/responses, file changes, and errors, including saved logs from older runs. Live updates
require updated worker code; saved-history viewing requires only the updated tracer. Docker still
retains up to two 50 MB log files. Public inputs and launch identity live alongside it. Native model usage is not yet
translated into the AppSci tracer's accounting, so benchmark-side totals are incomplete for these
harnesses. Artifact checkpoints, submission validation, timeout, handover, and evaluation all use
the existing pipeline. A clean CLI exit without an accepted `complete` call is `no_submission`,
not a completed build. See the [HTTP contract](http_contract.md).
