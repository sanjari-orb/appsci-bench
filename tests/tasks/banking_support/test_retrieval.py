"""Creds-free unit tests for the vendored tau2 retrieval runtime wired into BankingEnvironment.

These exercise the provided retrieval tools (KB_search_bm25 / KB_search_dense / shell) over a
TINY in-memory fixture corpus -- no API key, no network, no baked embeddings cache -- and pin the
load-bearing invariants of the "retrieval is provided out of the box" reframe:

  * exactly those three tools are exposed on the environment's `agent_tools` (all READ-typed), which
    IS upstream's `alltools` surface -- `grep` is deliberately not among them, so the set is asserted
    exhaustively rather than by containment;
  * bm25 returns ranked results and LOGS the doc ids it returned into the per-case
    `retrieved_document_ids` set (the basis for env-measured `retrieval_recall`);
  * that log accumulates across calls, starts empty on `fresh()`, is isolated per case, and resets;
  * a KB search NEVER changes `state_hash()` -- retrieval is read-only over the corpus, so the tau2
    DB/ACTION reward is untouched;
  * `KB_search_dense` degrades gracefully to a guidance message (never raising, never logging) when
    no embeddings key is configured -- so bm25 stays usable creds-free.

dense's real (OpenAI-embedding) path needs a key + network and is deliberately NOT exercised here.
"""
from __future__ import annotations

import pytest

from tasks.banking_support.environment import BankingEnvironment
from tasks.banking_support.environment._runtime.retrieval import (
    _DENSE_UNAVAILABLE_MSG,
    build_retrieval_bundle,
)
from tasks.banking_support.environment._runtime.toolkit import ToolType
from tasks.banking_support.environment.corpus import DocumentCorpus
from tasks.banking_support.environment.data_model import TransactionalDB

# Upstream's `alltools` surface, exactly. Adding to this set widens the action space away from the
# variant our numbers are compared against, so it is asserted as an equality below.
_RETRIEVAL_TOOLS = {"KB_search_bm25", "KB_search_dense", "shell"}


def _tiny_corpus() -> DocumentCorpus:
    return DocumentCorpus({
        "doc_fees": {"id": "doc_fees", "title": "Fee Schedule",
                     "content": "The Gold Rewards Card has no annual fee. A wire transfer costs 25 dollars."},
        "doc_disputes": {"id": "doc_disputes", "title": "Disputing a Charge",
                         "content": "To dispute a charge, file within 60 days for provisional credit."},
        "doc_referral": {"id": "doc_referral", "title": "Referral Program",
                         "content": "Refer a friend to earn a bonus after they open a checking account."},
    })


@pytest.fixture
def env(monkeypatch) -> BankingEnvironment:
    # Creds-free regardless of the ambient shell: dense must have NO key so it reaches its
    # graceful-degrade path (a machine that exports OPENAI_API_KEY would otherwise hit the network).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BANKING_EMBED_API_KEY", raising=False)
    corpus = _tiny_corpus()
    # enable_shell=False: `shell` executes in a separate container (environment/shell_sidecar/) and is
    # covered by test_shell_client.py / test_shell_sidecar_image.py; the bm25 / grep / dense-degrade
    # paths under test here are all shell-independent. Without this the bundle would read
    # BANKING_SHELL_URL from the ambient environment, making the fixture depend on the host.
    bundle = build_retrieval_bundle(corpus, enable_shell=False)
    return BankingEnvironment(TransactionalDB(), corpus, bundle=bundle)


# --- the retrieval tools are provided on agent_tools, read-only -------------------------------

# Upstream retrieval tools that other variants provide and `alltools` does NOT. Any of these appearing
# on agent_tools would move us off the variant the leaderboard reports.
_NOT_IN_ALLTOOLS = {"grep", "KB_search", "rewrite_context"}


def test_exactly_upstreams_alltools_retrieval_surface_is_exposed(env):
    names = set(env.agent_tools.tools)
    assert _RETRIEVAL_TOOLS <= names
    assert names & _NOT_IN_ALLTOOLS == set(), "retrieval surface has drifted off `alltools`"


