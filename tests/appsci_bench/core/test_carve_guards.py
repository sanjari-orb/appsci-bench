"""Repo-wide checks on every task's build/eval data split, plus the heuristics they rest on.

The build environment must hold no record any eval case touches (docs/task_env_split.md). One check
covers every task: nothing here is per-task, and a task that splits later is covered without this file
being edited.

READ THIS BEFORE TRUSTING A GREEN RUN. Three tasks have split (airline_rebooking, order_to_cash,
banking_support) and the rest deliberately tie both phases to one dataset, so the per-task checks below
still SKIP for most tasks -- and would go on skipping for those if the logic they call were deleted. Three
things stop that being worthless:

  * the heuristic tests in the middle, which run against airline's real store and real eval cases, so
    they fail if the identifier or enumeration inference breaks;
  * the banking section at the bottom, which REPRODUCES that task's carve from its real store and its real
    97 eval cases and asserts the things that used to make banking uncheckable, including that the waiver
    in its spec names exactly the gaps its data produces;
  * tests/appsci_bench/core/test_carve_fires.py, which plants each kind of leak in a synthetic task and
    proves the checks actually fire.

Do not "simplify" the skipping checks away. They are what keeps a task that splits next covered.
"""
from __future__ import annotations

import json
import pathlib

import pytest
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
from appsci_bench.core.spec import TASKS_DIR

TASK_IDS = sorted(
    p.name for p in TASKS_DIR.iterdir()
    if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "spec.yaml").exists()
)

AIRLINE = "airline_rebooking"


def test_tasks_discovered() -> None:
    """If this list is ever empty the whole file passes for the wrong reason."""
    assert TASK_IDS, "no tasks discovered"
    assert AIRLINE in TASK_IDS


# --- every task ----------------------------------------------------------------------

@pytest.mark.parametrize("task_id", TASK_IDS)
def test_task_carve_is_clean(task_id: str) -> None:
    """For a task that has split: no leaked record, no dangling reference, no missing category."""
    results = cg.check_task(task_id, TASKS_DIR)
    if not results:
        pytest.skip(f"{task_id} has not split yet (both phases load the same data)")
    failures = [str(r) for r in results if not r.ok]
    assert not failures, f"{task_id}:\n" + "\n".join(failures)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_a_split_task_gets_every_check_measured(task_id: str) -> None:
    """A carve must not pass because the checks could not see its data.

    Closure and coverage need parseable records. If a task splits and ships its data in a form the guard
    cannot read, that has to surface as a decision to make -- not as a green run.
    """
    results = cg.check_task(task_id, TASKS_DIR)
    if not results:
        pytest.skip(f"{task_id} has not split yet")
    skipped = [str(r) for r in results if r.skipped]
    assert not skipped, (
        f"{task_id} has split, but some checks could not be measured:\n" + "\n".join(skipped)
    )


# --- the heuristics, against real data ------------------------------------------------

def test_identifier_extraction_finds_real_ids_and_rejects_timestamps() -> None:
    """The disjointness check is only as good as what it recognises as an identifier.

    Airline's eval cases name customers (`emma_kim_9957`), bookings (`EHGLP3`) and flights (`HAT003`).
    They also contain timestamps, which split into fragments like `04T23` that mix a letter and digits --
    and timestamps appear on BOTH sides of any carve, so treating one as an identifier would flag every
    correct carve. This is the line the heuristic has to hold.
    """
    tokens: set[str] = set()
    for f in cg.eval_case_files(AIRLINE, TASKS_DIR):
        tokens |= cg.identifier_tokens(f.read_text())

    assert tokens, "no identifiers extracted from airline's eval cases at all"
    for real in ("emma_kim_9957", "EHGLP3", "HAT003"):
        assert real in tokens, f"{real} is an identifier an eval case uses, and was not recognised"
    for fragment in ("04T23", "2024", "05", "12"):
        assert fragment not in tokens, f"{fragment} is timestamp debris, not an identifier"


