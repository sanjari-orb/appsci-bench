"""Carve the banking BUILD-phase share out of the full transactional store, and write both phases'
count manifests.

WHY THIS EXISTS. During the build phase the AppSci agent explores the task environment and can read
every record in it -- through the banking tools or by writing its own client. The 97 eval cases are
drawn from those same records, so an agent that reads them can shape its artifact around the very rows
it will be scored on, and a score cannot then separate "elicited the rules well" from "read the answers
off the substrate". The build/eval environment split (docs/task_env_split.md) closes that by giving each
phase its own store. This script authors the build phase's share.

A CARVE, NOT A SYNTHESIS, AND THE RESIDUE IS SMALL ON PURPOSE. The task owner's decision is a strict
carve-out with no invented records: withhold every record any eval case touches, close over the
reference graph, keep whatever is left. On this store that leaves **8 of 768 records** -- 6 customers and
2 bank accounts, and nothing else at all -- because almost every other record hangs off a customer and 32
of the 39 customers are touched by name. Eight is the honest answer, not a bug: the store is densely
cross-referenced, so a referentially closed carve that also excludes everything a case touches cannot be
larger. Do not pad it, and do not relax the withholding rule to make it bigger. The cost was accepted
with eyes open and is written down in `tasks/banking_support/environment/build/README.md`: build-phase
quality on this task should be expected to drop, and that is the carve working.

TWO OF THOSE WITHHELD RECORDS ARE HAND-SEEDED, and that is the interesting part of this file. Automated
derivation only ever sees a record a case names as a VALUE. Two records the derivation kept turned out to
be exactly what an eval case is ABOUT, named in the case's prose instead -- see `WITHHELD_BY_EYE`. They
are seeded INTO the closure rather than deleted from its output, so their dependents leave with them
through the same incoming-reference rule as everything else.

THE KNOWLEDGE BASE IS NOT CARVED AND THIS SCRIPT DOES NOT TOUCH IT. The 698 documents are shared whole
with both phases and stay baked into the image. 46 of the 48 specialized tools are named ONLY by
documents the eval cases require, so withholding those documents would not make the task harder -- it
would make it unsolvable and would silently gut the retrieval metric. The corpus also contains no record
id from the store at all, so sharing it leaks nothing the carve is protecting.

HOW "TOUCHED" IS DECIDED, and the trap in it. A record is touched when a case NAMES it, when a case's
starting state writes over it, or when a golden action changes it. Naming is read by walking every value
in the case file -- and the walk must decode JSON carried inside a string, because banking keeps a case's
whole oracle as a JSON string and 522 of its 955 golden actions carry their real arguments as a second
JSON string inside that. A walker that stops at the first string sees the oracle as one opaque token and
finds NOTHING: measured, it withholds zero records rather than 149. Matching is on whole VALUES, never on
substrings: three customers are keyed `123`, `125`, `126`, and a substring scan of the case files
"finds" `123` and `125` inside a zip code, an income and several transaction ids -- which is exactly the
error behind the previously recorded figure of 34 touched customers against the true 32.

Mutation is then checked rather than assumed: the script replays all 97 cases through the real
environment and refuses to write if any base-store record the replay changes was left in the build
share. Today every mutated record is also a named one, so the naming pass alone is sufficient -- but a
future case that mutates a record it does not name would break the carve silently, and this is what
catches it.

Run:  python scripts/generate_banking_build_share.py [--check]
      --check validates and reports without writing, and additionally fails if what is on disk differs
      from what this script would produce -- which is how a reviewer confirms the carved files still
      regenerate. It resolves the full store through the environment package's own EVAL_STORE_PATH
      rather than spelling a path of its own, deliberately: airline's generator spelled its own, the
      store moved under it, and its check mode has been crashing ever since.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
# The authored synthetic records live beside this script. Added explicitly rather than relying on
# sys.path[0]: the tests import this module by path, and sys.path[0] is then the test runner's directory.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from banking_build_synthetic import (  # noqa: E402  (needs the path above)
    RECONCILING_ACCOUNTS,
    REFERENCE_DATE,
    SYNTHETIC,
)

from tasks.banking_support.environment import (  # noqa: E402  (needs the path above)
    BUILD_STORE_PATH,
    EVAL_STORE_PATH,
    BankingEnvironment,
)

TASK = REPO_ROOT / "tasks" / "banking_support"
CASES_DIR = TASK / "eval_data"

#: Written beside each phase's store and bundled with it. The phase's setup script asserts the counts it
#: got back from the load route against this, exactly and per collection -- see setup.sh for why an exact
#: manifest is the only assertion that works on a split whose two sides differ by two orders of magnitude.
#: A FLAT mapping of collection name to integer, and that shape is load-bearing: it is what keeps
#: scripts/carve_guard.py from reading the manifest as if it were a collection of records.
MANIFEST_NAME = "expected_counts.json"

#: What the carve is expected to leave, BEFORE the synthetic augmentation below. Asserted, not assumed:
#: a materially different residue means either the eval cases changed or the derivation broke, and both
#: deserve a human before any file moves. Deliberately still checked against the CARVE's own output
#: rather than the finished file: the augmentation must never be able to mask a carve that started
#: keeping records it should have withheld.
EXPECTED_RESIDUE = {"users": 6, "accounts": 2}

#: What the SYNTHETIC augmentation adds, per collection. The carve's residue and this are checked
#: separately and never summed into one number, because they fail for opposite reasons: a change in the
#: first means the withholding rule moved (a leak risk), a change in the second means somebody edited the
#: authored records (a data-quality question). One combined total would report both as the same event.
EXPECTED_SYNTHETIC = {
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

#: Fields whose value is a DATE the environment's frozen clock bounds (rule 5). Card expiry is excluded
#: on purpose and checked the other way round -- a card that expired before "now" is the error there.
_PAST_DATE_FIELDS = frozenset({
    "date", "date_opened", "date_of_account_open", "closed_date", "transaction_date", "issue_date",
    "payment_date", "submitted_date", "decision_date", "logged_at", "order_date", "applied_at",
    "effective_date", "purchase_date", "issue_noticed_date", "submitted_at",
})
_FUTURE_DATE_FIELDS = frozenset({"expiration_date"})

#: Records an eval case is ABOUT but which no derivation over this data can recognise, seeded into the
#: withheld set by hand. Keyed (collection, record key); the value says which case, and why the machinery
#: is blind to it.
#:
#: WHY A HAND-WRITTEN LIST IS THE RIGHT ANSWER HERE, rather than a smarter matcher. The derivation matches
#: record keys as whole VALUES, and that rule is not negotiable: banking keys three customers `123`, `125`
#: and `126`, so a substring scan of the case files "finds" two of them inside a zip code, an income and
#: several transaction ids -- which is the error behind the previously recorded figure of 34 touched
#: customers against the true 32. Whole-value matching is what makes the derivation trustworthy, and it is
#: also precisely why an identity a case claims in PROSE is invisible to it. Nothing short of reading the
#: cases as English closes that, which is why the plan's step 10 is a human reading the finished carve
#: over by eye, and this constant is what that reading produced. Both entries were confirmed by hand
#: against the case file and the store record, not inferred.
#:
#: THESE ARE SEEDED INTO THE CLOSURE, NOT DELETED FROM ITS OUTPUT, and the difference is the whole design.
#: Customer 125 owns two bank accounts and a debit card. Deleting her row from the finished share would
#: leave those three records pointing at a customer the build environment does not hold -- a dangling
#: pointer the agent reads as the schema, which is the exact failure referential closure exists to
#: prevent. Feeding her in as a seed makes the existing incoming-reference rule carry all three out with
#: her, with no cascade written down anywhere and nothing to keep in step when the data next moves.
#:
#: WHAT IT COSTS, because it is not free: withholding these two takes the residue from 13 records to 8 and
#: empties the last two non-empty collections beyond customers and accounts. The build environment now
#: shows the agent NO card record and NO application record of any kind, so it cannot learn the shape of
#: either from data. The task owner chose that knowing it; see
#: `tasks/banking_support/environment/build/README.md`.
WITHHELD_BY_EYE: dict[tuple[str, str], str] = {
    ("users", "125"): (
        "tau2_banking_34 -- the case is about this customer. Its brief opens \"You are Ayan Wang, a "
        "Rho-Bank customer (user_id: 125). You have a Green Account checking account.\", and that "
        "sentence is the only place `125` occurs in the whole file. A short id sitting inside English "
        "prose is not a value, so whole-value matching cannot see it. Her two bank accounts and her "
        "debit card follow her out through incoming references."),
    ("credit_card_applications", "f1a2b3c4d5e6f789"): (
        "tau2_banking_10 -- the case is about Wei Chen, and this application's `customer_name` is Wei "
        "Chen. The record carries no `user_id` at all, so even with her customer row withheld there is "
        "no reference edge from it for closure to follow. The link exists only in a person's name."),
}


# ---------------------------------------------------------------------------------------------------
# reading the cases
# ---------------------------------------------------------------------------------------------------

def case_files() -> list[pathlib.Path]:
    return sorted(CASES_DIR.rglob("sample.json"))


def every_value(node, out: set) -> None:
    """Every scalar a case document holds, decoding JSON carried inside a string as it goes.

    The decoding is the whole difference between a working derivation and a vacuous one. A banking case
    keeps its oracle as a JSON string, and inside it 522 of the 955 golden actions carry the arguments of
    the tool they wrap as a SECOND JSON string. Stop at the first string and the oracle is one opaque
    token: no record is ever recognised as named, so the carve withholds nothing and looks clean.

    Numbers are stringified before being added, because a case may name a short numeric key as a number
    where the store keys it as a string.
    """
    if isinstance(node, dict):
        for value in node.values():
            every_value(value, out)
    elif isinstance(node, list):
        for value in node:
            every_value(value, out)
    elif isinstance(node, str):
        out.add(node)
        if node.strip()[:1] in "[{":
            try:
                every_value(json.loads(node), out)
            except ValueError:
                pass                      # an ordinary string that merely starts like JSON
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.add(str(node))


def oracle_of(case: dict) -> dict:
    """A case's golden ground truth: the actions, the starting state and the reward basis."""
    return json.loads(case["expected_output"]["json"])


