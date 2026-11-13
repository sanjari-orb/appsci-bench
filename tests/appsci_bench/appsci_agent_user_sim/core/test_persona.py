"""Tests for the persona rendering module."""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from appsci_bench.appsci_agent_user_sim.core.persona import (
    REPO_ROOT,
    TEMPLATE_MAP,
    TEMPLATES_DIR,
    build_template_context,
    load_user_persona,
    render_persona_prompt,
    resolve_known_resources,
)
from appsci_bench.core.spec import TaskSpec


@dataclass
class MockTaskSpec:
    """Mock TaskSpec for testing."""
    task_id: str = "test_task"
    name: str = "Test Task"
    description: str = "A test task description"
    task_source_type: str = "internal"
    environment: dict = field(default_factory=dict)
    user_personas: list[str] = field(default_factory=list)
    evaluation_data: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    gold_solution: dict = field(default_factory=dict)
    owner: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    business_goals: list[str] = field(default_factory=list)
    dataset_files: list[dict] = field(default_factory=list)
    technical_docs: list[dict] = field(default_factory=list)
    interface_design: str = ""


class TestLoadUserPersona:
    """Tests for load_user_persona function."""

    def test_load_absolute_path(self, tmp_path: pathlib.Path) -> None:
        """Test loading persona from an absolute path."""
        persona_file = tmp_path / "test_persona.yaml"
        persona_file.write_text("""
persona_id: test_persona
task_id: test_task
level: L1
role: Applied-science manager
""")
        persona = load_user_persona(persona_file)
        assert persona["persona_id"] == "test_persona"
        assert persona["level"] == "L1"
        assert persona["role"] == "Applied-science manager"

    def test_load_relative_path(self) -> None:
        """Test loading persona from a relative path (resolved against REPO_ROOT)."""
        # Use a real, schema-valid committed persona (the _TEMPLATE example has literal
        # placeholders like `<snake_case>` and intentionally fails schema validation).
        relative_path = "tasks/order_to_cash/user_personas/l1.yaml"
        persona = load_user_persona(relative_path)
        assert "persona_id" in persona
        assert "level" in persona


class TestBuildTemplateContext:
    """Tests for build_template_context function."""

    def test_merges_task_spec_and_persona(self) -> None:
        """Test that context merges fields from both task_spec and persona."""
        task_spec = MockTaskSpec(
            description="Build an invoice checker",
            business_goals=["Reduce manual review", "Catch discrepancies"],
            dataset_files=[{"name": "invoices.csv", "description": "Invoice data"}],
            technical_docs=[{"name": "api.md", "description": "API docs"}],
            interface_design="REST API with JSON responses",
        )
        persona = {
            "level": "L3",
            "role": "AP director",
            "domain_knowledge": {"key": "value"},
        }

        context = build_template_context(task_spec, persona)

        # Check task_spec fields
        assert context["task_description"] == "Build an invoice checker"
        assert context["business_goals"] == ["Reduce manual review", "Catch discrepancies"]
        assert context["dataset_files"] == [{"name": "invoices.csv", "description": "Invoice data"}]
        assert context["technical_docs"] == [{"name": "api.md", "description": "API docs"}]
        assert context["interface_design"] == "REST API with JSON responses"

        # Check persona fields
        assert context["role"] == "AP director"
        assert context["domain_knowledge"] == {"key": "value"}

    def test_default_values_for_missing_persona_fields(self) -> None:
        """Test that missing persona fields get default values."""
        task_spec = MockTaskSpec()
        persona = {"level": "L1"}  # Minimal persona

        context = build_template_context(task_spec, persona)

        assert context["role"] == ""
        assert context["domain_knowledge"] == {}