def test_enumerations_are_inferred_from_the_real_store() -> None:
    """Coverage infers which fields are categories from how many values they have.

    Checked against airline's real store: it must find membership tier, cabin, trip type and insurance,
    and must not mistake identifiers, timestamps or airport codes for categories -- those have hundreds
    of values and demanding full coverage of them would make a small build share impossible.
    """
    full = cg.load_records(AIRLINE, TASKS_DIR, ["environment/eval/data/"])
    assert full, "airline's store did not parse"

    found = {f"{coll}.{field}" for coll, field in cg.enumerations(full)}
    for expected in ("users.membership", "reservations.cabin",
                     "reservations.flight_type", "reservations.insurance"):
        assert expected in found, f"{expected} is a category and was not inferred as one"
    for not_a_category in ("users.user_id", "users.dob", "reservations.created_at",
                           "reservations.origin", "reservations.destination"):
        assert not_a_category not in found, f"{not_a_category} was wrongly treated as a category"

    # Bags-per-booking is a QUANTITY. It has nine values (0-8) and low cardinality, so a rule based on
    # cardinality alone treats it as a category -- and no small build share can hold all nine, so every
    # correct carve gets flagged. Measured on the real store, this was the only false positive left.
    assert "reservations.total_baggages" not in found, (
        "a numeric quantity was treated as a category; a small build share can never cover it"
    )


def test_a_date_is_not_a_category_in_either_spelling() -> None:
    """Dates in ISO order and dates in day-first US order both have to be excluded.

    Banking writes every date as `MM/DD/YYYY` -- a card's expiry and issue dates, a card order's date and
    expected delivery, a payment date, a date of birth, a card-closure timestamp -- while airline and
    order_to_cash write ISO. With only the ISO pattern recognised, those seven banking fields were inferred
    as CATEGORIES, and its coverage report asked a reviewer to accept that the build share "is missing
    ['01/31/2029', '03/31/2028', ...]". Nothing can fix a gap like that: no small share holds every date a
    large store holds, so the only response available was to name all seven in the waiver, burying the real
    findings under seven artefacts of a date format.

    `payment_history.payment_date` is why the rule demands that EVERY value in a field be a date: that one
    field carries both spellings, and the check has to see the whole field as temporal rather than as a
    mix. It is asserted directly below, because a rule of "most values" would pass this test and would then
    also swallow a genuine category that happened to contain one date-shaped value.
    """
    full = cg.load_records(BANKING, TASKS_DIR, [_banking_store_src()])
    found = {f"{coll}.{field}" for coll, field in cg.enumerations(full)}
    for date_field in ("debit_cards.expiration_date", "debit_cards.issue_date",
                       "credit_card_orders.order_date", "credit_card_orders.expected_delivery",
                       "payment_history.payment_date", "verification_history.date_of_birth",
                       "credit_card_closure_reasons.logged_at"):
        assert date_field not in found, f"{date_field} holds dates and was treated as a category"

    # both spellings, and the mixed field that forces the all-values rule
    assert cg._is_temporal({"11/15/2025", "01/31/2029"})
    assert cg._is_temporal({"2025-06-15", "2025-07-15"})
    assert cg._is_temporal({"05/15/2025", "2025-06-15"}), "the mixed payment_date field must count"
    assert not cg._is_temporal({"11/15/2025", "lost"}), "one non-date must disqualify the whole field"

    # NOT extended to these, and that is the decision rather than an oversight: a card's verification code,
    # its last four digits and a verification record's address are not dates, no structural rule separates
    # them from a category, and they look enumerable only because banking has eight debit cards and three
    # verification records. Silencing them would take a wildcard, which would also absorb a future gap in a
    # NEW field of the same collection with nobody told. So they stay gaps, named one by one in the waiver.
    for still_a_gap in ("debit_cards.cvv", "debit_cards.last_4_digits", "verification_history.address"):
        assert still_a_gap in found, (
            f"{still_a_gap} stopped being reported -- if that was deliberate, the waiver entry naming it is "
            f"now dead weight; if it was a widened date or identifier rule, check what else it swallowed")


