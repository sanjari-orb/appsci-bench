"""What the build phase actually DELIVERS to the agent, and what it must never carry.

This task's environment is a delivery channel with no database behind it: the build phase's bundle is
the only thing that puts a file where the agent can read it, and `eval: none` means there is no second
environment at all. Both halves went unchecked, and the first one broke silently -- the bundle shipped
only the setup script and RESOURCES.md while four documents (the spec's resources block, RESOURCES.md,
environment/README.md and a Dockerfile comment) promised the agent /data/samples/* and /sop/kpis.md.
The agent explored an empty /data and a missing /sop. Nothing failed: the workspace container is
`tail -f /dev/null` and reports healthy holding nothing, so there is no runtime symptom to notice.

So these tests assert the two directions that matter:
  * every location the task PROMISES the agent is one the build bundle DELIVERS; and
  * the bundle carries nothing from the held-out side, and nothing that merely looks like it does.

They go through the real BundleManager and TaskConfig rather than re-reading the spec by hand, so they
exercise the same code path a run does -- including the bundler's unfiltered recursive glob, which is
why a stray .DS_Store or a dead script in a bundled directory becomes agent-visible.
"""
from __future__ import annotations

import hashlib
import pathlib
import tarfile
import tempfile

import pytest
import yaml

from tests.tasks.conftest import REPO

TASK = "slides_generator"
TASK_DIR = REPO / "tasks" / TASK

#: Office formats that are Git-LFS-tracked repo-wide (see the root .gitattributes).
_OFFICE = {".pptx", ".docx", ".xlsx"}
#: How a Git-LFS pointer stub begins when the real bytes were never pulled.
_LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"
#: How a real OOXML file begins (it is a zip).
_ZIP_MAGIC = b"PK\x03\x04"


def _build_phase():
    from appsci_bench.task_runner.config import TaskConfig

    return TaskConfig.from_spec(TASK, REPO / "tasks").phase("build")


def _bundle_names() -> list[str]:
    """The container-relative paths the build phase's bundle actually delivers."""
    from appsci_bench.task_runner.bundle import BundleManager

    with tempfile.TemporaryDirectory() as tmp:
        path = BundleManager().create_bundle(TASK_DIR, _build_phase().bundle, pathlib.Path(tmp))
        with tarfile.open(path) as tar:
            return sorted(tar.getnames())


def _bundled_host_files() -> list[pathlib.Path]:
    """Every host file the build phase's bundle picks up, via the bundler's own globbing rule."""
    out: list[pathlib.Path] = []
    for entry in _build_phase().bundle:
        src = TASK_DIR / entry["src"]
        if src.is_dir():
            out.extend(p for p in src.rglob("*") if p.is_file())
        elif src.is_file():
            out.append(src)
    return out


# --- the promise/delivery contract ---------------------------------------------------------------

def test_every_promised_resource_location_is_delivered_by_the_build_bundle():
    """The regression test for the empty build env.

    `environment.resources[].location` is the agent's map of its own environment -- it is rendered into
    RESOURCES.md and into the personas' known_resources. A location the bundle does not deliver sends
    the agent looking for a file that is not there, and then falling back on assumption.
    """
    spec = yaml.safe_load((TASK_DIR / "spec.yaml").read_text())
    dests = [e["dest"] for e in _build_phase().bundle]

    promised = [r["location"] for r in spec["environment"]["resources"]]
    assert promised, "the task promises no resources at all -- this test would pass vacantly"

    undelivered = [
        loc for loc in promised
        # A location is covered when some bundle destination is a prefix of it. Compare on the
        # directory part so that a glob (/data/samples/*) and a file (/sop/kpis.md) both resolve.
        if not any(loc.startswith(d.rstrip("/") + "/") for d in dests if d != "/")
    ]
    assert not undelivered, (
        f"{TASK} promises the agent these locations but its build bundle delivers none of them: "
        f"{undelivered}. Declared destinations: {sorted(set(dests))}"
    )


def test_the_sample_request_and_the_kpi_doc_are_actually_in_the_bundle():
    """The two inputs by name, so a mapping cannot be dropped without a red test."""
    names = _bundle_names()
    assert "sop/kpis.md" in names, f"the KPI doc is not in the bundle: {names}"
    assert any(n.endswith("samples/slidesgen-example-01/template.pptx") for n in names), (
        f"the sample's template is not in the bundle: {names}"
    )
    assert any(n.endswith("samples/slidesgen-example-01/PROMPT.md") for n in names), (
        f"the sample's prompt is not in the bundle: {names}"
    )


