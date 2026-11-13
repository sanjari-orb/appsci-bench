"""Slim runtime shim for the vendored tau2 banking domain modules.

The domain code (data_model.py, db_query.py, utils.py, tools.py in this package) is reused
VERBATIM from tau2-bench (see tasks/banking_support/seed_data/upstream_src/), so the DB /
ACTION reward is scored against tau2's own tool logic. Those modules import two small pieces of
tau2's framework -- a pydantic DB base and the ToolKit framework (tool decorators + discoverable
tool registry). We reimplement ONLY the subset they use here, dropping the parts tied to LLM
tool-schema generation (Tool/as_tool/get_tools) and tau2's hashing utils, which the environment
does not need (it executes tools and computes its own canonical state hash).
"""