# ---------------------------------------------------------------------------------------------------
# the carve
# ---------------------------------------------------------------------------------------------------

def owners_by_key(store: dict) -> dict[str, set[str]]:
    """Which collection(s) key a record by each value. The store's own keys, which is the only reliable
    place to read a primary key from -- 14 of banking's 17 collections do not name their key field after
    themselves (debit cards are keyed `card_id`, card accounts `account_id`, payments `payment_id`)."""
    owners: dict[str, set[str]] = {}
    for collection, wrapper in store.items():
        for key in wrapper["data"]:
            owners.setdefault(key, set()).add(collection)
    return owners


def named_by_cases(store: dict, owners: dict[str, set[str]]) -> set[tuple[str, str]]:
    """Records the eval cases name, anywhere: in a golden action's arguments, in a starting state, in
    the customer's own brief. Whole-value matching, never substring -- see the module docstring."""
    touched: set[tuple[str, str]] = set()
    for path in case_files():
        values: set = set()
        every_value(json.loads(path.read_text()), values)
        for value in values & set(owners):
            for collection in owners[value]:
                touched.add((collection, value))
    return touched


def close_over_references(store: dict, owners: dict[str, set[str]],
                         seed: set[tuple[str, str]]) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Grow `seed` until it is closed under INCOMING references, and report the edges that did it.

    The direction matters and is easy to get backwards. If a record the build share KEEPS points at a
    record the carve withheld, the build environment ships a dangling pointer and the agent draws wrong
    conclusions about the schema -- so the referring record has to go too. Closing the other way (pulling
    the referent back IN) would readmit exactly the scored records the carve exists to withhold.

    Iterated to a fixed point rather than done in one pass, because references chain: a dispute points at
    a card transaction, which points at a card account, which points at a customer.
    """
    withheld = set(seed)
    edges: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for collection, wrapper in store.items():
            for key, record in wrapper["data"].items():
                if (collection, key) in withheld:
                    continue
                values: set = set()
                every_value(record, values)
                for value in values - {key}:
                    pointed_at = [other for other in owners.get(value, ())
                                  if other != collection and (other, value) in withheld]
                    if pointed_at:
                        withheld.add((collection, key))
                        edges |= {(collection, other) for other in pointed_at}
                        changed = True
                        break
    return withheld, edges


def withhold_seed(store: dict, owners: dict[str, set[str]]) -> set[tuple[str, str]]:
    """Everything the closure starts from: the records the cases NAME, plus the by-eye additions.

    The union happens here, before closure, rather than after it. Both halves are the same kind of thing
    -- "an eval case is about this record" -- so both belong on the same side of the fixed-point walk, and
    the hand-seeded half then loses its dependents to the incoming-reference rule for free. Subtracting
    the by-eye records from the finished share instead would leave their dependents behind, still pointing
    at records the build environment no longer holds.

    A key in `WITHHELD_BY_EYE` that names no record in the store is a hard error rather than a no-op. A
    typo there would silently withhold nothing, and the only symptom would be a residue count that is
    three or four too high -- which reads as "a case changed", sends the reader to the eval data, and
    tells them nothing about the misspelt key that actually caused it.
    """
    unknown = sorted(f"{collection}[{key}]" for collection, key in WITHHELD_BY_EYE
                     if key not in store.get(collection, {}).get("data", {}))
    if unknown:
        raise SystemExit(
            f"WITHHELD_BY_EYE names {len(unknown)} record(s) the full store does not hold: {unknown}. "
            f"Fix the spelling, or drop the entry if the record is gone -- a key that matches nothing "
            f"withholds nothing, and the only symptom would be an unexplained residue count.")
    return named_by_cases(store, owners) | set(WITHHELD_BY_EYE)


def carve(store: dict) -> tuple[dict, set[tuple[str, str]], set[tuple[str, str]]]:
    """(the build share, the withheld records, the reference edges closure travelled)."""
    owners = owners_by_key(store)
    withheld, edges = close_over_references(store, owners, withhold_seed(store, owners))
    build = {
        collection: {
            "data": {key: record for key, record in wrapper["data"].items()
                     if (collection, key) not in withheld},
            # The prose comes across unchanged. It is the task owner's field documentation, 15 of the 17
            # collections carry it, and the agent exploring the build environment reads it AS the schema
            # -- which is most of what is left to learn from a store this small. Dropping it would cost
            # the agent real information while leaving every carve invariant green.
            "notes": wrapper["notes"],
        }
        for collection, wrapper in store.items()
    }
    return build, withheld, edges


def augment(carved: dict) -> dict:
    """The carve's output plus the authored synthetic records (`banking_build_synthetic.SYNTHETIC`).

    A SEPARATE STAGE, AFTER THE CARVE, AND THAT SEPARATION IS THE DESIGN. The carve answers "which real
    records may the build phase see" and its answer is 8; the augmentation answers "what else does the
    agent need in front of it to be able to test its own work", and its records are invented. Folding the
    two together would mean the withholding rule and the authored data shared one residue number, so a
    carve that started leaking could be hidden by an augmentation that shrank. Kept apart, each has its
    own expected shape and its own failure.

    Nothing here overwrites a carved record: a synthetic key colliding with one the carve kept is a hard
    error, not a merge, because silently replacing a real customer with an invented one is the single
    worst thing this function could do.
    """
    build = {collection: {"data": dict(wrapper["data"]), "notes": wrapper["notes"]}
             for collection, wrapper in carved.items()}
    for collection, records in SYNTHETIC.items():
        if collection not in build:
            raise SystemExit(
                f"synthetic records name collection {collection!r}, which the store does not have. "
                f"Fix the name in banking_build_synthetic.py -- an unknown collection would otherwise "
                f"add a table the full store has no counterpart for.")
        for key, record in records.items():
            if key in build[collection]["data"]:
                raise SystemExit(
                    f"synthetic {collection}[{key}] collides with a record the carve KEPT. Rename the "
                    f"synthetic record: overwriting a real customer's row with an invented one would "
                    f"leave the build share describing a customer who does not exist upstream.")
            build[collection]["data"][key] = record
    return build


def _date_ok(value: str, *, future: bool) -> bool:
    """Is `value` an MM/DD/YYYY date on the right side of the environment's frozen clock?

    Anything that is not a date in that format passes: this checks the DATE fields, and a field listed in
    `_PAST_DATE_FIELDS` that happens to hold a timestamp ("2025-11-09 10:41:00 EST") or a free-text
    delivery window ("7-10 business days") is not the thing being bounded here.
    """
    parts = str(value).split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return True
    month, day, year = (int(p) for p in parts)
    ref_month, ref_day, ref_year = (int(p) for p in REFERENCE_DATE.split("/"))
    stamp, reference = (year, month, day), (ref_year, ref_month, ref_day)
    return stamp > reference if future else stamp <= reference


def validate_synthetic(store: dict, build: dict, base: BankingEnvironment) -> list[str]:
    """The augmentation's own five rules, plus the internal-consistency checks that make the records
    SOUND rather than merely well-formed. Returns a list of problems; empty means safe to write.

    Rules 2, 3 and 4 (not named by a case, no collision with a minted id, references resolve) are checked
    for every kept record by `validate` already -- the carve needs them too, and a synthetic record is a
    kept record. What is here is what only applies to invented data: that its keys are the eval store's to
    reuse (rule 1), that its dates respect the frozen clock (rule 5), and that a customer's details do not
    contradict the carved customer row they are attached to.

    THE CONSISTENCY CHECKS ARE THE POINT OF THIS FUNCTION. An invented record is easy to make
    well-formed and easy to make quietly wrong -- a cardholder name that does not match the customer, a
    dispute whose contact block came from a different person, a card that expired last year. The agent
    reads all of it as the schema AND as the truth about these customers, so a contradiction here teaches
    it that the store disagrees with itself.
    """
    problems: list[str] = []
    customers = build["users"]["data"]

    for collection, records in SYNTHETIC.items():
        upstream = store[collection]["data"]
        for key, record in records.items():
            where = f"{collection}[{key}]"

            # -- rule 1: the key must not be the eval store's.
            if key in upstream:
                problems.append(
                    f"{where}: a record with this key already exists in the FULL store, so the id is not "
                    f"ours to reuse -- a build record and an eval record would be indistinguishable by id")

            # -- the key must be the record's own declared id, which is the class of error that produced
            #    nine bad keys the first time this file was authored (non-ASCII bytes, embedded spaces,
            #    and keys that simply disagreed with the record). A store keyed inconsistently with its
            #    own records breaks every lookup that goes through an id field.
            declared = record.get(_OWN_ID_FIELD.get(collection, ""))
            if declared is not None and declared != key:
                problems.append(f"{where}: keyed {key!r} but its own "
                                f"{_OWN_ID_FIELD[collection]} says {declared!r}")

            # -- rule 5: dates on the right side of the frozen clock.
            for field, value in record.items():
                if field in _PAST_DATE_FIELDS and not _date_ok(value, future=False):
                    problems.append(f"{where}: {field} is {value}, after the environment's fixed now "
                                    f"({REFERENCE_DATE})")
                if field in _FUTURE_DATE_FIELDS and not _date_ok(value, future=True):
                    problems.append(f"{where}: {field} is {value}, which is not after the environment's "
                                    f"fixed now ({REFERENCE_DATE}) -- an already-expired card")

            # -- every customer a synthetic record names must be one the build share holds, and the
            #    details it restates must match that customer's row exactly.
            user_id = record.get("user_id") or record.get("referrer_id")
            if user_id is not None and user_id not in customers:
                problems.append(f"{where}: names user {user_id!r}, which the build share does not hold")
            elif user_id is not None:
                customer = customers[user_id]
                for field, source in (("full_name", "name"), ("cardholder_name", "name"),
                                      ("customer_name", "name"), ("name", "name"),
                                      ("phone", "phone_number"), ("phone_number", "phone_number"),
                                      ("email", "email"), ("address", "address"),
                                      ("shipping_address", "address"),
                                      ("date_of_birth", "date_of_birth")):
                    if field not in record:
                        continue
                    want = customer[source]
                    got = record[field]
                    if field == "cardholder_name":
                        # As printed on a card: uppercased, and the store strips the accents a name may
                        # carry ("MARIA ELENA RODRIGUEZ" for "Maria Elena Rodríguez").
                        ok = got == _card_name(want)
                    else:
                        ok = got == want
                    if not ok:
                        problems.append(f"{where}: {field} is {got!r}, but user {user_id}'s row says "
                                        f"{want!r}")

    # -- card secrets must be unique, or "verify the last four" stops identifying a card.
    for field in ("last_4_digits", "cvv"):
        seen: dict[str, str] = {}
        for key, record in SYNTHETIC["debit_cards"].items():
            value = record[field]
            if value in seen:
                problems.append(f"debit_cards[{key}]: {field} {value!r} is already used by "
                                f"{seen[value]} -- card secrets must identify one card")
            seen[value] = key

    # -- the accounts young enough for their whole life to fit in the transaction window must reconcile
    #    to the cent. This is the one arithmetic claim the data makes, so it is the one worth checking:
    #    an account opened in 2021 carries a recent slice of a longer history and no sum is asserted.
    for account_id, balance in RECONCILING_ACCOUNTS.items():
        rows = [r for r in SYNTHETIC["bank_account_transaction_history"].values()
                if r["account_id"] == account_id]
        total = round(sum(r["amount"] for r in rows), 2)
        if total != round(float(balance), 2):
            problems.append(
                f"accounts[{account_id}]: its {len(rows)} synthetic transaction(s) sum to {total}, but "
                f"the account record states current_holdings {balance!r}. This account opened inside the "
                f"transaction window, so the two must agree.")

    # -- a synthetic record must not be keyed the same as one a golden action mints. `validate` checks
    #    this for the whole share; repeated here only so the message names the augmentation, which is the
    #    only place a collision can now come from.
    created, _ = created_by_golden_actions(base)
    synthetic_keys = {(collection, key) for collection, records in SYNTHETIC.items() for key in records}
    if clash := sorted(created & synthetic_keys):
        problems.append(f"synthetic records are keyed the same as ids the golden actions mint: {clash}")

    return problems


#: The field each collection keys its records by. Only the collections the augmentation writes to need an
#: entry; 14 of the 17 do not name their key field after themselves, so this cannot be inferred.
_OWN_ID_FIELD = {
    "accounts": "account_id",
    "bank_account_transaction_history": "transaction_id",
    "cash_back_disputes": "dispute_id",
    "credit_card_account_flags": "flag_id",
    "credit_card_accounts": "account_id",
    "credit_card_applications": "application_id",
    "credit_card_closure_reasons": "record_id",
    "credit_card_orders": "order_id",
    "credit_card_transaction_history": "transaction_id",
    "credit_limit_increase_requests": "request_id",
    "debit_card_orders": "order_id",
    "debit_cards": "card_id",
    "payment_history": "payment_id",
    "referrals": "referral_id",
    "transaction_disputes": "dispute_id",
    "users": "user_id",
}


def _card_name(name: str) -> str:
    """A name as printed on a card: uppercase, accents stripped. Matches how the full store writes them."""
    folded = (name.replace("í", "i").replace("é", "e").replace("á", "a").replace("ó", "o")
                  .replace("ú", "u").replace("ñ", "n"))
    return folded.upper()


# ---------------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------------

def created_by_golden_actions(base: BankingEnvironment) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Replay all 97 cases and report (records the replay created, base records it changed or removed).

    The created set is a collision check: a build-share record keyed the same as one a golden action
    mints would make a live creation indistinguishable from the starting state. It cannot happen under a
    carve-out -- the ids all come from the same store -- but a future edit that hand-adds a record would
    find out here rather than from a case that scores zero for no visible reason.

    The changed set is what verifies the naming pass. Every base record a golden action mutates must be
    withheld; today all of them are also NAMED by their case, so naming alone suffices, but a future case
    that mutates a record it never names would otherwise leave a scored record in the build share
    silently.
    """
    before = base.db.model_dump()
    created: set[tuple[str, str]] = set()
    changed: set[tuple[str, str]] = set()
    for path in case_files():
        oracle = oracle_of(json.loads(path.read_text()))
        actions = oracle.get("actions") or []
        env = base.fresh()
        env.set_read_log_allowlist(env.read_log_allowlist_for(actions))
        env.seed(oracle.get("initial_state"))
        env.apply_actions(actions)
        after = env.db.model_dump()
        for collection, rows in before.items():
            live = after[collection]["data"]
            for key in rows["data"]:
                if key not in live or live[key] != rows["data"][key]:
                    changed.add((collection, key))
            for key in live:
                if key not in rows["data"]:
                    created.add((collection, key))
    return created, changed


