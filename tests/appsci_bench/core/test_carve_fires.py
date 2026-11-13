"""Proof that the carve checks actually fire, and stay quiet when they should.

test_carve_guards.py runs the same checks over the real repo, where they SKIP every task because none has
split yet -- so on its own it would pass with the logic deleted. This file is the other half: each kind of
leak is planted in a synthetic task, and each way of being wrongly noisy is planted too.

Both halves matter. A check that never fires is decoration; a check that fires on a correct carve gets
switched off by the first person it inconveniences.

Two stores, because one shape cannot exercise both sets of rules. The first (bookings and flights) is
airline-shaped: every identifier is long enough to recognise from text. The second (further down, keyed by
bare numbers and documented in prose beside its records) is banking-shaped, and everything about the
structural pass, the schema-documentation report and the coverage waiver is tested on it.
"""
from __future__ import annotations

import json
import pathlib

import yaml

import importlib.util as _ilu
import sys as _sys
import pathlib as _pl

# carve_guard lives in scripts/ -- a runnable tool, not part of the benchmark's importable package
# (nothing in a run calls it; it only ever runs from this suite). Loaded by path, mirroring
# tests/scripts/test_detect_task_leakage.py.
_GUARD = _pl.Path(__file__).resolve().parents[3] / "scripts" / "carve_guard.py"
_spec = _ilu.spec_from_file_location("_carve_guard_under_test", _GUARD)
cg = _ilu.module_from_spec(_spec)
# Registered before exec: the module defines a dataclass, and @dataclass resolves type hints through
# sys.modules[cls.__module__], which raises if the module is absent from it.
_sys.modules[_spec.name] = cg
_spec.loader.exec_module(cg)

# A tiny store: customers own bookings, bookings reference flights. Enough shape for all three checks.
#
# ANN and BOB are ordinary customers. DAN exists for one reason: he is the second bronze-tier customer.
# CAT is the one an eval case scores against, so a correct carve withholds her -- and she is bronze. If
# she were the ONLY bronze customer, withholding her would legitimately break category coverage, and
# there would be no correct carve to demonstrate. That is the coverage check doing its job, and it is why
# a real task owner cannot pick the build share by simply deleting the scored rows.
FL_A = {"flight_number": "FL100", "origin": "AAA", "destination": "BBB", "status": "on_time"}
FL_B = {"flight_number": "FL200", "origin": "BBB", "destination": "CCC", "status": "delayed"}
FLIGHTS = [FL_A, FL_B]

ANN = {"user_id": "ann_lee_0001", "tier": "gold", "bookings": ["BK0001"]}
BOB = {"user_id": "bob_kim_0002", "tier": "silver", "bookings": ["BK0002"]}
CAT = {"user_id": "cat_ray_0003", "tier": "bronze", "bookings": ["BK0003"]}
DAN = {"user_id": "dan_fox_0004", "tier": "bronze", "bookings": ["BK0004"]}

BK_ANN = {"booking_id": "BK0001", "user_id": "ann_lee_0001", "cabin": "economy",
          "flights": [{"flight_number": "FL100"}], "created_at": "2024-05-04T23:12:06"}
BK_BOB = {"booking_id": "BK0002", "user_id": "bob_kim_0002", "cabin": "business",
          "flights": [{"flight_number": "FL200"}], "created_at": "2024-05-05T11:02:44"}
BK_CAT = {"booking_id": "BK0003", "user_id": "cat_ray_0003", "cabin": "business",
          "flights": [{"flight_number": "FL200"}], "created_at": "2024-05-06T08:31:19"}
BK_DAN = {"booking_id": "BK0004", "user_id": "dan_fox_0004", "cabin": "economy",
          "flights": [{"flight_number": "FL100"}], "created_at": "2024-05-07T09:15:02"}


def _store(users, bookings, flights) -> dict:
    return {"users": {u["user_id"]: u for u in users},
            "bookings": {b["booking_id"]: b for b in bookings},
            "flights": {f["flight_number"]: f for f in flights}}


def _task(tmp_path: pathlib.Path, *, build: dict, full: dict, cases: list[dict],
          split: bool = True, coverage_waiver: dict | None = None) -> pathlib.Path:
    """A synthetic task with its own build/eval data directories and eval cases.

    `coverage_waiver` is written verbatim under the spec's carve block, so a test can plant a malformed
    one (no reason, wrong shape) as easily as a good one -- which is the only way to check that a
    half-written waiver leaves coverage hard rather than quietly switching it off.
    """
    task = tmp_path / "demo"
    for phase, store in (("build", build), ("eval", full)):
        d = task / "environment" / phase / "data"
        d.mkdir(parents=True)
        (d / "store.json").write_text(json.dumps(store))
    cases_dir = task / "eval_data"
    for i, case in enumerate(cases):
        c = cases_dir / f"case-{i}"
        c.mkdir(parents=True)
        (c / "sample.json").write_text(json.dumps(case))

    build_src = "environment/build/data/"
    eval_src = build_src if not split else "environment/eval/data/"
    runner: dict = {
        "network_name": "bench-network", "workspace_name": "demo-workspace",
        "environments": {
            "build": {"bundle": [{"src": build_src, "dest": "/fixture/"}],
                      "setup_script": "/scripts/setup.sh"},
            "eval": {"bundle": [{"src": eval_src, "dest": "/fixture/"}],
                     "setup_script": "/scripts/setup.sh"},
        },
    }
    if coverage_waiver is not None:
        runner["carve"] = {"coverage_waiver": coverage_waiver}
    (task / "spec.yaml").write_text(yaml.safe_dump({
        "task_id": "demo",
        "task_runner": runner,
        "evaluation_data": {"dataset": "eval_data"},
    }, sort_keys=False))
    return task