def test_the_date_rule_changes_nothing_for_the_tasks_that_write_iso() -> None:
    """The other half of the measurement above: airline and order_to_cash must be untouched.

    A heuristic loosened for one task's data is only safe if it is inert on the others, and the failure it
    would cause elsewhere is silent -- a field that stops being checked reports nothing at all, so a
    reviewer sees a shorter clean report and no reason to look. These are the enumeration counts and the
    clean verdicts measured before the day-first pattern was added.
    """
    for task, expected in ((AIRLINE, 4), ("order_to_cash", 7)):
        src = cg.phase_sources(task, TASKS_DIR)
        full = cg.load_records(task, TASKS_DIR, src["eval"])
        assert len(cg.enumerations(full)) == expected, (
            f"{task} now infers {len(cg.enumerations(full))} enumerations, not {expected} -- a date rule "
            f"meant for banking has changed what is checked on a task that writes ISO dates")
        results = {r.name: r for r in cg.check_task(task, TASKS_DIR)}
        assert all(r.ok and not r.skipped and not r.waived for r in results.values()), \
            "\n".join(str(r) for r in results.values())


def test_records_are_deduplicated() -> None:
    """Airline ships one bundled store beside the same rows unbundled per collection.

    Counting them twice would misreport how much data a phase holds, so the loader deduplicates. The
    real store is 300 flights, 500 customers and 2000 bookings.
    """
    full = cg.load_records(AIRLINE, TASKS_DIR, ["environment/eval/data/"])
    assert {k: len(v) for k, v in full.items()} == {"flights": 300, "users": 500, "reservations": 2000}


def test_the_complete_store_passes_its_own_checks() -> None:
    """A store containing everything is closed and covers every category, by definition.

    If either check fails here it is broken, not the data -- this is the fixed point that catches a
    closure or coverage rule that has become nonsense.
    """
    full = cg.load_records(AIRLINE, TASKS_DIR, ["environment/eval/data/"])
    closure = cg.check_closure(full, full)
    coverage = cg.check_coverage(full, full)
    assert closure.ok, str(closure)
    assert coverage.ok, str(coverage)
    assert not closure.skipped and not coverage.skipped, "the complete store should be measurable"


def test_own_keys_are_distinguished_from_references() -> None:
    """A booking carries its own id AND a customer reference; conflating them blinds the check.

    If every `*_id` field counted as the record's own key, a build share holding a booking whose customer
    was withheld would pass -- the exact dangling pointer closure exists to find.
    """
    full = cg.load_records(AIRLINE, TASKS_DIR, ["environment/eval/data/"])
    keys = cg._keys_by_collection(full)
    assert len(keys["reservations"]) == 2000
    assert len(keys["users"]) == 500
    # flights are keyed by number, not id -- and they are what a strict carve withholds most of
    assert len(keys["flights"]) == 300
    assert not (keys["reservations"] & keys["users"]), "customer ids leaked into booking keys"


# --- banking's store shape, and a simulated strict carve of it -------------------------
#
# banking_support has not split, so the per-task checks above skip it. These run against its REAL store and
# its REAL 97 eval cases, and simulate the split in a temporary directory -- so the three things that made
# it uncheckable are asserted on the data as it actually is, and go on being asserted if the data changes.
#
# The build share is COMPUTED here, never committed: withhold every record any eval case names, then keep
# withholding anything that references a withheld record until nothing changes. That is the strict rule from
# docs/task_env_split.md, and computing it means these tests measure banking's data rather than a snapshot
# of somebody's opinion about it. Authoring the real build share is the carve author's job, not this file's.

BANKING = "banking_support"


def _banking_store() -> pathlib.Path:
    """banking's full store, located by SHAPE rather than by a fixed path.

    Its environment layout is being authored while these tests exist, so a hardcoded path is a hostage to
    whatever directory the carve ends up using. The store needs no path to recognise: it is the file under
    the task's environment that parses as a store of many collections with documentation beside them, and
    where two exist (a carved build share beside the full store) it is the larger.
    """
    found = []
    for f in sorted((TASKS_DIR / BANKING / "environment").rglob("*.json")):
        rows, _keys, notes = cg._records_from(f)
        if len(rows) > 1 and any(n.strip() for n in notes.values()):
            found.append((sum(len(r) for r in rows.values()), f))
    assert found, "no documented store of collections under banking's environment directory"
    return max(found)[1]


