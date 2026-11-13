"""Prebuilt transcript judges.

A handful of cross-task metrics show up near-verbatim across multiple tasks' rubric lists --
conversation quality, task resolution, and process/policy compliance. Rather than each task
hand-writing its own near-identical jinja2 file for these, this module provides one shared
implementation per metric that task designers can import and point their `eval_function_path` at
directly.

Example cross-task overlap this targets: a `process_compliance`/`policy_compliance`-shaped "did the
agent follow the stated process" judge appears in the multi-turn customer-service tasks, such as `airline_rebooking`.
"""
from __future__ import annotations

from ..transcript import make_transcript_judge

BUILTIN_PROMPTS: dict[str, str] = {
    "conversation_quality": """\
You are evaluating the QUALITY of an AI agent's conversation with a user -- not whether the final
outcome was correct (that is scored separately), but whether the agent communicated well: asked
clarifying questions when needed, stayed on topic, was concise and clear, and did not confuse or
mislead the user.

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: Clear, concise, on-topic throughout. Asked for clarification when the
user's request was ambiguous. Never confused or misled the user.
**0.5-0.8 (Good)**: Generally clear and on-topic. Minor lapses in clarity or unnecessary verbosity,
but nothing that misled the user.
**0.2-0.5 (Poor)**: Confusing, off-topic, or overly verbose in ways that would frustrate a real
user. May have missed an obvious need for clarification.
**0.0-0.2 (Critical)**: Actively misleading, incoherent, or ignored the user's actual request.

## Rubric (if provided)
{{ eval_rubric }}

## Conversation Transcript
{{ transcript }}

Evaluate step by step: (1) note any point where the agent's communication was unclear, off-topic,
or misleading; (2) note where the agent handled ambiguity well; (3) weigh both against the rubric
above (if given); (4) give a final score and your reasoning.
""",
    "task_resolution": """\
You are evaluating whether an AI agent's conversation with a user actually RESOLVED the user's
task -- not communication quality (scored separately), but outcome: did the user's stated need get
met by the end of the conversation?

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: The user's task is fully resolved by the end of the transcript, matching
the expected outcome / rubric below.
**0.5-0.8 (Good)**: The task is mostly resolved; a minor aspect is incomplete or slightly off from
the expected outcome.
**0.2-0.5 (Poor)**: The task is only partially addressed, or resolved incorrectly relative to the
expected outcome / rubric.
**0.0-0.2 (Critical)**: The task is not resolved at all, or the agent's final state actively
contradicts the expected outcome.

## Rubric (if provided)
{{ eval_rubric }}

## Expected Outcome (if provided)
text: {{ expected_text }}
json: {{ expected_json }}

## Conversation Transcript
{{ transcript }}

Evaluate step by step: (1) identify what the user actually needed; (2) check whether the final
state of the conversation delivers that, against the expected outcome / rubric above; (3) give a
final score and your reasoning.
""",
    "process_compliance": """\
You are evaluating whether an AI agent FOLLOWED THE REQUIRED PROCESS while handling a user's
request -- not whether the final outcome was correct (scored separately), but whether the agent's
steps, in order, complied with the stated policy/process below (e.g. eligibility checks before
acting, required confirmations, no unauthorized actions).

## Scoring Criteria (0-1 Scale)

**0.8-1.0 (Excellent)**: Every required process step was followed, in the correct order, with no
unauthorized or out-of-policy actions.
**0.5-0.8 (Good)**: The process was followed in substance; a minor step was taken out of order or
lightly abbreviated, but no policy was actually violated.
**0.2-0.5 (Poor)**: A required step was skipped, or an action was taken that the process/policy
does not permit, even if the final outcome happened to be acceptable.
**0.0-0.2 (Critical)**: The agent acted in clear violation of the stated process/policy (e.g. wrote
data or took an action it was required to gate on a check it skipped).

## Required Process / Policy
{{ eval_rubric }}

## Conversation Transcript
{{ transcript }}

Evaluate step by step: (1) list the process/policy requirements above; (2) trace the agent's
actions in the transcript against each requirement, in order; (3) flag any skipped step or
unauthorized action; (4) give a final score and your reasoning.
""",
}

conversation_quality_judge = make_transcript_judge(BUILTIN_PROMPTS["conversation_quality"])
task_resolution_judge = make_transcript_judge(BUILTIN_PROMPTS["task_resolution"])
process_compliance_judge = make_transcript_judge(BUILTIN_PROMPTS["process_compliance"])