class TestRenderPersonaPrompt:
    """Tests for render_persona_prompt function."""

    def test_templates_exist(self) -> None:
        """Verify all template files exist."""
        for level, template_name in TEMPLATE_MAP.items():
            template_path = TEMPLATES_DIR / template_name
            assert template_path.exists(), f"Template for {level} not found at {template_path}"

    def test_invalid_level_raises(self, tmp_path: pathlib.Path) -> None:
        """Test that an invalid persona level raises ValueError.

        Now caught by schema validation at load time (persona_path invalid), earlier than the
        previous "Unknown persona level" check in render_persona_prompt.
        """
        persona_file = tmp_path / "bad_persona.yaml"
        persona_file.write_text("""
persona_id: bad
task_id: test
level: L9
""")
        task_spec = MockTaskSpec()

        with pytest.raises(ValueError, match="'level'.*is not one of"):
            render_persona_prompt(task_spec, persona_file)

    def test_render_each_level_loads_its_template(self, tmp_path: pathlib.Path) -> None:
        """Each level renders its own template and includes the task context. (Parametrized below.)"""
        # Distinctive, stable marker per level's template (role title, not a fictional name).
        markers = {"L1": "Applied Science Manager", "L2": "Senior Data Engineer", "L3": "Product Manager"}
        for level, marker in markers.items():
            persona_file = tmp_path / f"{level}.yaml"
            persona_file.write_text(f"persona_id: p\ntask_id: test\nlevel: {level}\n")
            prompt = render_persona_prompt(MockTaskSpec(description="Test task ctx"), persona_file)
            assert marker in prompt, f"{level} did not render its template (missing {marker!r})"
            assert "Test task ctx" in prompt


class TestIntegration:
    """Integration tests using real task specs and personas."""

    def test_render_order_to_cash_personas(self) -> None:
        """All real order_to_cash personas render to a non-trivial prompt. (Contract specifics are
        covered per-level by TestBehavioralContract; this just exercises the real spec + persona files.)"""
        from appsci_bench.core.spec import load_task_spec

        spec = load_task_spec("order_to_cash")

        assert spec.user_personas, "order_to_cash should declare personas"
        for persona_path in spec.user_personas:
            prompt = render_persona_prompt(spec, persona_path)
            assert len(prompt) > 100, f"Prompt for {persona_path} is too short"


# The universal behavioral clauses that must appear in EVERY level's prompt.
_CONTRACT_MARKERS = [
    "Your Behavioral Contract",
    "You are NOT the applied scientist",          # hard role frame / anti-over-help
    "no fabrication",                              # closed-world / anti-fabrication
    "Two kinds of",                                # punt-vs-decline two-bucket refusal
    "no identity to share",                        # non-disclosable identity
    "Don't volunteer, don't lead",                 # don't-lead
    "busy colleague",                              # no-formatting-tells
    "Point them at what they have",                # build-from-provided-resources
    "do not assume they have everything you know",  # #76: some knowledge has no file to point at
]


def _render_level(level: str, tmp_path: pathlib.Path) -> str:
    persona_file = tmp_path / f"{level}.yaml"
    persona_file.write_text(f"persona_id: p\ntask_id: test\nlevel: {level}\n")
    return render_persona_prompt(MockTaskSpec(description="t"), persona_file)


class TestBehavioralContract:
    """The universal behavioral contract is present in every level and identical."""

    @pytest.mark.parametrize("level", ["L1", "L2", "L3"])
    def test_all_clauses_present(self, level: str, tmp_path: pathlib.Path) -> None:
        prompt = _render_level(level, tmp_path)
        for marker in _CONTRACT_MARKERS:
            assert marker in prompt, f"{level} prompt missing behavioral clause: {marker!r}"

    def test_contract_block_identical_across_levels(self, tmp_path: pathlib.Path) -> None:
        # The shared partial must render byte-identically in all three (single source of truth).
        def contract_slice(prompt: str) -> str:
            start = prompt.index("## Your Behavioral Contract")
            end = prompt.index("## Task Context", start)
            return prompt[start:end]

        blocks = {lvl: contract_slice(_render_level(lvl, tmp_path)) for lvl in ("L1", "L2", "L3")}
        assert blocks["L1"] == blocks["L2"] == blocks["L3"]

    @pytest.mark.parametrize("level", ["L1", "L2", "L3"])
    def test_message_contract_intact_no_should_stop(self, level: str, tmp_path: pathlib.Path) -> None:
        prompt = _render_level(level, tmp_path)
        assert '"message"' in prompt
        assert "should_stop" not in prompt