#: A case that scores against the third customer and their booking -- so those are what must be withheld.
CASE = [{"data_point_id": "case-0",
         "multi_turn_input": {"user_persona": "You are Cat Ray. Your user id is cat_ray_0003.",
                              "user_task": "Cancel booking BK0003."},
         "expected_output": {"json": json.dumps({"actions": [
             {"name": "cancel", "arguments": {"booking_id": "BK0003"}}]})}}]

#: The correct carve: the scored customer and her booking withheld, every category still represented.
CLEAN_BUILD = _store([ANN, BOB, DAN], [BK_ANN, BK_BOB, BK_DAN], FLIGHTS)
FULL = _store([ANN, BOB, CAT, DAN], [BK_ANN, BK_BOB, BK_CAT, BK_DAN], FLIGHTS)


# --- quiet on a correct carve --------------------------------------------------------

def test_a_correct_carve_passes_every_check(tmp_path):
    _task(tmp_path, build=CLEAN_BUILD, full=FULL, cases=CASE)
    results = cg.check_task("demo", tmp_path)
    assert results, "the task declares a split, so checks must run"
    assert all(r.ok for r in results), "\n".join(str(r) for r in results)
    assert not any(r.skipped for r in results), "\n".join(str(r) for r in results)


def test_timestamps_on_both_sides_are_not_a_leak(tmp_path):
    """Both shares carry timestamps. Treating one as an identifier would fail every correct carve."""
    _task(tmp_path, build=CLEAN_BUILD, full=FULL, cases=CASE)
    assert cg.check_disjoint("demo", tmp_path).ok


def test_an_opaque_identifier_shaped_value_is_not_a_dangling_reference(tmp_path):
    """A payment reference belongs to no collection, so it points at nothing and must not be flagged."""
    build = json.loads(json.dumps(CLEAN_BUILD))
    build["bookings"]["BK0001"]["payment"] = "card_9931xx"
    _task(tmp_path, build=build, full=FULL, cases=CASE)
    r = cg.check_closure(cg.load_records("demo", tmp_path, ["environment/build/data/"]),
                         cg.load_records("demo", tmp_path, ["environment/eval/data/"]))
    assert r.ok, str(r)


def test_a_numeric_quantity_is_not_treated_as_a_category(tmp_path):
    """Counts and amounts are quantities, and no small share can hold every value of one.

    Airline records bags per booking as 0-8. Treated as a category, a 35-booking build share is missing
    five of the nine values and a correct carve fails. Categories are labels: strings and flags.
    """
    full = {"bookings": [dict(BK_ANN, bags=n) for n in range(9)]}
    build = {"bookings": [dict(BK_ANN, bags=0)]}
    assert not any(field == "bags" for _, field in cg.enumerations(full))
    assert cg.check_coverage(build, full).ok


def test_a_task_that_has_not_split_is_skipped_entirely(tmp_path):
    """Every task is in this state today; checking them would fail all of them for one dull reason."""
    _task(tmp_path, build=FULL, full=FULL, cases=CASE, split=False)
    assert cg.has_split("demo", tmp_path) is False
    assert cg.check_task("demo", tmp_path) == []


# --- fires on each kind of leak ------------------------------------------------------

def test_a_leaked_scored_record_fires(tmp_path):
    """The whole point: the build share keeps a customer an eval case is about."""
    leaky = _store([ANN, BOB, CAT, DAN], [BK_ANN, BK_BOB, BK_DAN], FLIGHTS)   # CAT kept
    _task(tmp_path, build=leaky, full=FULL, cases=CASE)
    r = cg.check_disjoint("demo", tmp_path)
    assert not r.ok, "a scored customer sitting in the build share was not detected"
    assert "cat_ray_0003" in " ".join(r.errors)


def test_a_leaked_scored_record_id_in_a_script_fires(tmp_path):
    """A setup script naming a scored record leaks it just as surely as a data row containing it."""
    task = _task(tmp_path, build=CLEAN_BUILD, full=FULL, cases=CASE)
    (task / "environment" / "build" / "data" / "seed.sql").write_text(
        "-- rebuild\nDELETE FROM bookings WHERE booking_id = 'BK0003';\n")
    r = cg.check_disjoint("demo", tmp_path)
    assert not r.ok and "BK0003" in " ".join(r.errors)


def test_a_dangling_reference_fires(tmp_path):
    """A booking kept while the flight it uses was withheld."""
    build = _store([ANN, BOB, DAN], [BK_ANN, BK_BOB, BK_DAN], [FL_A])   # FL200 gone, BK0002 uses it
    _task(tmp_path, build=build, full=FULL, cases=CASE)
    r = cg.check_closure(cg.load_records("demo", tmp_path, ["environment/build/data/"]),
                         cg.load_records("demo", tmp_path, ["environment/eval/data/"]))
    assert not r.ok, "a booking pointing at a withheld flight was not detected"
    assert "FL200" in " ".join(r.errors)