def _banking_store_src() -> str:
    """That store's directory, as a task-relative source a phase's bundle could declare."""
    return f"{_banking_store().parent.relative_to(TASKS_DIR / BANKING)}/"


def _every_value(node, out: set) -> None:
    """Every string a document holds, decoding JSON carried in a string as it goes.

    Banking's cases keep their golden actions as a JSON string whose action arguments are themselves JSON
    strings, so a walker that stopped at the first string would see the oracle as one opaque blob and find
    none of the records the case is scored on.
    """
    if isinstance(node, dict):
        for v in node.values():
            _every_value(v, out)
    elif isinstance(node, list):
        for v in node:
            _every_value(v, out)
    elif isinstance(node, str):
        out.add(node)
        if node.strip()[:1] in "[{":
            try:
                _every_value(json.loads(node), out)
            except ValueError:
                pass


#: The two records banking withholds by hand because an eval case is ABOUT them and names them only in
#: prose -- customer `125` ("You are Ayan Wang, a Rho-Bank customer (user_id: 125)") and the card
#: application carrying Wei Chen's name and no customer id. Repeated here rather than imported from
#: scripts/generate_banking_build_share.py on purpose: this file reproduces banking's carve from the data
#: to check the guard against it, and reading the generator's own constant would make the two agree by
#: construction however either one was edited. `WITHHELD_BY_EYE` there carries the full reasoning.
_BANKING_BY_EYE = {("users", "125"), ("credit_card_applications", "f1a2b3c4d5e6f789")}


def _banking_strict_carve() -> tuple[dict, dict, int]:
    """(full store, the strict closed build share, how many records were withheld).

    Reproduces what banking actually ships: the records its 97 eval cases NAME as values, plus the two a
    case names only in English, closed over the reference graph. The by-eye pair goes in as a SEED rather
    than being removed afterwards, exactly as the generator does it -- which is what carries customer 125's
    two bank accounts and her debit card out with her, and is why this share is closed rather than full of
    dangling pointers.
    """
    full = json.loads(_banking_store().read_text())
    owner: dict[str, set[str]] = {}
    for coll, wrapper in full.items():
        for key in wrapper["data"]:
            owner.setdefault(key, set()).add(coll)

    withheld: set[tuple[str, str]] = set(_BANKING_BY_EYE)
    for f in cg.eval_case_files(BANKING, TASKS_DIR):
        values: set = set()
        _every_value(json.loads(f.read_text()), values)
        for value in values & set(owner):
            for coll in owner[value]:
                withheld.add((coll, value))

    changed = True
    while changed:                       # close over the reference graph
        changed = False
        for coll, wrapper in full.items():
            for key, record in wrapper["data"].items():
                if (coll, key) in withheld:
                    continue
                values = set()
                _every_value(record, values)
                for value in values - {key}:
                    if any(other != coll and (other, value) in withheld
                           for other in owner.get(value, ())):
                        withheld.add((coll, key))
                        changed = True
                        break

    build = {coll: {"data": {k: r for k, r in w["data"].items() if (coll, k) not in withheld},
                    "notes": w["notes"]}
             for coll, w in full.items()}
    return full, build, len(withheld)


def _simulated_split(tmp_path: pathlib.Path, build: dict, full: dict, *,
                     waiver: dict | None = None) -> pathlib.Path:
    """banking's real eval cases and store, laid out as a split task under `tmp_path`."""
    task = tmp_path / BANKING
    for phase, store in (("build", build), ("eval", full)):
        d = task / "environment" / phase / "data"
        d.mkdir(parents=True, exist_ok=True)
        (d / "db.json").write_text(json.dumps(store))
    for f in cg.eval_case_files(BANKING, TASKS_DIR):
        dest = task / "eval_data" / f.parent.name
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f.name).write_text(f.read_text())

    runner: dict = {
        "network_name": "bench-network", "workspace_name": "banking-workspace",
        "environments": {
            "build": {"bundle": [{"src": "environment/build/data/", "dest": "/fixture/"}],
                      "setup_script": "/scripts/setup.sh"},
            "eval": {"bundle": [{"src": "environment/eval/data/", "dest": "/fixture/"}],
                     "setup_script": "/scripts/setup.sh"},
        },
    }
    if waiver:
        runner["carve"] = {"coverage_waiver": waiver}
    (task / "spec.yaml").write_text(yaml.safe_dump(
        {"task_id": BANKING, "task_runner": runner,
         "evaluation_data": {"dataset": "eval_data"}}, sort_keys=False))
    return task


