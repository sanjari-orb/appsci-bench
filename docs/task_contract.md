# Task Contract Specification

This document defines the contract that all benchmark tasks must follow. It complements the evaluation contract (defined separately) and covers the task environment, resources, and artifact interface.

## 1. Task Environment

### 1.1 Dev Environment
**Summary**: Shared container for appsci-agent and artifact-agent (temporary simplification)

The dev environment is a container-based playground accessible to the appsci-agent during the **build phase**.

**Rationale**: The appsci-agent needs a sandboxed environment to explore data, prototype solutions, and build the artifact. By providing a pre-configured container with all necessary dependencies and data, we reduce setup friction and ensure reproducibility across runs.

**Current simplification**: Both appsci-agent and artifact-agent share this environment temporarily. This allows us to iterate on agent design and code generation before introducing environment isolation.

**Contract**:
```yaml
environment:
  dev:
    base_image: string           # e.g., "python:3.11-slim"
    resources_doc: string        # path to RESOURCES.md describing available resources
    setup_script: string         # optional script to run on container start
    working_dir: string          # default: /work
    output_dir: string           # where artifacts are written, default: /work/out
```

### 1.2 Prod Environment

The prod environment is the runtime environment for the artifact-agent after build completes.

**Rationale**: In production, the artifact should run in isolation from build-time tooling. This separation ensures the artifact is self-contained and deployable.

**Current status**: Deferred. The appsci-agent will eventually be responsible for specifying/creating this environment. If this proves too complex, we'll introduce isolated sandboxes within the dev environment.

**Contract**: TBD

## 2. Resources
**Summary**: Resources two-tier model
  - scope: agent - mounted in task environment, visible to agents
  - scope: internal - only for the appsci-agent-user-sim/evaluators (e.g., validation_rules, fault_types)

Tasks define two resource scopes to support the separation between what agents can access and what the evaluation/appsci-agent-user-sim infrastructure needs.

**Rationale**: Some resources (e.g., validation rules, ground-truth labels) must be available to user personas and evaluators but hidden from agents to prevent data leakage. A two-tier resource model makes this explicit.

### 2.1 All Resources (Top-Level)

The complete list of resources associated with a task, including those not exposed to agents.

```yaml
resources:
  - name: string               # unique identifier
    type: enum                 # database | knowledge_base | api | docs | sample_data | kpi | other
    location: string           # connection string, file path, or URL
    description: string        # human-readable description
    scope: enum                # agent | internal
```

**Scope values**:
- `agent`: Resource is mounted/accessible in the task environment
- `internal`: Resource is only available to the appsci-agent-user-sim, evaluators, or other infrastructure

### 2.2 Agent-Accessible Resources

The subset of resources available inside the task environment. Derived from top-level resources where `scope: agent`.

**Contract**: Agent-accessible resources are mounted or configured in the dev environment. The `resources_doc` file should only document agent-accessible resources.

## 3. Artifact Interface
**Summary**:
  - Standard OpenAI completions API format
  - Explicit input/output schemas per task
All artifacts must expose a standard interface for evaluation.

**Rationale**: A uniform interface allows the evaluation harness to interact with any artifact without task-specific adapters. The OpenAI completions API format is chosen for its simplicity and wide adoption.

**Contract**:
```yaml
artifact_interface:
  type: openai-completions      # only supported type for now
  endpoint_url: string          # e.g., "http://localhost:8000/v1/completions"
  input_schema:
    type: object
    properties:
      prompt: { type: string }  # the evaluation query
      # additional task-specific input fields
    required: [prompt]
  output_schema:
    type: object
    properties:
      # task-specific output fields
    required: [...]
```

**Example** (order-to-cash):
```yaml
artifact_interface:
  type: openai-completions
  endpoint_url: "http://localhost:8000/v1/completions"
  input_schema:
    type: object
    properties:
      prompt: { type: string }
    required: [prompt]
  output_schema:
    type: object
    properties:
      flagged: { type: boolean }
      fault_type: { type: string, nullable: true }
      reason: { type: string }
    required: [flagged, reason]
```

## 4. Task Runner

**Summary**: spec.yaml `task_runner` config for container lifecycle management

The task runner orchestrates environment setup, data transfer, and container lifecycle. It reads configuration from spec.yaml to know how to bundle data, start containers, and run setup scripts.

**Rationale**: Tasks need a standardized way to declare their container setup requirements. The `task_runner` section in spec.yaml provides this configuration, allowing the benchmark harness to spin up task environments consistently.

**Contract**:
```yaml
task_runner:
  network_name: string          # Docker network name (e.g., "bench-network")
  workspace_name: string        # Container name suffix (e.g., "o2c-workspace")
  bundle:                       # Files to copy into container
    - { src: string, dest: string }   # e.g., { src: seed_data/fixture/, dest: /fixture/ }
  setup_script: string          # Script to run after bundle extraction (e.g., "environment/db/setup.sh")
```

**Example** (order-to-cash):
```yaml
task_runner:
  network_name: bench-network
  workspace_name: o2c-workspace
  bundle:
    - { src: seed_data/fixture/, dest: /fixture/ }
    - { src: environment/data/, dest: /data/ }
    - { src: environment/db/, dest: /scripts/ }
  setup_script: environment/db/setup.sh
```

## 5. Summary: Minimal Spec Structure

The following structure should be captured in each task's `spec.yaml` file (e.g., `tasks/order_to_cash/spec.yaml`):

```yaml
task_id: string
name: string
description: string

environment:
  dev:
    base_image: string
    resources_doc: string
    working_dir: /work
    output_dir: /work/out

resources:
  - { name: ..., type: ..., location: ..., description: ..., scope: agent }
  - { name: ..., type: ..., location: ..., description: ..., scope: internal }

artifact_interface:
  type: openai-completions
  endpoint_url: string
  input_schema: { ... }
  output_schema: { ... }

user_personas:
  - path/to/l3.yaml
  - path/to/l2.yaml
  - path/to/l1.yaml

evaluation_data:
  dataset: path/to/samples.jsonl
  provenance: synthetic | public | curated
  metrics: [...]

owner: string
co_owners: [...]
```