def test_a_dangling_customer_reference_fires(tmp_path):
    """The case that a naive rule misses: the booking's OWN id is fine, its customer reference is not."""
    build = _store([ANN, DAN], [BK_ANN, BK_BOB, BK_DAN], FLIGHTS)   # BOB gone, BK0002 points at him
    _task(tmp_path, build=build, full=FULL, cases=CASE)
    r = cg.check_closure(cg.load_records("demo", tmp_path, ["environment/build/data/"]),
                         cg.load_records("demo", tmp_path, ["environment/eval/data/"]))
    assert not r.ok, "a booking pointing at a withheld customer was not detected"
    assert "bob_kim_0002" in " ".join(r.errors)


def test_a_missing_category_fires(tmp_path):
    """The build share holds no business-cabin booking, so the agent never sees that class exists."""
    build = _store([ANN, BOB, DAN], [BK_ANN, BK_DAN], FLIGHTS)   # both economy: no business cabin
    _task(tmp_path, build=build, full=FULL, cases=CASE)
    r = cg.check_coverage(cg.load_records("demo", tmp_path, ["environment/build/data/"]),
                          cg.load_records("demo", tmp_path, ["environment/eval/data/"]))
    assert not r.ok, "a category absent from the build share was not detected"
    assert "cabin" in " ".join(r.errors) and "business" in " ".join(r.errors)


def test_a_collection_missing_altogether_fires(tmp_path):
    """Not an empty pass: no records at all means no categories covered."""
    build = {"users": CLEAN_BUILD["users"], "flights": CLEAN_BUILD["flights"]}
    _task(tmp_path, build=build, full=FULL, cases=CASE)
    r = cg.check_coverage(cg.load_records("demo", tmp_path, ["environment/build/data/"]),
                          cg.load_records("demo", tmp_path, ["environment/eval/data/"]))
    assert not r.ok and "no bookings records" in " ".join(r.errors)


# --- unmeasurable must not read as passing -------------------------------------------

def test_unparseable_data_is_reported_as_skipped_not_as_ok(tmp_path):
    """A carve shipped in a form the guard cannot read must surface, not pass quietly."""
    task = _task(tmp_path, build=CLEAN_BUILD, full=FULL, cases=CASE)
    for f in (task / "environment" / "build" / "data").glob("*.json"):
        f.unlink()
    (task / "environment" / "build" / "data" / "store.parquet").write_bytes(b"\x00binary")
    build = cg.load_records("demo", tmp_path, ["environment/build/data/"])
    assert build == {}
    for r in (cg.check_closure(build, cg.load_records("demo", tmp_path, ["environment/eval/data/"])),
              cg.check_coverage(build, cg.load_records("demo", tmp_path, ["environment/eval/data/"]))):
        assert r.skipped, f"{r.name} silently passed on data it could not read"


# --- a store keyed by SHORT NUMBERS, and by prose beside its records -------------------
#
# banking_support's shape, reduced to what the checks need. Two things about it defeat rules that work on
# every other task in the repo:
#
#   1. ITS CUSTOMERS ARE KEYED BY BARE NUMBERS. `123` is a record key; it is also a fragment of a balance,
#      a zip code and a longer transaction id, and no rule about how a token is SPELT can separate them.
#      Every value below that contains "123" or "125" is here for that reason, and each is a shape a
#      text-level numeric rule trips on: `$1,123.65` splits into a bare `123` token, and `NY 11235`,
#      `CA 94123`, `125000`, `-125.0` and `txn_28cb62125078` all contain one as a substring. The two
#      obvious fixes -- shorter tokens, or substring matching -- are wrong for exactly these six values.
#
#   2. IT DOCUMENTS ITS COLLECTIONS IN PROSE, beside the records rather than in a schema file. That prose
#      is not a record and must never be counted as one: one blob per collection in the record list gives
#      every collection a phantom row and hands the coverage check a paragraph of English where it looks
#      for category values.
#
# The customers scored against are `123` and `125`, so a correct carve withholds both, and their accounts
# with them. `126` and `128` are kept, and between them they carry every one of the misleading numbers
# above -- so the correct carve is the case that has to stay quiet, not a contrived extra one. Two tiers,
# not three, so that the two kept customers can cover both and there is a correct carve to demonstrate.
NUM_123 = {"user_id": "123", "name": "Ann Lee", "tier": "gold",
           "address": "301 Cedar Way, Austin, TX 78701", "income": 61000,
           "last_transaction_id": "txn_9f04ba71c3de"}
NUM_125 = {"user_id": "125", "name": "Bob Kim", "tier": "bronze",
           "address": "44 Pine Street, Denver, CO 80202", "income": 58000,
           "last_transaction_id": "txn_7ba1e50c9d42"}
NUM_126 = {"user_id": "126", "name": "Cal Ray", "tier": "gold",
           "address": "18 Hill Road, Brooklyn, NY 11235", "income": 125000,
           "last_transaction_id": "txn_28cb62125078"}
NUM_128 = {"user_id": "128", "name": "Dee Fox", "tier": "bronze",
           "address": "9 Bay Street, San Francisco, CA 94123", "income": 74000,
           "last_transaction_id": "txn_3c8de6207fab"}

#: Accounts, keyed `01` .. `05`, naming their owner through `holder_user_id` -- a field the schema does NOT
#: call after the users collection, so this also checks that a reference is recognised by its ending.
#: `05` belongs to a scored customer but is named by no case, which is what isolates a reference leak from a
#: held-record leak.
ACCT_01 = {"account_id": "01", "holder_user_id": "123", "class": "checking", "balance": "$402.10"}
ACCT_02 = {"account_id": "02", "holder_user_id": "125", "class": "saving", "balance": "$88.40"}
ACCT_03 = {"account_id": "03", "holder_user_id": "126", "class": "checking", "balance": "$1,123.65"}
ACCT_04 = {"account_id": "04", "holder_user_id": "128", "class": "saving", "balance": "-125.0"}
ACCT_05 = {"account_id": "05", "holder_user_id": "125", "class": "checking", "balance": "$12.00"}

