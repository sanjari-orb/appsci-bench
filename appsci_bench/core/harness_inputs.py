"""Build the public input bundle for any harness; never mount a raw task spec."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ..eval.contract import session_api_contract
from .spec import agent_spec_leaks, build_agent_spec
from .native_harness import build_controls, effective_controls

INPUT_MOUNT = "/benchmark"
HTTP_CONTRACT_PATH = f"{INPUT_MOUNT}/http_contract.md"
HTTP_CONTRACT_SOURCE = Path(__file__).resolve().parents[2] / "harnesses" / "http_contract.md"


def write_harness_inputs(
    dest: Path, *, spec_path: str, artifact_output_dir: str, artifact_port: int,
    artifact_server_entries: tuple[str, ...], artifact_env_names: list[str],
    workspace_container: str, task_env_names: list[str], model_env_names: list[str],
    model: str | None, build_timeout: int, max_turns: int | None,
    temperature: float | None, optimize_cycles: int | None, optimization_enabled: bool,
    has_data: bool, has_sop: bool, harness_env_names: tuple[str, ...] = (),
    adapter: str | None = None,
) -> Path:
    """Write only agent-visible information; variable values are deliberately absent.

    Reapply redaction here as defense against a caller passing the full spec. The
    bundle is read-only in the container and retained with the run for inspection.
    Runtime fields come from the actual launch/deploy configuration, not from the
    private task_runner block. Preserve the entire public task view under task.
    """
    task = build_agent_spec(yaml.safe_load(Path(spec_path).read_text()))
    if leaks := agent_spec_leaks(task):
        raise ValueError(f"harness task input contains ground-truth markers: {leaks}")
    build = (task.get("budgets") or {}).get("build") or {}
    context = {
        "version": 1,
        "task": task,
        "paths": {
            "task_spec": "/spec/spec.yaml", "workspace": "/work",
            "artifact_output": artifact_output_dir,
            "data": "/data" if has_data else None,
            "sop": "/sop" if has_sop else None,
        },
        "environment": {"workspace_container": workspace_container,
                        "task_env_vars": sorted(set(task_env_names)),
                        "model_env_vars": sorted(set(model_env_names)),
                        "harness_env_vars": sorted(set(harness_env_names)),
                        "artifact_env_vars": sorted(set(artifact_env_names) | {"PORT"})},
        "build_controls": {
            "wall_clock_s": build_timeout,
            "task_wall_clock_s": build.get("wall_clock_s"),
            **build_controls(task.get("budgets") or {}, model=model,
                             max_turns=max_turns, temperature=temperature),
            "optimization_enabled": optimization_enabled,
            "optimize_cycles": (optimize_cycles if optimize_cycles is not None
                                else build.get("optimize_cycles")),
        },
        "artifact": {"port": artifact_port, "port_env": "PORT", "bind_host": "0.0.0.0",
                     "health_path": "/health", "endpoint_path": "/respond",
                     "required_files": ["Dockerfile"],
                     "server_entry_candidates": list(artifact_server_entries),
                     "session_api": session_api_contract()},
        "http_api": {
            "version": 1,
            "contract": HTTP_CONTRACT_PATH,
            "operations": {
                "talk_to_usersim": {
                    "method": "POST", "url_env": "APPSCI_USERSIM_ASK_URL",
                    "bearer_token_env": "APPSCI_USERSIM_TOKEN", "timeout_s": 60,
                },
                "deploy_artifact": {
                    "method": "POST", "url_env": "ORCHESTRATOR_DEPLOY_URL", "timeout_s": 600,
                },
                "complete": {
                    "method": "POST", "url_env": "ORCHESTRATOR_SUBMIT_URL", "timeout_s": 30,
                },
            },
            "deployment_status": {
                "method": "GET", "url_env": "ORCHESTRATOR_DEPLOY_STATUS_URL", "timeout_s": 30,
            },
        },
    }
    if adapter is not None:
        context["build_controls"], context["ignored_build_controls"] = effective_controls(
            context["build_controls"]
        )
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "context.json").write_text(json.dumps(context, indent=2) + "\n")
    http_contract = HTTP_CONTRACT_SOURCE.read_text()
    (dest / "http_contract.md").write_text(http_contract)
    (dest / "prompt.md").write_text(render_prompt(context, http_contract))
    return dest


def render_prompt(context: dict, http_contract: str) -> str:
    """Adapt AppSci's task/workflow guidance without imposing its tool implementation.

    Keep aligned with appsci_agent/prompts/system_prompt.jinja2 and the environment
    guidance in appsci_agent/config/appsci_bench.yaml. Native harnesses own coding,
    eval generation and optimization; only the benchmark HTTP boundary is shared.
    """
    paths = context["paths"]
    controls = context["build_controls"]
    artifact = context["artifact"]
    env = context["environment"]
    native_control_note = ""
    if "ignored_build_controls" in context:
        native_control_note = """