def validate(store: dict, carved: dict, build: dict, withheld: set[tuple[str, str]],
             base: BankingEnvironment) -> list[str]:
    """Return a list of problems; empty means the build share is safe to write.

    TWO SHARES ARE PASSED IN AND THE DISTINCTION IS LOAD-BEARING. `carved` is what the withholding rule
    left -- the 8 real records -- and is what the residue and byte-identity checks run against, because
    those two are claims about the CARVE. `build` is that plus the authored synthetic records, and is what
    closure, disjointness and the minted-id collision check run against, because those three are claims
    about the FILE the agent will read. Running the first pair against the finished file would let the
    augmentation mask a carve that leaked; running the second pair against the carve alone would leave the
    synthetic records unchecked against the eval cases, which is the one thing that must never happen.
    """
    problems: list[str] = []

    def bad(message: str) -> None:
        problems.append(message)

    owners = owners_by_key(store)
    kept = {(collection, key) for collection, wrapper in build.items() for key in wrapper["data"]}
    residue = {c: len(w["data"]) for c, w in carved.items() if w["data"]}
    synthesized = {c: len(r) for c, r in SYNTHETIC.items() if r}

    # -- the residue is what the owner signed off on. A materially different number is a finding, not a
    #    new baseline: it means the eval cases moved, the derivation broke, or someone edited
    #    WITHHELD_BY_EYE. Both the per-collection shape and the total are checked, because the two fail
    #    differently: a changed shape says which collection moved, and the total is the number every
    #    document about this carve quotes, so a reader chasing "8 records" needs it named here too.
    if residue != EXPECTED_RESIDUE:
        bad(f"the carve now keeps {residue}, not the agreed {EXPECTED_RESIDUE} -- stop and read why. "
            f"Do NOT edit EXPECTED_RESIDUE to match: it is the owner's signed-off number, and the two "
            f"things that legitimately move it (an eval case reaching further, or a new WITHHELD_BY_EYE "
            f"entry) both want a human and both want the documents updated with it.")
    if sum(residue.values()) != sum(EXPECTED_RESIDUE.values()):
        bad(f"the carve now keeps {sum(residue.values())} records, not the agreed "
            f"{sum(EXPECTED_RESIDUE.values())}")
    if synthesized != EXPECTED_SYNTHETIC:
        bad(f"the synthetic augmentation now adds {synthesized}, not the recorded "
            f"{EXPECTED_SYNTHETIC}. Unlike the carve residue this number IS editable -- authored records "
            f"are meant to be added to -- but update EXPECTED_SYNTHETIC in the same commit, and the "
            f"counts in environment/build/README.md and KNOWN_ISSUES.md section 6 with it.")
    if len(kept) + len(withheld) != (sum(len(w["data"]) for w in store.values())
                                     + sum(EXPECTED_SYNTHETIC.values())):
        bad("kept + withheld does not account for every record in the store plus every synthetic one")

    # -- the by-eye withholds, and their dependents, must actually be gone. Cheap, and it is the only
    #    thing standing between a regeneration and quietly restoring a record an eval case is ABOUT:
    #    WITHHELD_BY_EYE is a hand-written list, and a hand-written list is the kind of thing a later
    #    refactor of the seed plumbing can stop consulting without any other symptom.
    for collection, key in sorted(WITHHELD_BY_EYE):
        if (collection, key) in kept:
            bad(f"{collection}[{key}] is in the build share, but WITHHELD_BY_EYE says an eval case is "
                f"about it: {WITHHELD_BY_EYE[(collection, key)]}")

    # -- schema parity. The build share must be the same SHAPE as the full store, or the agent learns a
    #    schema the artifact will then be scored against a different version of.
    if set(build) != set(store):
        bad(f"the build share holds collections {sorted(set(build) ^ set(store))} the store does not")
    for collection, wrapper in build.items():
        if wrapper["notes"] != store[collection]["notes"]:
            bad(f"{collection}: the field documentation was altered or dropped")

    # -- a carve COPIES, it never reformats. Byte-identical serialization per record is the strongest
    #    form of that, and it is what stops a monetary value changing shape on the way through: this
    #    store carries an annual income as the float 125000.0, and a value that re-serialized as 125000
    #    would move the state hash of any case seeded from it while looking numerically identical.
    #    Scoped to the CARVED records: a synthetic record has no counterpart upstream to be identical to,
    #    which is exactly why it is checked by `validate_synthetic` instead.
    for collection, wrapper in carved.items():
        for key, record in wrapper["data"].items():
            mine = json.dumps(record, sort_keys=True)
            theirs = json.dumps(store[collection]["data"][key], sort_keys=True)
            if mine != theirs:
                bad(f"{collection}[{key}]: re-serialized differently from the full store")

    # -- referential closure: nothing the build share keeps may point at a record it does not. Owners are
    #    read from the full store AND from the finished share, so a synthetic record pointing at a
    #    withheld eval record and one pointing at a key that exists nowhere both fail here. Without the
    #    second half, a synthetic reference to a mistyped synthetic key would resolve to nothing and pass.
    owners_final = {value: set(collections) for value, collections in owners.items()}
    for collection, wrapper in build.items():
        for key in wrapper["data"]:
            owners_final.setdefault(key, set()).add(collection)
    for collection, wrapper in build.items():
        for key, record in wrapper["data"].items():
            values: set = set()
            every_value(record, values)
            for value in values - {key}:
                for other in owners_final.get(value, ()):
                    if other != collection and (other, value) not in kept:
                        bad(f"{collection}[{key}] references {other}[{value}], which the build share "
                            f"does not hold")

    # -- disjointness, on whole values in both directions.
    for path in case_files():
        values = set()
        every_value(json.loads(path.read_text()), values)
        for collection, key in sorted(kept):
            if key in values:
                bad(f"{collection}[{key}] is named by eval case {path.parent.name}")

    created, changed = created_by_golden_actions(base)
    if leaked := sorted(changed & kept):
        bad(f"the golden actions change these records the build share keeps: {leaked[:8]}")
    if clash := sorted(created & kept):
        bad(f"the build share keys records the golden actions mint: {clash[:8]}")

    # -- the required-reads derivation must not depend on which store is loaded. It decides which
    #    discoverable READ calls are logged into the hashed table, and the live environment and the
    #    golden replay both derive it -- from different stores, once the phases differ. If it ever became
    #    data-dependent, the two sides would disagree and a PERFECT artifact would score zero. Built with
    #    `with_store` so the toolkits are genuinely rebound to the build share: overwriting the store on
    #    an existing environment would leave the tools bound to the old one and the comparison vacuous.
    build_env = base.with_store(_load_build(build))
    for path in case_files():
        actions = oracle_of(json.loads(path.read_text())).get("actions") or []
        if base.read_log_allowlist_for(actions) != build_env.read_log_allowlist_for(actions):
            bad(f"the required-reads derivation differs across the two stores for {path.parent.name}")
            break

    return problems


