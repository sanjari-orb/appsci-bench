"""Installer/setup tests use fake executables; never npm install or log in for real."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = dict(line.split("=", 1) for line in (ROOT / "harnesses/versions.env").read_text().splitlines()
                if line and not line.startswith("#"))


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)


@pytest.fixture
def installation(tmp_path):
    repo = tmp_path / "repo"
    for name in ("scripts/install_harness_clis.sh", "harnesses/versions.env"):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    env = {"PATH": f"{fakebin}:/usr/bin:/bin", "APPSCI_CLI_PREFIX": str(repo / ".harness-clis"),
           "TEST_LOG": str(tmp_path / "calls"), "VIRTUAL_ENV": "test-venv", "APPSCI_SKIP_S3_SETUP": "1"}
    return repo, fakebin, env


def pinned_clis(bin_dir):
    executable(bin_dir / "codex", f'echo "codex-cli {VERSIONS["CODEX_VERSION"]}"')
    executable(bin_dir / "claude", f'echo "{VERSIONS["CLAUDE_CODE_VERSION"]} (Claude Code)"')


def run_install(installation, *args):
    repo, _, env = installation
    return subprocess.run(["/bin/bash", str(repo / "scripts/install_harness_clis.sh"), *args],
                          env=env, capture_output=True, text=True, timeout=10)


def test_matching_path_installations_do_not_need_node_npm_or_auth(installation):
    _, fakebin, env = installation
    pinned_clis(fakebin)
    executable(fakebin / "npm", 'echo npm-called >> "$TEST_LOG"; exit 99')
    for args in ((), ("--check",)):
        result = run_install(installation, *args)
        assert result.returncode == 0, result.stderr
    assert not Path(env["TEST_LOG"]).exists()


def test_missing_versions_fail_check_without_installing(installation):
    _, fakebin, env = installation
    executable(fakebin / "npm", 'echo npm-called >> "$TEST_LOG"; exit 99')
    result = run_install(installation, "--check")
    assert result.returncode == 1
    assert "Missing pinned CLIs" in result.stderr
    assert not Path(env["TEST_LOG"]).exists()


def test_old_node_is_rejected_before_npm(installation):
    _, fakebin, env = installation
    executable(fakebin / "node", "exit 1")
    executable(fakebin / "npm", 'echo npm-called >> "$TEST_LOG"; exit 99')
    result = run_install(installation)
    assert result.returncode == 1
    assert "Node.js 22+" in result.stderr
    assert not Path(env["TEST_LOG"]).exists()


def test_install_uses_pins_local_prefix_and_required_optional_dependencies(installation):
    repo, fakebin, env = installation
    executable(fakebin / "node", "exit 0")
    staged = repo / "staged"
    pinned_clis(staged)
    executable(fakebin / "npm", f'''
printf '%s\\n' "$@" > "$TEST_LOG"
mkdir -p "$APPSCI_CLI_PREFIX/bin"
cp "{staged}/codex" "{staged}/claude" "$APPSCI_CLI_PREFIX/bin/"
''')
    result = run_install(installation)
    assert result.returncode == 0, result.stderr
    args = Path(env["TEST_LOG"]).read_text().splitlines()
    assert f'@openai/codex@{VERSIONS["CODEX_VERSION"]}' in args
    assert f'@anthropic-ai/claude-code@{VERSIONS["CLAUDE_CODE_VERSION"]}' in args
    assert args[args.index("--prefix") + 1] == env["APPSCI_CLI_PREFIX"]
    assert "--include=optional" in args and "--ignore-scripts=false" in args
    assert "login" not in args and "sudo" not in args


def test_npm_failure_propagates_and_does_not_report_ready(installation):
    _, fakebin, _ = installation
    executable(fakebin / "node", "exit 0")
    executable(fakebin / "npm", "exit 23")
    result = run_install(installation)
    assert result.returncode == 23
    assert "ready" not in result.stdout


def test_nonmatching_installed_version_does_not_pass_check(installation):
    _, fakebin, _ = installation
    pinned_clis(fakebin)
    executable(fakebin / "codex", 'echo "codex-cli 0.0.1"')
    result = run_install(installation, "--check")
    assert result.returncode == 1
    assert f'@openai/codex@{VERSIONS["CODEX_VERSION"]}' in result.stderr




def test_image_is_non_root_and_does_not_copy_benchmark_or_ground_truth():
    dockerfile = (ROOT / "harnesses/Dockerfile").read_text()
    assert "USER builder" in dockerfile
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY")]
    assert len(copies) == 3
    assert all("tasks/" not in line and "COPY . " not in line for line in copies)
    assert any("native_harness.py" in line for line in copies)
    assert "node:22-" in dockerfile
