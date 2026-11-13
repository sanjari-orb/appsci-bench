"""The banking BUILD-phase share must be loadable, closed, disjoint — and actually wired in.

WHY THIS FILE EXISTS at all, given `docs/task_env_carve_spec.md` says a task owner writes data and
scripts and never test code. That principle rests on the repo-wide guard in `test_carve_guards.py`,
which checks three invariants over whatever the two phases declare. It cannot check the fourth thing
that decides whether this change measures anything — that the store stopped being baked into the image
and that the phase declarations differ — and it cannot check anything about the service that loads a
phase's data. Those are what this file adds, alongside the properties the guard's own functions cover.

The failure this file is aimed at is the one both airline's and banking's notes single out as the worst
of the partial states: the carved files exist, the checker reports a clean split, CI is green, the
container is healthy — **and the agent reads the full store anyway**, because the image still bakes it.
Everything says finished; nothing measures anything. There is no runtime symptom, so a test is the only
place it can be caught.

The second-worst is here too: the environment package's default store path deciding which records the
golden replay computes against. Point it at the build share and every state check fails with nothing in
the score to say why — indistinguishable from an artifact that answered wrongly.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import pathlib
import sys as _sys

import pytest
import yaml
from fastapi.testclient import TestClient

from appsci_bench.core.spec import TASKS_DIR
from tasks.banking_support.environment import (
    BUILD_STORE_PATH,
    EVAL_STORE_PATH,
    BankingEnvironment,
    _default_db_path,
)
from tasks.banking_support.environment.data_model import TransactionalDB
from tasks.banking_support.environment.service import create_app

# The carve guard lives in `scripts/` -- a runnable tool, not part of the importable package -- so it is
# loaded by path, the same way tests/appsci_bench/core/test_carve_guards.py finds it.
_GUARD = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "carve_guard.py"
_spec = _ilu.spec_from_file_location("_carve_guard_for_banking_build_share", _GUARD)
cg = _ilu.module_from_spec(_spec)
# Registered before exec: the module defines a dataclass, and @dataclass resolves type hints through
# sys.modules[cls.__module__], which raises if the module is absent from it.
_sys.modules[_spec.name] = cg
_spec.loader.exec_module(cg)

_TASK = TASKS_DIR / "banking_support"
_ENV_DIR = _TASK / "environment"
_MANIFEST = "expected_counts.json"

#: What the strict carve leaves. Asserted rather than derived, because the number IS the owner's
#: decision: 8 records, from withholding everything the 97 cases touch, adding the two records a case is
#: about only in its prose, and closing over the reference graph. A share that grew means the withholding
#: rule was relaxed, or the by-eye seed stopped being consulted; one that shrank means a case now reaches
#: further. Either way a human should look before this number is edited.
_EXPECTED_RESIDUE = {"users": 6, "accounts": 2}
_EXPECTED_CARVED_TOTAL = 8

#: What the authored SYNTHETIC augmentation adds on top of that residue
#: (`scripts/banking_build_synthetic.py`). Spelled out here for the same reason `_EXPECTED_RESIDUE` is:
#: the generator is what is under test.
#:
#: THIS NUMBER IS EDITABLE AND THE ONE ABOVE IS NOT, which is the whole reason they are two constants.
#: The residue is the withholding rule's output, so a change there is a possible leak and wants a human.
#: These are invented records meant to be added to, so a change here is a data-quality question -- update
#: it, and the counts in environment/build/README.md and KNOWN_ISSUES.md section 6 with it.
_EXPECTED_SYNTHETIC = {
    "accounts": 11,
    "bank_account_transaction_history": 29,
    "cash_back_disputes": 1,
    "credit_card_account_flags": 1,
    "credit_card_accounts": 7,
    "credit_card_applications": 1,
    "credit_card_closure_reasons": 2,
    "credit_card_orders": 2,
    "credit_card_transaction_history": 18,
    "credit_limit_increase_requests": 2,
    "debit_card_orders": 1,
    "debit_cards": 8,
    "payment_history": 7,
    "referrals": 6,
    "transaction_disputes": 1,
    "verification_history": 3,
}
_EXPECTED_TOTAL = _EXPECTED_CARVED_TOTAL + sum(_EXPECTED_SYNTHETIC.values())

#: The two records the derivation would keep and the by-eye pass withheld, with the case each one belongs
#: to. Spelled out here rather than imported from the generator on purpose: the generator is the thing
#: under test, so a test that read its constant would agree with it however the constant was edited.
#: `scripts/generate_banking_build_share.py`'s WITHHELD_BY_EYE carries the full reasoning.
_WITHHELD_BY_EYE = {
    ("users", "125"): "tau2_banking_34",
    ("credit_card_applications", "f1a2b3c4d5e6f789"): "tau2_banking_10",
}

#: Everything that leaves with customer `125` once she is withheld: her two bank accounts and her debit
#: card. Listed so the pin below is on the OUTCOME rather than on the mechanism -- the generator seeds her
#: into the closure and lets the incoming-reference rule carry these out, and this is what checks that the
#: rule actually did it rather than that the code says it should.
_CASCADED_WITH_125 = {("accounts", "03"), ("accounts", "04"), ("debit_cards", "dbc_7a3f9c2b1e")}


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def build() -> dict:
    return _load(BUILD_STORE_PATH)


@pytest.fixture(scope="module")
def full() -> dict:
    return _load(EVAL_STORE_PATH)


@pytest.fixture(scope="module")
def cases() -> list[pathlib.Path]:
    found = sorted((_TASK / "eval_data").rglob("sample.json"))
    assert len(found) == 97, f"expected 97 eval cases, found {len(found)}"
    return found


@pytest.fixture(scope="module")
def eval_env() -> BankingEnvironment:
    """The full store through the real environment code, loaded once (it also builds the retrieval
    indexes over ~700 documents, which is not worth paying per test)."""
    return BankingEnvironment.from_fixture()


def _every_value(node, out: set) -> None:
    """Every scalar a document holds, decoding JSON carried inside a string as it goes.

    The decoding is not an optimisation. A banking case keeps its whole oracle as a JSON string, and
    inside it 522 of the 955 golden actions carry the arguments of the tool they wrap as a SECOND JSON
    string. A walker that stops at the first string sees the oracle as one opaque token and finds no
    record named by any case -- so every disjointness assertion below would pass vacuously.
    """
    if isinstance(node, dict):
        for value in node.values():
            _every_value(value, out)
    elif isinstance(node, list):
        for value in node:
            _every_value(value, out)
    elif isinstance(node, str):
        out.add(node)
        if node.strip()[:1] in "[{":
            try:
                _every_value(json.loads(node), out)
            except ValueError:
                pass
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.add(str(node))


def _oracle(path: pathlib.Path) -> dict:
    return json.loads(_load(path)["expected_output"]["json"])


def _as_records(store: dict) -> dict[str, list[dict]]:
    """The shape the carve guard's pure checks take: collection name -> list of records."""
    return {name: list(wrapper["data"].values()) for name, wrapper in store.items()}