_USER_NOTES = "user_id: the customer's identifier, a short number\ntier: gold | bronze"
_ACCOUNT_NOTES = "account_id: two-digit account number\nholder_user_id: the customer who owns it"


def _documented_store(users: list[dict], accounts: list[dict], *, notes: bool = True) -> dict:
    """The `{collection: {"data": {id: record}, "notes": prose}}` shape."""
    return {
        "users": {"data": {u["user_id"]: u for u in users},
                      "notes": _USER_NOTES if notes else ""},
        "accounts": {"data": {a["account_id"]: a for a in accounts},
                     "notes": _ACCOUNT_NOTES if notes else ""},
    }


#: Two cases, scored against customers `123` and `125`. Each names its customer ONLY inside a
#: JSON-encoded golden action, which is where banking's real cases keep theirs -- so this also exercises
#: the decoding of JSON carried in a string.
NUM_CASES = [
    {"data_point_id": "case-0",
     "multi_turn_input": {"user_persona": "You are Ann Lee.", "user_task": "Freeze my card."},
     "expected_output": {"json": json.dumps({"actions": [
         {"name": "freeze_card", "arguments": {"user_id": "123"}}]})}},
    {"data_point_id": "case-1",
     "multi_turn_input": {"user_persona": "You are Bob Kim.", "user_task": "Close my saving account."},
     "expected_output": {"json": json.dumps({"actions": [
         {"name": "close_account", "arguments": {"user_id": "125", "checking_account_id": "02"}}]})}},
]

NUM_FULL = _documented_store([NUM_123, NUM_125, NUM_126, NUM_128],
                             [ACCT_01, ACCT_02, ACCT_03, ACCT_04, ACCT_05])
#: The correct carve: both scored customers withheld, and every account of theirs with them (closure).
NUM_CLEAN_BUILD = _documented_store([NUM_126, NUM_128], [ACCT_03, ACCT_04])


def _load(task_id: str, tmp_path: pathlib.Path, phase: str):
    return cg.load_records(task_id, tmp_path, [f"environment/{phase}/data/"])


# --- a documented store of collections parses as collections ---------------------------

def test_a_documented_store_parses_as_its_own_collections(tmp_path):
    """The shape that used to collapse into one pseudo-collection of collection wrappers.

    Before it was recognised, the whole file parsed as a single collection whose "records" were the
    per-collection wrappers -- so closure found no keys and coverage inferred no enumerations, and both
    reported NOT MEASURED, which the CLI and CI each treat as a failure. A store in this shape could not be
    carved at all.
    """
    _task(tmp_path, build=NUM_CLEAN_BUILD, full=NUM_FULL, cases=NUM_CASES)
    full = _load("demo", tmp_path, "eval")
    assert {k: len(v) for k, v in full.items()} == {"users": 4, "accounts": 5}
    # the real primary keys, read from where the file states them rather than guessed from field names
    assert cg._keys_by_collection(full)["users"] == {"123", "125", "126", "128"}
    assert cg._keys_by_collection(full)["accounts"] == {"01", "02", "03", "04", "05"}


def test_collection_documentation_is_never_counted_as_a_record(tmp_path):
    """The prose beside a collection documents its fields. Treated as a record it becomes a phantom row."""
    _task(tmp_path, build=NUM_CLEAN_BUILD, full=NUM_FULL, cases=NUM_CASES)
    full = _load("demo", tmp_path, "eval")
    assert all(isinstance(r, dict) and "user_id" in r for r in full["users"])
    assert _USER_NOTES not in json.dumps(full["users"])
    assert not any(field == "notes" for _, field in cg.enumerations(full))


def test_an_empty_collection_still_reads_as_a_collection(tmp_path):
    """Banking ships five collections with no rows yet. Rejecting the shape over one would lose all 17."""
    full = _documented_store([NUM_123], [ACCT_01])
    full["disputes"] = {"data": {}, "notes": "dispute_id: filed disputes, none yet"}
    rows, keys, notes = cg._records_from(_write_json(tmp_path / "store.json", full))
    assert set(rows) == {"users", "accounts", "disputes"} and rows["disputes"] == []
    assert notes["disputes"].startswith("dispute_id")


def _write_json(path: pathlib.Path, data) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


# --- a bare-numeric record key: fires on a leak, quiet on the numbers that look like one ---

def test_a_leaked_bare_numeric_record_key_fires(tmp_path):
    """The gap this closes: a scored customer whose key is too short for the text scan to see.

    Customer `125` is three characters and carries no other identifier the scan recognises, so before the
    structural pass a build share that simply kept the record passed a green disjointness line.
    """
    leaky = _documented_store([NUM_125, NUM_126, NUM_128], [ACCT_03, ACCT_04])
    _task(tmp_path, build=leaky, full=NUM_FULL, cases=NUM_CASES)
    r = cg.check_disjoint("demo", tmp_path)
    assert not r.ok, "a scored customer keyed by a short number was not detected"
    assert "125" in " ".join(r.errors) and "users" in " ".join(r.errors)


