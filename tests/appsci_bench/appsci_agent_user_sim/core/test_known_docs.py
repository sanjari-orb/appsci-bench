"""Tests for sim-only document loading: estimate_tokens + resolve_known_docs (issue #76, A2/A3).

`known_docs` is the channel that gives a persona document content the appsci-agent cannot read. Two
properties matter more than the happy path:

  - **it fails loud.** The mechanism it replaces returned None on every problem, so a mis-declared
    document produced a sim that simply did not know its policy -- indistinguishable in a trace from an
    agent that failed to elicit. Every failure mode here must raise.
  - **nothing escapes the docs root.** The resolver loads whatever a declaration resolves to and injects
    it into a prompt, so `..` and out-of-tree symlinks have to be rejected even though the schema
    already rejects the obvious spellings (personas can be loaded unvalidated).

See docs_local/09_usersim_file_access_design.md (D5, D7) for the design.
"""
import pathlib

import pytest

from appsci_bench.appsci_agent_user_sim.core.persona import (
    HARD_DOC_TOKENS,
    SUGGESTED_DOC_TOKENS,
    UserSimDocsError,
    _MAX_DOC_BYTES,
    estimate_tokens,
    resolve_known_docs,
)


def _root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "user_sim_docs"
    root.mkdir()
    return root


def _contents(docs: list[dict]) -> list[str]:
    return [d["content"] for d in docs]


# --- A2: estimate_tokens ----------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_known_string_locks_the_formula(self) -> None:
        """Pinned deliberately: changing the estimator should be a conscious test edit, not a silent
        drift in what the budget means."""
        assert estimate_tokens("x" * 400) == 100
        assert estimate_tokens("x" * 401) == 101  # ceil, never floor -- don't under-report

    def test_monotonic_in_length(self) -> None:
        assert estimate_tokens("x" * 10) <= estimate_tokens("x" * 100) <= estimate_tokens("x" * 1000)

    def test_deterministic(self) -> None:
        text = "a policy paragraph with numbers 500 and 3\n" * 20
        assert estimate_tokens(text) == estimate_tokens(text)

    def test_hard_cap_above_suggested(self) -> None:
        """Guards a typo that would make the suggested budget unreachable."""
        assert HARD_DOC_TOKENS > SUGGESTED_DOC_TOKENS

    def test_airline_arithmetic_fits_suggested(self) -> None:
        """Regression anchor from the design: airline's policy (8,234 B) plus its tool reference
        (11,273 B) is the largest real declaration we know of, and it must sit inside the suggested
        budget -- otherwise the guidance is wrong, not the task."""
        assert estimate_tokens("x" * 19_507) < SUGGESTED_DOC_TOKENS


# --- A3: happy paths -------------------------------------------------------------------------

class TestResolveKnownDocsHappyPath:
    def test_declared_docs_in_declaration_order(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "policy.md").write_text("POLICY")
        (root / "kpis.md").write_text("KPIS")

        docs = resolve_known_docs({"known_docs": ["kpis.md", "policy.md"]}, root)

        assert _contents(docs) == ["KPIS", "POLICY"]

    def test_common_is_auto_included_before_declared(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "common").mkdir()
        (root / "common" / "tools.json").write_text("TOOLS")
        (root / "policy.md").write_text("POLICY")

        docs = resolve_known_docs({"known_docs": ["policy.md"]}, root)

        assert _contents(docs) == ["TOOLS", "POLICY"]

    def test_common_only_when_nothing_declared(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "common").mkdir()
        (root / "common" / "tools.json").write_text("TOOLS")

        assert _contents(resolve_known_docs({}, root)) == ["TOOLS"]

    def test_common_is_sorted_not_filesystem_ordered(self, tmp_path: pathlib.Path) -> None:
        """Prompt bytes must not depend on directory iteration order, or the same declaration renders
        differently on two machines."""
        root = _root(tmp_path)
        (root / "common").mkdir()
        for name in ("zebra.md", "alpha.md", "middle.md"):
            (root / "common" / name).write_text(name)

        assert _contents(resolve_known_docs({}, root)) == ["alpha.md", "middle.md", "zebra.md"]

    def test_common_is_recursive(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "common" / "nested").mkdir(parents=True)
        (root / "common" / "nested" / "deep.md").write_text("DEEP")

        assert _contents(resolve_known_docs({}, root)) == ["DEEP"]

    def test_dotfiles_in_common_are_skipped(self, tmp_path: pathlib.Path) -> None:
        """.DS_Store must not fail a run. Any *other* unsupported extension still raises."""
        root = _root(tmp_path)
        (root / "common").mkdir()
        (root / "common" / ".DS_Store").write_bytes(b"\x00\x01binary")
        (root / "common" / "tools.json").write_text("TOOLS")

        assert _contents(resolve_known_docs({}, root)) == ["TOOLS"]

    def test_doc_in_both_common_and_declared_appears_once(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "common").mkdir()
        (root / "common" / "tools.json").write_text("TOOLS")

        docs = resolve_known_docs({"known_docs": ["common/tools.json"]}, root)

        assert _contents(docs) == ["TOOLS"]

    def test_nested_declared_path(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "sub").mkdir()
        (root / "sub" / "doc.md").write_text("NESTED")

        assert _contents(resolve_known_docs({"known_docs": ["sub/doc.md"]}, root)) == ["NESTED"]

    @pytest.mark.parametrize("name", ["a.md", "a.txt", "a.jsonl", "a.json"])
    def test_every_allowed_extension_loads(self, tmp_path: pathlib.Path, name: str) -> None:
        """.json is the new one -- airline's tool reference is JSON and the old loader excluded it."""
        root = _root(tmp_path)
        (root / name).write_text("BODY")

        assert _contents(resolve_known_docs({"known_docs": [name]}, root)) == ["BODY"]

    def test_returns_content_only_no_path_or_name(self, tmp_path: pathlib.Path) -> None:
        """The structural half of D5: a template cannot render a path it was never given. The agent
        cannot open these documents, so the sim must not be able to point at one."""
        root = _root(tmp_path)
        (root / "policy.md").write_text("POLICY")

        docs = resolve_known_docs({"known_docs": ["policy.md"]}, root)

        assert docs == [{"content": "POLICY"}]