# A sample resources list for resolve_known_resources tests.
_RESOURCES = [
    {"name": "oltp_db", "type": "database", "location": "postgres://x", "description": "the DB"},
    {"name": "kpis", "type": "kpi", "location": "/data/kpis.md", "description": "metrics"},
]


class TestResolveKnownResources:
    """Tests for resolve_known_resources (deterministic, no LLM)."""

    def test_happy_path_resolves_in_order(self) -> None:
        spec = MockTaskSpec(environment={"resources": _RESOURCES})
        persona = {"known_resources": ["kpis", "oltp_db"]}

        resolved = resolve_known_resources(spec, persona)

        assert [r["name"] for r in resolved] == ["kpis", "oltp_db"]
        assert resolved[0]["location"] == "/data/kpis.md"

    def test_absent_known_resources_returns_empty(self) -> None:
        spec = MockTaskSpec(environment={"resources": _RESOURCES})
        assert resolve_known_resources(spec, {}) == []

    def test_empty_known_resources_returns_empty(self) -> None:
        spec = MockTaskSpec(environment={"resources": _RESOURCES})
        assert resolve_known_resources(spec, {"known_resources": []}) == []

    def test_no_resources_in_spec_returns_empty(self) -> None:
        spec = MockTaskSpec(environment={})
        assert resolve_known_resources(spec, {}) == []

    def test_unknown_name_raises(self) -> None:
        spec = MockTaskSpec(environment={"resources": _RESOURCES})
        persona = {"known_resources": ["does_not_exist"]}

        with pytest.raises(ValueError, match="unknown resource 'does_not_exist'"):
            resolve_known_resources(spec, persona)

    def test_duplicate_resource_name_raises(self) -> None:
        dupes = _RESOURCES + [{"name": "kpis", "type": "kpi", "location": "/other"}]
        spec = MockTaskSpec(environment={"resources": dupes})

        with pytest.raises(ValueError, match="Duplicate resource name in task spec: 'kpis'"):
            resolve_known_resources(spec, {"known_resources": ["kpis"]})

    def test_resource_missing_name_raises(self) -> None:
        spec = MockTaskSpec(environment={"resources": [{"type": "database", "location": "x"}]})

        with pytest.raises(ValueError, match="missing required 'name'"):
            resolve_known_resources(spec, {})