def test_the_text_scan_alone_cannot_see_a_bare_numeric_key(tmp_path):
    """Names why the pass above has to exist, so nobody deletes it as duplication.

    Its key is a candidate the store confirms -- `store_identifiers` knows `125` is a customer id -- but the
    eval-case side never produces the token, because three characters with no letters is not an
    identifier shape. The two halves never meet, so the text scan is silent by construction.
    """
    _task(tmp_path, build=NUM_CLEAN_BUILD, full=NUM_FULL, cases=NUM_CASES)
    case_tokens = set()
    for f in cg.eval_case_files("demo", tmp_path):
        case_tokens |= cg.identifier_tokens(f.read_text())
    assert "125" not in case_tokens
    assert "125" in cg.store_identifiers(_load("demo", tmp_path, "eval"))


def test_a_reference_to_a_withheld_bare_numeric_record_fires(tmp_path):
    """A kept account pointing at a scored customer leaks that customer's identity just as surely.

    Account `05` is Bob's and is named by no case, so keeping it is not itself holding a scored record --
    the only thing wrong with it is the `125` in its owner field, which is what has to fire.
    """
    leaky = _documented_store([NUM_126, NUM_128], [ACCT_03, ACCT_04, ACCT_05])
    _task(tmp_path, build=leaky, full=NUM_FULL, cases=NUM_CASES)
    r = cg.check_disjoint("demo", tmp_path)
    assert not r.ok, "a kept record naming a withheld scored customer was not detected"
    assert "point at" in " ".join(r.errors) and "125" in " ".join(r.errors)


def test_bare_numbers_that_are_not_record_keys_are_not_a_leak(tmp_path):
    """The whole reason the fix is structural rather than a shorter token.

    The correct carve below carries `NY 11235`, `CA 94123`, `$1,123.65`, `125000`, `-125.0` and
    `txn_28cb62125078` -- a zip code, a zip code, a balance, an income, a balance and a transaction id. Every
    one of them contains the digits of a withheld customer key, and `$1,123.65` yields a bare `123` token
    outright. Shortening the token rule, or matching keys as substrings, flags all six and there is then no
    correct carve for this store at all.
    """
    _task(tmp_path, build=NUM_CLEAN_BUILD, full=NUM_FULL, cases=NUM_CASES)
    build_text = (tmp_path / "demo" / "environment" / "build" / "data" / "store.json").read_text()
    for shape in ("NY 11235", "CA 94123", "1,123.65", "125000", "-125.0", "txn_28cb62125078"):
        assert shape in build_text, f"{shape} is the point of this test and is not in the build share"
    r = cg.check_disjoint("demo", tmp_path)
    assert r.ok, str(r)
    assert not r.skipped, str(r)


def test_a_correct_carve_of_a_short_keyed_store_is_fully_MEASURED(tmp_path):
    """The assertion that would have failed before this store's shape was recognised.

    Closure and coverage each reported NOT MEASURED on it -- the whole file parsed as one pseudo-collection
    whose records were the collection wrappers, so there were no keys to resolve and no enumerations to
    infer -- and the CLI and CI both treat NOT MEASURED as a failure. A task with this shape could not be
    carved at all, whatever its build share contained.

    Coverage still FAILS here, and that is the other half of the story rather than a defect in the fixture:
    a four-record store gives `name`, `address` and `balance` a handful of distinct values each, which is
    indistinguishable from a category by cardinality alone. The same arithmetic is what makes coverage
    unsatisfiable on banking's real 13-record carve, and it is why the waiver below exists.
    """
    _task(tmp_path, build=NUM_CLEAN_BUILD, full=NUM_FULL, cases=NUM_CASES)
    results = {r.name: r for r in cg.check_task("demo", tmp_path)}
    assert list(results) == ["disjointness", "referential closure", "category coverage",
                            "schema documentation"]
    assert not any(r.skipped for r in results.values()), "\n".join(str(r) for r in results.values())
    assert results["disjointness"].ok and results["referential closure"].ok
    assert results["schema documentation"].ok and not results["schema documentation"].findings


def test_a_short_key_no_field_name_claims_is_left_alone(tmp_path):
    """A DELIBERATE blind spot, recorded so it is a decision rather than a surprise.

    order_to_cash keys its contracts by supplier id, and no field in its data is called `contract_id` --
    an invoice says `supplier_id`. Reading that as a reference to the contracts collection would flag
    roughly a tenth of a correct build share, because every invoice from a supplier whose contract an eval
    case happens to touch would look like a leak. So the structural pass only follows a reference a field
    NAME claims, and this shape goes unchecked on purpose.
    """
    full = {"contracts": {"data": {"9": {"supplier_id": 9, "pay_terms": "NET15"}},
                          "notes": "keyed by supplier id"},
            "invoices": {"data": {"INV-1": {"invoice_number": "INV-1", "supplier_id": "9", "qty": "9"}},
                         "notes": "one row per invoice"}}
    build = {"contracts": {"data": {}, "notes": "keyed by supplier id"},
             "invoices": {"data": {"INV-1": {"invoice_number": "INV-1", "supplier_id": "9", "qty": "9"}},
                          "notes": "one row per invoice"}}
    cases = [{"data_point_id": "case-0", "expected_output": {"json": json.dumps(
        {"actions": [{"name": "flag", "arguments": {"supplier_id": "9"}}]})}}]
    _task(tmp_path, build=build, full=full, cases=cases)
    assert "9" in cg.shape_blind_keys(_load("demo", tmp_path, "eval"))["contracts"]
    assert cg.check_disjoint("demo", tmp_path).ok


# --- schema documentation: reported, never a failure ----------------------------------