# --- A3: absent root / nothing declared ------------------------------------------------------

class TestResolveKnownDocsAbsentRoot:
    def test_no_root_and_nothing_declared_is_empty(self) -> None:
        """Every task that has not adopted known_docs must keep working."""
        assert resolve_known_docs({}, None) == []

    def test_missing_root_dir_and_nothing_declared_is_empty(self, tmp_path: pathlib.Path) -> None:
        assert resolve_known_docs({}, tmp_path / "nope") == []

    def test_no_root_but_docs_declared_raises(self) -> None:
        """THE regression test for the silent-None failure this work exists to remove: in a container
        the docs root is a mount, and an absent mount used to mean 'the sim quietly knows nothing'."""
        with pytest.raises(UserSimDocsError, match="docs root"):
            resolve_known_docs({"known_docs": ["policy.md"]}, None)

    def test_missing_root_dir_but_docs_declared_raises(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(UserSimDocsError, match="docs root"):
            resolve_known_docs({"known_docs": ["policy.md"]}, tmp_path / "nope")

    def test_absent_common_dir_is_fine(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "policy.md").write_text("POLICY")

        assert _contents(resolve_known_docs({"known_docs": ["policy.md"]}, root)) == ["POLICY"]


# --- A3: failure modes ----------------------------------------------------------------------

class TestResolveKnownDocsFailsLoud:
    def test_missing_file_raises_naming_the_declared_path(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)

        with pytest.raises(UserSimDocsError, match="policy.md"):
            resolve_known_docs({"known_docs": ["policy.md"]}, root)

    def test_unsupported_extension_raises_naming_it(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "data.pdf").write_text("not really a pdf")

        with pytest.raises(UserSimDocsError, match=r"\.pdf"):
            resolve_known_docs({"known_docs": ["data.pdf"]}, root)

    def test_unsupported_extension_in_common_raises(self, tmp_path: pathlib.Path) -> None:
        """A non-dotfile a task owner deliberately placed in common/ must not be silently omitted."""
        root = _root(tmp_path)
        (root / "common").mkdir()
        (root / "common" / "sheet.csv").write_text("a,b")

        with pytest.raises(UserSimDocsError, match=r"\.csv"):
            resolve_known_docs({}, root)

    def test_directory_declared_as_doc_raises(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "adir.md").mkdir()

        with pytest.raises(UserSimDocsError, match="not a file"):
            resolve_known_docs({"known_docs": ["adir.md"]}, root)

    def test_traversal_out_of_root_raises_even_when_target_exists(self, tmp_path: pathlib.Path) -> None:
        """The schema rejects `../x`, but personas can be loaded unvalidated, so re-check here."""
        root = _root(tmp_path)
        (tmp_path / "secret.md").write_text("GROUND TRUTH")

        with pytest.raises(UserSimDocsError, match="outside the docs root"):
            resolve_known_docs({"known_docs": ["../secret.md"]}, root)

    def test_symlink_out_of_root_raises(self, tmp_path: pathlib.Path) -> None:
        """A symlink is the spelling the schema pattern cannot catch."""
        root = _root(tmp_path)
        (tmp_path / "secret.md").write_text("GROUND TRUTH")
        (root / "link.md").symlink_to(tmp_path / "secret.md")

        with pytest.raises(UserSimDocsError, match="outside the docs root"):
            resolve_known_docs({"known_docs": ["link.md"]}, root)

    def test_over_per_file_cap_raises(self, tmp_path: pathlib.Path) -> None:
        root = _root(tmp_path)
        (root / "huge.md").write_text("x" * (_MAX_DOC_BYTES + 1))

        with pytest.raises(UserSimDocsError, match="per-file cap"):
            resolve_known_docs({"known_docs": ["huge.md"]}, root)

    def test_over_hard_budget_raises_naming_total_and_cap(self, tmp_path: pathlib.Path) -> None:
        """Several individually-legal documents can still blow the per-turn prompt budget."""
        root = _root(tmp_path)
        per_file = 40_000  # ~10k tokens each, under the per-file byte cap
        for i in range(3):
            (root / f"doc{i}.md").write_text("x" * per_file)

        with pytest.raises(UserSimDocsError, match=str(HARD_DOC_TOKENS)):
            resolve_known_docs({"known_docs": ["doc0.md", "doc1.md", "doc2.md"]}, root)

    def test_exactly_at_hard_budget_passes(self, tmp_path: pathlib.Path) -> None:
        """Boundary: the cap is inclusive, so a declaration sized exactly to it is legal.

        Needs TWO files to get there. The per-file byte cap (50 KB, ~12.5k tokens) is deliberately
        stricter than the per-persona token cap (20k, ~80 KB), so no single document can reach the
        aggregate boundary -- one oversized file is a "split it" problem, several legal files summing
        past the budget is a "declare less" problem, and they get different errors.
        """
        root = _root(tmp_path)
        half = HARD_DOC_TOKENS * 4 // 2  # 40,000 B each -> 10k tokens each, under the per-file cap
        (root / "a.md").write_text("x" * half)
        (root / "b.md").write_text("x" * half)

        docs = resolve_known_docs({"known_docs": ["a.md", "b.md"]}, root)

        assert len(docs) == 2

    def test_over_suggested_but_under_hard_passes(self, tmp_path: pathlib.Path) -> None:
        """Suggested is advisory -- the validator warns, the resolver does not block."""
        root = _root(tmp_path)
        (root / "doc.md").write_text("x" * ((SUGGESTED_DOC_TOKENS + 1000) * 4))

        assert len(resolve_known_docs({"known_docs": ["doc.md"]}, root)) == 1

    def test_one_bad_entry_fails_the_whole_declaration(self, tmp_path: pathlib.Path) -> None:
        """Partial delivery is the failure mode we are removing: better no sim than a half-informed one."""
        root = _root(tmp_path)
        (root / "policy.md").write_text("POLICY")

        with pytest.raises(UserSimDocsError):
            resolve_known_docs({"known_docs": ["policy.md", "missing.md"]}, root)


class TestDocumentedExtensionsMatchTheCode:
    """The supported-type list is repeated in four author-facing places, so it can drift from the code.

    A drift here is not cosmetic: an unsupported type HARD-FAILS the task (by design -- silently skipping
    a document a task owner deliberately declared is the failure mode this mechanism removes). So a task
    owner trusting a stale list would hit a confusing run failure. Adding an extension to
    _LOADABLE_DOC_EXTENSIONS must therefore fail these until the docs are updated.
    """

    DOC_FILES = [
        "tasks/README.md",
        "docs/task_spec_reference.md",
        "schema/user_persona.schema.json",
        "tasks/_TEMPLATE/user_personas/example_persona.yaml",
    ]

    def _repo_root(self) -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parents[4]

    def test_four_types_are_supported(self) -> None:
        """Pinned so a change is deliberate. .json is here because airline's tool reference is JSON."""
        from appsci_bench.appsci_agent_user_sim.core.persona import _LOADABLE_DOC_EXTENSIONS

        assert _LOADABLE_DOC_EXTENSIONS == {".md", ".txt", ".json", ".jsonl"}

    @pytest.mark.parametrize("doc", DOC_FILES)
    def test_every_supported_extension_is_documented(self, doc: str) -> None:
        from appsci_bench.appsci_agent_user_sim.core.persona import _LOADABLE_DOC_EXTENSIONS

        text = (self._repo_root() / doc).read_text()
        missing = sorted(ext for ext in _LOADABLE_DOC_EXTENSIONS if ext not in text)

        assert not missing, f"{doc} does not document supported type(s): {missing}"

    def test_unsupported_types_are_called_out_where_authors_read(self) -> None:
        """The rejection is worth naming explicitly: .csv is the one people reach for, and a table is
        easy to convert to Markdown."""
        text = (self._repo_root() / "tasks/README.md").read_text()

        assert ".csv" in text and ".pdf" in text