class TestKnownResourcesIsMetadataOnly:
    """known_resources must NEVER load file content (issue #76, D4).

    Content injection used to live here and was removed: it could not work in a containerized run (the
    user-sim image ships no `tasks/` tree, so a repo-relative `location` resolved to nothing and the
    load failed silently) while looking functional on a developer host -- which invited task owners to
    build policy elicitation on it. Sim-only content now comes only from `known_docs`
    (see test_known_docs.py). These tests exist so it cannot come back by accident.
    """

    def test_content_not_loaded_even_when_the_file_exists(self, tmp_path: pathlib.Path) -> None:
        """The core regression: a real, loadable, readable docs-type file must still yield metadata
        only. (This assertion is the inverse of the one it replaces.)"""
        doc_file = tmp_path / "doc.md"
        doc_file.write_text("# Documentation\nThis is the doc content.")
        resources = [
            {"name": "my_doc", "type": "docs", "location": str(doc_file), "description": "A doc"}
        ]
        spec = MockTaskSpec(environment={"resources": resources})

        resolved = resolve_known_resources(spec, {"known_resources": ["my_doc"]})

        assert len(resolved) == 1
        assert resolved[0]["name"] == "my_doc"
        assert "content" not in resolved[0]

    @pytest.mark.parametrize("rtype", ["docs", "kpi", "sample_data", "other", "database"])
    def test_no_resource_type_gets_content(self, tmp_path: pathlib.Path, rtype: str) -> None:
        """The old code keyed on type (everything except `database` was loaded). No type does now."""
        doc_file = tmp_path / "doc.md"
        doc_file.write_text("BODY")
        spec = MockTaskSpec(environment={"resources": [
            {"name": "r", "type": rtype, "location": str(doc_file), "description": "d"}
        ]})

        resolved = resolve_known_resources(spec, {"known_resources": ["r"]})

        assert "content" not in resolved[0]

    def test_metadata_fields_survive(self, tmp_path: pathlib.Path) -> None:
        """Removing content must not remove the location/description the sim legitimately needs to
        describe an agent-visible resource."""
        spec = MockTaskSpec(environment={"resources": [
            {"name": "oltp_db", "type": "database", "location": "postgres://host/db",
             "description": "The DB"}
        ]})

        resolved = resolve_known_resources(spec, {"known_resources": ["oltp_db"]})

        assert resolved[0] == {"name": "oltp_db", "type": "database",
                               "location": "postgres://host/db", "description": "The DB"}

    def test_missing_file_is_not_an_error_here(self) -> None:
        """A nonexistent `location` is fine for known_resources -- it is a container path the SIM
        cannot see and is not supposed to read. Missing-file errors belong to known_docs."""
        spec = MockTaskSpec(environment={"resources": [
            {"name": "missing", "type": "docs", "location": "/nonexistent/file.md",
             "description": "Missing"}
        ]})

        resolved = resolve_known_resources(spec, {"known_resources": ["missing"]})

        assert resolved[0]["name"] == "missing"
        assert "content" not in resolved[0]

    def test_original_resource_not_mutated(self) -> None:
        """resolve_known_resources copies, so a template cannot mutate the task's resource list."""
        original_resources = [
            {"name": "doc", "type": "docs", "location": "/nonexistent.md", "description": "Doc"}
        ]
        spec = MockTaskSpec(environment={"resources": original_resources})

        resolved = resolve_known_resources(spec, {"known_resources": ["doc"]})
        resolved[0]["injected"] = True

        assert "injected" not in original_resources[0]


class TestResourceContentNotRendered:
    """No resource may put file content into the rendered prompt (the render-level half of D4)."""

    def test_content_absent_from_prompt_even_when_file_exists(self, tmp_path: pathlib.Path) -> None:
        doc_file = tmp_path / "rules.md"
        doc_file.write_text("# Rules\n- Rule 1\n- Rule 2")

        persona_file = tmp_path / "l2.yaml"
        persona_file.write_text("""
persona_id: l2_test
task_id: test
level: L2
known_resources:
  - rules_doc
""")
        resources = [
            {"name": "rules_doc", "type": "docs", "location": str(doc_file), "description": "Rules"}
        ]
        spec = MockTaskSpec(description="t", environment={"resources": resources})

        prompt = render_persona_prompt(spec, persona_file)

        # The resource is still described...
        assert "rules_doc" in prompt
        # ...but its body never appears, and no content block is emitted.
        assert "# Rules" not in prompt
        assert "- Rule 1" not in prompt
        assert "<details>" not in prompt

    def test_database_shows_location(self, tmp_path: pathlib.Path) -> None:
        persona_file = tmp_path / "l2.yaml"
        persona_file.write_text("""
persona_id: l2_test
task_id: test
level: L2
known_resources:
  - oltp_db
""")
        resources = [
            {"name": "oltp_db", "type": "database", "location": "postgres://host/db", "description": "The DB"}
        ]
        spec = MockTaskSpec(description="t", environment={"resources": resources})

        prompt = render_persona_prompt(spec, persona_file)

        assert "oltp_db" in prompt
        assert "postgres://host/db" in prompt
        assert "<details>" not in prompt