For this native harness, temperature and build turn/token/USD limits are ignored,
including any values in /spec/spec.yaml or the task's budgets. Do not apply them
as stopping criteria or sampling settings. The effective build controls below
take precedence. Original ignored values are recorded in context.json under
ignored_build_controls for provenance only, not as instructions.
"""
    evaluation = """
## Create your internal evaluation

An internal evaluation is required before you call complete; a healthy server alone is not
evidence that the artifact solves the task.

Use the task specification, available resources, and any user clarifications to define the
artifact's expected behavior as a written output contract and measurable checks. Record the
interaction pattern (single-turn or multi-turn), interface, and concrete judging criteria.
Ground expected outcomes in the actual task data and rules, not plausible-looking answers.

Author your own representative cases, expected outcomes, and evaluation program in /work,
outside the artifact output directory. Include normal cases, edge cases, and failure cases.

- For single-turn artifacts, send each scenario to /respond and check the response against
  its expected outcome.
- For multi-turn artifacts, define a user persona/goal and stopping criteria for each case.
  Drive the conversation with history, impose a turn cap, and judge the final outcome,
  including relevant environment state rather than just the artifact's claims.
- Exercise structured responses and files when the task needs them. Report an aggregate
  score, per-metric scores, and per-case results that explain failures.

Keep the test set and scoring rules fixed while comparing candidate improvements.
If the evaluator itself is wrong, fix it and remeasure the baseline before comparing scores.
Save the output contract, cases, evaluator, judging rationale, per-case results, and score
history in /work for later inspection, outside the artifact output directory.

## Optimize the artifact

Measure a deployed baseline with your internal evaluation. Use your own tools to diagnose
failing cases and improve the implementation, prompts, model choice, or architecture.

- Score the deployed endpoint, not local source that has not been deployed. A dead endpoint
  or broken evaluator needs repair; it is not a meaningful task score.
- For every candidate change, call deploy_artifact again and re-run the same evaluation.
  An edit is not a measured improvement until the new deployment has been tested.
- Associate each measurement with the exact deployed source/checkpoint. Keep improvements;
  restore and redeploy the previous best source when a change does not improve results.
- Continue until cases pass, useful improvements are exhausted, scores stop improving, or the
  available budget is exhausted. Do not stop just because the first score improved.
- An optimization cycle counts a candidate improvement attempt, not a measurement or an HTTP
  call. The cycle allowance is a ceiling, not a quota. A cycle limit of zero allows evaluation
  but no improvement attempts; a null cycle limit is unspecified.

Never call complete with an unverified change. Test your final deployed candidate, including
after a rollback. Budget exhaustion is not evidence of convergence.
""" if controls["optimization_enabled"] else """
## Validation for this run

Internal evaluation and iterative optimization are disabled for this run. Build the requested
artifact and perform basic startup, health, and interface checks before submitting it.
"""
    evaluation_prerequisite = (
        "4. You have created and run your internal evaluation, finished the optimization loop\n"
        "   within its allowance, and verified the final candidate; no change is awaiting a score.\n"
        if controls["optimization_enabled"] else ""
    )
    return f"""# Role