def _load_build(build: dict):
    """The build share parsed by the real model, which is the only proof it will load in the container."""
    from tasks.banking_support.environment.data_model import TransactionalDB
    return TransactionalDB.model_validate(build)


def manifest_for(path: pathlib.Path, base: BankingEnvironment) -> dict[str, int]:
    """The per-collection counts a phase's setup script will assert, produced by the SAME code the
    service reports them with -- so a manifest can never disagree with the load for a reason that is
    only about how it was counted."""
    from tasks.banking_support.environment.data_model import TransactionalDB
    return base.with_store(TransactionalDB.load(path)).store_counts()


# ---------------------------------------------------------------------------------------------------

def _serialize(payload: object) -> str:
    """The canonical form both stores are written in, so re-running produces an empty diff."""
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="validate and report without writing; fails if the files on disk differ")
    args = ap.parse_args()

    store = json.loads(EVAL_STORE_PATH.read_text())
    base = BankingEnvironment.from_fixture()          # the FULL store, via the default path
    carved, withheld, edges = carve(store)
    build = augment(carved)

    problems = validate(store, carved, build, withheld, base) + validate_synthetic(store, build, base)
    if problems:
        print(f"REFUSING TO WRITE -- {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    total = sum(len(w["data"]) for w in store.values())
    kept = sum(len(w["data"]) for w in carved.values())
    added = sum(len(r) for r in SYNTHETIC.values())
    print(f"banking build share: {kept} of {total} real records kept, {len(withheld)} withheld "
          f"({len(case_files())} eval cases, {len(edges)} reference edges closed over, "
          f"{len(WITHHELD_BY_EYE)} withheld by eye)")
    print(f"  plus {added} authored SYNTHETIC records -> {kept + added} records in the build store")
    for collection in sorted(store):
        held = len(build[collection]["data"])
        real = len(carved[collection]["data"])
        whole = len(store[collection]["data"])
        synth = held - real
        note = "" if held else "   (EMPTY in the build share)"
        extra = f"  (+{synth} synthetic)" if synth else ""
        print(f"  {collection:34} {real:4} / {whole:4}{extra}{note}")
    print(f"  field documentation carried across for "
          f"{sum(1 for w in build.values() if w['notes'].strip())} collection(s)")
    # Printed every run, not tucked into a comment: these are the two records the derivation would have
    # kept, and a reader who does not know they were removed on purpose will read the residue as a bug.
    for (collection, key), why in sorted(WITHHELD_BY_EYE.items()):
        print(f"  withheld by eye: {collection}[{key}] -- {why}")

    # Written to a temp path first, then compared or moved: --check must be able to report a difference
    # without leaving one behind.
    build_json = _serialize(build)
    on_disk = BUILD_STORE_PATH.read_text() if BUILD_STORE_PATH.exists() else None

    if args.check:
        if on_disk is None:
            print("\n--check: no build share on disk yet", file=sys.stderr)
            return 1
        if on_disk != build_json:
            print("\n--check: the build share on disk is NOT what this script produces", file=sys.stderr)
            return 1
        print("\n--check: the build share on disk is exactly what this script produces; nothing written")
        return 0

    BUILD_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUILD_STORE_PATH.write_text(build_json)
    print(f"\n  wrote {BUILD_STORE_PATH.relative_to(REPO_ROOT)}")

    # The manifests come LAST, and each is read back off the store it describes rather than computed
    # from the structures above. A manifest derived from the same dict as the file it guards would agree
    # with a file that never got written.
    for label, store_path in (("build", BUILD_STORE_PATH), ("eval", EVAL_STORE_PATH)):
        manifest = store_path.parent / MANIFEST_NAME
        counts = manifest_for(store_path, base)
        manifest.write_text(_serialize(counts))
        print(f"  wrote {manifest.relative_to(REPO_ROOT)} "
              f"({sum(counts.values())} records, {label} phase)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
