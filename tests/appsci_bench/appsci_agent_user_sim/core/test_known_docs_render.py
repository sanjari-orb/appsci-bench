"""Rendering tests for known_docs in the persona prompt (issue #76, B1/B2).

The single most important assertion here is a NEGATIVE one: the document's content reaches the prompt
and its filename/path never does. The agent has no way to open these documents, so a sim that can name
one will point the agent at something unreachable -- and if the declaration used a repo path, it leaks
the ground-truth tree too.

The behavioral rules (internalize rather than recite, quote specifics verbatim) can only be pinned as
*prompt text*; whether the model obeys them is a manual LLM check, not a unit test. See
docs_local/09_usersim_file_access_design.md (D5, D6).
"""
import pathlib

import pytest

from appsci_bench.appsci_agent_user_sim.core.persona import render_persona_prompt

LEVELS = ["L1", "L2", "L3"]


def _persona(tmp_path: pathlib.Path, level: str, body: str = "") -> pathlib.Path:
    p = tmp_path / f"{level.lower()}.yaml"
    p.write_text(f"persona_id: p_{level.lower()}\ntask_id: test\nlevel: {level}\n{body}")
    return p


def _flat(prompt: str) -> str:
    """Collapse whitespace before substring-matching prompt prose.

    Prompt sentences are hard-wrapped, so a phrase that reads as one line in the template can span two
    in the render. Matching against the flattened text keeps these assertions about wording rather than
    about where someone happened to break a line.
    """
    return " ".join(prompt.split())


def _docs_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "user_sim_docs"
    root.mkdir(parents=True)
    return root


class _Task:
    """Minimal TaskContextLike."""
    description = "a task"
    business_goals: list = []
    dataset_files: list = []
    technical_docs: list = []
    interface_design = ""
    environment: dict = {}


@pytest.mark.parametrize("level", LEVELS)
class TestKnownDocsRendering:
    def test_content_reaches_the_prompt(self, tmp_path: pathlib.Path, level: str) -> None:
        root = _docs_root(tmp_path)
        (root / "policy.md").write_text("Basic economy flights cannot be modified.")
        persona = _persona(tmp_path, level, "known_docs:\n  - policy.md\n")

        prompt = render_persona_prompt(_Task(), persona, root)

        assert "Basic economy flights cannot be modified." in prompt

    def test_filename_and_path_never_reach_the_prompt(self, tmp_path: pathlib.Path, level: str) -> None:
        """The sharp one (D5.1). If this fails, the sim can tell the agent to go read a file it has no
        way to open -- and the path may name the ground-truth tree."""
        root = _docs_root(tmp_path)
        (root / "policy.md").write_text("RULE BODY")
        persona = _persona(tmp_path, level, "known_docs:\n  - policy.md\n")

        prompt = render_persona_prompt(_Task(), persona, root)

        assert "RULE BODY" in prompt          # content did land...
        assert "policy.md" not in prompt      # ...but never its name
        assert str(root) not in prompt        # nor the docs root
        assert "user_sim_docs" not in prompt  # nor the tree's name

    def test_says_the_applied_scientist_does_not_have_it(self, tmp_path: pathlib.Path, level: str) -> None:
        root = _docs_root(tmp_path)
        (root / "policy.md").write_text("RULE BODY")
        persona = _persona(tmp_path, level, "known_docs:\n  - policy.md\n")

        prompt = render_persona_prompt(_Task(), persona, root)

        assert "does NOT have this material" in prompt
        assert "Never point them at it" in prompt
        assert "Never treat it as a file" in prompt

    def test_says_state_specifics_verbatim(self, tmp_path: pathlib.Path, level: str) -> None:
        """D6's carve-out: paraphrasing a threshold is a wrong answer, so the instruction must be
        present wherever documents are."""
        root = _docs_root(tmp_path)
        (root / "policy.md").write_text("Fee is $50.")
        persona = _persona(tmp_path, level, "known_docs:\n  - policy.md\n")

        prompt = render_persona_prompt(_Task(), persona, root)

        assert "State specifics EXACTLY as written" in prompt

    def test_block_absent_when_no_docs(self, tmp_path: pathlib.Path, level: str) -> None:
        """A persona with no documents must not carry rules about material it does not have."""
        prompt = render_persona_prompt(_Task(), _persona(tmp_path, level), None)

        assert "What You Know From Having Read It" not in prompt
        assert "does NOT have this material" not in prompt

    def test_block_absent_when_docs_root_has_only_empty_common(self, tmp_path: pathlib.Path, level: str) -> None:
        root = _docs_root(tmp_path)
        (root / "common").mkdir()

        prompt = render_persona_prompt(_Task(), _persona(tmp_path, level), root)

        assert "What You Know From Having Read It" not in prompt

    def test_common_and_declared_are_indistinguishable(self, tmp_path: pathlib.Path, level: str) -> None:
        """The sim must not be able to infer a tier from how a document arrived."""
        root = _docs_root(tmp_path)
        (root / "common").mkdir()
        (root / "common" / "tools.json").write_text("TOOLS_BODY")
        (root / "policy.md").write_text("POLICY_BODY")
        persona = _persona(tmp_path, level, "known_docs:\n  - policy.md\n")

        prompt = render_persona_prompt(_Task(), persona, root)

        assert "TOOLS_BODY" in prompt and "POLICY_BODY" in prompt
        assert "common" not in prompt
        assert "tools.json" not in prompt

    def test_multiple_docs_all_present(self, tmp_path: pathlib.Path, level: str) -> None:
        root = _docs_root(tmp_path)
        (root / "a.md").write_text("FIRST_BODY")
        (root / "b.md").write_text("SECOND_BODY")
        persona = _persona(tmp_path, level, "known_docs:\n  - a.md\n  - b.md\n")

        prompt = render_persona_prompt(_Task(), persona, root)

        assert prompt.index("FIRST_BODY") < prompt.index("SECOND_BODY")

    def test_render_is_byte_stable(self, tmp_path: pathlib.Path, level: str) -> None:
        root = _docs_root(tmp_path)
        (root / "policy.md").write_text("RULE BODY")
        persona = _persona(tmp_path, level, "known_docs:\n  - policy.md\n")

        first = render_persona_prompt(_Task(), persona, root)
        second = render_persona_prompt(_Task(), persona, root)

        assert first == second


