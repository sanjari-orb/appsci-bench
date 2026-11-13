"""Public harness inputs and launch wiring; Docker is mocked, task specs are real."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from appsci_bench.core.agent_container import AppSciAgentContainerConfig, AppSciAgentContainerManager
from appsci_bench.core.harness import HarnessConfig
from appsci_bench.core.harness_inputs import HTTP_CONTRACT_SOURCE, write_harness_inputs
from appsci_bench.core.native_harness import NATIVE_ENV, make_launch
from appsci_bench.core.spec import all_task_ids
from appsci_bench.orchestrator.deployer import LLM_CREDENTIAL_ENV_VARS, SERVER_ENTRY_CANDIDATES
from appsci_bench.task_runner.config import TaskConfig

ROOT = Path(__file__).resolve().parents[3]
RUNTIME_TASK_IDS = [task_id for task_id in all_task_ids()
                    if "task_runner" in yaml.safe_load((ROOT / "tasks" / task_id / "spec.yaml").read_text())]


def inputs(tmp_path, **overrides):
    args = dict(
        spec_path=str(ROOT / "tasks/order_to_cash/spec.yaml"),
        artifact_output_dir="/work/out", artifact_port=8080,
        artifact_server_entries=SERVER_ENTRY_CANDIDATES,
        artifact_env_names=["DB_PASSWORD", "OPENAI_API_KEY"],
        workspace_container="customer-workspace", task_env_names=["DB_PASSWORD"],
        model_env_names=["OPENAI_API_KEY"], model="test-model", build_timeout=123,
        max_turns=12, temperature=0, optimize_cycles=0, optimization_enabled=True,
        has_data=False, has_sop=True,
    )
    args.update(overrides)
    return write_harness_inputs(tmp_path / "inputs", **args)


def test_input_bundle_is_public_and_describes_the_actual_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-the-prompt")
    bundle = inputs(tmp_path)
    context = json.loads((bundle / "context.json").read_text())
    for forbidden in ("evaluation_data", "task_runner", "user_personas", "source", "gold_solution"):
        assert forbidden not in context["task"]
    assert context["paths"]["data"] is None
    assert context["paths"]["sop"] == "/sop"
    assert context["build_controls"]["wall_clock_s"] == 123
    assert context["build_controls"]["optimize_cycles"] == 0
    assert context["build_controls"]["temperature"] == 0
    assert context["build_controls"]["max_turns"] == 12
    assert context["build_controls"]["max_tokens"] == 20000000
    assert context["build_controls"]["max_usd"] == 20
    assert "ignored_build_controls" not in context
    assert context["artifact"]["session_api"]["version"] >= 1
    assert set(context["http_api"]["operations"]) == {
        "talk_to_usersim", "deploy_artifact", "complete",
    }
    text = "\n".join(p.read_text() for p in bundle.iterdir())
    assert "must-not-enter-the-prompt" not in text
    assert "OPENAI_API_KEY" in text
    assert "mini_swe_agent" not in text
    assert "eval_generator" not in text
    assert "optimize_agent" not in text
    assert "Create your internal evaluation" in text
    assert "Optimize the artifact" in text
    assert "tools" not in context
    assert not (bundle / "tools.json").exists()
    assert (bundle / "http_contract.md").read_text() == HTTP_CONTRACT_SOURCE.read_text()
    assert HTTP_CONTRACT_SOURCE.read_text() in (bundle / "prompt.md").read_text()


def test_input_rejects_leaking_free_text(tmp_path):
    raw = yaml.safe_load((ROOT / "tasks/order_to_cash/spec.yaml").read_text())
    raw["description"] = "Read tasks/order_to_cash/eval_data for the answers"
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="ground-truth"):
        inputs(tmp_path, spec_path=str(path))


@pytest.mark.parametrize("adapter", ["codex", "claude-code"])
def test_native_input_records_effective_and_ignored_controls_separately(tmp_path, adapter):
    bundle = inputs(tmp_path, adapter=adapter)
    context = json.loads((bundle / "context.json").read_text())
    assert context["ignored_build_controls"] == {
        "temperature": 0, "max_turns": 12, "max_tokens": 20000000, "max_usd": 20,
    }
    for key in context["ignored_build_controls"]:
        assert context["build_controls"][key] is None
    assert context["build_controls"]["wall_clock_s"] == 123
    assert context["build_controls"]["model"] == "test-model"
    assert context["build_controls"]["optimize_cycles"] == 0
    assert context["task"]["budgets"]["build"]["turns"] == 40  # Original task is unchanged.
    prompt = (bundle / "prompt.md").read_text()
    assert "temperature and build turn/token/USD limits are ignored" in prompt
    assert "including any values in /spec/spec.yaml" in prompt


@pytest.mark.parametrize("adapter", [None, "codex", "claude-code"])
def test_ablation_prompt_does_not_request_an_optimization_loop(tmp_path, adapter):
    bundle = inputs(tmp_path, optimization_enabled=False, adapter=adapter)
    text = (bundle / "prompt.md").read_text()
    assert "optimization are disabled" in text
    assert "## Optimize the artifact" not in text
    assert "An internal evaluation is required" not in text
    assert "finished the optimization loop" not in text
    assert "basic startup, health, and interface checks" in text


@pytest.mark.parametrize("optimization_enabled", [True, False])
@pytest.mark.parametrize("adapter", [None, "codex", "claude-code"])
def test_prompt_does_not_require_elicitation_before_implementation(tmp_path, optimization_enabled, adapter):
    bundle = inputs(tmp_path, optimization_enabled=optimization_enabled, adapter=adapter)
    text = " ".join((bundle / "prompt.md").read_text().split())
    assert "Clarify ambiguities" in text
    assert "domain rules as needed" in text
    assert "Record what you learn as you implement the artifact" in text
    assert "requirements you elicited" not in text
    assert "Record what you learn, then implement" not in text
    if optimization_enabled:
        assert "task specification, available resources, and any user clarifications" in text
        assert "written output contract and measurable checks" in text
        assert "Interleave exploration, clarification, implementation, and testing as useful" in text
        assert "Author your own representative cases, expected outcomes, and evaluation program" in text
        assert "Measure a deployed baseline with your internal evaluation" in text


@pytest.mark.parametrize("adapter", [None, "codex", "claude-code"])
@pytest.mark.parametrize("optimization_enabled", [True, False])
def test_prompt_preserves_appsci_user_guidance_without_reference_tool_mechanics(
    tmp_path, adapter, optimization_enabled,
):
    bundle = inputs(tmp_path, adapter=adapter, optimization_enabled=optimization_enabled)
    prompt = (bundle / "prompt.md").read_text()
    text = " ".join(prompt.split())
    reference = (ROOT / "appsci_agent/prompts/system_prompt.jinja2").read_text()
    # Anchored on the tool NAME, not on a heading number: the template computes those numbers
    # (`## {{ ns.n }}.`) because the ablation flags change how many tools there are, so "## 1." is
    # not a fixed string any more and "## 2." is not reliably the next section.
    user_guidance = reference.split("`talk_to_user`", 1)[1].split("\n## ", 1)[0]
    for line in user_guidance.splitlines():
        if line.startswith("- "):
            assert line.removeprefix("- ").rstrip(".") in text
    assert "meta-agent" in text
    assert "iterative and non-linear" in text
    assert "Start building early" in text
    assert "An unanswered question is not a reason to call complete on an unfinished artifact" in text
    assert prompt.index("## 1. talk_to_usersim") < prompt.index("# Benchmark HTTP contract")
    for reference_only in ("mini_swe_agent", "eval_generator", "optimize_agent",
                           "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "MUST include at least one bash"):
        assert reference_only not in prompt
    for shell_recipe in ("curl", "```sh", "```bash", "--fail-with-body"):
        assert shell_recipe not in prompt
    assert "Do not run docker build or docker run yourself" in text
    assert "source changed since the last successful deployment" in text
    assert "Use the exact endpoint returned by deployment" in text


@pytest.mark.parametrize("adapter", ["codex", "claude-code"])
def test_native_prompt_requires_grounded_evaluation_and_verified_optimization(tmp_path, adapter):
    prompt = (inputs(tmp_path, adapter=adapter) / "prompt.md").read_text()
    text = " ".join(prompt.split())
    for instruction in (
        "An internal evaluation is required before you call complete",
        "Ground expected outcomes in the actual task data and rules",
        "For single-turn artifacts", "For multi-turn artifacts",
        "user persona/goal and stopping criteria", "including relevant environment state",
        "aggregate score, per-metric scores, and per-case results",
        "Keep the test set and scoring rules fixed",
        "fix it and remeasure the baseline",
        "outside the artifact output directory",
        "restore and redeploy the previous best source",
        "A cycle limit of zero allows evaluation but no improvement attempts",
        "Never call complete with an unverified change",
        "Test your final deployed candidate, including after a rollback",
        "Budget exhaustion is not evidence of convergence",
    ):
        assert instruction in text


def test_prompt_keeps_artifact_channels_environment_and_task_boundaries(tmp_path):
    prompt = (inputs(tmp_path) / "prompt.md").read_text()
    text = " ".join(prompt.split())
    for instruction in (
        "Read /benchmark/context.json for the complete public task input",
        "Artifact directory: /work/out is for the deployable artifact only",
        "Keep harness/client scripts and evaluation results outside it",
        "Some tasks expose a database; others only an HTTP tool API",
        "they do not replace messages or choices",
        '"json" field containing a JSON-encoded string', "file_type, file_name, and base64 file_data",
        "Handle requests with or without files",
        "allow at most one tool call per model turn",
        "Do not assume harness-specific configuration or credentials are forwarded to the artifact",
        "APPSCI_AGENT_BASE_URL", "APPSCI_AGENT_API_KEY", "APPSCI_AGENT_CUSTOM_HEADERS",
        "Do not fetch external material", "Held-out evaluation data, private usersim files",
    ):
        assert instruction in text


@pytest.mark.parametrize("overrides", [
    {"command": "sh -c run"}, {"image": "--privileged"}, {"image": "bad image"},
    {"name": "../other"}, {"passthrough_env": ["A=B"]},
    {"passthrough_env": ["ORCHESTRATOR_SUBMIT_URL"]},
    {"passthrough_env": ["APPSCI_HARNESS_PROMPT"]},
])
def test_bad_launch_config_is_rejected(overrides):
    with pytest.raises(ValueError):
        HarnessConfig(**{**dict(name="custom", image="custom:latest"), **overrides})


def test_config_load_rejects_unknown_fields(tmp_path):
    path = tmp_path / "harness.yaml"
    path.write_text("name: custom\nimage: custom:latest\nmounts: [/repo]\n")
    with pytest.raises(ValueError, match="unknown"):
        HarnessConfig.load(path)


def test_cli_forwards_harness_and_controls(tmp_path, monkeypatch):
    from appsci_bench.orchestrator import run as run_mod

    path = tmp_path / "harness.yaml"
    path.write_text("name: custom\nimage: custom:latest\ncommand: [run-agent]\n")
    monkeypatch.setattr(sys, "argv", [
        "run", "--task", "order_to_cash", "--persona", "l3.yaml",
        "--harness-config", str(path), "--agent-model", "native-model",
        "--agent-temperature", "0", "--optimize-cycles", "0",
    ])
    run = AsyncMock(return_value=SimpleNamespace(
        task_id="order_to_cash", status="completed", scores={}, error=None,
    ))
    monkeypatch.setattr(run_mod.BenchmarkOrchestrator, "run", run)
    monkeypatch.setattr(run_mod, "_install_shutdown_handler", lambda: None)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(run_mod.main())
    assert exc.value.code == 0
    args = run.call_args.kwargs
    assert args["harness"] == HarnessConfig("custom", "custom:latest", ("run-agent",))
    assert args["agent_model"] == "native-model"
    assert args["agent_temperature"] == 0
    assert args["optimize_cycles"] == 0


@pytest.mark.parametrize("case", ["missing_file", "malformed", "missing_auth", "eval_only"])
def test_cli_rejects_invalid_harness_before_starting_any_services(tmp_path, monkeypatch, case):
    from appsci_bench.orchestrator import run as run_mod

    path = tmp_path / "harness.yaml"
    config = {"name": "custom", "image": "custom:latest"}
    if case == "missing_auth":
        config["passthrough_env"] = ["CUSTOM_AUTH"]
        monkeypatch.delenv("CUSTOM_AUTH", raising=False)
    if case != "missing_file":
        path.write_text("[broken" if case == "malformed" else yaml.safe_dump(config))
    argv = ["run", "--task", "order_to_cash", "--persona", "l3.yaml",
            "--harness-config", str(path)]
    if case == "eval_only":
        argv.extend(["--artifact-dir", str(tmp_path / "artifact")])
    monkeypatch.setattr(sys, "argv", argv)
    constructor = Mock(side_effect=AssertionError("preflight did not run"))
    monkeypatch.setattr(run_mod, "BenchmarkOrchestrator", constructor)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(run_mod.main())
    assert exc.value.code == 2
    constructor.assert_not_called()


def test_external_container_gets_bundle_and_native_command_without_reference_agent_auth(
    tmp_path, monkeypatch,
):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "APPSCI_AGENT_API_KEY",
                 "FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CUSTOM_AUTH", "a-secret")
    bundle = inputs(tmp_path)
    config = AppSciAgentContainerConfig(
        task_id="example", workspace_container="customer-workspace",
        appsci_usersim_url="http://customer:8000", appsci_usersim_token="customer-token",
        orchestrator_submit_url="http://host.docker.internal:9000/submit",
        spec_path=str(ROOT / "tasks/order_to_cash/spec.yaml"),
        host_artifact_dir=str(tmp_path / "artifact"),
        harness=HarnessConfig("custom", "custom:latest", ("run-agent", "--headless"), ("CUSTOM_AUTH",)),
        harness_inputs_dir=str(bundle),
    )
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    AppSciAgentContainerManager(config).start()
    cmd = calls[-1]
    assert cmd[-3:] == ["custom:latest", "run-agent", "--headless"]
    assert f"{bundle}:/benchmark:ro" in cmd
    assert not any("benchmark_tools.py" in arg or "APPSCI_HARNESS_TOOLS=" in arg for arg in cmd)
    assert "APPSCI_HARNESS_HTTP_CONTRACT=/benchmark/http_contract.md" in cmd
    assert "APPSCI_USERSIM_ASK_URL=http://customer:8000/ask" in cmd
    assert "ORCHESTRATOR_DEPLOY_URL=http://host.docker.internal:9000/deploy" in cmd
    assert "ORCHESTRATOR_DEPLOY_STATUS_URL=http://host.docker.internal:9000/deploy/status" in cmd
    assert "APPSCI_HARNESS_PROMPT=/benchmark/prompt.md" in cmd
    assert "CUSTOM_AUTH" in cmd and "a-secret" not in " ".join(cmd)
    assert "APPSCI_USERSIM_TOKEN=customer-token" in cmd
    assert not any("docker.sock" in arg or "user_sim_docs" in arg for arg in cmd)
    assert "appsci-bench-agent:latest" not in cmd


@pytest.mark.parametrize("skip_eval", [False, True])
@pytest.mark.parametrize("adapter", [None, "codex", "claude-code"])
@pytest.mark.parametrize("task_id", RUNTIME_TASK_IDS)
def test_generic_builder_reaches_the_existing_handover_and_evaluator(
    tmp_path, monkeypatch, skip_eval, task_id, adapter,
):
    from appsci_bench.eval.artifact_client import InProcessArtifactClient
    from appsci_bench.eval.datatypes import ArtifactAgentOutput, Objective
    from appsci_bench.eval.eval_system import run_evaluation
    from appsci_bench.orchestrator import run as run_mod
    from appsci_bench.orchestrator.submission_server import ArtifactSubmissionServer

    order = []
    docker_calls = []
    task_config = TaskConfig.from_spec(task_id, ROOT / "tasks")
    orchestrator = run_mod.BenchmarkOrchestrator(ROOT / "tasks", runs_dir=tmp_path / "runs")
    task_env = {"workspace_container": "customer-workspace", "network_name": "bench-network"}

    def start_env(task_id, phase):
        order.append(phase)
        return dict(task_env) if task_config.phase(phase) is not None else None

    monkeypatch.setenv("CUSTOM_HARNESS_TOKEN", "private-builder-token")
    for name in (*LLM_CREDENTIAL_ENV_VARS, *NATIVE_ENV):
        monkeypatch.delenv(name, raising=False)
    host_model_env = {"OPENAI_API_KEY": "private-host-key"}
    if adapter == "claude-code":
        host_model_env["ANTHROPIC_API_KEY"] = "private-anthropic-key"
    for name, value in host_model_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(run_mod, "llm_credential_env", lambda: dict(host_model_env))
    monkeypatch.setattr(run_mod, "_docker_env_value", lambda *args: "private-tool-token")
    resolve_env = run_mod.resolve_agent_env

    def task_env_with_model_override(config, **kwargs):
        # A task can override host model settings, or supply a key absent from the host.
        # The context must describe the effective deployment, not just host credentials.
        result = {**resolve_env(config, **kwargs), "APPSCI_AGENT_API_KEY": "private-task-model-key"}
        if adapter is None:
            # Native adapters reject task overrides of their own authentication variables.
            result["OPENAI_API_KEY"] = ""
        return result

    monkeypatch.setattr(run_mod, "resolve_agent_env", task_env_with_model_override)

    orchestrator.task_runner = Mock()
    orchestrator.task_runner.start.side_effect = start_env
    orchestrator.task_runner.teardown_descriptor.return_value = dict(task_env)
    server = ArtifactSubmissionServer()
    server.start = AsyncMock(return_value="http://orchestrator/submit")
    server.stop = AsyncMock()
    client = TestClient(server._create_app())

    def transport(request):
        if request.url.host == "customer":
            return httpx.Response(200, json={"message": "Return structured invoice decisions.", "turn": 1})
        if request.url.host == "artifact":
            return httpx.Response(405)
        result = client.request(request.method, request.url.path, content=request.content,
                                headers={"Content-Type": "application/json"})
        return httpx.Response(result.status_code, content=result.content)

    def builder(config):
        assert config.harness.name == "test_harness"
        assert config.harness_inputs_dir
        assert "evaluation_data" not in yaml.safe_load(Path(config.spec_path).read_text())
        context = json.loads((Path(config.harness_inputs_dir) / "context.json").read_text())
        assert context["task"] == yaml.safe_load(Path(config.spec_path).read_text())
        assert context["environment"]["workspace_container"] == config.workspace_container
        assert context["environment"]["task_env_vars"] == sorted(config.extra_env)
        expected_model_env = {"APPSCI_AGENT_API_KEY"}
        if adapter:
            expected_model_env.update(host_model_env)
        assert context["environment"]["model_env_vars"] == sorted(expected_model_env)
        assert context["environment"]["harness_env_vars"] == ["CUSTOM_HARNESS_TOKEN"]
        assert context["environment"]["artifact_env_vars"] == sorted({*server._deploy_context.env, "PORT"})
        assert context["artifact"]["port"] == server._deploy_context.port
        assert context["artifact"]["health_path"] == server._deploy_context.health_path
        assert context["artifact"]["endpoint_path"] == server._deploy_context.endpoint_path
        assert context["paths"]["artifact_output"] == config.artifact_output_dir
        assert context["paths"]["data"] == ("/data" if config.data_dir else None)
        assert context["paths"]["sop"] == ("/sop" if config.sop_dir else None)
        assert "private-" not in json.dumps(context)
        # Exercise the actual launch command too; only the Docker process is mocked.
        AppSciAgentContainerManager(config).start()
        launch = docker_calls[-1]
        supplied_env = {}
        for index, arg in enumerate(launch[:-1]):
            if arg == "-e":
                name, separator, value = launch[index + 1].partition("=")
                supplied_env[name] = value if separator else os.environ[name]
        assert supplied_env["APPSCI_HARNESS_CONTEXT"] == "/benchmark/context.json"
        assert f"{config.harness_inputs_dir}:/benchmark:ro" in launch
        assert f"{config.host_artifact_dir}:{context['paths']['artifact_output']}:rw" in launch
        for group in ("task_env_vars", "model_env_vars", "harness_env_vars"):
            assert set(context["environment"][group]) <= supplied_env.keys()
        for name, value in config.extra_env.items():
            assert supplied_env[name] == value
        for operation in context["http_api"]["operations"].values():
            assert supplied_env[operation["url_env"]]
            if "bearer_token_env" in operation:
                assert supplied_env[operation["bearer_token_env"]]
        assert supplied_env[context["http_api"]["deployment_status"]["url_env"]]
        for host, container in ((config.data_dir, "/data"), (config.sop_dir, "/sop")):
            if host:
                assert f"{Path(host).resolve()}:{container}:ro" in launch
        for entry in task_config.agent_env:
            if "{workspace_container}" in str(entry["value"]):
                assert config.workspace_container in config.extra_env[entry["name"]]
            if "{tool_token}" in str(entry["value"]):
                assert config.extra_env[entry["name"]] == "private-tool-token"
        if adapter:
            build_budgets = (context["task"].get("budgets") or {}).get("build") or {}
            ignored = {"temperature": 0, "max_turns": 12,
                       "max_tokens": build_budgets.get("tokens"), "max_usd": build_budgets.get("usd")}
            assert context["ignored_build_controls"] == {
                key: value for key, value in ignored.items() if value is not None
            }
            assert all(context["build_controls"][key] is None for key in ignored)
            assert config.temperature is None
            assert config.max_turns is None
            argv, _ = make_launch(adapter, config.harness.auth, context["build_controls"], supplied_env)
            assert argv[0] == ("codex" if adapter == "codex" else "claude")
            assert "--max-turns" not in argv
            assert "--max-budget-usd" not in argv
        agent = Mock()
        agent.get_logs.return_value = "Generic harness finished"
        agent.halt.side_effect = lambda: order.append("halt")
        if adapter:
            # Cleanup must retain the live log, not overwrite it with a second snapshot.
            agent.start_harness_log_capture.side_effect = lambda path: path.write_text(
                '2026-09-16T12:00:00Z {"type":"turn.started"}\n'
            )

        def build():
            order.append("builder")
            Path(config.host_artifact_dir, "Dockerfile").write_text("FROM scratch\n")
            # A harness uses its own HTTP code, not a benchmark client library.
            with httpx.Client(transport=httpx.MockTransport(transport)) as http:
                answer = http.post(f"{config.appsci_usersim_url}/ask", json={
                    "question": "What should the artifact return?",
                }, headers={"Authorization": f"Bearer {config.appsci_usersim_token}"})
                assert answer.status_code == 200
                deployed = http.post(config.orchestrator_submit_url.removesuffix("/submit") + "/deploy",
                                     json={}).json()
                assert deployed["success"] is True
                submitted = http.post(config.orchestrator_submit_url, json={
                    "artifact_endpoint": deployed["endpoint_url"],
                })
                assert submitted.status_code == 200
                assert submitted.json()["status"] == "accepted"

        agent.start.side_effect = build
        return agent

    def evaluate(spec, endpoint, environment, **kwargs):
        order.append("score")
        assert endpoint == "http://artifact/respond"
        assert order.index("halt") < order.index("eval") < order.index("score")
        # Exercise the real dataset/session/scoring pipeline after the generic submission.
        sample_spec = {"task_id": spec.task_id, "evaluation_data": {
            "type": "single_turn", "data": [{"data_point_id": "case",
                                               "single_turn_input": {"text": "invoice"}}],
        }}

        def score(**fields):
            return float(fields["conversation"][-1].response.text == "valid"), {}

        return run_evaluation(sample_spec, InProcessArtifactClient(
            lambda query: ArtifactAgentOutput(text="valid")), workdir=str(tmp_path / "eval"),
            objectives=[Objective("correct", "test.score", True, 1.0, eval_function=score)])

    monkeypatch.setattr(run_mod, "AppSciAgentContainerManager", builder)
    monkeypatch.setattr(run_mod, "ArtifactSubmissionServer", lambda **kw: server)
    monkeypatch.setattr(run_mod, "DockerAppsciAgentUserSimService", lambda **kw: Mock(
        url="http://customer", token="test-token"))
    monkeypatch.setattr(run_mod, "_translate_docker_url_to_host", lambda url: url)
    monkeypatch.setattr(run_mod, "evaluate_deployed", evaluate)
    def docker_run(cmd, **kwargs):
        docker_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", docker_run)
    monkeypatch.setattr("appsci_bench.orchestrator.submission_server.DockerArtifactDeployer.deploy",
                        lambda self: SimpleNamespace(success=True, endpoint_url="http://artifact/respond",
                                                     healthy=True, health_detail="ok",
                                                     container_name="artifact"))
    result = asyncio.run(orchestrator.run(
        task_id, ROOT / "tasks" / task_id / "user_personas/l3.yaml",
        harness=HarnessConfig("test_harness", "custom:latest", adapter=adapter,
                              auth="api" if adapter else None,
                              passthrough_env=("CUSTOM_HARNESS_TOKEN",)), skip_eval=skip_eval,
        agent_model="native-model" if adapter else None,
        agent_temperature=0, agent_max_turns=12,
    ))
    assert result.status == "completed", result.error
    assert result.metadata["harness"] == "test_harness"
    assert order[:3] == ["build", "builder", "halt"]
    if skip_eval:
        assert "eval" not in order
    else:
        assert result.scores["final_score"] == 1.0
    assert list((tmp_path / "runs").glob("*test_harness*/harness/launch.json"))
    native_logs = list((tmp_path / "runs").glob("*test_harness*/harness/native.log"))
    assert native_logs
    if adapter:
        assert native_logs[0].read_text().startswith('2026-09-16T12:00:00Z ')
    else:
        assert native_logs[0].read_text() == "Generic harness finished"
    assert list((tmp_path / "runs").glob("*/checkpoints_manifest.json"))