def _keys(store: dict) -> set[tuple[str, str]]:
    return {(name, key) for name, wrapper in store.items() for key in wrapper["data"]}


# --- it exists, it is the agreed size, and it is the same shape as the full store ----------------

def test_the_build_share_is_the_strict_carves_residue_plus_authored_records(
        build: dict, full: dict) -> None:
    """The build store is two things, and they are asserted separately.

    THE SPLIT IS RE-DERIVED HERE RATHER THAN TAKEN ON TRUST: a record whose key exists in the full store
    came from the carve, and one whose key does not is authored. That is not just a convenient
    discriminator -- it independently re-checks the augmentation's first rule, that no synthetic record
    reuses an id the eval environment already owns. If a synthetic record ever did, it would count as
    carved here and the residue assertion would fail, which is the right alarm.
    """
    assert set(build) == set(full), "the build share must hold the same collections as the full store"

    carved = {name: sum(1 for key in wrapper["data"] if key in full[name]["data"])
              for name, wrapper in build.items()}
    authored = {name: sum(1 for key in wrapper["data"] if key not in full[name]["data"])
                for name, wrapper in build.items()}

    assert {name: n for name, n in carved.items() if n} == _EXPECTED_RESIDUE
    assert sum(carved.values()) == _EXPECTED_CARVED_TOTAL
    assert {name: n for name, n in authored.items() if n} == _EXPECTED_SYNTHETIC
    assert sum(len(w["data"]) for w in build.values()) == _EXPECTED_TOTAL
    assert sum(len(w["data"]) for w in full.values()) == 768
    # Before the augmentation, 15 of the 17 collections were empty -- the fact every cost statement about
    # this carve rested on, and what made category coverage unsatisfiable. None is empty now, which is why
    # ten of that waiver's eighteen enumerations could be dropped from the spec.
    assert sum(1 for w in build.values() if not w["data"]) == 0