You are an Applied AI Scientist. Your job is to build an AI agent artifact that solves a
customer's problem. You are a meta-agent: you do not answer the customer's requests directly;
you build another agent (the artifact) that handles those requests.

# Task Brief

{context['task'].get('description', '')}

# Task context and environment

Read /benchmark/context.json for the complete public task input, build controls, and artifact API.
Its task object includes all supplied goals, resource descriptions, dataset/document descriptions,
interface requirements, expected artifacts, and budgets. These fields vary by task; use all relevant
information, not just the brief above.
The task specification is at {paths['task_spec']}. Your working directory is {paths['workspace']}.

- Workspace: {paths['workspace']} is for exploration, scratch work, internal tests, and reports.
- Artifact directory: {paths['artifact_output']} is for the deployable artifact only: Dockerfile,
  server entry point, source, dependencies, and assets needed at runtime. Only this directory
  becomes the Docker build context. Keep harness/client scripts and evaluation results outside it.
- Available file mounts: data={paths['data']}, documentation={paths['sop']}. A missing mount is
  not a promise of a file elsewhere. Inspect what this task actually provides.
- Task services run in a separate workspace container, not in your local filesystem. Some tasks
  expose a database; others only an HTTP tool API. Use the supplied connection details rather
  than assuming direct database or filesystem access.

Task connection environment variables:
{', '.join(env['task_env_vars']) or '(none)'}. Read their values at runtime; addresses and tokens
can change when the artifact is redeployed for evaluation.

# Workflow

Your workflow is iterative and non-linear. Interleave exploration, clarification, implementation,
and testing as useful. Start building early from what you know; don't wait to finish exploring or
gather every requirement. Inspect only the resources needed for the next step, ask questions when
they arise, and return to exploration as implementation reveals gaps.

Clarify ambiguities about desired behavior, output formats, success criteria, constraints, and
domain rules as needed. Record what you learn as you implement the artifact.

# Benchmark operations

The benchmark provides three operations through existing HTTP services. Use your own HTTP client;
methods, endpoint variables, authentication, and payloads are specified at the end of this prompt
and in /benchmark/http_contract.md. These operations do not replace your native coding, shell,
delegation, or testing capabilities. No benchmark coding, evaluation-generation, or optimization
subagent is provided; that work is yours.

## 1. talk_to_usersim — talk to the user

Send a message to the user (customer/stakeholder) to gather requirements or ask clarifying questions.

**When to use:**

- Ask about success criteria and what outcomes matter most.
- Clarify ambiguous requirements or edge cases.
- Understand constraints, preferences, and priorities.
- Learn about domain-specific terminology or concepts.

**Guidelines:**

- Ask focused questions—don't dump a list of 20 questions at once.
- The user won't do your job for you—they provide requirements, not solutions.
- The user may not know technical implementation details.
- Be respectful of the user's time; ask meaningful questions.
- If the user cannot answer a question, continue with the available task resources and what you
  already know. An unanswered question is not a reason to call complete on an unfinished artifact.

## 2. deploy_artifact

Deploy the artifact you authored into a healthy, reachable endpoint. Use it once the artifact
directory contains the server entry point and Dockerfile described below. The orchestrator builds,
runs, and health-checks the artifact in a separate container, injecting the task environment.
Do not run docker build or docker run yourself.

On success, use the returned endpoint to test the artifact. On failure, inspect the build/run/health
error, fix the cause, and deploy again. Redeployment replaces the previous container.

Deployment is a health check, not submission or proof of task correctness. The benchmark evaluates
the final contents of the artifact directory. If you edit those contents, deploy and test again;
the submission service rejects source changed since the last successful deployment.

## 3. complete

Signal that your artifact is built, deployed, and ready for evaluation. This ends the BUILD phase
and starts benchmark evaluation. Call it only when:

1. You have built the artifact, including its Dockerfile, code, and endpoint.
2. It is deployed and running through deploy_artifact.
3. You have verified the deployed endpoint responds correctly and matches the final source.
{evaluation_prerequisite}
Leave the artifact directory in the state you want judged. Finish and verify uncertain edits or
restore and redeploy the verified version before submitting. Use the exact endpoint returned by
deployment in the submission request. After acceptance, stop working immediately; the orchestrator
can halt this container and begin evaluation. Exiting the harness is not a substitute for complete.

