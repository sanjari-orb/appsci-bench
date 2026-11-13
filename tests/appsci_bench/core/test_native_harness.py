"""No paid model calls, Docker daemon, real credentials or browser login needed."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest

from appsci_bench.core.harness import HarnessConfig
from appsci_bench.core.native_harness import build_controls, effective_controls, make_launch

ROOT = Path(__file__).resolve().parents[3]
KEYS = {"OPENAI_API_KEY": "openai-test-secret", "ANTHROPIC_API_KEY": "anthropic-test-secret"}
GATEWAY = {
    "ANTHROPIC_AUTH_TOKEN": "gateway-test-secret",
    "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
    "APPSCI_CODEX_RESPONSES_BASE_URL": "https://gateway.example/v1",
    "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway-User: private@example.test client/1\nX-Trace: value:with:colons",
}


def controls(**changes):
    return {"model": "native-model-id", "temperature": None, "max_turns": None,
            "max_tokens": None, "max_usd": None, **changes}


@pytest.mark.parametrize("adapter", ["codex", "claude-code"])
@pytest.mark.parametrize("auth", ["api", "gateway"])
def test_authentication_is_explicit_and_credentials_stay_out_of_argv(adapter, auth):
    original = {**KEYS, **GATEWAY, "APPSCI_USERSIM_TOKEN": "customer-secret"}
    argv, env = make_launch(adapter, auth, controls(), original)
    assert original == {**KEYS, **GATEWAY, "APPSCI_USERSIM_TOKEN": "customer-secret"}
    for secret in (*KEYS.values(), GATEWAY["ANTHROPIC_AUTH_TOKEN"], "private@example.test"):
        assert secret not in " ".join(argv)
    assert env["APPSCI_USERSIM_TOKEN"] == "customer-secret"
    assert "native-model-id" in argv
    assert "login" not in argv
    if adapter == "codex":
        assert env["APPSCI_NATIVE_API_KEY"] == (KEYS["OPENAI_API_KEY"] if auth == "api"
                                                else GATEWAY["ANTHROPIC_AUTH_TOKEN"])
        config = tomllib.loads("\n".join(argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"))
        assert config["model_providers"]["appsci"]["wire_api"] == "responses"
        assert config["model_providers"]["appsci"]["requires_openai_auth"] is False
        assert config["shell_environment_policy"]["ignore_default_excludes"] is True
        if auth == "gateway":
            mapping = config["model_providers"]["appsci"]["env_http_headers"]
            assert env[mapping["X-Gateway-User"]] == "private@example.test client/1"
            assert env[mapping["X-Trace"]] == "value:with:colons"
    elif auth == "api":
        assert env["ANTHROPIC_API_KEY"] == KEYS["ANTHROPIC_API_KEY"]
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_CUSTOM_HEADERS" not in env
    else:
        assert "ANTHROPIC_API_KEY" not in env
        assert env["ANTHROPIC_AUTH_TOKEN"] == GATEWAY["ANTHROPIC_AUTH_TOKEN"]
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == GATEWAY["ANTHROPIC_CUSTOM_HEADERS"]
        assert "--bare" not in argv  # bare mode ignores the gateway bearer token


@pytest.mark.parametrize(("adapter", "auth", "env", "message"), [
    ("codex", "api", {}, "OPENAI_API_KEY"),
    ("codex", "api", {**KEYS, "OPENAI_BASE_URL": "https://gateway.example/v1"}, "first-party"),
    ("codex", "api", {"OPENAI_API_KEY": "same", "ANTHROPIC_AUTH_TOKEN": "same"}, "first-party"),
    ("codex", "gateway", {"ANTHROPIC_AUTH_TOKEN": "fake"}, "APPSCI_CODEX_RESPONSES_BASE_URL"),
    ("claude-code", "api", {}, "ANTHROPIC_API_KEY"),
    ("claude-code", "api", {"ANTHROPIC_API_KEY": "same", "ANTHROPIC_AUTH_TOKEN": "same"}, "gateway token"),
    ("claude-code", "gateway", {"ANTHROPIC_BASE_URL": "https://gateway.example"}, "ANTHROPIC_AUTH_TOKEN"),
])
def test_missing_or_misrouted_credentials_fail(adapter, auth, env, message):
    with pytest.raises(ValueError, match=message):
        make_launch(adapter, auth, controls(), env)


@pytest.mark.parametrize("url", ["https://user:secret@host/v1", "https://host?token=secret",
                               "http://gateway.example", "not-a-url", "https://host/#token"])
def test_gateway_urls_cannot_embed_credentials_or_send_them_over_plaintext(url):
    with pytest.raises(ValueError):
        make_launch("codex", "gateway", controls(), {**GATEWAY, "APPSCI_CODEX_RESPONSES_BASE_URL": url})


@pytest.mark.parametrize("header", ["bad", "Authorization: secret", "Host: other", "X: one\nx: two"])
def test_invalid_or_routing_headers_fail_without_echoing_secret_values(header):
    with pytest.raises(ValueError) as exc:
        make_launch("codex", "gateway", controls(), {**GATEWAY, "ANTHROPIC_CUSTOM_HEADERS": header})
    assert header not in str(exc.value)


@pytest.mark.parametrize("adapter", ["codex", "claude-code"])
@pytest.mark.parametrize("auth", ["api", "gateway"])
def test_native_launch_ignores_all_reference_agent_controls(adapter, auth):
    requested = controls(temperature=0, max_tokens=2000000, max_turns=40, max_usd=20)
    original = dict(requested)
    assert make_launch(adapter, auth, requested, {**KEYS, **GATEWAY}) == make_launch(
        adapter, auth, controls(), {**KEYS, **GATEWAY}
    )
    assert requested == original


def test_effective_controls_preserve_model_timeout_and_workflow_and_record_ignored_values():
    requested = controls(temperature=0, max_tokens=2000000, max_turns=0, max_usd=20,
                         wall_clock_s=123, optimization_enabled=False, optimize_cycles=None)
    effective, ignored = effective_controls(requested)
    assert ignored == {"temperature": 0, "max_tokens": 2000000, "max_turns": 0, "max_usd": 20}
    assert all(effective[key] is None for key in ignored)
    for key in ("model", "wall_clock_s", "optimization_enabled", "optimize_cycles"):
        assert effective[key] == requested[key]
    assert requested["max_turns"] == 0
    assert effective_controls(effective) == (effective, {})


def test_claude_ignores_budget_flags_and_cloud_routing_is_explicit():
    argv, env = make_launch("claude-code", "api", controls(max_turns=12, max_usd=3.5), {
        **KEYS, "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test",
    })
    assert "--max-turns" not in argv
    assert "--max-budget-usd" not in argv
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["DISABLE_AUTOUPDATER"] == "1"


@pytest.mark.parametrize("adapter", ["codex", "claude-code"])
@pytest.mark.parametrize("model", [None, "", " ", 123])
def test_invalid_model(adapter, model):
    with pytest.raises(ValueError):
        make_launch(adapter, "api", controls(model=model), KEYS)


def test_task_budget_defaults_and_explicit_zero_are_preserved():
    result = build_controls({"build": {"turns": 40, "tokens": 1000, "usd": 20}},
                            model="model", max_turns=0, temperature=0)
    assert result == {"model": "model", "max_turns": 0, "temperature": 0,
                      "max_tokens": 1000, "max_usd": 20}


@pytest.mark.parametrize("name", ["codex", "codex-gateway", "claude-code", "claude-code-gateway"])
def test_shipped_configs_load(name):
    config = HarnessConfig.load(ROOT / "harnesses" / f"{name}.yaml")
    assert config.adapter and config.auth
    make_launch(config.adapter, config.auth, controls(), {**KEYS, **GATEWAY})


@pytest.mark.parametrize("fields", [dict(auth="api"), dict(adapter="unknown", auth="api"),
                                   dict(adapter=[], auth="api"), dict(adapter="codex", auth=[]),
                                   dict(adapter="codex"), dict(adapter="codex", auth="api", command=["sh"])])
def test_invalid_adapter_configs(fields):
    with pytest.raises(ValueError):
        HarnessConfig("example", "image", **fields)


@pytest.mark.parametrize("adapter", ["codex", "claude-code"])
@pytest.mark.parametrize("effective_bundle", [False, True])
def test_real_entrypoint_delivers_prompt_on_stdin_and_propagates_exit(tmp_path, adapter, effective_bundle):
    # Exercise main + exec in a child process with a fake native CLI, not a paid model.
    cli = tmp_path / ("codex" if adapter == "codex" else "claude")
    cli.write_text(f"#!{sys.executable}\nimport sys\nprint(sys.stdin.read())\nsys.exit(17)\n")
    cli.chmod(0o755)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("complete benchmark prompt\nincluding HTTP contract and task setup\n")
    context = tmp_path / "context.json"
    requested = controls(temperature=0, max_tokens=2000000, max_turns=40, max_usd=20)
    effective, ignored = effective_controls(requested)
    context.write_text(json.dumps({"version": 1,
                                   "build_controls": effective if effective_bundle else requested,
                                   "ignored_build_controls": ignored if effective_bundle else {},
                                   "paths": {"workspace": str(tmp_path)}}))
    env = {"PATH": str(tmp_path), **KEYS,
           "APPSCI_HARNESS_ADAPTER": adapter, "APPSCI_HARNESS_AUTH": "api",
           "APPSCI_HARNESS_CONTEXT": str(context), "APPSCI_HARNESS_PROMPT": str(prompt)}
    # Only the container/non-root guard is faked; input reading, exec and exit are real.
    program = (
        "from unittest.mock import patch\n"
        "from appsci_bench.core.native_harness import main\n"
        "with patch('pathlib.Path.exists', return_value=True), patch('os.getuid', return_value=1001):\n"
        "    raise SystemExit(main())\n"
    )
    result = subprocess.run([sys.executable, "-c", program], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 17, result.stderr
    assert result.stdout.strip() == prompt.read_text().strip()
    assert "ignores reference-agent controls" in result.stderr
    for setting in ignored:
        assert setting in result.stderr
    assert all(secret not in result.stdout + result.stderr for secret in KEYS.values())


def test_entrypoint_refuses_host_execution(monkeypatch, capsys):
    from appsci_bench.core import native_harness

    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert native_harness.main() == 2
    assert "inside the benchmark Docker image" in capsys.readouterr().err


@pytest.mark.parametrize("adapter,key", [("codex", "OPENAI_API_KEY"),
                                         ("claude-code", "ANTHROPIC_API_KEY")])
def test_native_auth_preflight_still_fails_before_task_environment_starts(tmp_path, monkeypatch, adapter, key):
    from appsci_bench.orchestrator.run import BenchmarkOrchestrator

    monkeypatch.delenv(key, raising=False)
    orchestrator = BenchmarkOrchestrator(ROOT / "tasks", runs_dir=tmp_path)
    orchestrator.task_runner = Mock()
    with pytest.raises(ValueError, match=key):
        asyncio.run(orchestrator.run(
            "order_to_cash", ROOT / "tasks/order_to_cash/user_personas/l3.yaml",
            agent_model="model", agent_temperature=0, agent_max_turns=12,
            harness=HarnessConfig.load(ROOT / "harnesses" / f"{adapter}.yaml"),
        ))
    orchestrator.task_runner.start.assert_not_called()


@pytest.mark.parametrize("name", ["codex", "codex-gateway", "claude-code", "claude-code-gateway"])
def test_container_forwards_native_auth_by_name_and_uses_existing_endpoints(tmp_path, monkeypatch, name):
    from appsci_bench.core.agent_container import AppSciAgentContainerConfig, AppSciAgentContainerManager

    bundle = tmp_path / "inputs"
    bundle.mkdir()
    for filename in ("context.json", "prompt.md", "http_contract.md"):
        (bundle / filename).write_text("test input")
    for variable, value in {**KEYS, **GATEWAY}.items():
        monkeypatch.setenv(variable, value)
    harness = HarnessConfig.load(ROOT / "harnesses" / f"{name}.yaml")
    manager = AppSciAgentContainerManager(AppSciAgentContainerConfig(
        task_id="example", workspace_container="task-environment",
        appsci_usersim_url="http://usersim", appsci_usersim_token="customer-token",
        orchestrator_submit_url="http://orchestrator/submit",
        spec_path=str(ROOT / "tasks/airline_rebooking/spec.yaml"),
        harness=harness, harness_inputs_dir=str(bundle),
    ))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    manager.start()
    argv = next(call for call in calls if call[:3] == ["docker", "run", "-d"])
    assert argv[-1] == harness.image
    assert f"APPSCI_HARNESS_ADAPTER={harness.adapter}" in argv
    assert f"APPSCI_HARNESS_AUTH={harness.auth}" in argv
    assert "ANTHROPIC_CUSTOM_HEADERS" in argv
    assert "APPSCI_CODEX_RESPONSES_BASE_URL" in argv
    assert "ORCHESTRATOR_DEPLOY_URL=http://orchestrator/deploy" in argv
    assert "ORCHESTRATOR_SUBMIT_URL=http://orchestrator/submit" in argv
    assert "APPSCI_USERSIM_ASK_URL=http://usersim/ask" in argv
    assert not any(secret in " ".join(argv) for secret in (*KEYS.values(), GATEWAY["ANTHROPIC_AUTH_TOKEN"]))
    assert "docker.sock" not in " ".join(argv)
    assert "max-size=50m" in argv