def test_the_field_documentation_came_across(build: dict, full: dict) -> None:
    """The agent exploring the build environment reads this prose AS the schema, and with 8 records it is
    most of what is left to learn. A carve that copied the records and dropped the notes would cost real
    information while leaving every invariant green, which is why the guard reports on it."""
    for name, wrapper in build.items():
        assert wrapper["notes"] == full[name]["notes"], f"{name}: documentation altered or dropped"
    assert sum(1 for w in build.values() if w["notes"].strip()) == 15


# --- it loads, and it survives the per-case copy -------------------------------------------------

def test_the_share_loads_through_the_real_environment_code(eval_env: BankingEnvironment) -> None:
    """The model forbids extra fields, so a share with a stray key would fail here rather than in the
    container. `fresh()` is the per-case isolation primitive the between-case reset rebuilds from: a
    store that could not round-trip through it would break resets rather than failing at load."""
    env = eval_env.with_store(TransactionalDB.load(BUILD_STORE_PATH))
    assert env.store_counts()["users"] == 6
    assert env.fresh().state_hash() == env.state_hash()
    # and a read against a mostly-empty store answers rather than raising
    env.execute("unlock_discoverable_agent_tool",
                {"agent_tool_name": "get_all_user_accounts_by_user_id_3847"})
    answer = env.execute("call_discoverable_agent_tool", {
        "agent_tool_name": "get_all_user_accounts_by_user_id_3847",
        "arguments": json.dumps({"user_id": "123"})})
    assert "Bank Accounts" in answer


def test_a_read_against_a_collection_the_carve_emptied_answers_rather_than_raising(
        eval_env: BankingEnvironment) -> None:
    """Fifteen of the seventeen collections are empty in the build share, so nearly the whole tool surface
    queries nothing. That has to read as "no records" and not as a broken environment, or the agent spends
    its build phase debugging our carve."""
    env = eval_env.with_store(TransactionalDB.load(BUILD_STORE_PATH))
    env.execute("unlock_discoverable_agent_tool", {"agent_tool_name": "get_payment_history_6183"})
    answer = env.execute("call_discoverable_agent_tool", {
        "agent_tool_name": "get_payment_history_6183",
        "arguments": json.dumps({"account_id": "01"})})
    assert isinstance(answer, str) and answer, "an empty collection must still produce an answer"


# --- the three carve invariants ------------------------------------------------------------------

def test_the_share_is_referentially_closed(build: dict, full: dict) -> None:
    """Via the guard's own check, so the test and CI agree on what a reference is."""
    result = cg.check_closure(_as_records(build), _as_records(full))
    assert result.ok, str(result)


def test_no_kept_record_points_at_a_withheld_one_on_any_edge(build: dict, full: dict) -> None:
    """The same property again, but walked over the store's own 17 reference edges rather than through
    the guard's field-name inference -- which finds a key for only 3 of banking's 17 collections and so
    could not see most of them. Values are matched whole, so a customer keyed `123` cannot be
    "referenced" by a zip code that contains those digits.
    """
    owners: dict[str, set[str]] = {}
    for name, wrapper in full.items():
        for key in wrapper["data"]:
            owners.setdefault(key, set()).add(name)
    kept = _keys(build)

    edges: set[tuple[str, str]] = set()
    for name, wrapper in build.items():
        for key, record in wrapper["data"].items():
            values: set = set()
            _every_value(record, values)
            for value in values - {key}:
                for other in owners.get(value, ()):
                    if other == name:
                        continue
                    edges.add((name, other))
                    assert (other, value) in kept, (
                        f"{name}[{key}] references {other}[{value}], which the carve withheld")
    # the share is small, but it must not be so small that this check has nothing to walk
    assert edges, "no reference was followed at all -- the closure check would be vacuous"


def test_no_record_any_eval_case_names_is_in_the_share(build: dict, cases) -> None:
    """The leak the split exists to close, on whole values in both directions.

    Whole-value matching rather than substring is what makes this both sharp and quiet. Three of the
    kept customers are keyed `123`, `125` and `126`, and a substring scan of the case files "finds" two
    of them inside a zip code, an income and several transaction ids -- the error behind the previously
    recorded figure of 34 touched customers against the true 32.
    """
    kept = _keys(build)
    named: set = set()
    for path in cases:
        _every_value(_load(path), named)
    assert named, "no values extracted from the eval cases -- the check would be vacuous"
    leaked = sorted((name, key) for name, key in kept if key in named)
    assert not leaked, f"the build share keeps records the eval cases name: {leaked}"