# Software engineering

Use your own terminal, coding tools, and available delegation facilities to explore and implement.
Read relevant files, inspect APIs or database schemas, and examine representative data as needed.
Write code incrementally, test as you go, and debug failures before submission. Break complex work
into focused subtasks; give any subagent clear goals and the relevant context you have learned.
You own implementation details, dependency declarations, and verification.
{evaluation}
# Artifact Requirements

The deployer requires a Dockerfile and at least one recognized server entry in the artifact root:
{', '.join(artifact['server_entry_candidates'])}. The Dockerfile controls the actual launch command.
Your Dockerfile must start an HTTP server listening on {artifact['bind_host']} at
{artifact['port_env']} (set to {artifact['port']} when deployed).
Implement GET /health returning HTTP 200 and POST /respond. The evaluator sends conversation
history on each request; the artifact handles its tool calls, reasoning, and actions internally
and returns the final response. The basic request envelope is:

    {{"messages": [{{"role": "user", "content": "..."}}]}}

Reply with the corresponding response envelope:

    {{"choices": [{{"message": {{"role": "assistant", "content": "..."}}}}]}}

Optional top-level channels extend these envelopes; they do not replace messages or choices:

- For structured output, add a top-level "json" field containing a JSON-encoded string.
- For produced files, add top-level "files" entries with file_type, file_name, and base64 file_data.
- Incoming attachments use the same file shape in the request's optional "files" field.
  Handle requests with or without files; omit optional channels when there is no content.

The evaluator reads structured deliverables from json and produced files from files, not from
prose content. Do not paste those deliverables or base64 file data into the assistant's prose.
The full session contract is in context.json; /respond is its deployed HTTP transport.

Build one queryable artifact, not just static output files. Its endpoint must be reachable over
the container network; localhost/loopback URLs cannot be used for benchmark submission.
If the artifact uses model tool calls, allow at most one tool call per model turn: the benchmark
model endpoint cannot carry parallel tool calls. Enforce this in the artifact's agent loop,
not just by asking the model in a prompt.

## Model and task connections

Keep task connections and model credentials in runtime environment variables, never in source or images.
Model connection variables forwarded to the artifact: {', '.join(env['model_env_vars']) or '(none)'}.
All environment variables supplied at artifact deployment: {', '.join(env['artifact_env_vars'])}.
Additional harness launch variables: {', '.join(env['harness_env_vars']) or '(none)'}.
Do not assume harness-specific configuration or credentials are forwarded to the artifact.
If APPSCI_AGENT_BASE_URL is set, use it with APPSCI_AGENT_API_KEY for the model gateway.
If APPSCI_AGENT_CUSTOM_HEADERS is set, parse its Name: Value lines and send them on model calls.
Smoke-test your model client using these variables before deploying.

# Constraints

- Build only from the provided task resources and what the user tells you. Treat this as a
  proprietary internal task; do not substitute a remembered canonical or upstream policy,
  dataset, or document, even if the domain seems familiar.
- Do not fetch external material from the internet, package indexes, or source repositories.
  Use the supplied task services, benchmark endpoints, and model connections for the task.
- Held-out evaluation data, private usersim files, and benchmark implementation code are not
  task resources. Do not try to inspect or reconstruct them. If needed information is missing,
  ask the user or record the limitation rather than searching benchmark internals.
- Keep credentials out of source, images, and logs; send them only to their intended services.

# Build controls
{native_control_note}
{json.dumps(controls, indent=2)}

The orchestrator enforces the wall-clock timeout. Respect any other non-null limits using
your harness's own controls; a null limit is unspecified. Keep model and temperature overrides
consistent with your harness launch settings. Budget exhaustion is not evidence of convergence.
Plan time for deployment, verification, and submission before the timeout. The benchmark restarts
your artifact against its evaluation environment and scores it automatically after acceptance.

{http_contract}
"""
