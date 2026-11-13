# Running another agent harness

The benchmark supplies the customer's build environment, a filtered task spec,
the stakeholder service, and three HTTP operations. Your harness supplies its own coding,
shell, internal evaluation, and optimization capabilities.

For the built-in Codex and Claude Code adapters, start with
[native CLI setup and runs](native_clis.md). The rest of this page is the generic integration contract.

## Launch contract

Build an image containing your harness and adapter entry point, a writable `/work`,
and whatever HTTP tooling the harness uses (for example, curl). The benchmark does
not require Python, httpx, a client library, or native tool registration. The adapter must:

1. Read `APPSCI_HARNESS_PROMPT` and pass the complete prompt to its agent.
   Read `APPSCI_HARNESS_CONTEXT` for structured task inputs.
2. Let the agent call the existing benchmark endpoints using its terminal or its
   own HTTP code. The prompt includes the complete [HTTP contract](http_contract.md);
   full URLs and authentication arrive through environment variables.
3. Apply the requested model, temperature, and budget controls. If a requested
   control cannot be honored, fail clearly. The orchestrator enforces the build
   timeout; other limits need native support. The built-in Codex and Claude Code
   adapters use an explicit exception: reference-agent temperature and turn/token/USD
   limits are ignored and recorded separately, not presented as enforced controls.
4. Write the deployable artifact to `APPSCI_HARNESS_ARTIFACT_DIR`, deploy it,
   test its endpoint, and call `complete`. Stop after successful submission.
   A zero exit code alone never counts as a submission.

Select your image using [example.yaml](example.yaml):

```bash
python -m appsci_bench.orchestrator.run \
  --task order_to_cash \
  --persona tasks/order_to_cash/user_personas/l3.yaml \
  --harness-config harnesses/example.yaml \
  --enable-observability --limit 3
```

`name` and `image` are required. `command` is an optional argv list replacing
Docker CMD; the image's ENTRYPOINT still applies. Omitting it uses the image's
default command. `passthrough_env` lists additional required variable names to
forward from the host. Benchmark connection variables cannot be overridden.
These additional variables reach only the harness; the existing model-provider
and task connection variables also reach its deployed artifact.

Without `--harness-config`, the reference AppSci Agent runs as before. The selector
also works through `BenchmarkOrchestrator.run(..., harness=HarnessConfig(...))`.
Pass `--harness-config` to `python -m appsci_bench.orchestrator.run`; the
`scripts/run_tasks.sh` wrapper does not expose this selector yet.
Native configs additionally select `adapter` (`codex` or
`claude-code`) and `auth` (`api` or `gateway`), and omit `command`.

## Input bundle

| Container path | Contents |
|---|---|
| `/benchmark/prompt.md` | Task brief; discovery, coding, internal evaluation, optimization, and handoff instructions |
| `/benchmark/context.json` | Version 1 input: filtered task, paths, environment-variable names, controls, artifact API, HTTP endpoint descriptions |
| `/benchmark/http_contract.md` | HTTP methods, authentication, payloads, responses, errors, and timeouts; also included in the prompt, without prescribing client commands |
| `/spec/spec.yaml` | The same filtered task spec given to the reference agent |
| `/data`, `/sop` | Task build material, when present, mounted read-only |
| `/work` | Writable scratch work, internal tests, reports, and score history |
| `APPSCI_HARNESS_ARTIFACT_DIR` | Writable Docker build context, normally `/work/out` |
| `APPSCI_HARNESS_HTTP_CONTRACT` | Path to the read-only HTTP contract |

The bundle contains variable names, never credential values. Actual values arrive
in the process environment. Task services are reachable over the Docker network.
Read their URLs and tokens at runtime: evaluation can start several copies of
the task environment with different addresses and credentials.

The prompt adapts the reference agent's [system instructions](../appsci_agent/prompts/system_prompt.jinja2)
and [engineering guidance](../appsci_agent/config/appsci_bench.yaml): focused stakeholder questions,
interleaved exploration/building, task-resource boundaries, and verified deployment. It assigns
engineering, internal evaluation, and optimization to the harness's own capabilities, without
copying reference-only tools, shell-response rules, or submission markers. HTTP details remain
a transport contract, not a shell tutorial. Optimization-disabled runs retain basic validation only.

### Context coverage

`context.json` is generated before each harness build and retained on the worker at
`runs/<build-run-id>/harness/context.json`. Its `task` object preserves the complete **public**
spec projection, not just the short description: goals, resource descriptions, dataset/document
descriptions, interface requirements, expected artifacts and budgets when declared. Tasks may omit
these optional fields. Resource locations, hidden persona documents, evaluation data and host-side
environment setup remain private; task-specific discovery still happens through the supplied
resources and stakeholder service. The reference agent's deny filtering is applied before writing
either the mounted spec or the context bundle.

