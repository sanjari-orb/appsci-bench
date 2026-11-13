"""Banking-support task environment: transactional DB + tau2 tools + raw KB corpus.

The "real resource" the banking artifact acts against at eval time. It reuses tau2-bench's actual
banking_knowledge tool logic (see data_model.py / db_query.py / utils.py / tools.py in this package,
copied verbatim from the vendored upstream) so the DB/ACTION reward is faithful. tau2's retrieval
tools are provided out of the box: KB_search_bm25 / KB_search_dense / shell run over the
document corpus (see _runtime/retrieval.py). Retrieval is READ-only over the corpus -- it never
touches the TransactionalDB -- so state_hash() and the DB/ACTION reward are unaffected by any search.

    env = BankingEnvironment.from_fixture()   # load DB + corpus + build retrieval backends once
    case = env.fresh()                         # isolated DB copy for one eval case (shares backends)
    case.seed(initial_state)                   # apply the sample's per-case DB seed
    case.execute("get_current_time", {})       # agent tool
    case.execute("KB_search_bm25", {"query": "dispute a charge"})   # retrieval tool (read-only)
    case.execute("apply_for_credit_card", {...}, requestor="user")  # user-held tool
    case.retrieved_document_ids                # doc ids returned by KB searches this case
    case.state_hash()                          # DB-state fingerprint for scoring

WHICH TRANSACTIONAL STORE IS THE DEFAULT, AND WHY IT MATTERS MORE THAN IT LOOKS. There are now two
stores -- the carved build share the agent explores and the full store the artifact is scored against
-- and the default below resolves to the FULL one. The scoring code loads a base environment with no
path argument, on the HOST, and replays each case's golden actions against it, while the container
loads whatever its own phase bundled. Point the default at the build share and the replay computes
its expected states from a different set of records than the container ever held: every state check
fails, with no error, no warning and nothing in the score to say why. `docs/task_env_carve_spec.md`
calls that the worst failure in the whole change, and EVAL_STORE_PATH is the thing that prevents it.

THE KNOWLEDGE BASE IS NOT CARVED, and that is a decision rather than an omission. Both phases hold
the same 698 documents, so the corpus stays baked into the image and its resolver keeps resolving one
shared directory. 46 of the 48 specialized tools are named ONLY by documents the eval cases require,
so withholding those documents would not make the task harder, it would make it unsolvable.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib

from .corpus import DocumentCorpus
from .data_model import TransactionalDB
from .tools import KnowledgeUserTools
from ._runtime.retrieval import BankingAgentTools, RetrievalBundle, build_retrieval_bundle
from ._runtime.toolkit import MUTATES_STATE_ATTR

# Actions whose requestor is one of these are executed by the USER toolkit; all others by the AGENT.
_USER_REQUESTORS = {"user", "customer"}

#: The EVAL store: the full 768-record store the 97 eval cases are drawn from and the scoring code
#: replays their golden actions against. Resolved relative to THIS file so the package needs no
#: repo-root anchor -- the task-env container ships only this package (no appsci_bench, no shared).
#: Nothing bakes it into the image any more, so on the container side this path does not exist at all;
#: it is read on the HOST only, by the scoring code, the tests and the build-share generator. Anything
#: that needs to name the full store must go through this constant rather than spell the path again:
#: airline's generator spelled its own, the store moved under it, and its `--check` mode -- the mode
#: that exists so a reviewer can confirm the carved files still regenerate -- has been crashing since.
EVAL_STORE_PATH = pathlib.Path(__file__).resolve().parent / "eval" / "data" / "db.json"

#: The BUILD share: the 8 records left after withholding everything the eval cases touch, adding the two
#: records a case is about only in prose, and closing over the reference graph -- plus the 100 authored
#: synthetic records that give those customers something to hold (scripts/banking_build_synthetic.py).
#: Named here for the generator and the tests; never the default.
BUILD_STORE_PATH = pathlib.Path(__file__).resolve().parent / "build" / "data" / "db.json"


class BankingEnvironment:
    """A bound (DB + agent tools + user tools + corpus) banking environment for one eval case."""

    def __init__(self, db: TransactionalDB, corpus: DocumentCorpus,
                 bundle: RetrievalBundle | None = None):
        self.db = db
        self.corpus = corpus
        # Retrieval backends (bm25/dense pipelines + the read-only shell driver) are derived from
        # the read-only corpus, so they are built ONCE and shared across every fresh() copy. Passing
        # `bundle` through fresh() avoids re-indexing ~700 docs per eval case.
        self._bundle = bundle if bundle is not None else build_retrieval_bundle(corpus)
        # agent_tools = the transactional/discoverable KnowledgeTools + tau2 retrieval tools.
        self.agent_tools = BankingAgentTools(db, self._bundle)
        self.user_tools = KnowledgeUserTools(db)

    @classmethod
    def from_fixture(cls, db_path=None, docs_path=None) -> "BankingEnvironment":
        db = TransactionalDB.load(str(db_path)) if db_path else _load_default_db()
        return cls(db, DocumentCorpus.load(docs_path))

    @classmethod
    def with_empty_store(cls, docs_path=None) -> "BankingEnvironment":
        """The whole knowledge base and NO transactional records. What the container boots on.

        Both phases run from one image and the harness copies a phase's data in only AFTER the
        container is running, so a service that read its store at process start could only ever serve
        whatever was baked into the image -- which is exactly how a carved build share ends up
        decorative while the agent reads the full store. Booting with no records is what makes which
        records exist a per-phase decision; the phase's setup script loads them.

        The corpus is NOT empty here, and that asymmetry is the point. It is shared whole with both
        phases, so it stays baked in -- which also spares the service the three problems a corpus swap
        would have created: the retrieval bundle refuses to build over an empty corpus, the keyword and
        regex pipelines are built once at construction precisely so ~700 documents are not re-indexed
        per case, and the on-disk embeddings cache is keyed by embedder config rather than by corpus,
        so a swap would have been a silent-correctness question. Nothing swaps, so none of it arises.

        Verified rather than assumed: an empty store constructs cleanly, the retrieval indexes build
        over the baked corpus as they always did, and a read tool against no records answers "no
        accounts found" rather than raising. `/health` answers while the store is empty, deliberately --
        readiness says the process is up, not that it holds data, which is why each phase's setup
        script is what asserts the records actually landed.
        """
        return cls(TransactionalDB(), DocumentCorpus.load(docs_path))

    def with_store(self, db: TransactionalDB) -> "BankingEnvironment":
        """This environment's corpus and retrieval backends, over a DIFFERENT transactional store.

        What the admin load route swaps in. The corpus and the pipelines built over it are read-only
        and identical in both phases, so they are carried across rather than rebuilt -- rebuilding
        would re-index the whole knowledge base on every load for no change in what it contains.
        """
        return BankingEnvironment(db, self.corpus, bundle=self._bundle)

    def store_counts(self) -> dict[str, int]:
        """Records per collection, for the whole model rather than the file.

        What a phase's setup script asserts against its manifest to prove its data actually landed.
        Derived from the model's own collections, not from the loaded file's keys, so a collection the
        file omits reports zero instead of vanishing -- the difference between "no dispute has been
        filed yet" and "the carve lost the disputes table" is exactly what a count check is for.

        Deliberately not `TransactionalDB.get_statistics()`: that method is vendored upstream code and
        counts 19 of the 23 collections, so four of them -- both debit-card collections, the
        bank-account transaction history and the per-case task configuration -- could empty out without
        moving a single number. It also keys one count by a name that is not its collection's, so a
        caller matching on collection names would silently lose the card transaction history too.
        """
        return {name: len(getattr(self.db, name).data) for name in type(self.db).model_fields}

    def fresh(self) -> "BankingEnvironment":
        """New environment over an isolated deep copy of the DB (per-case isolation). Corpus and the
        retrieval backends are read-only, so they are shared; the per-case retrieved-doc-id log
        starts empty on the new BankingAgentTools."""
        return BankingEnvironment(self.db.model_copy(deep=True), self.corpus, bundle=self._bundle)

    @property
    def retrieved_document_ids(self) -> set[str]:
        """Doc ids returned by any KB_search_bm25 / KB_search_dense call in this case.
        Read-only bookkeeping over the corpus (the DB is never touched, so state_hash() is
        unaffected). A per-case set: fresh() starts it empty; the eval harness reads it to measure
        retrieval recall."""
        return self.agent_tools.retrieved_document_ids

    def reset_retrieval_log(self) -> None:
        """Clear the per-case retrieved-doc-id log. fresh() already starts empty, so this is only for
        in-place reuse of a live env instance."""
        self.agent_tools.retrieved_document_ids.clear()

    def set_read_log_allowlist(self, allowlist) -> None:
        """Read-only discoverable tools whose calls should be logged to agent_discoverable_tools
        (derived per-case from the golden trajectory so required reads discriminate while extra
        validation reads don't pollute the DB hash). Mirrors tau2's get_environment(read_log_allowlist=...)."""
        self.agent_tools.set_read_log_allowlist(set(allowlist or []))

    def read_log_allowlist_for(self, actions) -> list[str]:
        """Derive this case's required-reads allowlist from its golden trajectory.

        THE ONE derivation, called by BOTH sides that need it -- the live env (via POST /admin/seed)
        and the golden replay (via the eval function's state-hash helper). That is deliberate and
        load-bearing: the allowlist changes which discoverable READ calls get logged into the hashed
        `agent_discoverable_tools` table, so setting it on one side only makes a PERFECT agent score
        zero, because one side logs required reads the other never does. Sharing the function makes
        the two sides agree by construction instead of by a comment asking future edits to remember.

        A discoverable tool needs allowlisting only when its underlying method does NOT mutate state:
        mutating ones are logged regardless (see the gate in `call_discoverable_agent_tool`), so
        including them would be a no-op. Restricting to the golden trajectory's own read calls is what
        restores upstream's discrimination -- an agent that reaches the right end state without doing
        the reads the task required lands on a different hash, and so scores 0 rather than 1.

        Returns a sorted list (not a set) so the value is JSON-serializable for the seed route and
        deterministic in a trace.
        """
        discoverable = self.agent_tools.get_discoverable_tools()
        required: set[str] = set()
        for action in actions or []:
            if action.get("name") != "call_discoverable_agent_tool":
                continue
            tool_name = (action.get("arguments") or {}).get("agent_tool_name")
            if not tool_name:
                continue
            method = discoverable.get(tool_name)
            if method is None:
                continue                     # names a tool this env doesn't define -- nothing to log
            if not getattr(method, MUTATES_STATE_ATTR, False):
                required.add(tool_name)      # a READ the golden trajectory required
        return sorted(required)

    def seed(self, initial_state: dict | None) -> "BankingEnvironment":
        """Apply a sample's per-case `initial_state` to the DB (best-effort mirror of tau2 set_state):
        merge initialization_data (agent_data/user_data table seeds, e.g. task_config) and replay any
        initialization_actions. `message_history` is conversation seeding, not env state, so ignored.

        The seed is DEEP-COPIED in. Without that, the records inserted here are the caller's own nested
        dicts, and a tool that writes to one writes through into the caller's `initial_state` -- so the
        case's declared starting point silently becomes wherever the previous run of it finished. Seeded
        at $5,000.00, a credit limit came back as $7500.00 with the card marked CLOSED, and the same
        trajectory replayed twice produced two different state hashes. Production happens to be safe
        because every caller re-parses the oracle JSON per call, so each gets a fresh dict; a test, a
        script, or a batch path that reuses one does not, and the resulting non-determinism reads as an
        unwinnable case rather than as a bug. The environment owns its own state.
        """
        if not initial_state:
            return self
        init_data = initial_state.get("initialization_data") or {}
        for section in ("agent_data", "user_data"):
            tables = init_data.get(section) or {}
            if not isinstance(tables, dict):
                continue
            for table_name, tbl in tables.items():
                target = getattr(self.db, table_name, None)
                if target is None or not isinstance(tbl, dict):
                    continue
                records = tbl.get("data", tbl) if isinstance(tbl, dict) else {}
                if isinstance(records, dict):
                    target.data.update(copy.deepcopy(records))
                if isinstance(tbl, dict) and isinstance(tbl.get("notes"), str):
                    target.notes = tbl["notes"]
        self.apply_actions(initial_state.get("initialization_actions") or [])
        return self

    def execute(self, name: str, arguments: dict, requestor: str = "assistant"):
        """Execute a tool by name against the shared DB. `requestor` routes to the user toolkit for
        customer-held tools (apply_for_credit_card, call_discoverable_user_tool, ...) and the agent
        toolkit otherwise. Raises ValueError for an unknown tool (surfaced as 400 by the service)."""
        toolkit = self.user_tools if requestor in _USER_REQUESTORS else self.agent_tools
        return toolkit.use_tool(name, **(arguments or {}))

    def apply_actions(self, actions) -> list[dict]:
        """Replay golden {name, arguments, requestor} actions against the DB, routing by requestor.
        Returns per-action {name, ok, error?}; a failing action is recorded but does not abort the
        replay (mirrors an agent's tool call erroring out)."""
        results = []
        for a in actions or []:
            name = a.get("name")
            requestor = a.get("requestor") or "assistant"
            try:
                self.execute(name, a.get("arguments") or {}, requestor=requestor)
                results.append({"name": name, "ok": True})
            except Exception as e:  # noqa: BLE001 - a failed action is a graded outcome, not a crash
                results.append({"name": name, "ok": False, "error": repr(e)})
        return results

    def state_hash(self) -> str:
        """Canonical sha256 over the full transactional DB (order-independent, sort_keys). Applying
        the golden actions vs the agent's actions to identical fresh+seeded DBs and comparing this
        hash IS the tau2 DB check."""
        blob = json.dumps(self.db.model_dump(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def _load_default_db() -> TransactionalDB:
    return TransactionalDB.load(str(_default_db_path()))


def _default_db_path() -> pathlib.Path:
    # The EVAL store. See EVAL_STORE_PATH: the scoring code reaches this by loading with no path
    # argument, so what this resolves to decides which records the golden replay computes against.
    return EVAL_STORE_PATH