def test_block_is_identical_across_levels(tmp_path: pathlib.Path) -> None:
    """The docs block is level-independent: only WHICH documents a persona gets varies by level, never
    the rules about them. Mirrors test_contract_block_identical_across_levels."""
    marker = "### What You Know From Having Read It"
    end = "Everything below is yours to draw on when asked:"
    blocks = []
    for level in LEVELS:
        root = _docs_root(tmp_path / level)
        (root / "policy.md").write_text("RULE BODY")
        persona = _persona(tmp_path, level, "known_docs:\n  - policy.md\n")
        prompt = render_persona_prompt(_Task(), persona, root)
        blocks.append(prompt[prompt.index(marker):prompt.index(end)])

    assert blocks[0] == blocks[1] == blocks[2]


def test_docs_block_survives_the_adversarial_overlay(tmp_path: pathlib.Path) -> None:
    """The adversarial overlay overrides "don't volunteer, don't lead", so verify it does not also
    strip the do-not-recite rules: an adversarial persona must still not paste the document."""
    root = _docs_root(tmp_path)
    (root / "policy.md").write_text("RULE BODY")
    persona = _persona(
        tmp_path, "L1",
        "known_docs:\n  - policy.md\nadversarial_pressures:\n  - pushback\n  - time_pressure\n",
    )

    prompt = render_persona_prompt(_Task(), persona, root)

    assert "Adversarial Behavioral Overlay" in prompt
    assert "Never treat it as a file" in prompt
    assert "State specifics EXACTLY as written" in prompt
    assert "policy.md" not in prompt


def test_resolver_errors_propagate_through_render(tmp_path: pathlib.Path) -> None:
    """A mis-declared document must fail the render, not silently produce a doc-less persona."""
    from appsci_bench.appsci_agent_user_sim.core.persona import UserSimDocsError

    root = _docs_root(tmp_path)
    persona = _persona(tmp_path, "L1", "known_docs:\n  - missing.md\n")

    with pytest.raises(UserSimDocsError):
        render_persona_prompt(_Task(), persona, root)


