"""Validation of sim-only document declarations (issue #76, phase D).

Because no real task has adopted `known_docs` yet, the repo-wide checks pass vacantly -- green here
would prove nothing on its own. So every guard is exercised against a SYNTHETIC task built in tmp_path,
with the leak deliberately introduced, to show the guard actually fires.

The errors-vs-warnings split is the load-bearing behavior: an error must stop the task (a sim that
silently lacks its policy looks exactly like an agent that never asked), a warning must not.
"""
from __future__ import annotations

import pathlib

import pytest

from appsci_bench.core.usersim_docs import (
    DOCS_DIRNAME,
    usersim_docs_dir,
    validate_many,
    validate_usersim_docs,
)

SPEC = """
task_id: demo
name: Demo
description: a demo task
business_goals: ["ship it"]
environment:
  resources:
    - {name: oltp_db, type: database, location: "postgres://db/x", description: "the DB"}
artifacts:
  output_dir: /work/out
"""


def _task(tmp_path: pathlib.Path, *, spec: str = SPEC) -> pathlib.Path:
    """A minimal task tree: tasks/demo/{spec.yaml,user_personas/}."""
    task = tmp_path / "demo"
    (task / "user_personas").mkdir(parents=True)
    (task / "spec.yaml").write_text(spec)
    return task


def _persona(task: pathlib.Path, name: str = "l1", body: str = "") -> pathlib.Path:
    p = task / "user_personas" / f"{name}.yaml"
    p.write_text(f"persona_id: {name}\ntask_id: demo\nlevel: L1\nrole: Manager\n{body}")
    return p


def _docs(task: pathlib.Path) -> pathlib.Path:
    d = task / DOCS_DIRNAME
    d.mkdir(exist_ok=True)
    return d


