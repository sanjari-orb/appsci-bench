"""Service startup and the docs mount (issue #76, C1).

build_session is where a bad `known_docs` declaration must stop the run: the container fails its health
check and the orchestrator fails THAT task. The alternative -- starting anyway -- produces a sim that
answers "I don't know" about its own policy, which reads in a trace exactly like an agent that never
asked. So these tests are mostly about refusing to start.
"""
from __future__ import annotations

import pathlib

import pytest

from appsci_bench.appsci_agent_user_sim.core.persona import UserSimDocsError
from appsci_bench.appsci_agent_user_sim.service.app import DEFAULT_DOCS_DIR, build_session

SPEC = """
task_id: demo
name: Demo
description: a demo task
business_goals: []
environment:
  resources: []
"""


@pytest.fixture
def env(tmp_path: pathlib.Path, monkeypatch):
    """Minimal startup env: token, spec, persona. Model calls never happen (no /ask here)."""
    spec = tmp_path / "spec.yaml"
    spec.write_text(SPEC)
    monkeypatch.setenv("APPSCI_USERSIM_TOKEN", "tok")
    monkeypatch.setenv("APPSCI_USERSIM_SPEC_PATH", str(spec))
    monkeypatch.setenv("MODEL_NAME", "claude-opus-4-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("APPSCI_USERSIM_DOCS_DIR", raising=False)
    return tmp_path, monkeypatch


def _persona(tmp_path: pathlib.Path, monkeypatch, body: str = "") -> pathlib.Path:
    p = tmp_path / "persona.yaml"
    p.write_text(f"persona_id: l1\ntask_id: demo\nlevel: L1\nrole: Manager\n{body}")
    monkeypatch.setenv("APPSCI_USERSIM_PERSONA_PATH", str(p))
    return p


def _docs(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    root = tmp_path / "user_sim_docs"
    root.mkdir()
    monkeypatch.setenv("APPSCI_USERSIM_DOCS_DIR", str(root))
    return root


class TestStartsSuccessfully:
    def test_declared_doc_reaches_the_system_prompt(self, env) -> None:
        tmp_path, monkeypatch = env
        root = _docs(tmp_path, monkeypatch)
        (root / "policy.md").write_text("Refunds within 24 hours only.")
        _persona(tmp_path, monkeypatch, "known_docs:\n  - policy.md\n")

        session = build_session()

        assert "Refunds within 24 hours only." in session._sim.system_prompt
        assert "policy.md" not in session._sim.system_prompt

    def test_starts_with_no_docs_dir_and_no_declarations(self, env) -> None:
        """Every task that has not adopted known_docs must still start."""
        tmp_path, monkeypatch = env
        _persona(tmp_path, monkeypatch)

        session = build_session()

        assert session is not None

    def test_starts_when_docs_dir_env_points_nowhere_and_nothing_declared(self, env) -> None:
        tmp_path, monkeypatch = env
        monkeypatch.setenv("APPSCI_USERSIM_DOCS_DIR", str(tmp_path / "absent"))
        _persona(tmp_path, monkeypatch)

        assert build_session() is not None


class TestRefusesToStart:
    def test_missing_declared_doc_raises_naming_it(self, env) -> None:
        tmp_path, monkeypatch = env
        _docs(tmp_path, monkeypatch)
        _persona(tmp_path, monkeypatch, "known_docs:\n  - policy.md\n")

        with pytest.raises(UserSimDocsError, match="policy.md"):
            build_session()

    def test_declared_docs_but_no_mount_raises(self, env) -> None:
        """The container case: the persona declares documents and the mount is simply absent."""
        tmp_path, monkeypatch = env
        monkeypatch.setenv("APPSCI_USERSIM_DOCS_DIR", str(tmp_path / "never_mounted"))
        _persona(tmp_path, monkeypatch, "known_docs:\n  - policy.md\n")

        with pytest.raises(UserSimDocsError, match="docs root"):
            build_session()

    def test_over_budget_declaration_raises(self, env) -> None:
        tmp_path, monkeypatch = env
        root = _docs(tmp_path, monkeypatch)
        for i in range(3):
            (root / f"d{i}.md").write_text("x" * 40_000)
        _persona(tmp_path, monkeypatch, "known_docs:\n  - d0.md\n  - d1.md\n  - d2.md\n")

        with pytest.raises(UserSimDocsError, match="hard cap"):
            build_session()


class TestDocsDirDefault:
    def test_defaults_to_the_container_mount_point(self, env, monkeypatch) -> None:
        """Unset env must mean the container's mount point, so the Dockerfile, the lifecycle manager,
        and the service cannot disagree about where the docs live."""
        assert DEFAULT_DOCS_DIR == "/user_sim_docs"

    def test_explicit_env_overrides_the_default(self, env) -> None:
        tmp_path, monkeypatch = env
        root = _docs(tmp_path, monkeypatch)
        (root / "policy.md").write_text("HOST PATH DOC")
        _persona(tmp_path, monkeypatch, "known_docs:\n  - policy.md\n")

        session = build_session()

        assert "HOST PATH DOC" in session._sim.system_prompt