def test_contract_reconciles_point_them_at_what_they_have(tmp_path: pathlib.Path) -> None:
    """B2: the pre-existing "point them at what they have" rule would otherwise send the agent looking
    for an unreachable document. The carve-out must be present even with no docs declared, since the
    contract is shared."""
    prompt = render_persona_prompt(_Task(), _persona(tmp_path, "L1"), None)

    assert "Point them at what they have" in prompt
    assert "do not assume they have everything you know" in prompt


@pytest.mark.parametrize("level", LEVELS)
def test_nothing_scopes_the_persona_knowledge_to_a_subset(
    tmp_path: pathlib.Path, level: str,
) -> None:
    """Two rules predating this channel used to enumerate what the persona knows, and both named only
    the inline facts and the resources: the contract said "Nothing else" and the domain-facts preamble
    said "you can only reveal information from here". The document block renders after both, so a
    persona reading either literally can classify sim-only material as outside its knowledge and
    decline to give it -- silently defeating the elicitation the channel exists for. The fix was to
    stop enumerating: an unbounded "what this prompt gives you" cannot exclude a section by omission,
    and cannot go stale when a fourth channel is added."""
    root = _docs_root(tmp_path)
    (root / "policy.md").write_text("RULE BODY")
    # Inline facts too, so the domain-facts preamble renders and can be checked.
    persona = _persona(
        tmp_path, level, "domain_knowledge:\n  k: v\nknown_docs:\n  - policy.md\n",
    )

    prompt = _flat(render_persona_prompt(_Task(), persona, root))

    assert "Your knowledge is limited to what this prompt gives you." in prompt
    # The two retired enumerations, neither of which named the document section.
    assert "You can only reveal information from here" not in prompt
    assert "Nothing else." not in prompt
    assert "RULE BODY" in prompt


def test_l3_does_not_refuse_a_specific_it_holds(tmp_path: pathlib.Path) -> None:
    """L3's scripted refusals answer threshold and number questions with "I can't give you a number",
    and they render AFTER the document block -- so recency favours refusing exactly the material that
    moved behind the sim. The carve-out states the precedence at the point of collision."""
    root = _docs_root(tmp_path)
    (root / "policy.md").write_text("Change fee is $75.")
    persona = _persona(tmp_path, "L3", "known_docs:\n  - policy.md\n")

    prompt = _flat(render_persona_prompt(_Task(), persona, root))

    assert "these refusals cover only what you truly do not have" in prompt
    assert "that fact wins over every example below" in prompt
    assert "Never perform not-knowing about something you hold." in prompt
    # The scripted refusals stay -- the carve-out qualifies them, it does not delete them.
    assert "I can\'t give you a number" in prompt
    # The output-shape rule stays too, demoted from sole exception to the sharpest instance.
    assert "THE SHARPEST CASE OF THAT" in prompt


def test_l2_does_not_punt_a_specific_it_holds(tmp_path: pathlib.Path) -> None:
    """Same collision on L2: "What threshold should I use?" is on its punt list, and anything in
    common/ reaches every persona, so L2 can hold a policy threshold and hand it back as the ML
    expert's call."""
    root = _docs_root(tmp_path)
    (root / "policy.md").write_text("Change fee is $75.")
    persona = _persona(tmp_path, "L2", "known_docs:\n  - policy.md\n")

    prompt = _flat(render_persona_prompt(_Task(), persona, root))

    assert "punt only what you genuinely do not have" in prompt
    assert "that fact wins over every example below" in prompt
    assert "What threshold should I use?" in prompt


@pytest.mark.parametrize("level", ["L2", "L3"])
def test_the_carve_out_does_not_mention_reading_when_there_are_no_docs(
    tmp_path: pathlib.Path, level: str,
) -> None:
    """The carve-out names the document channel conditionally. A persona with no sim-only material must
    not be told that "anything you have read" counts as knowledge -- there is nothing it has read, and
    the phrase invites inventing it."""
    prompt = _flat(render_persona_prompt(_Task(), _persona(tmp_path, level), None))

    assert "that fact wins over every example below" in prompt   # carve-out still applies...
    assert "anything you have read" not in prompt                # ...without the docs clause
