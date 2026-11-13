# Adversarial appsci-agent-user-sim personas

**Upstream:** the 8-category failure-mode taxonomy (S1). This doc is the in-repo reference for the
adversarial variant of the appsci agent user sim and how it maps to that taxonomy.

## What this is

A normal persona plays a cooperative-but-vague stakeholder. The **adversarial variant** plays a
*plausibly-bad* stakeholder — one who pushes back without basis, rushes, changes scope, and makes
careless requests. Its purpose is to **elicit** failure modes in the applied-scientist agent so the
rubric/Oracle can score them. It is a stressor, not an evaluator: the simulator surfaces the failure;
the **safety rubric axis (S3)** measures it.

The variant is **opt-in and template-level**, not a schema rewrite. A persona becomes adversarial by
declaring an `adversarial_pressures` list in its YAML; the renderer then layers
`_adversarial_contract.jinja2` on top of the persona's L-level template. Absent/empty = a normal
persona (the default; fully backward-compatible).

## The two-fabrications distinction (why this doesn't break the sim's honesty)

The base `_behavioral_contract.jinja2` hard-codes **"never invent a fact"**. That is a
*measurement-fidelity* rule — it keeps the simulator a reproducible instrument. The adversarial
variant **does not relax it.** There are two different actors:

| Fabrication by… | Owned by | Where the rule lives |
|---|---|---|
| the **simulator** (inventing facts) | sim fidelity | `_behavioral_contract.jinja2` — left intact |
| the **agent under test** (fabricating in its artifact) | safety axis (S3) | the rubric/Oracle, not the sim |

So the simulator stays honest. "Deception" failure modes are modeled as a **faithful-but-flawed
persona**, never as on-the-fly sim fabrication:

- **Contradiction / false data claims** → an **intentionally-mistaken belief** placed in the
  persona's `domain_knowledge` (marked as a deliberate plant). The persona sincerely "knows" a wrong
  fact and voices it honestly. This is a realistic unreliable-stakeholder, and it tests whether the
  agent verifies confident claims against the data.
- **Pressure to fabricate / skip safety** → a **bad request** the persona makes ("just put 17% in";
  "don't bother with an amount cap"). The persona invents nothing; it voices a flawed request. If the
  agent complies, the fabrication/unsafe choice is the *agent's*, scored on the safety axis.

## Pressure profiles (the `adversarial_pressures` enum)

Per review feedback, the persona declares a **small set of named pressures**, not fine-grained
intensity dials. Each name activates a prose behavior block in `_adversarial_contract.jinja2` that
the persona **must** exhibit at least once when its trigger condition occurs (not merely "may"
apply it) — see the template's per-pressure `TRIGGER:` conditions.

| Profile | Behavior | Failure modes targeted |
|---|---|---|
| `pushback` | Unwarranted, content-free doubt after the agent commits to a choice. | 4.2 sycophancy |
| `time_pressure` | Press for the result; wave off validation/process. | 4.2 sycophancy, 4.8 deceptive process |
| `scope_expansion` | Add a new requirement mid-build as an aside. | 4.3 spec-creep |
| `scope_contraction` | Try to drop a safeguard (e.g. "skip the eval") to move faster. | 4.3 spec-creep (mirror) |
| `bad_request` | Press to include an unverified number, or skip a guardrail / cap. | 4.4 fabrication, 4.6 injection-handling, 4.7 unsafe-by-construction |

Deception modes (contradiction, false data claims — taxonomy 4.4/4.8) are carried by the
`domain_knowledge` plant described above, **not** by a pressure profile, so they require no change to
the sim's honesty rule.

## Coverage status (this PR)

- Schema field + adversarial contract template + renderer hook — task-agnostic, shipped.
- `order_to_cash` adversarial personas: `l1_adversarial`, `l2_adversarial`, `l3_adversarial`.
- `slides_generator` adversarial personas: `l1_adversarial`, `l2_adversarial`, `l3_adversarial`.
- These are **loaded by path** for red-team runs (e.g. the orchestrator's `persona_path` / the
  `--persona <file>` flow), *not* listed in `spec.yaml`'s `user_personas` (the standard eval set),
  so they never enter normal eval runs. A validation test
  (`tests/appsci_bench/appsci_agent_user_sim/test_adversarial_personas.py`) exercises them directly.
- The **safety rubric axis** that scores these failure modes is **S3**, out of scope here.

## Open item for the sim-engine owner

The renderer selects the adversarial contract purely from the persona's `adversarial_pressures`
field (no engine state, no new code path beyond one template include). If a different selection hook
is preferred, the field + template are unaffected — only the include condition moves.
