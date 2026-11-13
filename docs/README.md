# appsci-bench docs

**Start here → [OVERVIEW.md](OVERVIEW.md).**

| Doc | What it covers |
|-----|----------------|
| [OVERVIEW.md](OVERVIEW.md) | What the benchmark is, the task list, how a run works, how to run it. |
| [task_spec_reference.md](task_spec_reference.md) | The files that define a task (`spec.yaml`, `RESOURCES.md`, `user_personas/`, `eval_data/`), field by field. |
| [evaluation_framework.md](evaluation_framework.md) | How the `appsci_bench` evaluator scores an agent (the new dataset + objectives + eval-functions system). |
| [evaluator_system_design.md](evaluator_system_design.md) | Design of the evaluator system (the four-part split; session API; datasets/objectives). |
| [elicitation_score_design.md](elicitation_score_design.md) | The **elicitation score**: the standalone diagnostic for how much of a task's must-ask material the agent actually got out of the build-time customer sim — the per-task rubric, the three verdicts, where it runs. |
| [optimize_agent_design.md](optimize_agent_design.md) | How the agent scores and improves its own candidate during the build: when the artifact is scored vs optimized, why a change is judged by the NEXT call, the cycle budget and saturation rules, the three trust boundaries, and why the internal eval is not the benchmark eval. |
| [artifact_agent_api.md](artifact_agent_api.md) | The session-API contract the produced artifact must serve (`create`/`query`/`delete`). |
| [environment_usage.md](environment_usage.md) | Build/run/debug guide for Docker task environments (v0). |
| [user_simulator_api.md](user_simulator_api.md) | The build-time appsci-agent-user-sim service your agent interviews over HTTP (`/health`, `/ask`). |
| checkpoint_trend.md | The **checkpoint trend**: re-scoring every artifact version an agent saved during a build against the real eval, to see whether an independent grader agrees the series improved — how to run it, where results land, how to read the report, and what it costs. |
| [concurrency.md](concurrency.md) | Why task runs are sequential-only today, what blocks concurrent runs, and what it would take to enable them. |

**Which model runs what:** four roles, one model var each — `AGENT_MODEL` (agent under test),
`APPSCI_USERSIM_MODEL` (build-time customer sim), `JUDGE_MODEL` (eval-side judges), and
`ARTIFACT_USERSIM_MODEL` (eval-time user sim, pinned to tau-bench's `openai/gpt-5.2`).
The two middle ones default to `MODEL_NAME`. Table + rationale:
[evaluator_system_design.md](evaluator_system_design.md#model), summarized in the
[root README](../README.md#models-used-in-a-run).

## Where things live
- **Schemas:** [`schema/`](../schema/) — `task_spec`, `user_persona`, `dataset`, `objective`, `artifact_agent_api` (+ its README). (`eval_sample` is the legacy per-case format.)
- **Tasks:** [`tasks/`](../tasks/) — one dir per task; `order_to_cash` is the worked example.
- **Agent protocol:** [`prompts/applied_scientist_protocol.md`](../prompts/applied_scientist_protocol.md).
- **Benchmark code:** [`appsci_bench/`](../appsci_bench/) (the evaluator is [`appsci_bench/eval/`](../appsci_bench/eval/)). **Example agent under test:** [`appsci_agent/`](../appsci_agent/).
