"""slides_generator's elicitation rubric: the shared soundness checks plus this task's own risk.

The risk here is a mounted worked example. The task ships a real request under `environment/data/samples/`
-- a brief, a spreadsheet, a source document and the template deck -- and anything an example reveals is
handed rather than elicited. So check 5 is extended to look inside those files, at the text a reader
would actually see rather than at the OOXML around it.

The mount is deliberately inputs-only: `request_intake.md` says so in as many words, and there is no gold
deck under `environment/`. These tests hold that line, because the day a finished example lands in the
mount, every item about what has to come back stops being elicitable.
"""
from __future__ import annotations


from tests.tasks.conftest import (
    REPO,
    agent_visible_text,
    assert_rubric_is_sound,
    mounted_office_text,
)

TASK = "slides_generator"

# The structured-response key names. These are the contract the customer states during the build.
CONTRACT_KEYS = ("aspectRatio", "slideNumber", "sourceUrl", "altText", "bullet_list")


def _rubric():
    from appsci_bench.core.spec import load_task_spec
    from appsci_bench.eval.elicitation_rubric import load_rubric

    return load_rubric(load_task_spec(TASK))


def test_the_rubric_is_sound_including_the_mounted_example():
    assert_rubric_is_sound(TASK, extra_visible=mounted_office_text(TASK))


def test_no_contract_key_is_readable_from_anything_the_agent_is_given():
    """The response shape is the customer's to state -- that is the whole premise of the two format
    documents in the mount, both of which say so explicitly. If a key shows up here, an item about the
    structured response has quietly become an item about reading a file."""
    visible = agent_visible_text(TASK) + mounted_office_text(TASK)
    leaked = [k for k in CONTRACT_KEYS if k in visible]
    assert leaked == [], f"these response keys are readable by the agent: {leaked}"


def test_the_sample_request_carries_no_finished_deck():
    """The mount is inputs-only by design. A gold deck appearing here would satisfy most of this rubric
    by example, so its absence is an invariant worth failing on rather than a fact about today."""
    samples = REPO / "tasks" / TASK / "environment" / "data" / "samples"
    assert samples.is_dir()
    decks = [p.name for p in samples.rglob("*.pptx")]
    assert decks == ["template.pptx"], f"unexpected deck(s) in the agent's sample mount: {decks}"
    for path in samples.rglob("*"):
        if path.is_file():
            assert "gold" not in path.name.lower(), f"a gold artifact is mounted: {path.name}"


def test_the_slide_count_guidance_is_not_readable_by_the_agent():
    # Four deck types with four ranges. Nothing the agent is given states any of them.
    visible = (agent_visible_text(TASK) + mounted_office_text(TASK)).lower()
    for phrase in ("8-12", "6-8", "3-5", "qbr"):
        assert phrase not in visible, f"slide-count guidance leaked: {phrase!r}"


def test_the_format_mechanics_the_agent_is_taught_are_not_items():
    """The environment explains the .pptx format on purpose, so the rubric must not claim credit for
    eliciting it. This is the granularity line for this task: the mechanics are handed, what has to come
    BACK is not."""
    facts = " ".join(i.fact for i in _rubric().items).lower()
    for handed in ("ooxml", "zip archive", "slide master", "placeholder is a pre-positioned",
                   "python-pptx", "libreoffice"):
        assert handed not in facts, f"an item restates handed format mechanics: {handed!r}"


def test_the_response_contract_is_covered_end_to_end():
    facts = " ".join(i.fact for i in _rubric().items)
    for required in ("presentation", "slideNumber", "elements", "sourceUrl", "aspectRatio",
                     "editable", "failed requests"):
        assert required in facts, f"no item states {required!r}"