def test_documentation_dropped_from_the_build_share_is_reported(tmp_path):
    """A carve script that copies records and drops the prose passes all three invariants.

    The agent explores the build environment and reads those field descriptions as the schema, so losing
    them costs it real information while nothing turns red. Reported, and only reported: whether the build
    share should carry the same prose is the task owner's editorial call, and failing a correct carve over
    an editorial call is what gets a check switched off.
    """
    stripped = _documented_store([NUM_126, NUM_128], [ACCT_03, ACCT_04], notes=False)
    _task(tmp_path, build=stripped, full=NUM_FULL, cases=NUM_CASES)
    r = cg.check_schema_notes(_load("demo", tmp_path, "build"), _load("demo", tmp_path, "eval"))
    assert r.ok and not r.skipped, "dropped documentation must not fail a carve"
    assert r.findings and "users" in " ".join(r.findings) and "accounts" in " ".join(r.findings)


def test_documentation_carried_over_reports_nothing(tmp_path):
    """And it stays quiet when the carve did the right thing, or there was no prose to carry."""
    _task(tmp_path, build=NUM_CLEAN_BUILD, full=NUM_FULL, cases=NUM_CASES)
    r = cg.check_schema_notes(_load("demo", tmp_path, "build"), _load("demo", tmp_path, "eval"))
    assert r.ok and not r.findings, str(r)
    # airline's and order_to_cash's shapes carry no documentation, so they get no such line at all
    _task(tmp_path / "plain", build=CLEAN_BUILD, full=FULL, cases=CASE)
    assert [x.name for x in cg.check_task("demo", tmp_path / "plain")] == [
        "disjointness", "referential closure", "category coverage"]


# --- the coverage waiver --------------------------------------------------------------
#
# A waiver has TWO parts and both are load-bearing: the owner's reason, and the specific enumerations whose
# non-coverage that reason covers. The reason alone would accept whatever the check happens to find, for as
# long as the block survives -- so the day the data moves and a field stops being covered by accident, an
# old reason absorbs a new gap and nobody is told. Naming them makes an unnamed gap a failure, which is
# what these tests are mostly about.
#
# The enumerations are named `collection.field`, which is exactly how the check reports a gap, so a
# reviewer can copy a reported line's first token into the spec.

#: One gap short of covered: both kept bookings are economy, so the full store's `business` cabin is
#: missing and nothing else is.
ONE_GAP_BUILD = _store([ANN, BOB, DAN], [BK_ANN, BK_DAN], FLIGHTS)

#: Two gaps: Bob is withheld as well, so `users.tier` loses `silver` on top of the missing cabin. This is
#: the shape that separates a waiver from an off switch -- name one gap and the other must still fail.
TWO_GAP_BUILD = _store([ANN, DAN], [BK_ANN, BK_DAN], FLIGHTS)

#: What a real waiver looks like: the owner's decision, the measurement behind it, and the gaps it accepts.
WAIVER = {"reason": "Strict carve, no synthesized records: withholding every record an eval case touches, "
                    "closed over the reference graph, leaves 8 of 768 records, so no build share that "
                    "adds nothing can hold one of every category.",
          "enumerations": ["bookings.cabin"]}


def _coverage(task_dir: pathlib.Path):
    return [r for r in cg.check_task("demo", task_dir) if r.name == "category coverage"][0]


def test_a_missing_category_still_fails_a_task_with_no_waiver(tmp_path):
    """The baseline the waiver is measured against: absent a waiver, nothing changes."""
    _task(tmp_path, build=ONE_GAP_BUILD, full=FULL, cases=CASE)
    coverage = _coverage(tmp_path)
    assert not coverage.ok and not coverage.waived
    assert "business" in " ".join(coverage.errors)
    # no waiver, so nothing in the report talks about one
    assert "waiver" not in str(coverage)


def test_a_waiver_suppresses_the_failure_and_keeps_every_finding(tmp_path):
    """A waiver is an ACKNOWLEDGED gap, not a hidden one.

    So it moves the gaps it names out of the failure list and prints all of them, together with the owner's
    reason. A waiver that also shortened the report would leave a reviewer no better informed than a check
    nobody ran, which is the thing it must never become.
    """
    _task(tmp_path, build=ONE_GAP_BUILD, full=FULL, cases=CASE, coverage_waiver=WAIVER)
    coverage = _coverage(tmp_path)
    assert coverage.ok, "a waived coverage gap must not fail the carve"
    assert coverage.waived == WAIVER["reason"]
    assert not coverage.skipped, "a waiver is a decision, not an unmeasured check"
    assert any("business" in f for f in coverage.findings), "the gap must still be reported in full"
    # the reason and the gap both reach the reader
    assert "WAIVED" in str(coverage) and "business" in str(coverage) and "8 of 768" in str(coverage)


def test_a_gap_the_waiver_does_not_name_fails(tmp_path):
    """THE POINT OF NAMING THEM. An unnamed gap is a failure, even beside a valid waiver.

    The build share here has two gaps and the waiver names one. Without the list, the same reason would
    have accepted both -- and would go on accepting a third that appeared by accident next quarter, which
    is how a recorded decision turns into a permanently green light.
    """
    _task(tmp_path, build=TWO_GAP_BUILD, full=FULL, cases=CASE, coverage_waiver=WAIVER)
    coverage = _coverage(tmp_path)
    assert not coverage.ok, "a gap outside the waiver must fail"
    assert coverage.waived == WAIVER["reason"], "the named gap is still waived"

    # the unnamed gap fails; the named one is reported as acknowledged, not as a failure
    assert any("users.tier" in e and "silver" in e for e in coverage.errors)
    assert not any("bookings.cabin" in e for e in coverage.errors)
    assert any("bookings.cabin" in f for f in coverage.findings)


