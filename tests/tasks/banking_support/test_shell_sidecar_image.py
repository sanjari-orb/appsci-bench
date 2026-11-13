"""The sidecar image's contract: it must not contain what the shell is confined to withhold.

This is a STATIC guard on the Dockerfile and compose service, not a container test. The real
confinement was verified by running the image; what rots over time is the build recipe -- someone adds
a COPY to fix an import, or relaxes a flag to debug something, and the shell quietly gains the ability
to read the policy this task expects the agent to elicit. These assertions fail loudly when that
happens, in a suite that needs no Docker.

The material at stake is environment/tools.py: it encodes the identity-verification rule, the
unlock-then-call protocol, the discoverable-tool catalogue and the escalation rules. It reaches the
agent by elicitation or not at all.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

_ENV = pathlib.Path(__file__).resolve().parents[3] / "tasks" / "banking_support" / "environment"
_DOCKERFILE = _ENV / "shell_sidecar" / "Dockerfile"
_COMPOSE = _ENV / "docker-compose.yml"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return _DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def copy_directives(dockerfile: str) -> list[str]:
    """Every COPY/ADD line, comments stripped. The prose in this Dockerfile names the sensitive files
    on purpose (to explain why they are excluded), so scanning raw text would flag its own rationale."""
    return [ln.strip() for ln in dockerfile.splitlines()
            if ln.strip().startswith(("COPY", "ADD"))]


@pytest.fixture(scope="module")
def runtime_stage(dockerfile: str) -> str:
    """Only the FINAL stage. The first stage legitimately holds the corpus and the exporter and is
    discarded, so asserting against the whole file would forbid the build from working at all."""
    stages = dockerfile.split("\nFROM ")
    return stages[-1]


def test_the_runtime_stage_copies_no_task_python_package(runtime_stage: str) -> None:
    copies = [ln for ln in runtime_stage.splitlines()
              if ln.strip().startswith("COPY") and "--from=" not in ln]
    # The executor is the one file allowed in, and it is stdlib-only by design.
    assert len(copies) == 1, f"unexpected COPY into the runtime stage: {copies}"
    assert "shell_sidecar/server.py" in copies[0]


@pytest.mark.parametrize("forbidden", [
    "environment/tools.py", "environment/data_model.py", "environment/db_query.py",
    "environment/service.py", "environment/corpus.py", "environment/setup",
    "environment/eval", "environment/build", "appsci_bench", "provenance", "gold/",
])
def test_nothing_sensitive_is_copied_into_the_sidecar_at_all(
    copy_directives: list[str], forbidden: str,
) -> None:
    # Checked across BOTH stages: a first-stage copy would leak through any later `COPY --from`.
    offenders = [ln for ln in copy_directives if forbidden in ln]
    assert offenders == [], f"sidecar image must never receive {forbidden}: {offenders}"


def test_the_exporter_is_the_only_runtime_module_the_build_stage_takes(
    copy_directives: list[str],
) -> None:
    # kb_export is deliberately a stdlib-only leaf: copying the whole _runtime package would drag
    # tools.py in behind it via the import chain.
    assert any("_runtime/kb_export.py" in ln for ln in copy_directives)
    runtime_copies = [ln for ln in copy_directives if "_runtime" in ln]
    assert all("kb_export.py" in ln for ln in runtime_copies), runtime_copies


def test_the_image_runs_unprivileged(dockerfile: str) -> None:
    assert "USER 65534:65534" in dockerfile


# --- the compose service is where Docker does the confining ------------------------------------

@pytest.fixture(scope="module")
def shell_service() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())["services"]["shell"]


def test_every_confinement_flag_is_declared(shell_service: dict) -> None:
    assert shell_service["user"] == "65534:65534"
    assert shell_service["read_only"] is True
    assert shell_service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in shell_service["security_opt"]
    # Bounds so one pathological command cannot take a 97-case run down with it.
    assert shell_service["pids_limit"] > 0
    assert shell_service["mem_limit"]


def test_the_sidecar_mounts_no_volumes(shell_service: dict) -> None:
    # The corpus is baked in, so there is no host path that could be widened by accident.
    assert "volumes" not in shell_service


def test_the_only_writable_path_is_a_capped_in_memory_tmpfs(shell_service: dict) -> None:
    tmpfs = shell_service["tmpfs"]
    assert any(t.startswith("/tmp:") and "noexec" in t and "size=" in t for t in tmpfs), tmpfs


def test_the_sidecar_is_unreachable_from_the_agent(shell_service: dict) -> None:
    # bench-network also carries the appsci-agent container. A sidecar attached to it would be
    # addressable by the artifact directly, bypassing the tool API's gating and accounting.
    nets = shell_service["networks"]
    assert "bench-network" not in nets
    assert nets == ["shell-network"]


def test_the_shell_network_is_internal() -> None:
    compose = yaml.safe_load(_COMPOSE.read_text())
    assert compose["networks"]["shell-network"]["internal"] is True


def test_the_workspace_waits_for_the_sidecar_to_be_healthy() -> None:
    # Otherwise an early shell call races the sidecar's boot and reads as a backend failure.
    workspace = yaml.safe_load(_COMPOSE.read_text())["services"]["workspace"]
    assert workspace["depends_on"]["shell"]["condition"] == "service_healthy"


def test_the_agent_is_never_handed_the_shell_token() -> None:
    spec = yaml.safe_load((_ENV.parent / "spec.yaml").read_text())
    names = {e["name"] for e in spec["task_runner"]["agent_env"]}
    assert "BANKING_SHELL_TOKEN" not in names and "BANKING_SHELL_URL" not in names