class TestKnownResourcesRendering:
    """known_resources is rendered into the prompt when present, omitted when absent."""

    def test_block_rendered_when_present(self, tmp_path: pathlib.Path) -> None:
        persona_file = tmp_path / "l1.yaml"
        persona_file.write_text("""
persona_id: l1_test
task_id: test
level: L1
known_resources:
  - oltp_db
""")
        spec = MockTaskSpec(description="t", environment={"resources": _RESOURCES})

        prompt = render_persona_prompt(spec, persona_file)

        assert "Systems / Data You Know About" in prompt
        assert "oltp_db" in prompt
        assert "postgres://x" in prompt
        # Only the listed resource is rendered, not the unlisted one.
        assert "kpis" not in prompt

    def test_block_omitted_when_absent(self, tmp_path: pathlib.Path) -> None:
        persona_file = tmp_path / "l1.yaml"
        persona_file.write_text("""
persona_id: l1_test
task_id: test
level: L1
""")
        spec = MockTaskSpec(description="t", environment={"resources": _RESOURCES})

        prompt = render_persona_prompt(spec, persona_file)

        # The section HEADING, not the bare phrase: the behavioural contract names the section when it
        # enumerates what the persona knows, so a substring check would match that mention too.
        assert "### Systems / Data You Know About" not in prompt

    def test_the_block_says_the_persona_has_not_read_them(self, tmp_path: pathlib.Path) -> None:
        """Review on PR #80: the block used to open with "You can speak to these systems and data (what
        they are, where they live)", which reads as though the sim could open them. It cannot -- /data and
        /sop are mounted into the AGENT's container only, and `known_resources` is metadata. An LLM given
        the old wording can reasonably answer "the terms say X" from nothing at all, which is fabrication
        the closed-world contract is supposed to prevent.
        """
        persona_file = tmp_path / "l1.yaml"
        persona_file.write_text("""
persona_id: l1_test
task_id: test
level: L1
known_resources:
  - oltp_db
""")
        spec = MockTaskSpec(description="t", environment={"resources": _RESOURCES})

        prompt = render_persona_prompt(spec, persona_file)

        assert "have NOT read their" in prompt
        assert "cannot quote from them" in prompt
        assert "speak to these systems" not in prompt

    @pytest.mark.parametrize("level", ["L1", "L2", "L3"])
    def test_the_block_is_worded_identically_at_every_level(
        self, level: str, tmp_path: pathlib.Path
    ) -> None:
        """It is duplicated across the three templates rather than being a shared partial, so the wording
        can drift level by level. What the sim may and may not do with a resource is not level-dependent.
        """
        persona_file = tmp_path / f"{level}.yaml"
        persona_file.write_text(f"""
persona_id: p_test
task_id: test
level: {level}
known_resources:
  - oltp_db
""")
        spec = MockTaskSpec(description="t", environment={"resources": _RESOURCES})

        prompt = render_persona_prompt(spec, persona_file)

        expected = (
            "### Systems / Data You Know About\n"
            "You know these exist, what they are, and where the agent can find them. You have NOT read "
            "their\ncontents, so you cannot quote from them -- if they ask what one actually says, tell "
            "them to look."
        )
        assert expected in prompt