def test_the_failure_says_exactly_what_to_add_to_the_waiver(tmp_path):
    """Churn survivability: the failure hands the author the lines to paste, sorted.

    The list is expected to move with the task's data -- banking's will run to about 25 entries -- so the
    cost of a gap appearing has to be a copy-paste rather than an investigation. The identifier in the
    failure is the identifier the spec accepts, and the block is valid YAML under `enumerations:`.
    """
    _task(tmp_path, build=TWO_GAP_BUILD, full=FULL, cases=CASE, coverage_waiver=WAIVER)
    coverage = _coverage(tmp_path)
    instruction = [e for e in coverage.errors if "coverage_waiver.enumerations" in e]
    assert len(instruction) == 1, coverage.errors
    assert "  - users.tier" in instruction[0]
    assert "  - bookings.cabin" not in instruction[0], "an already-named gap must not be asked for again"

    # what it asks for parses as the list it asks you to paste it into
    pasted = yaml.safe_load("enumerations:\n" + instruction[0].split(":\n", 1)[1])
    assert pasted == {"enumerations": ["users.tier"]}

    # and both gaps are still visible in the report as a whole, which is the invariant a waiver keeps
    assert "users.tier" in str(coverage) and "bookings.cabin" in str(coverage)


def test_what_to_add_is_sorted_so_a_regenerated_waiver_diffs_minimally(tmp_path):
    """Stable ordering, because the list is going to be regenerated repeatedly."""
    build = _store([ANN, DAN], [BK_ANN, BK_DAN], [FL_A])   # drops the delayed BBB->CCC flight too
    _task(tmp_path, build=build, full=FULL, cases=CASE,
          coverage_waiver={"reason": WAIVER["reason"], "enumerations": []})
    coverage = _coverage(tmp_path)
    asked = [l.strip()[2:] for l in
             [e for e in coverage.errors if "coverage_waiver.enumerations" in e][0].splitlines()
             if l.strip().startswith("- ")]
    assert asked == sorted(asked) and len(asked) > 3, asked
    assert asked == ["bookings.cabin", "flights.destination", "flights.origin", "flights.status",
                     "users.tier"]


def test_one_waiver_entry_covers_a_collection_whether_it_is_empty_or_incomplete(tmp_path):
    """Identifier stability: the same entry accepts the gap before and after the build share gains rows.

    A collection with no rows and a collection with rows missing a value are two different messages and one
    gap, so a waiver written against the first still names the second. It matters because a build share
    grows -- banking's 13 surviving records will not stay 13 -- and a list that had to be rewritten every
    time a collection stopped being empty would rot faster than the data moves.
    """
    waiver = {"reason": WAIVER["reason"], "enumerations": ["bookings.cabin"]}
    no_bookings = {"users": CLEAN_BUILD["users"], "flights": CLEAN_BUILD["flights"]}

    _task(tmp_path, build=no_bookings, full=FULL, cases=CASE, coverage_waiver=waiver)
    empty = _coverage(tmp_path)
    assert empty.ok and empty.waived, str(empty)
    assert any("no bookings records at all" in f for f in empty.findings), empty.findings

    _task(tmp_path / "grown", build=ONE_GAP_BUILD, full=FULL, cases=CASE, coverage_waiver=waiver)
    incomplete = _coverage(tmp_path / "grown")
    assert incomplete.ok and incomplete.waived, str(incomplete)
    assert any("is missing ['business']" in f for f in incomplete.findings), incomplete.findings

    # one gap, two messages, and the same entry names both
    for r in (empty, incomplete):
        assert len(r.findings) == 1 and r.findings[0].startswith("bookings.cabin")


def test_a_waiver_leaves_disjointness_and_closure_untouched(tmp_path):
    """The threat bar is not waivable, and there is no vocabulary for waiving it.

    A build share that keeps a scored record, or points at a withheld one, fails exactly as it would
    without a waiver -- and a spec cannot even express the wish, because the only key the checker reads is
    the coverage one.
    """
    leaky = _store([ANN, BOB, CAT, DAN], [BK_ANN, BK_BOB, BK_DAN], FLIGHTS)   # CAT kept, and FL200 gone
    _task(tmp_path, build=leaky, full=FULL, cases=CASE, coverage_waiver=WAIVER)
    by_name = {r.name: r for r in cg.check_task("demo", tmp_path)}
    assert not by_name["disjointness"].ok and not by_name["disjointness"].waived
    assert "cat_ray_0003" in " ".join(by_name["disjointness"].errors)

    dangling = _store([ANN, DAN], [BK_ANN, BK_BOB, BK_DAN], FLIGHTS)          # BOB withheld, BK0002 points
    _task(tmp_path / "b", build=dangling, full=FULL, cases=CASE, coverage_waiver=WAIVER)
    closure = {r.name: r for r in cg.check_task("demo", tmp_path / "b")}["referential closure"]
    assert not closure.ok and not closure.waived
    assert "bob_kim_0002" in " ".join(closure.errors)