def test_the_two_records_an_eval_case_names_only_in_prose_are_absent(build: dict, cases) -> None:
    """The one leak no automated derivation over this data can find, pinned by hand.

    Every other disjointness check here matches record keys as whole VALUES, and that rule is not
    negotiable: banking keys three customers `123`, `125` and `126`, so a substring scan "finds" two of
    them inside a zip code, an income and several transaction ids. Whole-value matching is what makes those
    checks trustworthy -- and it is exactly why an identity a case claims in ENGLISH is invisible to them.

    Two records are named that way, and both were withheld on the task owner's decision after a human read
    the finished carve. `tau2_banking_34` opens "You are Ayan Wang, a Rho-Bank customer (user_id: 125)", and
    that sentence is the only place `125` occurs in the file. `tau2_banking_10` is about Wei Chen, and card
    application `f1a2b3c4d5e6f789` carries her name in `customer_name` and no customer id at all, so
    referential closure has no edge to follow to it even with her customer row gone.

    Nothing else in this file or in scripts/carve_guard.py would notice their return. A regeneration that
    dropped the hand-written seed -- a refactor of the seed plumbing, a merge that lost the constant, someone
    "simplifying" the generator -- would restore records two eval cases are ABOUT, and every check would
    stay green. So this test pins the outcome, and it names the cases so the next reader can re-derive the
    decision instead of taking it on trust.

    The prose claim is verified rather than asserted: the test reads the case file and requires the
    identifying phrase to still be there. If a case is ever rewritten to name its customer as a structured
    value, the derivation will see it on its own and this pin becomes redundant -- but the failure here will
    say so, rather than the pin silently guarding nothing.
    """
    kept = _keys(build)
    by_case = {path.parent.name: path for path in cases}

    for (collection, key), case_id in sorted(_WITHHELD_BY_EYE.items()):
        assert (collection, key) not in kept, (
            f"{collection}[{key}] is back in the build share, but eval case {case_id} is ABOUT it and no "
            f"derivation over this data can see that -- it is named in the case's prose, not as a value. "
            f"See WITHHELD_BY_EYE in scripts/generate_banking_build_share.py before changing this.")
        assert case_id in by_case, f"eval case {case_id} is gone, so this pin no longer guards anything"

    # the prose that makes each one invisible to the derivation, still present in its case
    assert "You are Ayan Wang, a Rho-Bank customer (user_id: 125)" in \
        by_case["tau2_banking_34"].read_text(), (
            "tau2_banking_34 no longer names customer 125 in prose -- re-derive whether the withhold is "
            "still needed, and whether the derivation can now see it as a value")
    assert "Wei Chen" in by_case["tau2_banking_10"].read_text(), (
        "tau2_banking_10 no longer names Wei Chen, whose name is the only link to the card application")

    # ...and everything that leaves WITH customer 125 is gone too. This is the half that would break
    # silently: withholding her by deleting her row from the finished share, rather than seeding her into
    # the closure, would leave these three pointing at a customer the build environment does not hold --
    # a dangling pointer the agent reads as the schema.
    assert not (_CASCADED_WITH_125 & kept), (
        f"customer 125 is withheld but {sorted(_CASCADED_WITH_125 & kept)} stayed -- she was removed from "
        f"the carve's OUTPUT instead of seeded into its closure, so the share now has dangling references")


def test_no_identifier_token_from_an_eval_case_appears_anywhere_in_the_share(build: dict, cases) -> None:
    """Stronger than key disjointness: an identifier a case NAMES must not appear in any field of any
    kept record, not just as a key. Uses the guard's own token heuristic so both agree on what counts as
    an identifier -- and narrowed to tokens the full store actually uses as identifiers, because shape
    alone cannot tell a key from an attribute that happens to contain a digit.
    """
    known = cg.store_identifiers(_as_records(_load(EVAL_STORE_PATH)))
    wanted: set[str] = set()
    for path in cases:
        wanted |= cg.identifier_tokens(path.read_text()) & known
    assert wanted, "no eval-case identifier matched the store -- the check would be vacuous"
    blob = json.dumps(build)
    assert not sorted(t for t in wanted if t in blob)


# --- the properties the guard cannot express ------------------------------------------------------