class TestDecoupling:
    """The appsci_agent_user_sim subpackage must not depend on the REST of appsci_bench.

    It lives under appsci_bench/ for org reasons, but stays shippable on its own: it may not import
    appsci_bench.core / .eval / .runner / etc. (Importing it naturally pulls in the bare `appsci_bench`
    namespace package + its own `appsci_bench.appsci_agent_user_sim.*` subtree -- those don't count.)

    ONE exception: `appsci_bench.common` (currently just `model_config.py`, the `ModelConfig`/
    `complete` LiteLLM transport). It's a zero-dependency leaf -- depends only on litellm, never on
    core/eval/appsci_agent_user_sim -- shared with the eval-side LLM-judge helpers so neither side has to
    own what both need. A standalone deployment of appsci_agent_user_sim just vendors that one small module
    alongside it, same as any third-party dependency.
    """

    def test_appsci_agent_user_sim_does_not_import_rest_of_appsci_bench(self) -> None:
        # Run in a SUBPROCESS so we observe a clean interpreter and never mutate this process's
        # sys.modules (mutating it here pollutes other tests' mock.patch targets).
        import subprocess
        import sys

        code = (
            "import importlib, sys\n"
            "importlib.import_module('appsci_bench.appsci_agent_user_sim.core')\n"
            "ALLOWED = ('appsci_bench.appsci_agent_user_sim', 'appsci_bench.common')\n"
            "bad = [n for n in sys.modules\n"
            "       if n.startswith('appsci_bench.') and not n.startswith(ALLOWED)]\n"
            "print(','.join(sorted(bad)))\n"
            "sys.exit(1 if bad else 0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"appsci_agent_user_sim must not import the rest of appsci_bench (except appsci_bench.common); "
            f"pulled in: {result.stdout.strip()}\nstderr: {result.stderr}"
        )


def _write_persona(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    """Write a persona YAML to a temp file and return its path."""
    f = tmp_path / "persona.yaml"
    f.write_text(body)
    return f


class TestAdversarialOverlay:
    """The opt-in adversarial overlay (safety dimension).

    A persona becomes adversarial by declaring `adversarial_pressures`. The renderer then layers
    _adversarial_contract.jinja2 on top of the L-level template WITHOUT dropping the base contract's
    no-fabrication rule. Absent/empty pressures => a normal persona (no overlay).
    """

    def test_no_pressures_omits_overlay(self, tmp_path: pathlib.Path) -> None:
        persona = _write_persona(tmp_path, """
persona_id: l3
task_id: t
level: L3
role: "PM"
""")
        prompt = render_persona_prompt(MockTaskSpec(), persona)
        assert "Adversarial Behavioral Overlay" not in prompt
        # base contract still present
        assert "never invent a fact" in prompt.lower() or "no fabrication" in prompt.lower()

    def test_pressures_add_overlay_and_keep_base_contract(self, tmp_path: pathlib.Path) -> None:
        persona = _write_persona(tmp_path, """
persona_id: l3_adversarial
task_id: t
level: L3
role: "PM"
adversarial_pressures:
  - pushback
  - bad_request
""")
        prompt = render_persona_prompt(MockTaskSpec(), persona)
        assert "Adversarial Behavioral Overlay" in prompt
        # the no-fabrication rule is NOT relaxed by the overlay
        assert "never invent a fact" in prompt.lower() or "no fabrication" in prompt.lower()

    def test_only_selected_pressure_blocks_render(self, tmp_path: pathlib.Path) -> None:
        persona = _write_persona(tmp_path, """
persona_id: l3_adversarial
task_id: t
level: L3
role: "PM"
adversarial_pressures:
  - pushback
""")
        prompt = render_persona_prompt(MockTaskSpec(), persona)
        assert "Unwarranted pushback" in prompt
        # blocks for unselected pressures must not render
        assert "Scope expansion" not in prompt
        assert "Time pressure" not in prompt

    def test_overlay_works_for_all_levels(self, tmp_path: pathlib.Path) -> None:
        for level in ("L1", "L2", "L3"):
            persona = _write_persona(tmp_path, f"""
persona_id: p
task_id: t
level: {level}
role: "x"
adversarial_pressures:
  - time_pressure
""")
            prompt = render_persona_prompt(MockTaskSpec(), persona)
            assert "Adversarial Behavioral Overlay" in prompt, f"{level} missing overlay"
            assert "Time pressure" in prompt, f"{level} missing selected block"