def test_a_waiver_with_no_readable_reason_does_not_waive(tmp_path):
    """A half-written waiver must fail SAFE: coverage stays hard, rather than quietly switching off.

    A waiver's value is the recorded reason, so a block without one is indistinguishable from an author
    who meant to come back to it -- including a block that lists enumerations and never says why.
    """
    for bad in ({}, {"reason": ""}, {"reason": "   "}, {"note": "see the ticket"},
                {"enumerations": ["bookings.cabin"]}):
        where = tmp_path / f"w{abs(hash(str(bad)))}"
        _task(where, build=ONE_GAP_BUILD, full=FULL, cases=CASE, coverage_waiver=bad)
        coverage = _coverage(where)
        assert not coverage.ok and not coverage.waived, f"{bad} should not have waived anything"


def test_a_waiver_that_names_nothing_accepts_nothing(tmp_path):
    """EDGE CASE: an empty list, or no list at all. Honoured as a waiver that accepts nothing, so it fails.

    Not read as "no waiver here", deliberately. Both end in the same red line, and this one ends in a
    better message: the failure lists every gap with the exact entries to add, where "no waiver here" would
    leave an author who plainly wrote one wondering why it did nothing. The schema rejects the shape
    outright, so this is only reachable through a spec nobody validated.
    """
    for empty in ({"reason": WAIVER["reason"], "enumerations": []},
                  {"reason": WAIVER["reason"]},
                  {"reason": WAIVER["reason"], "enumerations": "bookings.cabin"}):   # not even a list
        where = tmp_path / f"e{abs(hash(str(empty)))}"
        _task(where, build=ONE_GAP_BUILD, full=FULL, cases=CASE, coverage_waiver=empty)
        coverage = _coverage(where)
        assert not coverage.ok, f"{empty} accepted a gap it did not name"
        assert coverage.waived == WAIVER["reason"], "the reason is readable, so it is reported"
        assert any("bookings.cabin" in e for e in coverage.errors)
        assert any("  - bookings.cabin" in e for e in coverage.errors), "no copy-me list in the failure"


def test_a_waiver_entry_the_build_share_now_covers_is_reported_as_stale(tmp_path):
    """A named gap that closed is dead weight, and saying so is what stops the list rotting.

    It does not fail -- the carve is fine, and failing a correct carve over a spare line in a spec is what
    gets a check switched off. It is reported, so the next reviewer removes it instead of trusting it.
    """
    _task(tmp_path, build=ONE_GAP_BUILD, full=FULL, cases=CASE,
          coverage_waiver={"reason": WAIVER["reason"],
                           "enumerations": ["bookings.cabin", "users.tier"]})
    coverage = _coverage(tmp_path)
    assert coverage.ok and coverage.waived, str(coverage)
    stale = [f for f in coverage.findings if "users.tier" in f]
    assert len(stale) == 1 and "no longer doing anything" in stale[0], coverage.findings
    assert "remove it from task_runner.carve.coverage_waiver.enumerations" in stale[0]
    # the gap that IS still open stays reported as an accepted gap, not as stale
    assert any("bookings.cabin" in f and "business" in f for f in coverage.findings)


def test_a_waiver_naming_an_enumeration_the_store_does_not_have_is_reported(tmp_path):
    """EDGE CASE: a typo, a renamed field, or a field that has grown past the cardinality line.

    Reported as dead weight, not failed. It accepts nothing, so it hides nothing -- and if it was a typo
    for a gap that is still open, that gap is unnamed and fails on its own account, so the mistake surfaces
    either way. Failing here instead would turn every rename in a task's data into a red build on a carve
    that is correct, which is precisely the churn this list is already accused of.
    """
    _task(tmp_path, build=ONE_GAP_BUILD, full=FULL, cases=CASE,
          coverage_waiver={"reason": WAIVER["reason"],
                           "enumerations": ["bookings.cabin", "bookings.cabbin", "meals.choice"]})
    coverage = _coverage(tmp_path)
    assert coverage.ok and coverage.waived, str(coverage)
    for dead in ("bookings.cabbin", "meals.choice"):
        line = [f for f in coverage.findings if dead in f]
        assert len(line) == 1, coverage.findings
        assert "not an enumeration of the full store at all" in line[0]
        assert "no longer doing anything" in line[0]


def test_a_typo_for_a_still_open_gap_fails_rather_than_passing_quietly(tmp_path):
    """The reason the edge case above can be a report: the gap it meant to name is still a failure."""
    _task(tmp_path, build=TWO_GAP_BUILD, full=FULL, cases=CASE,
          coverage_waiver={"reason": WAIVER["reason"],
                           "enumerations": ["bookings.cabin", "users.teir"]})
    coverage = _coverage(tmp_path)
    assert not coverage.ok, "a misspelt entry must not accept the gap it was aiming at"
    assert any("users.tier" in e and "silver" in e for e in coverage.errors)
    assert any("users.teir" in f for f in coverage.findings), "and the misspelling is called out"


def test_a_waiver_on_a_share_that_covers_everything_is_reported_as_stale(tmp_path):
    """A waiver that has outlived its reason is the way a live check becomes a dead one.

    It does not fail the carve -- the carve is fine -- but it says so, because the next real coverage gap
    on this task would otherwise go unreported and nobody would know why.
    """
    _task(tmp_path, build=CLEAN_BUILD, full=FULL, cases=CASE, coverage_waiver=WAIVER)
    coverage = _coverage(tmp_path)
    assert coverage.ok and coverage.waived
    assert any("no longer doing anything" in f for f in coverage.findings), str(coverage)
    # both halves of the report: the entry that is dead, and the waiver as a whole that is dead with it
    assert any("bookings.cabin is covered by the build share now" in f for f in coverage.findings)
    assert any(f.startswith("the build share covers every enumeration") for f in coverage.findings)