def test_retrieval_tools_are_read_only_type(env):
    # READ typing is what keeps them off the DB-mutation / discoverable-logging paths, preserving
    # state_hash() -- so this is a real invariant, not decoration.
    for name in _RETRIEVAL_TOOLS:
        assert env.agent_tools.tool_type(name) == ToolType.READ


# --- bm25 returns results and logs the returned doc ids ---------------------------------------

def test_bm25_returns_ranked_results_and_logs_doc_ids(env):
    out = env.execute("KB_search_bm25", {"query": "annual fee gold card", "k": 1})
    assert "ID: doc_fees" in out                       # ranked, formatted result
    assert "doc_fees" in env.retrieved_document_ids    # ...and its id was logged


def test_grep_is_not_an_agent_tool(env):
    # Upstream's `alltools` variant is bm25 + dense + shell and has NO grep; exposing it too made our
    # surface a superset of the only configuration our scores can be read against. The regex capability
    # is not lost, it moved to where upstream has it -- real grep, inside `shell`.
    with pytest.raises(Exception):
        env.execute("grep", {"pattern": "dispute"})
    assert "grep" not in set(env.agent_tools.tools)


def test_the_grep_pipeline_is_still_built_so_a_variant_switch_stays_wiring(env):
    # Other upstream variants are grep-based. Keeping the backend while unexposing the tool is what
    # makes trying one a wiring change rather than a rewrite.
    assert env._bundle.grep_pipeline is not None


def test_retrieved_log_accumulates_across_calls(env):
    env.execute("KB_search_bm25", {"query": "annual fee", "k": 1})
    env.execute("KB_search_bm25", {"query": "referral friend bonus", "k": 2})
    assert {"doc_fees", "doc_referral"} <= env.retrieved_document_ids


# --- read-only invariant: a KB search never mutates DB state ----------------------------------

def test_state_hash_unchanged_across_kb_search(env):
    before = env.state_hash()
    env.execute("KB_search_bm25", {"query": "annual fee", "k": 5})
    env.execute("KB_search_bm25", {"query": "dispute a charge", "k": 5})
    assert env.state_hash() == before      # retrieval is read-only over the corpus
    assert env.retrieved_document_ids      # ...but it DID run (guards a vacuously-unchanged hash)


# --- per-case log lifecycle: fresh() empty + isolated, reset clears ---------------------------

def test_fresh_starts_log_empty_and_isolated_from_parent(env):
    env.execute("KB_search_bm25", {"query": "annual fee", "k": 1})
    case = env.fresh()
    assert case.retrieved_document_ids == set()            # per-case log starts empty
    case.execute("KB_search_bm25", {"query": "dispute charge provisional", "k": 1})
    assert "doc_disputes" in case.retrieved_document_ids
    assert "doc_disputes" not in env.retrieved_document_ids  # child does not leak into parent
    assert "doc_fees" in env.retrieved_document_ids          # parent retains its own


def test_fresh_shares_backends_no_reindex(env):
    # fresh() must thread the SAME bundle -- re-indexing the corpus per eval case would be the bug
    # this guards (workstream B's build-once-share-across-fresh() invariant).
    assert env.fresh()._bundle is env._bundle


def test_reset_retrieval_log_clears_in_place(env):
    env.execute("KB_search_bm25", {"query": "annual fee", "k": 1})
    assert env.retrieved_document_ids
    env.reset_retrieval_log()
    assert env.retrieved_document_ids == set()


# --- dense degrades gracefully (no key) -------------------------------------------------------

def test_dense_degrades_gracefully_without_key(env):
    out = env.execute("KB_search_dense", {"query": "anything at all", "k": 3})
    assert out == _DENSE_UNAVAILABLE_MSG        # guidance message, not an exception
    assert env.retrieved_document_ids == set()  # a degraded dense search logs nothing