def test_no_kept_record_is_pre_seeded_with_state_a_golden_action_creates(
        eval_env: BankingEnvironment, build: dict, cases) -> None:
    """Replays all 97 cases and asserts two things about the records the share keeps.

    A kept record must not be one a golden action CHANGES -- otherwise a scored record is in the build
    share and the naming pass missed it. And a kept record must not be keyed the same as one a golden
    action MINTS -- otherwise a live creation is indistinguishable from the starting state, which is the
    banking analogue of pre-seeding a cancelled booking with the marker `cancel` adds.

    Both hold today for a reason worth knowing: every base record the golden actions mutate is also
    named by its own case, so deriving the withheld set from naming alone is sufficient. A future case
    that mutates a record it never names would break that silently, and this is what catches it.
    """
    kept = _keys(build)
    before = eval_env.db.model_dump()
    changed: set[tuple[str, str]] = set()
    created: set[tuple[str, str]] = set()
    for path in cases:
        oracle = _oracle(path)
        actions = oracle.get("actions") or []
        env = eval_env.fresh()
        env.set_read_log_allowlist(env.read_log_allowlist_for(actions))
        env.seed(oracle.get("initial_state"))
        env.apply_actions(actions)
        after = env.db.model_dump()
        for name, rows in before.items():
            live = after[name]["data"]
            for key, record in rows["data"].items():
                if key not in live or live[key] != record:
                    changed.add((name, key))
            created |= {(name, key) for key in live if key not in rows["data"]}
    assert changed and created, "the replay changed or created nothing -- the check would be vacuous"
    assert not sorted(changed & kept), f"the golden actions change kept records: {sorted(changed & kept)}"
    assert not sorted(created & kept), f"the share keys records the actions mint: {sorted(created & kept)}"


def test_every_carved_record_is_byte_identical_to_the_full_stores_copy(build: dict, full: dict) -> None:
    """A carve COPIES; it never reformats. This is where a monetary value would change shape.

    The store carries an annual income as the float `125000.0`, and the state hash is taken over a
    canonical serialization of the whole store -- so a value that came through as `125000` would hash
    differently while looking numerically identical, and any case seeded from that record would fail on
    a difference no reader could see. Comparing the serialized record rather than field-by-field covers
    every number, string and nesting level at once.

    SCOPED TO THE CARVED RECORDS, which is what "kept" now means: an authored synthetic record has no
    counterpart in the full store to be identical to. The authored records get the equivalent guarantee
    from the generator's `validate_synthetic`, which checks their internal consistency instead -- that
    each is keyed by its own id, that its dates respect the environment's frozen clock, and that any
    customer detail it restates matches the carved customer row it hangs off.
    """
    for name, wrapper in build.items():
        for key, record in wrapper["data"].items():
            if key not in full[name]["data"]:
                continue                      # authored, not carved
            assert json.dumps(record, sort_keys=True) == \
                   json.dumps(full[name]["data"][key], sort_keys=True), f"{name}[{key}] was reformatted"
    # THE CONCRETE EXAMPLE IS BACK, from the other side. The record that motivated this check -- a card
    # application carrying an annual income as the float 125000.0 -- is one of the two the by-eye pass
    # withheld, so for a while the residue held no float at all and the check was general but
    # unexercised. The authored bank transactions restore it: `amount` is a float there, deliberately,
    # because that is the shape the full store uses for that one field while every other monetary value
    # in this schema is a formatted string. An agent that read the build store and concluded amounts are
    # strings would be wrong about the collection it will be scored against, so the float has to survive
    # the round trip -- and it is the state hash that would silently move if it did not.
    floats = [(name, key, field) for name, w in build.items() for key, r in w["data"].items()
              for field, v in r.items() if isinstance(v, float)]
    assert {(name, field) for name, _, field in floats} == {
        ("bank_account_transaction_history", "amount"),
        ("credit_card_applications", "annual_income"),
    }, "the set of float-valued fields moved -- see the comment above before updating this"
    assert build["accounts"]["data"]["02"]["current_holdings"] == "129.02"


def test_the_required_reads_derivation_is_the_same_on_both_stores(
        eval_env: BankingEnvironment, cases) -> None:
    """It decides which discoverable READ calls are logged into the hashed table, and the live
    environment and the golden replay each derive it -- from different stores, now that the phases
    differ. If it ever became data-dependent the two sides would disagree, and a PERFECT artifact would
    score zero because one side logged reads the other never did.
    """
    build_env = eval_env.with_store(TransactionalDB.load(BUILD_STORE_PATH))
    derived = 0
    for path in cases:
        actions = _oracle(path).get("actions") or []
        mine = build_env.read_log_allowlist_for(actions)
        assert mine == eval_env.read_log_allowlist_for(actions), f"{path.parent.name}"
        derived += len(mine)
    assert derived, "no case derived a required read -- the check would be vacuous"


# --- the wiring, which is what makes any of the above measure anything ---------------------------