def test_bankings_store_parses_as_its_own_collections() -> None:
    """17 collections with their real records and real primary keys, not one bag of 17 wrappers.

    Banking documents each collection in prose beside its records. That shape defeated the store-of-
    collections branch, and the whole 768-record file parsed as a single pseudo-collection whose "records"
    were the collection wrappers -- so closure found no keys, coverage inferred no enumerations, and both
    reported NOT MEASURED, which the CLI and the CI test each treat as a failure.
    """
    full = cg.load_records(BANKING, TASKS_DIR, [_banking_store_src()])
    assert len(full) == 17, f"parsed {len(full)} collections, not banking's 17"
    assert sum(len(rows) for rows in full.values()) == 768
    assert {"users", "accounts", "credit_card_accounts", "payment_history"} <= set(full)

    keys = cg._keys_by_collection(full)
    assert len(keys["users"]) == 39 and len(keys["accounts"]) == 33
    # Collections whose key field is NOT named after them -- these are the ones name inference misses, and
    # they are 14 of the 17. Without the file's own declared keys, closure runs on almost none of the store.
    assert len(keys["debit_cards"]) == 8          # keyed card_id
    assert len(keys["credit_card_accounts"]) == 38  # keyed account_id
    assert len(keys["payment_history"]) == 18     # keyed payment_id

    # the documentation is documentation, never a record
    assert all("notes" not in r for rows in full.values() for r in rows)
    assert sum(1 for p in full.notes.values() if p.strip()) == 15


def test_bankings_short_record_keys_are_invisible_to_the_text_scan() -> None:
    """And two of them are named by a real eval case's golden action, so the gap is live, not theoretical.

    Fifteen of banking's keys cannot be extracted from text at all: three customers keyed `123`, `125`, `126`,
    three more whose ten-character ids carry a single letter, eight bank accounts keyed `01` .. `08`, and one
    verification record. Accounts `05` and `07` are the argument of a `close_account`-style golden action in
    three of the 97 cases -- so a build share that kept them leaked a scored record past a green line.
    """
    full = cg.load_records(BANKING, TASKS_DIR, [_banking_store_src()])
    blind = cg.shape_blind_keys(full)
    assert blind["users"] >= {"123", "125", "126"}
    assert blind["accounts"] == {"01", "02", "03", "04", "05", "06", "07", "08"}
    assert sum(len(v) for v in blind.values()) == 15

    # not a shape the text scan can produce, on either side of the carve
    for key in ("123", "05", "6680a37184"):
        assert cg.identifier_tokens(key) == set(), f"{key} would have been seen after all"
    # but the store confirms they are real keys, which is what the structural pass runs on
    assert {"05", "07"} <= cg._keys_by_collection(full)["accounts"]


