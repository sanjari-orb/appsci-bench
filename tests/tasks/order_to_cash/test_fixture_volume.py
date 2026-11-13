"""Guards on the order_to_cash fixture after the background-volume expansion.

The point of these tests is the boundary between the 60 SCORED cases and the unlabelled background
rows. The background exists so that browsing the database during BUILD does not enumerate the eval
set; the tests below check that adding it did not disturb what is scored, and that the properties the
loader relies on actually hold in the committed data.

The background rows carry no label file on purpose, so the fault-mix tests re-derive what each one is
by comparing the invoice to its PO line and contract -- the same comparison the task itself asks for.
That classifier is validated against the 60 rows whose labels ARE known before it is trusted on the
1,035 that are not, which is what makes re-deriving safe rather than circular.

Everything here is in memory: no database, no credentials, and nothing written to disk.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parents[3] / "tasks" / "order_to_cash"
# The two phase fixtures. EVAL holds the full store and is what the scored cases resolve against;
# BUILD holds the carved share the AppSci agent explores. Tests that talk about "the fixture" without
# qualification mean the eval one, because that is the store the eval cases are drawn from.
FIXTURE = TASK / "environment" / "eval" / "fixture"
BUILD_FIXTURE = TASK / "environment" / "build" / "fixture"
LABELS = TASK / "seed_data" / "labels.csv"
SCORED_CASES = 60

# Mirrors TASK_SOP.md, and the generator's own constants. Duplicated here rather than imported
# because the generator lives under scripts/ and is not an importable package -- which is safe only
# because `test_the_classifier_reproduces_the_known_labels` fails the moment these drift from what
# actually produced the fixture.
MATERIAL_DELTA = 500.0
ROUNDING_TOL = 3.0


def _rows(name: str) -> list[dict]:
    with open(FIXTURE / name, newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def invoices() -> list[dict]:
    return _rows("invoices.csv")


@pytest.fixture(scope="module")
def po_lines() -> list[dict]:
    return _rows("po_lines_used.csv")


@pytest.fixture(scope="module")
def labels() -> list[dict]:
    with open(LABELS, newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def contracts() -> dict:
    return json.loads((FIXTURE / "contracts.json").read_text())


def classify(invoice: dict, po_line: dict, contract: dict) -> str:
    """Re-derive which scenario produced one fixture invoice, from the data alone.

    The generator applies exactly ONE mutation per row, and each mutation leaves a distinguishable
    trace, so the scenario is recoverable. Order matters: a fee or a quantity change also moves the
    total, so the checks that look at the total have to come last or they would swallow the others.
    """
    po_qty = float(po_line["QUANTITY"])
    po_price = float(po_line["UNIT_PRICE"])
    qty = float(invoice["invoiced_qty"])
    price = float(invoice["unit_price"])
    amount = float(invoice["amount"])

    if invoice["duplicate_of"]:
        return "DUPLICATE"
    # Per FEE, against the contract -- not "does the invoice carry a fee". Invoices routinely carry a
    # legitimately permitted fee, which is part of the expected total and not a fault. This branch used
    # to return on any non-empty fee list; it agreed with the labels only while no contract permitted
    # anything, and started disagreeing on 10 cases the moment permitted fees existed. That failure is
    # the point of those cases: the naive rule is now wrong, and this classifier had to stop using it.
    fees = json.loads(invoice["extra_fees"] or "[]")
    allowed = contract.get("allowed_fees") or []
    unpermitted = [f for f in fees if f.get("label") not in allowed]
    if unpermitted:
        return "OFF_CONTRACT_FEE"
    if int(invoice["item_id"]) not in contract["allowed_item_ids"]:
        return "OFF_CONTRACT_ITEM"
    if qty != po_qty:
        return "QTY_MISMATCH"
    if abs(price - po_price) > 0.005:
        total_delta = abs(price - po_price) * po_qty
        return "PRICE_DELTA" if total_delta > MATERIAL_DELTA else "SUBTHRESHOLD_PRICE"
    # A PERMITTED fee is part of what we agreed to pay, so it belongs in the expected total. Leaving it
    # out makes every clean invoice that carries one look like a rounding discrepancy the size of the
    # fee -- which is how this classifier failed when permitted fees were introduced, and the same
    # mistake an artifact makes if it computes the expected total as quantity times price alone.
    permitted_total = sum(f.get("amount") or 0.0 for f in fees if f.get("label") in allowed)
    math_delta = abs(amount - (qty * price + permitted_total))
    if math_delta > 0.005:
        return "TOTAL_MATH" if math_delta > MATERIAL_DELTA else "ROUNDING"
    return "CLEAN"


FLAGGED_TYPES = {"DUPLICATE", "OFF_CONTRACT_FEE", "OFF_CONTRACT_ITEM",
                 "QTY_MISMATCH", "PRICE_DELTA", "TOTAL_MATH"}

#: Mirrors BUILD_WITHHELD_FAULTS in scripts/gen_o2c_eval_and_fixture.py. Duplicated rather than
#: imported: the test is meant to fail if the generator's list changes without anyone deciding to
#: change it here too. Four entries, not the original two -- the two contract-dependent faults now go
#: together, and re-invoicing joined them; see that constant for the inclusion test.
WITHHELD_FROM_BUILD = {"OFF_CONTRACT_FEE", "TOTAL_MATH", "OFF_CONTRACT_ITEM", "DUPLICATE"}


def derive_scenarios(rows: list[dict], po_lines: list[dict], contracts: dict) -> list[str]:
    by_line = {r["PO_LINE_ID"]: r for r in po_lines}
    return [classify(r, by_line[r["po_line_id"]], contracts[r["supplier_id"]]) for r in rows]


# --- the scored set is untouched -----------------------------------------------------------------

def test_the_scored_cases_are_the_first_rows_of_the_invoice_fixture(invoices, labels):
    """Order is load-bearing, not cosmetic: it is what lets a regrow be diffed against the previous
    generation to show the scored rows did not move."""
    assert len(labels) == SCORED_CASES
    scored_numbers = [r["invoice_number"] for r in labels]
    assert [r["invoice_number"] for r in invoices[:SCORED_CASES]] == scored_numbers


def test_the_label_file_covers_only_the_scored_cases(labels, invoices):
    """labels.csv is bundled into the task-env container, so it must never grow to describe the
    background rows -- their key lives outside the bundle instead (KNOWN_ISSUES.md section 8)."""
    assert len(labels) == SCORED_CASES < len(invoices)
    assert {r["id"] for r in labels} == {f"p2p-{i:04d}" for i in range(1, SCORED_CASES + 1)}


def test_every_scored_case_has_a_sample_directory_and_vice_versa(labels):
    on_disk = {p.name for p in (TASK / "eval_data").iterdir()
               if p.is_dir() and p.name.startswith("p2p-")}
    assert on_disk == {r["id"] for r in labels}


def test_a_not_flagged_sample_declares_no_fault_type():
    """A clean invoice has no fault to classify, and the scoring function treats these as not
    applicable before it reads the field -- so a name there would describe the answer while being
    scored by nothing. The scenario name is kept in labels.csv, which is never scored against."""
    for sample_dir in sorted((TASK / "eval_data").glob("p2p-*")):
        oracle = json.loads(json.loads((sample_dir / "sample.json").read_text())
                            ["expected_output"]["json"])
        if not oracle["flagged"]:
            assert oracle["fault_type"] is None, sample_dir.name


# --- the properties the loader depends on --------------------------------------------------------

def test_no_invoice_number_appears_twice(invoices):
    """The invoices table keys on invoice number and the loader skips conflicts silently, so a
    duplicate here would drop a row without complaint. The generator skips colliding substrate
    candidates rather than renaming them precisely so this holds (KNOWN_ISSUES.md section 2)."""
    numbers = [r["invoice_number"] for r in invoices]
    dupes = {n for n in numbers if numbers.count(n) > 1}
    assert not dupes, f"duplicate invoice numbers would be silently dropped at load: {sorted(dupes)}"


def test_no_background_row_collides_with_a_scored_invoice(invoices, labels):
    """The failure this guards is not a missing row but a scored case reading a DIFFERENT invoice
    than its answer key describes -- a plausible wrong score with nothing in the logs."""
    scored = {r["invoice_number"] for r in labels}
    background = {r["invoice_number"] for r in invoices[SCORED_CASES:]}
    assert not (scored & background)


def test_every_invoice_resolves_to_a_po_line_in_the_fixture(invoices, po_lines):
    """The invoices table has a foreign key onto po_lines; a dangling reference fails the load."""
    available = {r["PO_LINE_ID"] for r in po_lines}
    dangling = {r["po_line_id"] for r in invoices} - available
    assert not dangling, f"invoices reference absent PO lines: {sorted(dangling)[:10]}"


def test_po_lines_are_unique(po_lines):
    ids = [r["PO_LINE_ID"] for r in po_lines]
    assert len(ids) == len(set(ids))


def test_every_invoice_supplier_is_in_the_contracts(invoices):
    contracts = json.loads((FIXTURE / "contracts.json").read_text())
    missing = {r["supplier_id"] for r in invoices} - set(contracts)
    assert not missing, f"invoices from suppliers with no contract: {sorted(missing)}"


# --- the background is dilution, not decoration --------------------------------------------------

def test_the_background_is_large_enough_to_hide_the_scored_set(invoices):
    """Before the background existed the database held exactly the 60 scored invoices, so an agent
    that browsed it recovered the whole eval set. The specific number matters less than the order of
    magnitude; this asserts the leak is actually closed rather than narrowed."""
    assert len(invoices) > 10 * SCORED_CASES


def test_the_classifier_reproduces_the_known_labels(invoices, po_lines, labels, contracts):
    """The keystone. Re-derives the scenario for the 60 rows whose labels ARE committed and demands
    an exact match, which is what licenses the two tests below to trust the same classifier on the
    1,035 background rows that carry no labels.

    It doubles as a drift guard: if the fixture and its answer key ever disagree, or if the
    materiality constants above stop matching what generated the data, this fails first and says so.
    """
    derived = derive_scenarios(invoices[:SCORED_CASES], po_lines, contracts)
    mismatches = [(lab["invoice_number"], lab["fault_type"], got)
                  for lab, got in zip(labels, derived, strict=True)
                  if lab["fault_type"] != got]
    assert not mismatches, (
        "re-derivation disagrees with labels.csv (invoice, labelled, derived): "
        f"{mismatches[:10]}"
    )


def test_the_background_carries_a_fault_mix_like_the_scored_set(invoices, po_lines, labels, contracts):
    """The substrate is clean on every dimension the task scores, so background rows only carry
    faults because they are injected. Clean background would put a 0% fault rate next to the scored
    set's 68% and the scored rows would stand out by inspection again -- which is the whole thing
    the background is for (KNOWN_ISSUES.md section 3)."""
    background = derive_scenarios(invoices[SCORED_CASES:], po_lines, contracts)
    scored_rate = sum(r["flagged"] == "True" for r in labels) / len(labels)
    background_rate = sum(s in FLAGGED_TYPES for s in background) / len(background)

    assert background_rate > 0.4, "a near-clean background does not hide a 68%-faulty scored set"
    assert abs(scored_rate - background_rate) < 0.15, (
        f"fault rates diverge too far to be indistinguishable: scored {scored_rate:.0%} vs "
        f"background {background_rate:.0%}"
    )


def test_every_fault_type_appears_in_the_background(invoices, po_lines, labels, contracts):
    """Per-type coverage, not just the headline rate: a background missing a fault type makes the
    scored cases of that type identifiable, and leaves the future build-env carve with no example
    of it to include."""
    scored_types = {r["fault_type"] for r in labels if r["fault_type"]}
    background_types = set(derive_scenarios(invoices[SCORED_CASES:], po_lines, contracts))
    assert scored_types <= background_types, f"missing from background: {scored_types - background_types}"


def test_the_injection_signatures_no_longer_identify_a_scored_case(invoices):
    """The remaining fault injections leave signatures -- a fabricated 'ITEM-X' number. Each of those
    used to mark a scored invoice and nothing else. They are still unnatural (KNOWN_ISSUES.md section 7)
    but they must no longer be diagnostic of the eval set."""
    for signature, field in (("ITEM-X", "item_number"),):
        scored_hits = sum(signature in r[field] for r in invoices[:SCORED_CASES])
        background_hits = sum(signature in r[field] for r in invoices[SCORED_CASES:])
        assert scored_hits, f"expected the fixture to contain {signature} rows at all"
        assert background_hits > scored_hits, (
            f"{signature!r} appears on {scored_hits} scored rows but only {background_hits} "
            f"background rows -- it still points at the eval set"
        )


def test_no_invoice_number_announces_that_it_is_a_duplicate(invoices):
    """The '-DUP' suffix is GONE rather than diluted, and this is what keeps it gone.

    It used to be the second signature checked above, tolerated because the background rows diluted it
    29:1 so it no longer pointed at the eval set. Dilution was the wrong bar. Three things were wrong
    with it that volume could not fix:

      * an artifact could flag every duplicate by matching the row's NAME, so the eval set could not
        distinguish a real implementation of the rule from a string match;
      * no ledger renames a document to announce it is a copy;
      * `INV-\\d+` -- a reasonable regex, and one a real artifact wrote -- captures `INV-3046` out of
        `INV-3046-DUP` and stops, so the lookup misses and the case scores zero with the duplicate rule
        correctly implemented. Four of six measured build phases lost exactly the three duplicate cases
        this way, and it read as a difficulty result rather than as a data defect.

    Duplicates now carry an offset number in a range the substrate does not use, so `duplicate_of` is
    the only in-store signal that a row is a copy -- which is the whole point, because that is the field
    the elicited rule names. Asserted over the WHOLE store, scored rows and background alike: fixing one
    population and not the other would turn the shape into a sharper tell than it was to begin with.
    """
    import re
    marked = [r["invoice_number"] for r in invoices if not re.fullmatch(r"INV-\d+", r["invoice_number"])]
    assert not marked, (
        f"{len(marked)} invoice number(s) are not plain `INV-<digits>`, so the row's own name betrays "
        f"its fault: {marked[:5]}"
    )
    truncated = [n for n in (r["invoice_number"] for r in invoices)
                 if re.search(r"INV-\d+", n).group(0) != n]
    assert not truncated, f"an `INV-\\d+` match does not span these numbers: {truncated[:5]}"
    assert any(r["duplicate_of"] for r in invoices), \
        "no row carries duplicate_of, so the duplicate rule has become unscoreable"


def test_neither_phase_bundles_an_answer_key():
    """Each phase's fixture directory is copied wholesale into the task-env container, so whatever is
    in it ships. Both must hold exactly the three data files and nothing else -- in particular no
    answer key, which now lives at seed_data/labels.csv and enters no container at all (this is what
    KNOWN_ISSUES.md section 8 used to flag as an open latent leak)."""
    expected = {"invoices.csv", "po_lines_used.csv", "contracts.json"}
    for fixture in (FIXTURE, BUILD_FIXTURE):
        assert {p.name for p in fixture.iterdir() if p.is_file()} == expected, fixture
    assert LABELS.exists(), "the answer key should still exist, host-side"


def test_the_background_carries_no_committed_answer_key():
    """Removed on purpose: what a background row is, is re-derivable from the fixture (see
    `classify`), so a committed label file would be duplicated state whose only possible future is
    to drift from the data it describes. If one comes back, decide deliberately rather than by
    accident -- and keep it out of fixture/ per the test above."""
    assert not (TASK / "seed_data" / "background_labels.csv").exists()


# --- the store is single-currency, deliberately ---------------------------------------------------

def test_the_whole_store_is_denominated_in_one_currency(invoices, po_lines, contracts):
    """Locks in the decision recorded in KNOWN_ISSUES.md section 1.

    The materiality rule is stated in DOLLARS ($500 material, $3 rounding tolerance) and compared as a
    plain number, so a mixed-currency store made the rule meaningless -- a EUR 501 delta counted as
    exceeding "$500", and the answer key printed dollar signs on euro invoices. Every amount here is
    USD so the threshold and the data agree about what a unit is.

    If someone reintroduces a second currency, this fails and sends them to the known-issues entry:
    putting currencies back requires a currency-mismatch fault type and per-currency thresholds, not
    just different data.
    """
    assert {r["currency"] for r in invoices} == {"USD"}
    assert {r["VEN_CURRENCY"] for r in po_lines} == {"USD"}
    assert {c["currency"] for c in contracts.values()} == {"USD"}


def test_every_invoice_currency_matches_its_suppliers_contract_currency(invoices, contracts):
    """Redundant while the store is single-currency, kept because it is the property that actually
    matters: an invoice must never disagree with the contract it is judged against. If currencies are
    ever reintroduced this is the check that still means something, whereas the one above becomes the
    thing to delete."""
    for r in invoices:
        expected = contracts[r["supplier_id"]]["currency"]
        assert r["currency"] == expected, (
            f"{r['invoice_number']} carries {r['currency']} but supplier {r['supplier_id']} "
            f"trades in {expected} -- see KNOWN_ISSUES.md section 1 before changing this"
        )


# --- the build/eval carve -------------------------------------------------------------------------
#
# The repo-wide guard (scripts/carve_guard.py) checks disjointness, closure and coverage generically
# for every split task, so these do not repeat it. What they pin is the task-specific shape: that the
# build share is a SUBSET of the eval store, and the two overlaps that are invisible until you look.

def _build_rows(name: str) -> list[dict]:
    with open(BUILD_FIXTURE / name, newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def build_invoices() -> list[dict]:
    return _build_rows("invoices.csv")


@pytest.fixture(scope="module")
def build_po_lines() -> list[dict]:
    return _build_rows("po_lines_used.csv")


def test_the_build_share_is_a_subset_of_the_eval_store(build_invoices, invoices):
    """The carve is asymmetric: eval keeps the full store, build gets a share carved out of it.

    Asserted as set containment rather than a count, because a build row NOT in the eval store would
    mean the build share was authored rather than carved -- which breaks score parity as an acceptance
    test and reintroduces the identifier-collision risk carving avoids by construction.
    """
    eval_numbers = {r["invoice_number"] for r in invoices}
    build_numbers = {r["invoice_number"] for r in build_invoices}
    assert build_numbers < eval_numbers, "the build share must be a strict subset of the eval store"


def test_the_build_share_holds_no_scored_invoice(build_invoices, labels):
    """The rule the whole split exists for."""
    scored = {r["invoice_number"] for r in labels}
    assert not (scored & {r["invoice_number"] for r in build_invoices})


def test_the_build_share_shares_no_purchase_order_with_a_scored_case(build_invoices, invoices):
    """Withholding the scored invoices is not enough, and this is the overlap that proves it.

    A purchase order has several lines. A scored case names its PO number in the prompt, and a
    background invoice can sit on a DIFFERENT line of that same PO -- so the PO the case names would be
    readable during BUILD while every scored invoice was correctly withheld. 37 rows were in that
    position; they stay in the eval store and are withheld from the build share.
    """
    scored_pos = {r["po_number"] for r in invoices[:SCORED_CASES]}
    leaked = scored_pos & {r["po_number"] for r in build_invoices}
    assert not leaked, f"build share shares a PO with a scored case: {sorted(leaked)[:10]}"


def test_the_build_share_reuses_no_invented_item_from_a_scored_case(build_invoices, invoices,
                                                                   contracts):
    """The second overlap. An off-contract-item fault invents an item number, and the background
    injects the same fault type, so the two sides collide on a handful of invented ids.

    Real catalogue numbers are deliberately NOT covered here: there are only twenty of them and they
    are shared attributes rather than records a case touches, so excluding on those would collapse the
    build share to a few dozen rows.
    """
    real_items = {int(i) for c in contracts.values() for i in c["agreed_unit_price"]}
    invented = {r["item_number"] for r in invoices[:SCORED_CASES]
                if int(r["item_id"]) not in real_items}
    assert invented, "expected the scored set to contain invented off-contract items at all"
    leaked = invented & {r["item_number"] for r in build_invoices}
    assert not leaked, f"build share reuses a scored case's invented item: {sorted(leaked)}"


def test_the_build_share_keeps_coverage_of_every_field_the_guard_can_see(build_invoices, invoices):
    """Small is fine, an accidental gap is not. Scoped to the fields a fixture actually carries:
    supplier, unit, currency. Fault type is NOT among them and is not checked here -- four fault types
    are withheld from the build share on purpose (BUILD_WITHHELD_FAULTS in the generator), and that is
    a difficulty choice. A missing supplier or unit of measure is still a bug, and this check is what
    earns the difficulty choice the right to be called deliberate: withholding 354 rows by fault must
    not also cost a supplier or a unit of measure, which would be a carve defect wearing the same
    clothes."""
    for field in ("supplier_id", "uom", "currency"):
        assert {r[field] for r in build_invoices} == {r[field] for r in invoices}, field


def test_the_build_share_holds_no_example_of_a_withheld_fault_type(build_invoices, build_po_lines,
                                                                  contracts):
    """The point of this carve: no example of any withheld fault anywhere in the build share, so none
    of those rules can be induced from data instead of elicited from the customer. Four categories as
    of the hardening -- both contract-dependent faults, the materiality-threshold one, and
    re-invoicing. PRICE_DELTA and QTY_MISMATCH are deliberately still here: an agent can derive those
    two from the invoice and its PO line without asking anyone, so withholding them would price a
    failure to compare two numbers as a failure to elicit."""
    found = {}
    for inv, verdict in zip(build_invoices,
                            derive_scenarios(build_invoices, build_po_lines, contracts),
                            strict=True):
        if verdict in WITHHELD_FROM_BUILD:
            found.setdefault(verdict, []).append(inv["invoice_number"])
    assert not found, f"withheld fault types in the build share: {({k: v[:5] for k, v in found.items()})}"


def test_the_withheld_fault_types_are_still_present_in_the_eval_store(invoices, po_lines, contracts):
    """The other half of the same invariant -- withheld from BUILD, never removed from EVAL. Were a
    regeneration to drop them from both, the two categories would silently stop being scored and the
    build-side test above would still pass."""
    seen = set(derive_scenarios(invoices, po_lines, contracts))
    missing = WITHHELD_FROM_BUILD - seen
    assert not missing, f"eval store holds no case at all of {sorted(missing)}"


def test_the_build_share_still_shows_that_fees_exist(build_invoices):
    """Withholding the fee FAULT must not withhold fees themselves. The build share has to contain
    invoices carrying permitted fees, or the agent never learns the field is populated and the eval
    cases turn unanswerable rather than harder."""
    with_fees = [r for r in build_invoices if json.loads(r["extra_fees"] or "[]")]
    assert len(with_fees) > 50, f"only {len(with_fees)} build invoices carry a fee"


def test_the_build_share_marks_no_invoice_as_a_duplicate(build_invoices):
    """The accepted cost of withholding the re-invoicing fault, pinned so it stays a decision.

    Every duplicate the store holds IS the fault -- the marker naming the duplicated invoice is only
    ever populated by that injection -- so withholding the category empties the column across the whole
    build share. Unlike the fee field above there is no benign form to leave behind, and the choice was
    to accept the empty column rather than to keep the category: an agent that notices a column that
    never carries a value has a reason to ask what it is for, which is the behaviour being measured,
    and an agent that concludes re-invoicing does not happen scores zero on the category at eval.
    Neither reading is misled, because the column is empty rather than wrong.

    Asserting emptiness rather than tolerating it is the point: a later regeneration that quietly
    repopulated this column would hand back a worked example of the exact rule the carve withholds,
    and nothing else in the suite would notice."""
    marked = [r["invoice_number"] for r in build_invoices if r["duplicate_of"]]
    assert not marked, f"build share marks {len(marked)} invoice(s) as duplicates: {marked[:5]}"


def test_the_build_share_still_shows_that_every_item_can_be_on_contract(build_invoices, contracts):
    """The other half of withholding both contract-dependent faults. Every item the build share shows
    is on its supplier's contract, which is what makes the category unlearnable -- but the contracts
    themselves are bundled into both phases identically, so the agent can still see that an
    item-to-contract relation EXISTS and that the invoices it holds satisfy it. Without that the
    withholding would read as "contracts are irrelevant" rather than "you have not been shown a
    violation", and the distinction is the whole difficulty argument."""
    off_contract = [r["invoice_number"] for r in build_invoices
                    if int(r["item_id"]) not in contracts[r["supplier_id"]]["allowed_item_ids"]]
    assert not off_contract, f"off-contract items in the build share: {off_contract[:5]}"
    assert all(contracts[r["supplier_id"]]["allowed_item_ids"] for r in build_invoices), \
        "a build-share supplier has an empty contract, so the relation is unobservable"


def test_the_two_phases_are_declared_separately_in_the_spec():
    """A carve on disk means nothing while both phases still load the same directory -- the guard skips
    a task it sees as unsplit, so that mistake looks like success. Pins the declaration itself."""
    import yaml
    spec = yaml.safe_load((TASK / "spec.yaml").read_text())
    envs = spec["task_runner"]["environments"]
    srcs = {phase: sorted(e["src"] for e in envs[phase]["bundle"]) for phase in ("build", "eval")}
    assert srcs["build"] != srcs["eval"], "both phases still load the same sources"