class TestCleanCases:
    def test_no_docs_tree_no_declarations_is_clean(self, tmp_path) -> None:
        """Every task in the repo today. Must never be flagged."""
        task = _task(tmp_path)
        _persona(task)

        result = validate_usersim_docs("demo", tmp_path)

        assert result.ok and not result.warnings, (result.errors, result.warnings)

    def test_declared_and_present_is_clean(self, tmp_path) -> None:
        task = _task(tmp_path)
        (_docs(task) / "policy.md").write_text("Refunds within 24h.")
        _persona(task, body="known_docs:\n  - policy.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert result.ok and not result.warnings, (result.errors, result.warnings)

    def test_common_only_is_clean(self, tmp_path) -> None:
        task = _task(tmp_path)
        common = _docs(task) / "common"
        common.mkdir()
        (common / "tools.json").write_text("{}")
        _persona(task)

        assert validate_usersim_docs("demo", tmp_path).ok

    def test_no_such_task_is_an_error_not_a_crash(self, tmp_path) -> None:
        result = validate_usersim_docs("nope", tmp_path)

        assert not result.ok
        assert "no such task" in result.errors[0]


class TestErrorsBlockTheRun:
    def test_missing_declared_doc(self, tmp_path) -> None:
        task = _task(tmp_path)
        _docs(task)
        _persona(task, body="known_docs:\n  - policy.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert "demo/l1" in result.errors[0] and "policy.md" in result.errors[0]

    def test_declared_doc_with_no_docs_tree_at_all(self, tmp_path) -> None:
        task = _task(tmp_path)
        _persona(task, body="known_docs:\n  - policy.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert "docs root" in result.errors[0]

    def test_over_hard_budget(self, tmp_path) -> None:
        task = _task(tmp_path)
        docs = _docs(task)
        for i in range(3):
            (docs / f"d{i}.md").write_text("x" * 40_000)
        _persona(task, body="known_docs:\n  - d0.md\n  - d1.md\n  - d2.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert "hard cap" in result.errors[0]

    def test_unsupported_extension(self, tmp_path) -> None:
        task = _task(tmp_path)
        (_docs(task) / "sheet.csv").write_text("a,b")
        _persona(task, body="known_docs:\n  - sheet.csv\n")

        assert not validate_usersim_docs("demo", tmp_path).ok

    def test_every_bad_persona_is_reported_not_just_the_first(self, tmp_path) -> None:
        """Fixing one error per re-run is miserable; the caller logs them all."""
        task = _task(tmp_path)
        _docs(task)
        _persona(task, "l1", body="known_docs:\n  - a.md\n")
        _persona(task, "l2", body="known_docs:\n  - b.md\n")
        _persona(task, "l3", body="known_docs:\n  - c.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert len(result.errors) == 3
        assert {"demo/l1", "demo/l2", "demo/l3"} == {e.split(":")[0] for e in result.errors}

    def test_adversarial_variants_are_checked(self, tmp_path) -> None:
        """They are bound BY PATH and never appear in spec.user_personas, so a spec-driven list would
        miss exactly the personas most likely to drift."""
        task = _task(tmp_path)
        _docs(task)
        _persona(task, "l1_adversarial", body="known_docs:\n  - missing.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert "l1_adversarial" in result.errors[0]

    def test_unreadable_persona_is_reported(self, tmp_path) -> None:
        task = _task(tmp_path)
        (task / "user_personas" / "broken.yaml").write_text("key: [unclosed\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert "broken" in result.errors[0]


class TestLeakGuards:
    def test_docs_tree_symlinked_into_an_agent_mounted_subtree(self, tmp_path) -> None:
        """The reachable version of "the agent can read these": symlink user_sim_docs/ at
        environment/sop/, which the agent bind-mounts. The checker resolves before comparing, so it
        catches this even though the directory name is at the canonical location.

        The reverse direction is NOT a hazard and is deliberately unchecked: a symlink INSIDE
        environment/sop pointing out to user_sim_docs is not followed by a Docker bind-mount (see
        tasks/airline_rebooking/KNOWN_ISSUES.md, which is why the airline docs are copied, not linked).
        """
        task = _task(tmp_path)
        real = task / "environment" / "sop"
        real.mkdir(parents=True)
        (real / "policy.md").write_text("RULES")
        (task / DOCS_DIRNAME).symlink_to(real, target_is_directory=True)
        _persona(task, body="known_docs:\n  - policy.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert any("bind-mounted to the agent" in e for e in result.errors), result.errors

    def test_filename_named_in_agent_facing_spec(self, tmp_path) -> None:
        """The airline case: the spec advertises policy.md while no such file is readable, so the agent
        hunts for a file that does not exist and then falls back to memory."""
        leaky = SPEC + """
technical_docs:
  - {name: "policy.md", description: "the policy"}
"""
        task = _task(tmp_path, spec=leaky)
        (_docs(task) / "policy.md").write_text("RULES")
        _persona(task, body="known_docs:\n  - policy.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert any("AGENT-FACING spec" in e for e in result.errors)

    def test_filename_named_in_resources_md(self, tmp_path) -> None:
        task = _task(tmp_path)
        (task / "RESOURCES.md").write_text("| Policy | docs | `/sop/policy.md` | the rules |")
        (_docs(task) / "policy.md").write_text("RULES")
        _persona(task, body="known_docs:\n  - policy.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert any("RESOURCES.md" in e for e in result.errors)

    def test_filename_named_in_an_agent_mounted_file(self, tmp_path) -> None:
        task = _task(tmp_path)
        sop = task / "environment" / "sop"
        sop.mkdir(parents=True)
        (sop / "kpis.md").write_text("See policy.md for the eligibility rules.")
        (_docs(task) / "policy.md").write_text("RULES")
        _persona(task, body="known_docs:\n  - policy.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert any("kpis.md" in e for e in result.errors)

    def test_no_false_positive_when_nothing_names_the_doc(self, tmp_path) -> None:
        task = _task(tmp_path)
        (task / "RESOURCES.md").write_text("| DB | database | postgres://db/x | the DB |")
        sop = task / "environment" / "sop"
        sop.mkdir(parents=True)
        (sop / "kpis.md").write_text("Success is measured by resolution rate.")
        (_docs(task) / "policy.md").write_text("RULES")
        _persona(task, body="known_docs:\n  - policy.md\n")

        assert validate_usersim_docs("demo", tmp_path).ok


class TestWarningsDoNotBlock:
    def test_over_suggested_budget_warns_only(self, tmp_path) -> None:
        from appsci_bench.appsci_agent_user_sim.core.persona import SUGGESTED_DOC_TOKENS

        task = _task(tmp_path)
        (_docs(task) / "big.md").write_text("x" * ((SUGGESTED_DOC_TOKENS + 500) * 4))
        _persona(task, body="known_docs:\n  - big.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert result.ok, result.errors
        assert any("over the suggested" in w for w in result.warnings)

    def test_empty_docs_tree_warns_only(self, tmp_path) -> None:
        task = _task(tmp_path)
        _docs(task)
        _persona(task)

        result = validate_usersim_docs("demo", tmp_path)

        assert result.ok
        assert any("no documents" in w for w in result.warnings)


class TestApiShape:
    def test_errors_are_returned_not_raised(self, tmp_path) -> None:
        """Each caller applies its own policy: CI fails the build, the preflight skips a row, the
        orchestrator fails one task."""
        task = _task(tmp_path)
        _docs(task)
        _persona(task, body="known_docs:\n  - missing.md\n")

        result = validate_usersim_docs("demo", tmp_path)  # must not raise

        assert not result.ok

    def test_validate_many_preserves_order(self, tmp_path) -> None:
        for name in ("a", "b", "c"):
            (tmp_path / name / "user_personas").mkdir(parents=True)
            (tmp_path / name / "spec.yaml").write_text(SPEC)

        results = validate_many(["c", "a", "b"], tmp_path)

        assert [r.task_id for r in results] == ["c", "a", "b"]

    def test_summary_is_human_readable(self, tmp_path) -> None:
        task = _task(tmp_path)
        _persona(task)

        assert validate_usersim_docs("demo", tmp_path).summary() == "demo: ok"

    def test_docs_dir_helper_is_shared_with_the_orchestrator(self, tmp_path) -> None:
        """core owns the convention; the orchestrator re-exports it, so they cannot drift."""
        from appsci_bench.orchestrator.run import usersim_docs_dir as orch_helper

        task = _task(tmp_path)
        _docs(task)

        assert usersim_docs_dir(tmp_path, "demo") == orch_helper(tmp_path, "demo")


class TestEveryRealTask:
    def test_all_tasks_validate_clean_today(self) -> None:
        """No task has adopted known_docs, so all must be clean. This will keep them honest as they
        migrate, and it is the check the CI layer (phase E) builds on."""
        from appsci_bench.core.spec import TASKS_DIR

        ids = sorted(p.name for p in TASKS_DIR.iterdir()
                     if p.is_dir() and not p.name.startswith(("_", ".")))
        for result in validate_many(ids):
            assert result.ok, f"{result.task_id}: {result.errors}"


class TestPersonaProseGuard:
    """A persona must not point the agent at a sim-only document via an agent mount path.

    This is the leak that survives every other guard: the document is correctly sim-only and correctly
    rendered with no path, but the persona's own prose says "it's in /sop/x.md" -- so the sim sends the
    agent to a path that does not exist in its container.
    """

    def test_domain_knowledge_naming_a_sop_path_is_an_error(self, tmp_path) -> None:
        task = _task(tmp_path)
        (_docs(task) / "contract_terms.md").write_text("RULES")
        _persona(task, "l2", body=(
            "domain_knowledge:\n"
            '  data_shape: "terms are free text in the contracts table and in /sop/contract_terms.md"\n'
            "known_docs:\n  - contract_terms.md\n"
        ))

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert any("prose points the agent at /sop/contract_terms.md" in e for e in result.errors)

    def test_data_mount_path_also_caught(self, tmp_path) -> None:
        task = _task(tmp_path)
        (_docs(task) / "examples.jsonl").write_text('{"a":1}')
        _persona(task, "l1", body=(
            'domain_knowledge:\n  where: "see /data/examples.jsonl"\n'
            "known_docs:\n  - examples.jsonl\n"
        ))

        assert not validate_usersim_docs("demo", tmp_path).ok

    def test_bare_filename_in_prose_is_not_flagged(self, tmp_path) -> None:
        """Only MOUNT paths are matched. A persona says "the contract terms" or even names a document in
        passing all the time; flagging that would make the guard unusable."""
        task = _task(tmp_path)
        (_docs(task) / "contract_terms.md").write_text("RULES")
        _persona(task, "l2", body=(
            'domain_knowledge:\n  data_shape: "the contract terms are free text"\n'
            "known_docs:\n  - contract_terms.md\n"
        ))

        assert validate_usersim_docs("demo", tmp_path).ok

    def test_prose_path_to_an_agent_facing_doc_is_fine(self, tmp_path) -> None:
        """Pointing at something the agent DOES have is correct behavior -- only sim-only documents are
        unreachable. (This is why order_to_cash's l2 prose is legal today and becomes an error only when
        contract_terms.md moves into the docs tree.)"""
        task = _task(tmp_path)
        (_docs(task) / "policy.md").write_text("RULES")
        _persona(task, "l2", body=(
            'domain_knowledge:\n  data_shape: "examples are in /data/examples.jsonl"\n'
            "known_docs:\n  - policy.md\n"
        ))

        assert validate_usersim_docs("demo", tmp_path).ok

    def test_role_string_is_scanned_too(self, tmp_path) -> None:
        task = _task(tmp_path)
        (_docs(task) / "policy.md").write_text("RULES")
        _persona(task, "l1", body=(
            'role: "owner of /sop/policy.md"\nknown_docs:\n  - policy.md\n'
        ))

        assert not validate_usersim_docs("demo", tmp_path).ok


class TestAdversarialConsistency:
    def test_variant_differing_from_base_warns(self, tmp_path) -> None:
        """Almost always a forgotten edit: a variant is the same stakeholder with a different stance."""
        task = _task(tmp_path)
        docs = _docs(task)
        (docs / "a.md").write_text("A")
        (docs / "b.md").write_text("B")
        _persona(task, "l1", body="known_docs:\n  - a.md\n")
        _persona(task, "l1_adversarial", body="known_docs:\n  - a.md\n  - b.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert result.ok, result.errors  # a warning, never a blocker
        assert any("differs from its base" in w for w in result.warnings)

    def test_matching_variant_is_silent(self, tmp_path) -> None:
        task = _task(tmp_path)
        (_docs(task) / "a.md").write_text("A")
        _persona(task, "l1", body="known_docs:\n  - a.md\n")
        _persona(task, "l1_adversarial", body="known_docs:\n  - a.md\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert result.ok and not result.warnings

    def test_variant_without_a_base_is_ignored(self, tmp_path) -> None:
        task = _task(tmp_path)
        (_docs(task) / "a.md").write_text("A")
        _persona(task, "orphan_adversarial", body="known_docs:\n  - a.md\n")

        assert validate_usersim_docs("demo", tmp_path).ok


class TestResourceLocationGuard:
    """A resource a persona lists must not carry a repo-relative `location`.

    The fourth way a sim can point the agent somewhere it cannot go, and the only one that never touches
    `user_sim_docs/`: the document is agent-facing and correctly mounted, but the spec records its REPO
    path while the agent sees a CONTAINER path. `resolve_known_resources` hands that string to the
    template, which renders it verbatim.

    Both instances found when this was written were pre-existing and live on `main`: order_to_cash's
    contract_terms_doc/examples/kpis, and airline_rebooking's airline_db/kpis.
    """

    def _spec_with(self, location: str) -> str:
        return f"""
task_id: demo
name: Demo
description: a demo task
business_goals: ["ship it"]
environment:
  resources:
    - {{name: terms, type: docs, location: "{location}", description: "the terms"}}
artifacts:
  output_dir: /work/out
"""

    def _task_listing_terms(self, tmp_path, location: str) -> None:
        task = _task(tmp_path, spec=self._spec_with(location))
        _persona(task, "l1", body="known_resources:\n  - terms\n")

    # --- flagged ---------------------------------------------------------------------------

    def test_repo_path_is_an_error(self, tmp_path) -> None:
        self._task_listing_terms(tmp_path, "tasks/demo/environment/sop/terms.md")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert "exists in neither container" in result.errors[0]
        assert "tasks/demo/environment/sop/terms.md" in result.errors[0]

    def test_a_real_repo_path_is_caught_even_without_the_tasks_prefix(self, tmp_path) -> None:
        """Detection is evidence-based: a token that RESOLVES inside the repo is a host path whatever it
        is spelled like. This is what catches `environment/db/`-style entries."""
        self._task_listing_terms(tmp_path, "schema/user_persona.schema.json")

        assert not validate_usersim_docs("demo", tmp_path).ok

    def test_the_error_names_the_persona_and_the_resource(self, tmp_path) -> None:
        self._task_listing_terms(tmp_path, "tasks/demo/x.md")

        error = validate_usersim_docs("demo", tmp_path).errors[0]

        assert "demo/l1" in error and "'terms'" in error

    # --- not flagged -----------------------------------------------------------------------

    @pytest.mark.parametrize("location", [
        "/sop/terms.md",                                    # the agent's own mount path: the fix
        "/data/examples.jsonl",
        "postgres://sandbox:sandbox@db:5432/oltp",           # connection string
        "https://internal.example.com/terms",               # URL
        "the airline tool set",                             # prose, no slash
        "read/write/transfer tools, one call at a time",     # prose WITH a slash -- must not trip
        "db.json + per-collection flights/users/reservations",
        "in-memory store behind the tool API (no direct file access)",
        "",                                                 # no location at all
    ])
    def test_reachable_or_prose_locations_are_clean(self, tmp_path, location) -> None:
        self._task_listing_terms(tmp_path, location)

        result = validate_usersim_docs("demo", tmp_path)

        assert result.ok, (location, result.errors)

    def test_unlisted_resource_is_not_flagged(self, tmp_path) -> None:
        """A location no persona can see never reaches a prompt. Rewriting those would be churn -- and
        the trap is disarmed: it fails loudly the moment a persona adds the resource, which is exactly
        when it starts to matter. (airline's `rebooking_policy` is in this state today.)"""
        task = _task(tmp_path, spec=self._spec_with("tasks/demo/environment/sop/terms.md"))
        _persona(task, "l1")  # declares no known_resources

        assert validate_usersim_docs("demo", tmp_path).ok

    def test_adversarial_variants_are_checked_too(self, tmp_path) -> None:
        task = _task(tmp_path, spec=self._spec_with("tasks/demo/environment/sop/terms.md"))
        _persona(task, "l1_adversarial", body="known_resources:\n  - terms\n")

        result = validate_usersim_docs("demo", tmp_path)

        assert not result.ok
        assert "l1_adversarial" in result.errors[0]

    def test_it_is_checked_with_no_docs_tree_at_all(self, tmp_path) -> None:
        """This channel exists whether or not a task has adopted known_docs, so unlike every other check
        here it must NOT be gated on the docs tree existing. order_to_cash is exactly that case."""
        task = _task(tmp_path, spec=self._spec_with("tasks/demo/environment/sop/terms.md"))
        _persona(task, "l1", body="known_resources:\n  - terms\n")
        assert not (task / DOCS_DIRNAME).exists()

        assert not validate_usersim_docs("demo", tmp_path).ok


class TestStrayOsFilesDoNotBreakValidation:
    """A file the validator cannot read is a file it has nothing to say about -- not a dead run.

    The concrete incident: a code snapshot published from macOS shipped 1,038 AppleDouble companion
    files (`._l3.yaml`, `._l3_adversarial.yaml`, `._spec.yaml` -- binary resource forks that BSD tar
    emits for any file carrying an extended attribute). They unpacked as real files on the Linux jobs
    host, `glob("*.yaml")` and `glob("*_adversarial.yaml")` both matched them because `*` matches a
    leading dot, and `read_text()` died on byte 0xa3. Every run of every task then failed pre-flight
    with a UnicodeDecodeError twelve frames deep that mentioned neither macOS nor tar.

    Fixed at the source too -- `publish_code_snapshot.sh` now refuses to build a tarball containing
    them -- but this layer must hold independently, because it is the layer that decides whether an
    hour of GPU time gets spent.
    """

    #: The real header BSD tar writes, truncated to the byte that raised. `0xa3` at offset 45 is the
    #: exact failure seen on the box, so this fixture is the incident rather than an approximation.
    APPLEDOUBLE = (
        b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        "
        b"\x00\x02\x00\x00\x00\t\x00\x00\x002\x00\x00\x00q\x00\x00\x00\x02\x00\x00\x00\xa3"
        + b"\x00" * 40
    )

    def test_an_appledouble_beside_every_persona_is_ignored_not_fatal(self, tmp_path) -> None:
        task = _task(tmp_path)
        _persona(task, "l1")
        _persona(task, "l1_adversarial")
        personas = task / "user_personas"
        for real in list(personas.glob("*.yaml")):
            (personas / f"._{real.name}").write_bytes(self.APPLEDOUBLE)
        (task / "._spec.yaml").write_bytes(self.APPLEDOUBLE)

        # The assertion is that this returns at all: before the fix it raised UnicodeDecodeError.
        result = validate_usersim_docs("demo", tmp_path)

        assert result.ok, result.errors
        # And the stray files must not be reported as personas with problems, either.
        assert not any("._" in msg for msg in result.errors + result.warnings)

    def test_a_non_utf8_persona_is_skipped_rather_than_crashing(self, tmp_path) -> None:
        """Same guarantee for a real (non-dotfile) persona that is simply not valid UTF-8.

        Dotfile filtering alone would not cover this, so the read sites carry an explicit encoding and
        catch the decode error. A latin-1 `£` is the cheapest way to be genuinely undecodable as UTF-8.
        """
        task = _task(tmp_path)
        _persona(task, "l1")
        (task / "user_personas" / "l2.yaml").write_bytes(
            b"persona_id: l2\nname: Costs \xa3100\n"
        )

        result = validate_usersim_docs("demo", tmp_path)

        assert isinstance(result.errors, list)  # returned rather than raised
