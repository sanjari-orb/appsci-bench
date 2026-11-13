"""appsci-bench -- a benchmark that evaluates the process of building a good agent from a vague
customer requirement.

Per task, the benchmark provides requirements (via an appsci agent user sim over the task's user personas)
and an environment to the applied-science agent under test; the agent returns an artifact, which the
benchmark queries and scores against held-out samples + oracle. A run is orchestrated per task via
`python -m appsci_bench.orchestrator.run` (Docker-based). The agent under test lives outside the
benchmark (root-level appsci_agent/). Start at docs/OVERVIEW.md.
"""
__all__ = ["core", "eval"]