def test_the_default_store_path_resolves_to_the_eval_copy() -> None:
    """The second-worst failure in the whole change. The scoring code loads a base environment with no
    path argument and replays golden actions against it on the host, while the container loads whatever
    its phase bundled. If this default resolved to the build share, every state check would fail with no
    error and nothing in the score to say why -- reading exactly like an artifact that answered wrongly.
    """
    assert _default_db_path() == EVAL_STORE_PATH
    assert EVAL_STORE_PATH.parent == _ENV_DIR / "eval" / "data"
    assert BUILD_STORE_PATH.parent == _ENV_DIR / "build" / "data"
    assert BankingEnvironment.from_fixture().store_counts()["users"] == 39


def test_the_image_bakes_no_transactional_records() -> None:
    """The worst partial state: the carved files exist, the checker reports a clean split, CI is green,
    the container is healthy -- and the agent reads the full store anyway, because the image copied it
    in. Both phases run from one image, so anything baked is present in the BUILD container whatever was
    bundled. There is no runtime symptom, which is why this is a test.

    The corpus is deliberately exempt: it is shared whole with both phases, so baking it leaks nothing.
    """
    dockerfile = (_ENV_DIR / "Dockerfile").read_text()
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY")]
    assert copies, "no COPY lines found -- this check would be vacuous"
    for line in copies:
        assert "db.json" not in line, f"the image bakes the store in: {line}"
        assert "/data/" not in line, f"the image bakes a phase's data directory in: {line}"
    assert "ENV BANKING_DB_PATH" not in dockerfile, (
        "BANKING_DB_PATH is set in the image, so the service reads a store at start-up -- before the "
        "phase's bundle has arrived")
    assert "BANKING_DOCS_PATH" in dockerfile, "the shared corpus must still be baked in and pointed at"

    compose = yaml.safe_load((_ENV_DIR / "docker-compose.yml").read_text())
    env = compose["services"]["workspace"]["environment"]
    assert "BANKING_DB_PATH" not in env, "compose sets a start-up store path; the service must boot empty"
    assert env.get("BANKING_DOCS_PATH"), "the shared corpus path must stay set"


def test_the_two_phases_declare_different_data_and_one_shared_loader() -> None:
    """A carve that exists on disk while both phases load the same thing looks finished and measures
    nothing -- and the repo-wide guard SKIPS a task whose phases declare the same sources, so it would
    not say so either."""
    spec = yaml.safe_load((_TASK / "spec.yaml").read_text())
    phases = spec["task_runner"]["environments"]
    sources = {p: sorted(e["src"] for e in phases[p]["bundle"]) for p in ("build", "eval")}
    assert sources["build"] != sources["eval"], "both phases still bundle the same sources"
    assert "environment/build/data/" in sources["build"]
    assert "environment/eval/data/" in sources["eval"]
    # one script, both phases: it loads whatever it finds at the fixed path, so it never learns which
    # phase it is in -- which is what keeps the phase difference entirely in the data.
    assert phases["build"]["setup_script"] == phases["eval"]["setup_script"] == "/scripts/setup.sh"
    for phase in ("build", "eval"):
        assert "environment/setup/" in sources[phase]
    assert cg.has_split("banking_support", TASKS_DIR), "the carve guard still reads this task as unsplit"


@pytest.mark.parametrize("phase", ["build", "eval"])
def test_each_phase_bundles_a_manifest_that_matches_what_the_service_will_report(
        phase: str, eval_env: BankingEnvironment) -> None:
    """The setup script's assertion is only as good as the manifest it asserts against, and a manifest
    is easy to leave stale.

    An EXACT per-collection manifest rather than the carve spec's "every collection loaded a non-zero
    count", because that check cannot be written for this task: 21 of the 23 collections a manifest lists
    are legitimately empty in the build share -- 15 of the 17 the store file carries, plus six the data
    model declares and this store never uses -- so a non-zero check would fail on a correct carve. A
    total-only check has the opposite defect and would pass a badly truncated store.
    """
    data_dir = _ENV_DIR / phase / "data"
    expected = _load(data_dir / _MANIFEST)
    assert all(isinstance(v, int) for v in expected.values()), (
        "the manifest must be a flat mapping of collection to integer -- anything richer is read as "
        "records by scripts/carve_guard.py")
    reported = eval_env.with_store(TransactionalDB.load(data_dir / "db.json")).store_counts()
    assert reported == expected, f"{phase}: the manifest disagrees with what the service would report"
    assert sum(expected.values()) == (_EXPECTED_TOTAL if phase == "build" else 768)