def test_bankings_strict_carve_is_disjoint_and_closed_but_cannot_cover(tmp_path) -> None:
    """The measurement behind the coverage waiver, taken from the data rather than quoted.

    A strict carve-out -- withhold every record an eval case touches or is about, close over the reference
    graph, add nothing -- leaves 8 of banking's 768 records: 6 customers and 2 bank accounts. No card of
    either kind, no card account, no transaction, no application, no referral, no dispute. So 15 of its 17
    collections are empty in the build share and every category they hold is uncovered, no matter how
    carefully the owner chooses. That is what makes coverage unsatisfiable for this task and nothing else.
    """
    full, build, withheld = _banking_strict_carve()
    kept = sum(len(w["data"]) for w in build.values())
    assert withheld == 760 and kept == 8, f"the strict carve now keeps {kept}, not 8"
    assert {c for c, w in build.items() if w["data"]} == {"users", "accounts"}
    # the by-eye pair, and everything that leaves with customer 125 through incoming references
    for coll, key in _BANKING_BY_EYE | {("accounts", "03"), ("accounts", "04"),
                                        ("debit_cards", "dbc_7a3f9c2b1e")}:
        assert key not in build[coll]["data"], f"{coll}[{key}] survived the carve"

    _simulated_split(tmp_path, build, full)
    results = {r.name: r for r in cg.check_task(BANKING, tmp_path)}
    assert not any(r.skipped for r in results.values()), "\n".join(str(r) for r in results.values())
    # the two invariants that ARE the threat bar hold on the strict carve, which is the point of it
    assert results["disjointness"].ok, str(results["disjointness"])
    assert results["referential closure"].ok, str(results["referential closure"])
    # and coverage cannot
    coverage = results["category coverage"]
    assert not coverage.ok and not coverage.waived
    joined = " ".join(coverage.errors)
    assert "no credit_card_transaction_history records at all" in joined
    assert "no bank_account_transaction_history records at all" in joined


#: A stand-in reason for the waiver MECHANICS below. Deliberately not the one in banking's spec: these
#: tests build a waiver, drop an entry from it, and check what the report does, so they need a reason they
#: control. The real one is cross-checked against the real data in
#: `test_bankings_shipped_waiver_names_exactly_the_gaps_its_data_produces`.
BANKING_REASON = ("Strict carve, no synthesized records: withholding every record an eval case touches, "
                  "closed over the reference graph, leaves 8 of 768 records, so no build share that adds "
                  "nothing can hold one of every category.")


def _uncovered(task_dir: pathlib.Path) -> list[str]:
    """Every enumeration the simulated build share fails to cover, named as a waiver would name it."""
    unwaived = cg.check_coverage(cg.load_records(BANKING, task_dir, ["environment/build/data/"]),
                                cg.load_records(BANKING, task_dir, ["environment/eval/data/"]))
    return [e.split()[0].rstrip(":") for e in unwaived.errors]


def test_bankings_strict_carve_passes_with_a_waiver_naming_its_gaps(tmp_path) -> None:
    """With a waiver naming them the carve is green -- and every gap is still printed, in full.

    That is the whole bargain. The waiver records why the gaps are acceptable and which ones they are; it
    does not shorten the report by one line, so a reviewer reading a green run still sees the 15 collections
    the agent will never meet.

    The list is BUILT FROM THE REPORT here, which is the ergonomics claim being tested as much as the
    behaviour: the identifier the failure prints is the identifier the spec accepts, so an author regenerates
    their waiver by copying what the guard just told them.
    """
    full, build, _ = _banking_strict_carve()
    task_dir = _simulated_split(tmp_path, build, full)
    gaps = _uncovered(task_dir.parent)
    assert len(gaps) == 18, f"banking's carve leaves 18 enumerations uncovered; found {len(gaps)}"
    assert gaps == sorted(gaps), "a regenerated waiver must diff minimally against the last one"
    assert all(g.count(".") == 1 for g in gaps), gaps

    _simulated_split(tmp_path, build, full,
                     waiver={"reason": BANKING_REASON, "enumerations": gaps})
    results = {r.name: r for r in cg.check_task(BANKING, tmp_path)}
    assert all(r.ok for r in results.values()), "\n".join(str(r) for r in results.values())
    assert not any(r.skipped for r in results.values())

    coverage = results["category coverage"]
    assert coverage.waived == BANKING_REASON
    unwaived = cg.check_coverage(cg.load_records(BANKING, tmp_path, ["environment/build/data/"]),
                                cg.load_records(BANKING, tmp_path, ["environment/eval/data/"]))
    assert coverage.findings == unwaived.errors, "a waiver must report exactly what it suppressed"

    # the waiver reaches only coverage. Nothing in the spec can waive the other two.
    assert results["disjointness"].waived is None
    assert results["referential closure"].waived is None