def test_the_eval_phase_declares_no_environment():
    """`eval: none` is deliberate here and load-bearing: the eval inputs travel as attachments on each
    case and eval.py reads nothing from disk. Pinned so it cannot become an empty env by accident."""
    from appsci_bench.task_runner.config import TaskConfig

    config = TaskConfig.from_spec(TASK, REPO / "tasks")
    assert config.phase("eval") is None
    assert "eval" in config.environments, "the eval phase must stay explicitly declared, not absent"


# --- what the bundle must never carry ------------------------------------------------------------

def test_the_bundle_carries_nothing_from_the_held_out_side():
    """The bundle is agent-facing by construction, so an eval sample id or a gold artifact reaching it
    would hand over the scored material. Checked on the delivered paths, not just on the source tree.
    """
    names = _bundle_names()
    offenders = [
        n for n in names
        if "gold" in n.lower()
        or "product_slidesgen" in n.lower()
        or "product-slidesgen" in n.lower()
        or "eval_data" in n.lower()
    ]
    assert not offenders, f"the agent-facing bundle carries held-out material: {offenders}"


def test_the_bundle_carries_no_editor_or_os_droppings():
    """BundleManager globs every file under a bundled directory, with no filter. So a .DS_Store or an
    Office lock file sitting in environment/data/ ships straight into the agent's container.
    """
    noise = [n for n in _bundle_names()
             if pathlib.PurePath(n).name.startswith((".DS_Store", "~$", "._"))]
    assert not noise, f"the bundle carries editor/OS droppings: {noise}"


def test_no_bundled_file_is_byte_identical_to_an_eval_data_file():
    """File-shaped disjointness: the record-based carve check's intent, for a task with no records.

    The build sample is purpose-built and non-eval-derived, and its template is a different brand from
    the eval ones. This is what fails if someone "helpfully" copies an eval template into the sample
    folder to save authoring one.
    """
    eval_dir = TASK_DIR / "eval_data"
    eval_hashes = {
        hashlib.md5(p.read_bytes()).hexdigest(): p.name
        for p in eval_dir.rglob("*")
        # READMEs are prose about the dataset, not dataset content, and are not agent-reachable anyway.
        if p.is_file() and p.name != "README.md"
    }
    assert eval_hashes, "no eval data found -- this test would pass vacantly"

    collisions = [
        f"{p.relative_to(TASK_DIR)} == eval_data/{eval_hashes[h]}"
        for p in _bundled_host_files()
        if (h := hashlib.md5(p.read_bytes()).hexdigest()) in eval_hashes
    ]
    assert not collisions, f"bundled file(s) are byte-identical to held-out eval data: {collisions}"


# --- the LFS hazard ------------------------------------------------------------------------------

def test_no_bundled_office_file_is_a_git_lfs_pointer_stub():
    """Skipped rather than failed on a checkout without LFS pulled, deliberately.

    The .pptx/.docx here are LFS-tracked. Without `git lfs pull` they are small text stubs that the
    bundler tars and tar extracts with no error anywhere, and the agent then meets a template
    python-pptx cannot open. That is a real hazard, but it is a property of the developer's checkout
    rather than of the repo, so failing here would redden the suite for someone whose only mistake was
    a partial clone. The HARD check lives in environment/scripts/setup.sh, where it aborts the run at
    the moment it actually matters -- this test just makes sure a real archive stays a real archive.
    """
    office = [p for p in _bundled_host_files() if p.suffix.lower() in _OFFICE]
    assert office, "no bundled office files found -- this test would pass vacantly"

    stubs = [p.name for p in office if p.read_bytes()[:len(_LFS_MAGIC)] == _LFS_MAGIC]
    if stubs:
        pytest.skip(f"Git-LFS objects not pulled ({stubs}); run 'git lfs install && git lfs pull'")

    not_archives = [p.name for p in office if p.read_bytes()[:4] != _ZIP_MAGIC]
    assert not not_archives, (
        f"bundled office file(s) are neither a real OOXML archive nor an LFS stub: {not_archives}"
    )


# --- the setup script's own contract -------------------------------------------------------------

def test_the_setup_script_asserts_its_inputs_landed():
    """A phase's setup script must end in assertions that its data arrived, because container health is
    not evidence of it (docs/task_env_split.md). With no DB to load, asserting delivery is the whole job
    this script has -- so a rewrite that reduced it back to an echo would silently give up the check.
    """
    script = (TASK_DIR / "environment" / "scripts" / "setup.sh").read_text()
    assert "set -euo pipefail" in script, "the script must abort the run on a failed assertion"
    assert "/sop/kpis.md" in script, "the script does not check the KPI doc landed"
    assert "/data/samples" in script, "the script does not check the sample request landed"
    assert "PK" in script, "the script does not check office files are real archives, not LFS stubs"