def test_the_setup_script_refuses_a_phase_that_bundled_nothing() -> None:
    """Silence is the failure worth being loud about: the service is healthy while holding no records,
    so without this the phase runs to completion against an empty store and reports itself fine."""
    script = (_ENV_DIR / "setup" / "setup.sh").read_text()
    assert "/admin/load" in script, "the setup script does not load this phase's store at all"
    assert "expected_counts.json" in script, "the setup script asserts against no manifest"
    assert "rm -rf" in script, (
        "the raw store is left readable on the container filesystem, which is a second, unaudited path "
        "to the records the tool API is supposed to be the only way to")
    assert "BANKING_ENV_ADMIN_TOKEN" in script and "exit 1" in script


# --- the load route itself ------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("BANKING_DB_PATH", raising=False)
    monkeypatch.setenv("BANKING_ENV_TOKEN", "tool-token")
    monkeypatch.setenv("BANKING_ENV_ADMIN_TOKEN", "admin-token")
    return TestClient(create_app())


def _admin(client: TestClient, method: str, path: str, **kw):
    return getattr(client, method)(path, headers={"Authorization": "Bearer admin-token"}, **kw)


def test_the_service_boots_with_the_corpus_and_no_records(client: TestClient) -> None:
    """Booting empty is what makes which records exist a per-phase decision: the bundle arrives after
    the container is running, so a start-up read could only serve a baked-in store. The corpus is the
    other way round -- shared whole, baked in, present from the first request."""
    tool = {"Authorization": "Bearer tool-token"}
    assert client.get("/health", headers=tool).json() == {"status": "ok"}
    # The corpus is no longer served raw, so its presence is asserted the way the artifact actually
    # reaches it: a search must return real hits from the baked knowledge base.
    hits = client.post("/tools/KB_search_bm25", headers=tool,
                       json={"arguments": {"query": "credit card annual fee"}})
    assert hits.status_code == 200 and "doc_" in str(hits.json()["result"])
    counts = _admin(client, "post", "/admin/load",
                    json={"path": str(BUILD_STORE_PATH)}).json()["counts"]
    # the pre-load state is observable only through the load's own report of what changed, so assert it
    # by loading the SHARE and finding exactly the share -- a merge would show 39 users, not 6
    assert counts["users"] == 6


def test_the_load_route_replaces_both_the_pristine_and_the_live_copy(client: TestClient) -> None:
    """The between-case reset rebuilds the live copy from the pristine one, so a load that replaced only
    the live copy -- or merged into the pristine one -- would make every reset restore boot-time state
    instead of this phase's, and the second eval case onward would run against an empty store."""
    _admin(client, "post", "/admin/load", json={"path": str(EVAL_STORE_PATH)})
    loaded = _admin(client, "get", "/admin/state_hash").json()["state_hash"]
    _admin(client, "post", "/admin/reset")
    assert _admin(client, "get", "/admin/state_hash").json()["state_hash"] == loaded, (
        "a reset after a load did not return to the loaded store")


def test_a_second_load_replaces_rather_than_merges(client: TestClient) -> None:
    """Idempotence, and the property that makes "replace" checkable: loading the full store and then the
    share must leave the share, not their union.

    Read on a collection where the two phases differ by a LOT rather than on one where the build share is
    empty: the full store holds 38 credit card accounts and the build share 7, so a merge shows up as 45
    and cannot be mistaken for either side. This used to read `== 0`, which stopped being a merge test the
    moment the build share gained records of its own -- a union would have satisfied it too."""
    _admin(client, "post", "/admin/load", json={"path": str(EVAL_STORE_PATH)})
    counts = _admin(client, "post", "/admin/load",
                    json={"path": str(BUILD_STORE_PATH)}).json()["counts"]
    assert counts["users"] == 6 and counts["credit_card_accounts"] == 7
    assert sum(counts.values()) == _EXPECTED_TOTAL


def test_a_load_leaves_the_per_case_action_trace_alone(client: TestClient) -> None:
    """Separate state, and the only way the nine ACTION-basis cases are scorable: their reward matches
    the golden action trajectory, which no state hash can show. Clearing it belongs to the between-case
    reset, not to a load that happens once before any case runs."""
    tool = {"Authorization": "Bearer tool-token"}
    client.post("/tools/get_current_time", headers=tool, json={"arguments": {}})
    before = _admin(client, "get", "/admin/action_trace").json()["agent_actions"]
    assert len(before) == 1, "the tool call was not traced, so this check would be vacuous"
    _admin(client, "post", "/admin/load", json={"path": str(BUILD_STORE_PATH)})
    assert _admin(client, "get", "/admin/action_trace").json()["agent_actions"] == before
    # ...whereas a reset is exactly where it should be cleared
    _admin(client, "post", "/admin/reset")
    assert _admin(client, "get", "/admin/action_trace").json()["agent_actions"] == []