def test_bankings_shipped_waiver_names_exactly_the_gaps_its_data_produces() -> None:
    """The real waiver against the real data, both directions -- which no other check does.

    The repo-wide run above only fails on a gap the waiver does NOT name. An entry that matches nothing is
    reported as dead weight and passes, deliberately: dead weight in a churn-prone list should cost a line in
    the report, not a red build on a carve that is correct. But that leaves an over-broad waiver green
    forever, and an over-broad waiver is the shape that quietly stops meaning anything -- so the equality is
    pinned here, where a human is meant to read it.

    Both halves matter and fail differently. An unnamed gap is an accepted-by-accident category. A named
    non-gap means the carve got better, the data moved, or somebody typed a field name wrong, and the entry is
    now pointing at nothing.
    """
    build = cg.load_records(BANKING, TASKS_DIR, cg.phase_sources(BANKING, TASKS_DIR)["build"])
    full = cg.load_records(BANKING, TASKS_DIR, cg.phase_sources(BANKING, TASKS_DIR)["eval"])
    assert build and full, "banking's stores did not parse, so this check would be vacuous"

    reported = {e.split()[0].rstrip(":") for e in cg.check_coverage(build, full).errors}
    waiver = cg.coverage_waiver(BANKING, TASKS_DIR)
    assert waiver is not None, "banking's spec no longer declares a coverage waiver"
    named = set(waiver.enumerations)

    assert not (reported - named), (
        f"banking's spec does not name {sorted(reported - named)}, so the carve FAILS on them -- add them "
        f"to task_runner.carve.coverage_waiver.enumerations, or carve a share that covers them")
    assert not (named - reported), (
        f"banking's spec names {sorted(named - reported)}, which are not gaps -- the build share covers "
        f"them now, or the field is misspelt or no longer low-cardinality. Dead weight passes the guard, "
        f"so remove them here")
    # EIGHT, down from eighteen. The synthetic augmentation of the build store
    # (scripts/banking_build_synthetic.py) filled every previously-empty collection, which closed ten of
    # these outright -- both account classes and all twelve levels, every bank-transaction type, both
    # credit limits, both closure reasons, both replacement-order reasons, and every debit-card status and
    # issue reason. The eight left are the ones that cannot be closed without copying the eval customers'
    # own identities into the build store (card secrets, printed names, verification contact details, a
    # shipping address), plus the card-type mix, which would need six customers holding eleven cards.
    assert len(named) == 8

    # ...and with that list, the shipped carve is green on every invariant
    results = {r.name: r for r in cg.check_task(BANKING, TASKS_DIR)}
    assert all(r.ok and not r.skipped for r in results.values()), \
        "\n".join(str(r) for r in results.values())
    assert results["category coverage"].waived, "coverage must be WAIVED, not silently passing"
    assert len(results["category coverage"].findings) == 8, (
        "a waiver must print every gap it accepts -- it suppresses the failure, never the finding")
    assert results["disjointness"].waived is None and results["referential closure"].waived is None


def test_a_banking_waiver_missing_one_of_its_gaps_fails_on_the_real_data(tmp_path) -> None:
    """The reason the list exists, measured on the store it was written for.

    A waiver that names 17 of banking's 18 gaps is not a waiver for the 18th. On this data the omission is a
    stand-in for the real risk: the store changes, a field that used to be covered stops being covered, and
    a reason written about 8 surviving records silently absorbs a gap nobody measured.
    """
    full, build, _ = _banking_strict_carve()
    task_dir = _simulated_split(tmp_path, build, full)
    gaps = _uncovered(task_dir.parent)
    dropped = gaps[-1]

    _simulated_split(tmp_path, build, full,
                     waiver={"reason": BANKING_REASON, "enumerations": gaps[:-1]})
    coverage = {r.name: r for r in cg.check_task(BANKING, tmp_path)}["category coverage"]
    assert not coverage.ok, f"omitting {dropped} was absorbed by the reason"
    assert coverage.waived == BANKING_REASON, "the other 17 are still waived"
    assert len(coverage.findings) == len(gaps) - 1
    assert any(dropped in e for e in coverage.errors)
    # and the failure hands the author the line to put back
    assert any(f"  - {dropped}" in e for e in coverage.errors), coverage.errors