Runtime information comes through separate paths and is recorded alongside `task`:

- `paths`: the actual artifact output and available `/data` and `/sop` mounts, plus workspace/spec paths.
- `environment.task_env_vars`: names resolved from the task's build-environment configuration.
- `environment.model_env_vars`: non-empty model settings from the effective deployment environment,
  including task overrides of host settings.
- `environment.harness_env_vars`: additional variables requested by the harness launch configuration.
- `environment.artifact_env_vars`: the exact deployment variable names, including the injected `PORT`;
  harness-only credentials are not implicitly forwarded.
- `artifact`: configured port, bind address, endpoint paths, required files, accepted server-entry
  names from the deployer, and the complete versioned session API contract.
- `build_controls` and `http_api`: effective run controls and the existing service contracts.

Build-phase database/tool-service data stays in the task workspace container; it is not copied into
the JSON or confused with the local file mounts. Tests enumerate all task specs (including spec-only
tasks), exercise the configured tasks through mocked build-only and build/evaluate runs, and require
an explicit visibility decision when the task schema gains a field. Adding context coverage does not
make an unimplemented task environment runnable.

Hidden personas, stakeholder-only documents, held-out cases, evaluator code, and
the Docker socket are not mounted. Harness images must not contain these materials.
The orchestrator halts the builder before starting the evaluation environment.

## Existing HTTP services

The benchmark supplies the endpoint contract, not client-side code. No new HTTP
service is introduced. AppSci Agent retains its existing clients; external
harnesses call the same services however they choose.

| Operation | Full URL environment variable | Request |
|---|---|---|
| `talk_to_usersim` | `APPSCI_USERSIM_ASK_URL` | `POST /ask` with `question`; bearer authentication using `APPSCI_USERSIM_TOKEN` |
| `deploy_artifact` | `ORCHESTRATOR_DEPLOY_URL` | `POST /deploy` with `{}` |
| `complete` | `ORCHESTRATOR_SUBMIT_URL` | `POST /submit` with the deployed `artifact_endpoint` |

`ORCHESTRATOR_DEPLOY_STATUS_URL` exposes the existing read-only `GET /deploy/status`
route for recovering the last successful endpoint and checking deployment freshness.
There is no session to create: stakeholder history and deployment state stay on
the servers. The original `APPSCI_USERSIM_URL` base URL remains available too.

See [the HTTP contract](http_contract.md) for exact payloads and examples. Inspect
response bodies as well as HTTP statuses: deployment failures can return HTTP 200
with `success: false`. Test the deployed artifact before submitting. For configured
benchmark runs, the existing submission endpoint rejects missing deployments,
mismatched endpoints, and source changed since deployment; these checks cannot be
bypassed by choosing a different client. Duplicate submission is also an error.
Standalone submission-server callers without a host-deployment configuration retain
their endpoint-only behavior.

No coding or optimization tools are supplied. The prompt asks the harness to create internal cases and checks,
measure a deployed baseline, improve and redeploy, compare against fixed tests,
restore regressions, and test the final candidate. `--optimize-cycles 0` permits
measurement but no improvements; `--no-optimization` requests basic checks only.

## Evaluation and records

Use the existing [artifact HTTP contract](../docs/artifact_agent_api.md):
`GET /health` and `POST /respond`, with conversation history and optional structured
output and files. Evaluation is independent of the harness that built the artifact.
`--skip-eval` produces a build; `--artifact-dir` can score its saved source later.

The run name includes the harness name. `runs/<run>/harness/` retains the public
inputs and launch configuration. Deployment checkpoints, stakeholder exchanges,
and evaluation reports use the existing paths. Enable observability to preserve
the stakeholder transcript for the elicitation diagnostic.

The worker captures native CLI output live into `harness/native.log`, with known credentials
redacted and a 50 MiB capture limit. The tracer's Build tab displays assistant messages, tool calls,
responses, and file changes from Codex and Claude Code; it also reads older cleanup-only logs.
See [native history](../appsci_bench/tracer/README.md#native-harness-history). These events are not
translated into the reference agent's model-call, token/cost, or optimization accounting.
The reference-tool optimization diagnostic is unmeasurable for an external harness,
not evidence that it made zero optimization attempts. Save native logs and internal
test results under `/work` for filesystem snapshots when observability is enabled;
existing snapshot size limits apply. Benchmark-side logs alone do not establish
complete token usage.

Only one benchmark run may execute per host. All harnesses share the existing
container resource limits, timeout handling, artifact snapshots, and cleanup.