def test_the_artifact_can_neither_load_records_nor_count_them(client: TestClient) -> None:
    """WHICH RECORDS EXIST is the entire difference between the two phases, so the artifact must reach
    neither the load route nor its counts. Two independent controls: the token stops it calling, and the
    schema omission stops it learning the route is there at all -- a real run had the build agent fetch
    /openapi.json into its workspace, so the schema is a disclosure channel that needs no token.
    """
    tool = {"Authorization": "Bearer tool-token"}
    assert client.post("/admin/load", headers=tool,
                       json={"path": str(BUILD_STORE_PATH)}).status_code == 401
    assert client.post("/admin/load", json={"path": str(BUILD_STORE_PATH)}).status_code == 401
    schema = client.get("/openapi.json")
    assert "/admin/load" not in schema.json()["paths"]
    assert "LoadRequest" not in schema.text


def test_a_missing_or_unreadable_store_is_a_400_the_setup_script_can_abort_on(
        client: TestClient, tmp_path: pathlib.Path) -> None:
    """A phase whose store did not arrive must fail loudly. A 500 would read as the environment being
    broken; a 200 with zero records would read as success and return a sheet of zeros."""
    assert _admin(client, "post", "/admin/load",
                  json={"path": str(tmp_path / "absent.json")}).status_code == 400
    truncated = tmp_path / "half.json"
    truncated.write_text('{"users": {"data": {')
    assert _admin(client, "post", "/admin/load", json={"path": str(truncated)}).status_code == 400
    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text('{"not_a_collection": {"data": {}, "notes": ""}}')
    assert _admin(client, "post", "/admin/load", json={"path": str(wrong_shape)}).status_code == 400


# --- the agent-facing description must hold in BOTH phases ---------------------------------------

def test_no_agent_facing_text_states_a_record_count(build: dict, full: dict) -> None:
    """A description the agent reads must be true of the environment the agent actually meets.

    The agent explores the BUILD environment; the artifact is scored against the EVAL one. Any
    transactional record count in an agent-facing description is therefore false in one of the two
    phases -- and naming the build volume instead would be equally wrong at eval time, and would invite
    an artifact built for a store of eight records.

    Corpus counts are checked too, in approximate as well as exact form, and that is a deliberate
    extension of airline's version of this test rather than a copy of it. Airline matches exact count
    strings, so `~700 official documents` -- which this spec carried, twice -- would have slipped past.
    The corpus is shared whole with both phases, so that phrase was not false in either one, and the
    agent can count the documents itself: the index is served on the tool token by design. It is removed
    anyway, because the honest line is to describe what the corpus contains and never how much of it
    there is, and because a guard that tolerates one approximate count teaches the next task owner that
    counts in descriptions are fine.
    """
    from appsci_bench.core.spec import build_agent_spec, resolve_spec_path

    raw = yaml.safe_load(resolve_spec_path("banking_support").read_text())
    view = build_agent_spec(raw)

    counts = {str(len(w["data"])) for store in (build, full) for w in store.values()}
    counts |= {str(sum(len(w["data"]) for w in store.values())) for store in (build, full)}
    counts |= {"698", "700"}          # the corpus, exactly and as the spec used to round it
    # Single digits are dropped: they are counts here (the share keeps 1 debit card, the full store 1
    # application) but they also appear inside ordinary prose and inside tool names, so keeping them
    # would fail correct text -- and a check that fails correct text is one someone switches off.
    counts -= {"0", "1", "2", "3", "4"}
    assert counts, "no counts derived -- the check would be vacuous"

    agent_facing = [
        *(r.get("description", "") for r in view.get("environment", {}).get("resources", [])),
        *(r.get("location", "") for r in view.get("environment", {}).get("resources", [])),
        *(d.get("description", "") for d in view.get("dataset_files", [])),
        *(d.get("description", "") for d in view.get("technical_docs", [])),
        *(view.get("business_goals") or []),
        view.get("description", ""),
        view.get("interface_design", ""),
    ]
    for text in agent_facing:
        leaked = sorted(c for c in counts if c in (text or ""))
        assert not leaked, (
            f"agent-facing text states record counts {leaked}, which hold in at most one phase: {text!r}")
