"""Validate the committed adversarial personas.

These files are loaded BY PATH for red-team runs (the orchestrator's persona_path / --persona <file>),
NOT via spec.user_personas, so the normal pipeline never exercises them. This test does: every
*_adversarial.yaml is (1) valid YAML, (2) conforms to schema/user_persona.schema.json, (3) declares
adversarial_pressures from the allowed enum, and (4) references only resources its task's spec defines
(and that the spec itself has no duplicate resource names -- the same failure resolve_known_resources
raises ValueError on in production).
Guards against a resource-name / schema drift bug found during review.
"""
import json
import pathlib

import jsonschema
import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[3]
SCHEMA = json.loads((REPO / "schema" / "user_persona.schema.json").read_text())
ADV = sorted(REPO.glob("tasks/*/user_personas/*_adversarial.yaml"))
VALID_PRESSURES = {"pushback", "time_pressure", "scope_expansion", "scope_contraction",
                   "bad_request", "confidence_inflation"}


def _spec_resource_names(task_dir: pathlib.Path) -> list[str]:
    spec = yaml.safe_load((task_dir / "spec.yaml").read_text())

    def find(d, acc):
        # Recurse into BOTH dict values and list elements, aggregating every `resources:` block
        # (don't short-circuit on the first hit) -- robust to specs that nest resources in a list.
        if isinstance(d, dict):
            if isinstance(d.get("resources"), list):
                acc.extend(d["resources"])
            for v in d.values():
                find(v, acc)
        elif isinstance(d, list):
            for v in d:
                find(v, acc)
        return acc

    # Not deduped: callers that need to check for duplicate resource names (the same failure mode
    # resolve_known_resources raises ValueError on in production) need the raw name list.
    return [r["name"] for r in find(spec, []) if isinstance(r, dict) and "name" in r]


def test_adversarial_personas_found():
    assert ADV, "no *_adversarial.yaml files found"


@pytest.mark.parametrize("path", ADV, ids=[f"{p.parent.parent.name}/{p.stem}" for p in ADV])
def test_adversarial_persona_valid(path: pathlib.Path):
    persona = yaml.safe_load(path.read_text())
    jsonschema.validate(persona, SCHEMA)
    # Schema validation above already enforces the enum (and additionalProperties:false catches a
    # typo'd field name). This assert instead catches an adversarial persona that declares an empty
    # or absent `adversarial_pressures` -- schema-valid, but meaningless for a file meant to carry
    # at least one pressure.
    pressures = set(persona.get("adversarial_pressures", []))
    assert pressures, f"{path.name}: no adversarial_pressures declared"
    assert pressures <= VALID_PRESSURES, f"{path.name}: unknown pressures {pressures - VALID_PRESSURES}"
    spec_names = _spec_resource_names(path.parents[1])
    dupes = {n for n in spec_names if spec_names.count(n) > 1}
    assert not dupes, (
        f"{path.name}: task spec declares duplicate resource name(s) {dupes} "
        f"(resolve_known_resources raises ValueError on this in production)"
    )
    spec_res = set(spec_names)
    bad = [r for r in persona.get("known_resources", []) if r not in spec_res]
    assert not bad, f"{path.name}: references non-spec resources {bad} (spec: {sorted(spec_res)})"
