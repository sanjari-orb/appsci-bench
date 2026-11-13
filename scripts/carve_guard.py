"""Repo-wide checks on a task's build/eval data split (the "carve").

A run brings the task environment up twice: the build env the AppSci agent explores, and the eval env
the artifact is scored against. The build env must hold no record any eval case touches -- otherwise the
agent can read the data it is about to be scored on and shape its artifact around those rows rather than
around the requirements it elicited. See docs/task_env_split.md and docs/task_env_carve_spec.md.

This module is the checking, factored out of the tests so ONE implementation serves both callers:

  * tests/appsci_bench/core/test_carve_guards.py  -- runs it over every real task in the repo;
  * tests/appsci_bench/core/test_carve_fires.py   -- runs it over synthetic tasks with planted
    leaks, which is the only thing proving the checks fire at all. No task has split yet, so the
    repo-wide run currently skips every task and would pass with the logic deleted.

Nothing here is per-task. A task owner writes data and scripts; the checks infer what to look for from
the data itself, so a task that splits next year is covered without anyone editing this file. The one
exception is deliberate and lives in the task's own spec rather than here -- see `coverage_waiver`.

DELIBERATE LIMITS, so a skip is never mistaken for a pass:
  * Referential closure and category coverage need parsed records. Where a phase's data is in a form
    this module cannot parse, the check reports SKIPPED with a reason rather than passing.
  * A relational task gets closure from its own database, which rejects a load violating its declared
    foreign keys -- a broken carve fails at load time, before any test runs.
  * Reproducing a carve from its script is not checked here: running a data generator inside the suite
    is slow, and it is a one-off property of one commit, so it belongs in review of the carve itself.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import pathlib
import re
from collections import defaultdict
from typing import Any

import yaml

#: Files this module knows how to read as records. Anything else is unparseable, not empty.
_PARSEABLE = {".json", ".jsonl", ".csv"}
#: Files worth scanning as raw text for leaked identifiers (a script naming a record counts).
_TEXTUAL = _PARSEABLE | {".sql", ".md", ".txt", ".yaml", ".yml", ".sh", ".py"}

#: A token is a candidate identifier when it mixes letters and digits: `emma_kim_9957`, `EHGLP3`,
#: `HAT156`, `INV-3046-DUP`, `PO-000681`. Pure words and pure numbers are not.
#:
#: HYPHENS ARE PART OF A TOKEN. Without them, a hyphenated identifier is not seen at all: `INV-3046-DUP`
#: splits into `INV`, `3046`, `DUP`, and every fragment then fails the letters-and-digits rule below --
#: two are pure letters and too short, one is pure digits. So the check silently recognised nothing in a
#: task whose identifiers use hyphens (order_to_cash: invoice, PO, item and case ids all do), which is
#: worse than failing: it would pass a build share that leaked every scored record.
#:
#: WHY TWO LETTERS AND NOT ONE. Splitting on non-word characters turns the timestamp
#: `2024-05-04T23:12:06` into `2024-05-04T23` and `12`, `06`. The first mixes a letter and digits and
#: would match a one-letter rule -- and timestamps appear on both sides of any carve, so every correct
#: carve would be flagged. Requiring two letters drops it while keeping every real identifier shape in
#: the repo's tasks. (The store-key filter in `identifier_tokens` is the second line of defence: a
#: timestamp fragment is not a record identifier in anyone's store.)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{5,}")
_MIN_LETTERS = 2

#: A field is treated as an enumeration when the full store holds few distinct values for it: tier,
#: cabin, status, fault type. Identifiers, timestamps and free text have many. Airline's real store puts
#: `membership` (3), `cabin` (3), `flight_type` (2) and `insurance` (2) under this line and leaves
#: `user_id`, `dob`, `created_at` and airport codes above it.
_MAX_ENUM_VALUES = 12

#: How many distinct values an identifier-shaped field needs, as a fraction of its collection's record
#: count, before its values count as record identifiers. A real key approaches 1.0 (every row has its
#: own); a shared reference sits far below (order_to_cash: 40 item numbers across 1,095 invoices, 0.04).
#: Half is a wide margin either side of everything in the repo's tasks -- airline's own keys land at or
#: above 1.0, its nested payment-method ids above 1.0, and no shared value comes near.
_KEY_CARDINALITY = 0.5


@dataclasses.dataclass
class CheckResult:
    """One check's outcome.

    Four states, and the distinctions between them are the whole point:

      * `errors` empty and nothing else set -- a pass.
      * `errors` non-empty -- a failure, listing what is wrong.
      * `skipped` set -- NOT a pass. The check could not see the data, so the carve is unverified in
        that dimension, and the CLI and CI both treat it as a failure.
      * `waived` set -- the task owner has recorded a decision to accept SPECIFIC findings of this
        check. Those findings still appear, in full, in `findings`; only their failure is suppressed. A
        waiver that emptied the report would be a silent skip wearing a label, which is the thing it must
        not be.

    `waived` and `errors` can BOTH be set, and that combination is the point of naming what a waiver
    accepts: a task whose owner accepted eight coverage gaps and has just grown a ninth is waived for the
    eight and failing for the one. So `ok` reads `errors` and nothing else, and the report prints both
    blocks -- the failures, then the acknowledged findings under the owner's reason.

    `findings` also carries the output of a check that reports rather than judges (schema
    documentation), so a reviewer sees the observation without a correct carve being blocked on it.
    """

    name: str
    errors: list[str] = dataclasses.field(default_factory=list)
    skipped: str | None = None
    #: Reported, never failing: waived findings, and observations from an advisory check.
    findings: list[str] = dataclasses.field(default_factory=list)
    #: The task owner's recorded reason for accepting the findings the waiver names.
    waived: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def __str__(self) -> str:
        if self.skipped:
            return f"{self.name}: SKIPPED ({self.skipped})"
        if self.errors:
            head = f"{self.name}: FAILED"
        elif self.waived:
            head = f"{self.name}: WAIVED ({self.waived})"
        else:
            head = f"{self.name}: ok"
        lines = list(self.errors)
        if self.findings:
            if self.errors and self.waived:
                # A partly-waived check. The reason has to reach the reader even though the verdict is
                # FAILED, because the findings below it are printed on that reason's authority.
                lines.append(f"and WAIVED ({self.waived}) -- which accepts the {len(self.findings)} "
                             f"finding(s) below, and none of the failures above")
            lines += self.findings
        # A line may itself be multi-line (the waiver's copy-me list of enumerations to add), so
        # continuations are indented with their line rather than left hanging at column zero.
        return head + "".join("\n  " + line.replace("\n", "\n  ") for line in lines)


#: Where a waiver is declared, quoted in every message that asks an author to edit one. One spelling, so
#: a failure message and the schema and the docs cannot drift apart.
_WAIVER_PATH = "task_runner.carve.coverage_waiver"


@dataclasses.dataclass(frozen=True)
class CoverageWaiver:
    """A task owner's recorded decision to accept named coverage gaps, and only those.

    TWO PARTS, AND BOTH ARE LOAD-BEARING. `reason` is why the gap is acceptable. `enumerations` is WHICH
    gaps -- each named `collection.field`, exactly as the coverage check reports them, so a reviewer copies
    a reported gap into the waiver without translating it.

    The list is what stops a waiver aging into an off switch. A waiver carrying only a reason accepts
    whatever the check happens to find, today and forever: the task's data moves, a new enumerated field
    stops being covered by accident, and the old reason silently absorbs it. Naming the gaps means a gap
    nobody named is still a failure, so the waiver keeps working as a record of a decision rather than
    becoming a permanently green light.

    The price is that the list churns with the data -- banking's runs to 18 entries -- and a list that rots
    is worse than a reason that does not. That is paid for in the report rather than the schema: an unnamed
    gap fails with the exact lines to add, a named gap that no longer exists is called out as dead weight,
    and both lists are sorted so a regenerated waiver diffs minimally.
    """

    reason: str
    #: `collection.field`, in the author's order; `check_coverage` sorts before reporting.
    enumerations: tuple[str, ...] = ()


def enumeration_id(collection: str, field: str) -> str:
    """How an enumeration is NAMED, in the report and in a waiver alike -- one spelling for both.

    A waiver is only copy-pasteable if the identifier a reviewer reads in a failure is the identifier the
    spec accepts, so both go through here. It is also the most stable name available for a gap: it depends
    on the schema and not on the data, so a build share gaining rows, or a full store gaining a value,
    leaves every waiver entry still pointing at the same thing. In particular a collection that is EMPTY in
    the build share today and merely incomplete tomorrow keeps the same identifier -- the two are different
    messages but one gap, and a waiver written against the first still names the second.
    """
    return f"{collection}.{field}"


# --------------------------------------------------------------------------------------
# reading a task
# --------------------------------------------------------------------------------------

def phase_sources(task_id: str, tasks_dir: pathlib.Path) -> dict[str, list[str]]:
    """Each phase's declared data sources (the `src` entries of its bundle), by phase name.

    A phase the task declares as needing no environment maps to an empty list.
    """
    spec = yaml.safe_load((tasks_dir / task_id / "spec.yaml").read_text())
    envs = ((spec.get("task_runner") or {}).get("environments")) or {}
    out: dict[str, list[str]] = {}
    for phase in ("build", "eval"):
        decl = envs.get(phase)
        if not isinstance(decl, dict):
            out[phase] = []
            continue
        out[phase] = sorted(e["src"] for e in (decl.get("bundle") or []) if "src" in e)
    return out


def has_split(task_id: str, tasks_dir: pathlib.Path) -> bool:
    """True when the two phases load DIFFERENT data, i.e. the task has actually been carved.

    Every task currently ties both phases to one dataset with a YAML anchor, deliberately, so the
    not-yet-split state is visible in the spec. Those tasks are skipped: there is no carve to check, and
    checking would fail every one of them for the same uninteresting reason.
    """
    src = phase_sources(task_id, tasks_dir)
    return bool(src["build"]) and bool(src["eval"]) and src["build"] != src["eval"]


def coverage_waiver(task_id: str, tasks_dir: pathlib.Path) -> CoverageWaiver | None:
    """The task owner's recorded decision to accept named coverage gaps, or None.

    WHY THIS EXISTS. Coverage is the one invariant a strict carve can be unable to satisfy at all, and
    banking_support is the measured case. Withholding every record its 97 eval cases touch, plus the two
    a case is about only in prose, closed over the reference graph, withholds 760 of its 768 records: 6
    customers and 2 bank accounts survive, and nothing else -- no card, no card account, no transaction,
    no referral, no dispute, no application. That leaves 15 of its 17 collections EMPTY in the build share,
    and an empty collection covers none of its categories however the records were chosen, so coverage
    fails permanently no matter how carefully the owner chooses. On the real store that is 18 enumerated
    fields uncovered, among them every transaction type and every card type the guard infers. (The store's
    13 card product types and 15 spend categories are above `_MAX_ENUM_VALUES` and so are not inferred as
    enumerations at all -- they are absent from the build share too, but the guard never claimed to check
    them, and a report of this carve should not be read as saying it did.) A check that cannot be satisfied
    and cannot be waived is a check that gets deleted.

    WHY IT IS SCOPED TO COVERAGE, AND WHY THERE IS NO OTHER WAIVER. Disjointness and closure are the
    threat bar and the schema-integrity bar; a task that cannot meet them has a broken carve, not a hard
    one. There is deliberately no vocabulary for waiving either -- not a flag defaulted to off, no
    vocabulary at all -- so no future spec can express the idea.

    WHY THE TASK SPEC AND NOT A FILE BESIDE THE BUILD SHARE. Three reasons, in order of weight:
      * The build share is agent-facing by construction. A waiver enumerates the categories the build
        share does not contain, which is precisely the "report of what was withheld" that
        docs/task_env_carve_spec.md lists among the mistakes that produce a wrong number -- so it must
        not live anywhere a phase's bundle can reach. The spec is redacted before the agent sees it, by
        an allowlist that drops `task_runner` wholesale, so a waiver declared here cannot leak.
      * spec.yaml is where every other per-task declaration the harness reads already lives, including
        the two phases' bundles that this module reads three lines above. One file per task, not two.
      * It is schema-validated (schema/task_spec.schema.json), so a waiver missing its reason, or naming
        no enumerations, fails validation instead of being silently ignored.

    WHAT IT MUST SAY, AND WHY IT IS NOT JUST A REASON. A waiver names the enumerations whose
    non-coverage is accepted, as `collection.field`, alongside the reason. A gap the list does not name
    still fails. Without the list a waiver accepts whatever the check finds for as long as it exists, so
    the day banking's data changes and a field stops being covered by accident, the old reason absorbs it
    and nobody is told. Naming them costs an author a list that churns with their data; not naming them
    costs the check its meaning.

    A waiver with no readable reason is treated as ABSENT rather than honoured: the failure mode of a
    misspelt or half-written block should be "coverage still hard", never "coverage quietly off".

    A waiver whose LIST is missing, empty or malformed is NOT treated as absent -- it is honoured as a
    waiver that names nothing, which by the rule above accepts nothing and so fails on every gap. The two
    routes end in the same red line, and this one ends in a better message: the failure lists every gap
    together with the exact lines to add, where "no waiver here" would leave an author who plainly wrote
    one wondering why it did nothing. (The schema rejects the shape outright, so this path is reached only
    by a spec nobody validated.)
    """
    spec = yaml.safe_load((tasks_dir / task_id / "spec.yaml").read_text())
    carve = ((spec.get("task_runner") or {}).get("carve")) or {}
    waiver = carve.get("coverage_waiver")
    if not isinstance(waiver, dict):
        return None
    reason = str(waiver.get("reason") or "").strip()
    if not reason:
        return None
    listed = waiver.get("enumerations")
    named = tuple(v.strip() for v in listed if isinstance(v, str) and v.strip()) \
        if isinstance(listed, list) else ()
    return CoverageWaiver(reason=reason, enumerations=named)


def _files(task_id: str, tasks_dir: pathlib.Path, sources: list[str], suffixes: set[str]) -> list[pathlib.Path]:
    task = tasks_dir / task_id
    found: list[pathlib.Path] = []
    for src in sources:
        p = task / src
        if p.is_file():
            candidates = [p]
        elif p.is_dir():
            candidates = sorted(f for f in p.rglob("*") if f.is_file())
        else:
            continue
        found += [f for f in candidates if f.suffix.lower() in suffixes]
    return found


def eval_case_files(task_id: str, tasks_dir: pathlib.Path) -> list[pathlib.Path]:
    """The task's held-out eval case files, from its declared dataset directory."""
    spec = yaml.safe_load((tasks_dir / task_id / "spec.yaml").read_text())
    dataset = ((spec.get("evaluation_data") or {}).get("dataset"))
    if not dataset:
        return []
    root = pathlib.Path(dataset)
    if not root.is_absolute():
        # dataset paths are declared repo-relative (e.g. tasks/<id>/eval_data)
        root = tasks_dir.parent / root if root.parts[:1] == ("tasks",) else tasks_dir / task_id / root
    if not root.is_dir():
        return []
    return sorted(f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in {".json", ".jsonl"})


# --------------------------------------------------------------------------------------
# identifiers
# --------------------------------------------------------------------------------------

def identifier_tokens(text: str) -> set[str]:
    """Identifier-SHAPED tokens in `text`: at least 5 characters, >=2 letters, >=1 digit.

    Shape only. This is the candidate set; `store_identifiers` narrows it to tokens the task's own data
    actually uses as identifiers, which is what makes the disjointness check precise.
    """
    out = set()
    for tok in _TOKEN.findall(text):
        letters = sum(c.isalpha() for c in tok)
        if letters >= _MIN_LETTERS and any(c.isdigit() for c in tok):
            out.add(tok)
    return out


def store_identifiers(store: dict[str, list[dict]]) -> set[str]:
    """Every value the store holds in an identifier-shaped field (`id`, `*_id`, `*_number`, ...).

    Shape alone cannot tell a record identifier from an ordinary attribute that happens to contain a
    digit, and getting that wrong breaks the disjointness check in the direction that blocks correct
    work. order_to_cash names its suppliers `Supplier_1` .. `Supplier_10`: identifier-shaped, and named
    by every eval case, but an attribute shared by roughly a hundred invoices rather than a key. Treated
    as an identifier it makes a correct carve impossible -- every invoice row carries its supplier's
    name, so no build share could ever be clean.

    Asking the data settles it. A supplier name lives in `supplier_name`, which is not an
    identifier-shaped field, so it is not an identifier; `invoice_number` and `po_number` are, so their
    values are. This deliberately includes REFERENCES as well as own keys, unlike `_keys_by_collection`
    (which needs the distinction to spot dangling pointers): an eval case's invoice pointing at a PO line
    makes that PO line a record the case touches, and the rule withholds it too.

    Walks the whole record, not just its top level. Airline keeps a customer's payment methods as nested
    records each carrying its own `id`, and its eval cases name those ids -- so a top-level-only sweep
    dropped every certificate and credit card from the check.

    An identifier-shaped NAME is not enough on its own either, so the values are kept only for fields
    that behave like keys -- distinct values numbering at least `_KEY_CARDINALITY` of the collection's
    records. That is what separates a record identifier from a shared catalogue value living in an
    identifier-shaped field. order_to_cash makes the distinction concrete: `invoice_number` has 1,095
    distinct values over 1,095 rows and identifies a record; `item_number` has 40, so it names a
    catalogue entry many invoices point at. Treating the latter as a key is the supplier-name mistake
    wearing a better disguise -- it would demand the build share withhold every invoice mentioning a
    catalogue item any eval case happens to charge, which withholds records no case touches.
    """
    per_field: dict[tuple[str, str], set[str]] = defaultdict(set)
    counts: dict[str, int] = {}

    def walk(coll: str, node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and v.strip() and _is_identifier_field(str(k).lower()):
                    per_field[(coll, str(k).lower())].add(v.strip())
                else:
                    walk(coll, v)
        elif isinstance(node, list):
            for v in node:
                walk(coll, v)

    for coll, rows in store.items():
        counts[coll] = max(len(rows), 1)
        walk(coll, rows)

    ids: set[str] = set()
    for (coll, field), values in per_field.items():
        if len(values) >= _KEY_CARDINALITY * counts[coll]:
            ids |= values
    return ids


def _identifier_positions(node: Any, out: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """field name (lowercased) -> every value that field carries, at any depth in a parsed document.

    This is the structural counterpart to `identifier_tokens`, and it exists because a value's POSITION
    tells you things its spelling cannot. Banking keys its customers `123`, `125`, `126` and its bank
    accounts `01` .. `08`, and the same digits appear all over the same store as zip codes (`NY 11235`),
    incomes (`125000`), balances (`-125.0`), amounts (`$1,123.65`) and inside longer identifiers
    (`txn_28cb62125078`, `78901234567890ab`). No spelling rule separates the key from the coincidence. The
    field a value sits under does: `checking_account_id` holds an account, `address` and `annual_income`
    never do.

    DECODES JSON CARRIED IN A STRING, because that is where the golden actions live. Banking's eval cases
    hold their oracle as a JSON string, whose action arguments are themselves JSON strings -- so the
    account id a case is scored on sits two levels of encoding down, and a walker that stopped at the first
    string would see the whole oracle as one opaque blob.
    """
    if out is None:
        out = defaultdict(set)
    if isinstance(node, dict):
        for k, v in node.items():
            name = str(k).lower()
            if isinstance(v, str):
                s = v.strip()
                if s:
                    out[name].add(s)
                if s[:1] in "[{":
                    try:
                        _identifier_positions(json.loads(s), out)
                    except ValueError:
                        pass
            elif isinstance(v, bool):
                pass                      # a flag is never a record key
            elif isinstance(v, int):
                out[name].add(str(v))     # a store keyed by integer is ordinary
            else:
                _identifier_positions(v, out)
    elif isinstance(node, list):
        for v in node:
            _identifier_positions(v, out)
    elif isinstance(node, str) and node.strip()[:1] in "[{":
        try:
            _identifier_positions(json.loads(node), out)
        except ValueError:
            pass
    return out


def shape_blind_keys(full: dict[str, list[dict]]) -> dict[str, set[str]]:
    """collection -> its record keys that `identifier_tokens` can never extract from text.

    A key is blind when tokenising it on its own does not yield it back: it is under five characters, or
    carries fewer than two letters. Banking has 15 such keys across three collections -- six customers
    (`123`, `125`, `126`, and three ten-character ids with a single letter in them), eight bank accounts
    (`01` .. `08`) and one verification record. Two of those bank accounts are named by a golden action of
    a real eval case, so a build share that kept them would leak a scored record past a green
    disjointness line today.

    The blind set is what the structural pass runs on, and NOTHING else runs on it. Lowering the token
    threshold instead was the obvious fix and is the wrong one: `123` and `125` occur as free text all over
    this store as zip codes, incomes, balances and fragments of longer ids, so a shorter token would flag
    a correct carve on every one of them. Keys the text scan can already see keep going through the text
    scan, whose thresholds are tuned against airline's and order_to_cash's real data.
    """
    return {coll: blind
            for coll, keys in _keys_by_collection(full).items()
            if (blind := {k for k in keys if k not in identifier_tokens(k)})}


def _structural_leaks(task_id: str, tasks_dir: pathlib.Path, cases: list[pathlib.Path],
                      sources: dict[str, list[str]], full: dict[str, list[dict]]) -> tuple[list[str], str | None]:
    """(errors, why nothing could be checked) for the record keys the text scan is blind to.

    Structural on both sides, and symmetrically. A blind key counts as USED BY AN EVAL CASE when the case
    carries it under a field named after its collection; it counts as PRESENT IN THE BUILD SHARE when the
    build share keys a record of that collection by it, or carries it under a field named after that
    collection. Free text on either side is ignored, which is exactly what makes the pass safe on values
    a spelling rule cannot tell from a zip code.

    The price of that symmetry: a blind key named only by a SETUP SCRIPT is not seen. The text scan covers
    scripts and this pass cannot, because a script has no fields. Worth stating rather than leaving to be
    discovered -- it is the one leak shape that falls between the two passes.
    """
    blind = shape_blind_keys(full)
    if not blind:
        return [], ("every record key in the task's store is identifier-shaped, so the text scan above "
                    "already sees all of them")

    case_fields: dict[str, set[str]] = defaultdict(set)
    for f in cases:
        try:
            doc = json.loads(f.read_text(errors="ignore"))
        except ValueError:
            continue
        for name, values in _identifier_positions(doc).items():
            case_fields[name] |= values

    referenced: dict[str, set[str]] = defaultdict(set)
    for name, values in case_fields.items():
        for coll, keys in blind.items():
            if _references_collection(name, coll):
                referenced[coll] |= values & keys
    if not any(referenced.values()):
        # Ran and found nothing to look for. Not a skip: the check is measured, and the answer is that no
        # eval case names one of the store's shape-blind keys.
        return [], None

    build = load_records(task_id, tasks_dir, sources["build"])
    build_keys = _keys_by_collection(build)
    build_fields: dict[str, set[str]] = defaultdict(set)
    for rows in build.values():
        for name, values in _identifier_positions(rows).items():
            build_fields[name] |= values

    errors: list[str] = []
    for coll, wanted in sorted(referenced.items()):
        held = sorted(wanted & build_keys.get(coll, set()))
        pointed = set()
        for name, values in build_fields.items():
            if _references_collection(name, coll):
                pointed |= values & wanted
        named = sorted(pointed - set(held))
        if held:
            errors.append(
                f"the build share HOLDS {len(held)} {coll} record(s) an eval case touches, keyed "
                f"{held[:8]}{' ...' if len(held) > 8 else ''} -- keys the text scan cannot see "
                f"(under five characters, or fewer than two letters)"
            )
        if named:
            errors.append(
                f"build records point at {len(named)} withheld {coll} record(s) an eval case touches: "
                f"{named[:8]}{' ...' if len(named) > 8 else ''}"
            )
    return errors, None


def check_disjoint(task_id: str, tasks_dir: pathlib.Path) -> CheckResult:
    """No identifier used by an eval case may appear anywhere in the build phase's data.

    TWO PASSES, because one cannot cover both kinds of identifier.

    The TEXT pass reads every build file as raw text. It works the same for JSON, CSV, JSONL and a SQL
    seed, and a setup script that names a scored record is just as much of a leak as a data row that
    contains it. It has two stages -- identifier-shaped tokens in the eval cases, narrowed to the ones the
    task's own store actually uses as identifiers -- and the narrowing is what stops an ordinary attribute
    that happens to look like an id (a supplier name, a product code in prose) from being treated as a
    record key and making a correct carve unachievable. See `store_identifiers`.

    The STRUCTURAL pass covers what that shape rule cannot recognise at all: a record key too short or too
    nearly word-free to be told from a number in prose. Banking keys customers `123` and bank accounts `01`
    .. `08`, and two of those accounts are named by a real eval case's golden action -- so before this pass
    existed a build share could keep a scored record and the check stayed green. It compares POSITIONS
    rather than spellings, which is the only thing that separates account `05` from the `05` in a date. See
    `_structural_leaks` and `shape_blind_keys`.

    A skip is reported only when NEITHER pass could run. Either one alone measures the invariant for the
    identifiers it is responsible for.
    """
    res = CheckResult("disjointness")
    cases = eval_case_files(task_id, tasks_dir)
    if not cases:
        res.skipped = "task declares no eval dataset"
        return res

    sources = phase_sources(task_id, tasks_dir)
    full = load_records(task_id, tasks_dir, sources["eval"])
    text_skip = _text_leaks(res, task_id, tasks_dir, cases, sources, full)
    struct_errors, struct_skip = _structural_leaks(task_id, tasks_dir, cases, sources, full)
    res.errors += struct_errors

    if text_skip and struct_skip and not res.errors:
        res.skipped = f"{text_skip}; and structurally, {struct_skip}"
    return res


def _text_leaks(res: CheckResult, task_id: str, tasks_dir: pathlib.Path, cases: list[pathlib.Path],
                sources: dict[str, list[str]], full: dict[str, list[dict]]) -> str | None:
    """Append the text pass's findings to `res`; return why it could not run, or None."""
    candidates: set[str] = set()
    for f in cases:
        candidates |= identifier_tokens(f.read_text(errors="ignore"))
    if not candidates:
        return "no identifier-shaped tokens found in the eval cases"

    known = store_identifiers(full)
    if not known:
        # Reported rather than silently falling back to shape alone: without the store we cannot tell a
        # key from an attribute, and the fallback fails correct carves (see `store_identifiers`).
        return ("the eval phase's records could not be parsed, so an eval case's identifiers "
                "cannot be told apart from ordinary attributes")
    wanted = candidates & known
    if not wanted:
        return "no eval-case token matches an identifier in the task's own store"

    build_files = _files(task_id, tasks_dir, sources["build"], _TEXTUAL)
    if not build_files:
        return "no readable files under the build phase's declared sources"

    for f in build_files:
        hits = sorted(identifier_tokens(f.read_text(errors="ignore")) & wanted)
        if hits:
            res.errors.append(
                f"{f.relative_to(tasks_dir)} contains {len(hits)} identifier(s) an eval case uses: "
                f"{hits[:8]}{' ...' if len(hits) > 8 else ''}"
            )
    return None


# --------------------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------------------

class Store(dict):
    """collection name -> records, carrying two things a plain dict cannot.

    `declared_keys` is the primary keys a source file states by KEYING ITS COLLECTIONS ON THEM, which is
    the only reliable place to read a primary key from. Inferring it from field NAMES works only where the
    field is named after its collection, and banking_support shows how often it is not: its debit cards
    are keyed by `card_id`, its credit-card accounts by `account_id`, its transaction histories by
    `transaction_id`, its closure reasons by `record_id`. Name inference finds a key for 3 of its 17
    collections, so closure would let a build share keep a payment whose card account was withheld.

    `notes` is the prose a store attaches to a collection to document its fields (again banking's shape).
    It is documentation, not a record, so it never enters the record lists -- but it is worth knowing
    whether it survived into the build share, since the agent explores the build env and reads those field
    descriptions as the schema. See `check_schema_notes`.

    A dict subclass rather than a new type so every existing caller -- `check_closure(build, full)`,
    `full.items()`, `build.get(coll)`, `assert build == {}` -- keeps working untouched, and so a plain
    dict handed in by a test still behaves (every reader goes through `_declared_keys`, which defaults).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.declared_keys: dict[str, set[str]] = {}
        self.notes: dict[str, str] = {}


def _declared_keys(store: dict[str, list[dict]]) -> dict[str, set[str]]:
    """`store`'s file-declared primary keys, empty for a plain dict built by hand in a test."""
    return getattr(store, "declared_keys", {}) or {}


def _documented_collections(store: dict[str, list[dict]]) -> dict[str, str]:
    """`store`'s per-collection documentation prose, empty where the shape carries none."""
    return getattr(store, "notes", {}) or {}


def _wrapped_collections(data: dict) -> dict[str, dict] | None:
    """`{collection: {"data": {id: record, ...}, "notes": "<prose>"}}`, or None if that is not the shape.

    banking_support's store is this, and before it was recognised the checker could not read banking at
    all. The store-of-collections branch below rejects it -- a wrapper's values are not all dicts, because
    `notes` is a string -- so it fell through to the keyed-single-collection branch and the whole 768-record
    file parsed as ONE pseudo-collection of 17 rows whose "records" were the collection wrappers. Closure
    then found no keys and coverage inferred no enumerations, so both reported NOT MEASURED, which the CLI
    and the CI test each treat as a failure. Banking was uncheckable, not merely unchecked.

    The `notes` string must never become a record: it is the task owner's field documentation, and one
    prose blob per collection dropped into the record list would give every collection a phantom row and
    put a paragraph of English where coverage looks for category values.

    Declining rather than guessing is the point of the strictness below. Anything beside `data` has to be
    a scalar; a second dict-of-dicts sibling means this is some other shape, and reading it as this one
    would misreport what a phase holds -- worse than reporting that it could not be read.
    """
    if not data or not all(isinstance(v, dict) and "data" in v for v in data.values()):
        return None
    for wrapper in data.values():
        rows = wrapper["data"]
        # An EMPTY collection is legitimate and must stay recognisable: banking ships five (no dispute has
        # been filed yet). Requiring non-empty here is what the store-of-collections branch below does,
        # and it is why that branch could not have been widened to cover this shape instead.
        if not isinstance(rows, dict) or not all(isinstance(r, dict) for r in rows.values()):
            return None
        if any(isinstance(v, (dict, list)) for k, v in wrapper.items() if k != "data"):
            return None
    return data


def _records_from(path: pathlib.Path) -> tuple[dict[str, list[dict]], dict[str, set[str]], dict[str, str]]:
    """One file's (records by collection, file-declared primary keys, per-collection documentation).

    All three are empty when the shape is not recognised.
    """
    stem = path.stem
    try:
        if path.suffix.lower() == ".csv":
            with path.open(newline="") as fh:
                return {stem: [dict(r) for r in csv.DictReader(fh)]}, {}, {}
        if path.suffix.lower() == ".jsonl":
            rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            return {stem: [r for r in rows if isinstance(r, dict)]}, {}, {}
        data = json.loads(path.read_text())
    except (ValueError, OSError, UnicodeDecodeError):
        return {}, {}, {}

    if isinstance(data, list):
        return {stem: [r for r in data if isinstance(r, dict)]}, {}, {}
    if not isinstance(data, dict):
        return {}, {}, {}
    # A documented store of collections: {"users": {"data": {id: {...}}, "notes": "..."}, ...} -- banking's
    # shape. Tried FIRST, because the two branches below both match it and both read it wrongly.
    wrapped = _wrapped_collections(data)
    if wrapped is not None:
        return ({name: list(w["data"].values()) for name, w in wrapped.items()},
                {name: set(w["data"].keys()) for name, w in wrapped.items()},
                {name: str(w.get("notes") or "") for name, w in wrapped.items()})
    # A store of collections: {"users": {id: {...}}, ...} -- airline's shape.
    if data and all(isinstance(v, dict) and v and all(isinstance(r, dict) for r in v.values())
                    for v in data.values()):
        return ({name: list(rows.values()) for name, rows in data.items()},
                {name: set(rows.keys()) for name, rows in data.items()}, {})
    # A single collection keyed by id: {id: {...}} -- order_to_cash's contracts.
    if data and all(isinstance(v, dict) for v in data.values()):
        return {stem: list(data.values())}, {stem: set(data.keys())}, {}
    return {}, {}, {}


def load_records(task_id: str, tasks_dir: pathlib.Path, sources: list[str]) -> Store:
    """Every record this module can parse under `sources`, grouped by collection name, deduplicated.

    Deduplication is not tidiness. A task may legitimately ship the same records more than once -- as
    airline does today, holding one bundled store beside the same rows unbundled per collection -- and
    counting them twice would misreport how much data a phase holds.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    out: Store = Store()
    keys: dict[str, set[str]] = defaultdict(set)
    notes: dict[str, str] = {}
    for f in _files(task_id, tasks_dir, sources, _PARSEABLE):
        rows_by_coll, declared, documented = _records_from(f)
        for name, rows in rows_by_coll.items():
            # Created even when it holds nothing. Banking ships five collections with no rows yet (no
            # dispute has been filed), and dropping them would report its store as 12 collections rather
            # than 17 -- and would make a phase holding an empty collection indistinguishable from one that
            # does not have that collection at all, which is the difference between "no disputes yet" and
            # "the carve lost the disputes table".
            out.setdefault(name, [])
            for r in rows:
                fingerprint = json.dumps(r, sort_keys=True, default=str)
                if fingerprint in seen[name]:
                    continue
                seen[name].add(fingerprint)
                out[name].append(r)
        for name, ks in declared.items():
            keys[name] |= ks
        for name, prose in documented.items():
            # Longest wins where a phase ships the same collection twice: a file that documents its
            # schema and one that does not are both correct, and the union of what a phase holds is the
            # honest answer to "can the agent read the field descriptions".
            if len(prose) > len(notes.get(name, "")):
                notes[name] = prose
    out.declared_keys = {k: v for k, v in keys.items() if v}
    out.notes = notes
    return out


def _own_id_fields(collection: str) -> set[str]:
    """Field names that hold a record's OWN identifier, for a collection of this name.

    The distinction matters: a booking record carries both its own `reservation_id` and a
    `user_id` REFERRING to a customer. Treating every `*_id` field as "own" would make the closure
    check blind to exactly the dangling pointers it exists to find -- a booking kept in the build share
    whose customer was withheld would pass.
    """
    singular = collection[:-1] if collection.endswith("s") else collection
    #: `_number` / `_code` / `_no` are here because real stores use them as primary keys as often as
    #: `_id`: airline identifies a flight by `flight_number`, and flights are precisely the records a
    #: strict carve withholds most of, so missing them would blind the check where it matters most.
    suffixes = ("id", "number", "code", "no")
    names = {"id"}
    for stem in (singular, collection):
        names |= {f"{stem}_{suffix}" for suffix in suffixes}
    return names


def _references_collection(field: str, collection: str) -> bool:
    """True when a field of this name points at records of `collection`.

    Name-based, and narrow on purpose. `checking_account_id` references `accounts` because it ends in that
    collection's own-key name; `supplier_id` does not reference `contracts`, even though order_to_cash
    keys its contracts by supplier id, because nothing in the name says so.

    That second case is why the bare `id` is dropped before the suffix test. Keeping it would make every
    `*_id` field a reference to every collection, and the structural disjointness pass would then read a
    build invoice's `supplier_id` of "9" as holding contract "9" -- flagging roughly a tenth of a correct
    build share. Declining to guess where the schema does not say is the cheaper error: it costs
    sensitivity on a reference nobody named after its target, and it costs no correct carve.
    """
    refs = _own_id_fields(collection) - {"id"}
    return field in refs or any(field.endswith(f"_{r}") for r in refs)


def _keys_by_collection(store: dict[str, list[dict]]) -> dict[str, set[str]]:
    """collection -> the set of its own record identifiers.

    Two sources, unioned. The store file's own KEYS where it keys its collections by them, which is a
    statement of the primary key rather than an inference from it; and values of fields NAMED after the
    collection, which is all a list-shaped or CSV collection offers.

    Both are needed. Airline's two sources agree exactly (300 flights, 500 customers, 2000 bookings either
    way). Banking's do not: name inference finds a key for 3 of its 17 collections, so on its store the
    declared keys are what closure runs on at all.
    """
    out: dict[str, set[str]] = {}
    declared = _declared_keys(store)
    for coll, rows in store.items():
        own = _own_id_fields(coll)
        keys = set(declared.get(coll, ()))
        for r in rows:
            for k, v in r.items():
                if k in own and isinstance(v, str):
                    keys.add(v)
        out[coll] = keys
    return out


def _strings_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set().union(*(_strings_in(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(_strings_in(v) for v in value.values())) if value else set()
    return set()


def check_closure(build: dict[str, list[dict]], full: dict[str, list[dict]]) -> CheckResult:
    """Every record the build share points at must be a record the build share also holds.

    Otherwise the agent reads dangling references and draws wrong conclusions about the schema.

    Deciding what counts as a reference is the whole difficulty, and the two shares together settle it:
    a string is a reference when it is a RECORD IDENTIFIER SOMEWHERE IN THE FULL STORE. That
    distinguishes a genuine dangling pointer (a booking id the build share mentions but does not hold)
    from an opaque value that merely looks like an identifier (a payment reference belonging to no
    collection), which a rule based on shape alone cannot do.

    A relational task never reaches this: its database rejects a load violating its declared foreign keys,
    so a broken carve fails at load time, before any test runs.
    """
    res = CheckResult("referential closure")
    if not build:
        res.skipped = "no parseable records under the build phase's sources"
        return res
    if not full:
        res.skipped = "no parseable records under the eval phase's sources, so references cannot be resolved"
        return res

    build_keys = _keys_by_collection(build)
    full_keys = _keys_by_collection(full)
    if not any(full_keys.values()):
        res.skipped = ("the store neither keys its collections by their primary key nor names an "
                       "identifier field after its collection, so references cannot be resolved")
        return res

    for coll, rows in build.items():
        dangling: dict[str, set[str]] = defaultdict(set)
        own_fields = _own_id_fields(coll)
        for r in rows:
            own = {v for k, v in r.items() if k in own_fields and isinstance(v, str)}
            for value in _strings_in(r) - own:
                for target, keys in full_keys.items():
                    if value in keys and value not in build_keys.get(target, set()):
                        dangling[target].add(value)
        for target, values in sorted(dangling.items()):
            missing = sorted(values)
            res.errors.append(
                f"{coll} records point at {len(missing)} {target} record(s) the build share does not "
                f"hold: {missing[:8]}{' ...' if len(missing) > 8 else ''}"
            )
    return res


#: Field-name endings that mark an identifier or a reference rather than a category. Excluded before
#: cardinality is considered at all, because cardinality alone cannot tell them apart in a SMALL store:
#: three bookings give `booking_id` three distinct values, which looks exactly like a three-valued
#: category and would flag every correct carve for "missing" the withheld record's id.
_ID_FIELD_ENDINGS = ("_id", "_number", "_code", "_no")

#: Values that are dates or timestamps. Same reasoning: in a small store a date column is low-cardinality
#: and looks like a category, but demanding that a build share cover every date is meaningless.
#:
#: TWO SPELLINGS OF A DATE, AND THE SECOND IS NOT A NICETY. Banking writes every date in day-first US
#: order rather than ISO -- a card's expiry and issue dates, a card order's date and its expected
#: delivery, a payment date, a date of birth, the timestamp on a card closure. With only the ISO form
#: here, all seven of those fields were inferred as CATEGORIES, so banking's coverage report asked a
#: reviewer to accept that its build share "is missing ['01/31/2029', '03/31/2028', ...]". That is the
#: worst kind of noise: it can never be fixed by carving better, because no small share holds every date
#: a large store holds, so the only available response is to name all seven in a waiver -- which buries
#: the gaps that are real findings under seven that are an artefact of how the store spells a date.
#: Measured: this drops 7 of banking's 25 gaps and changes nothing at all for airline or order_to_cash,
#: which both write ISO.
#:
#: `payment_history.payment_date` is why `_is_temporal` insists that EVERY value be a date rather than
#: most of them: that one field carries both spellings, so a field counts as temporal only when nothing
#: in it is anything but a date.
#:
#: WHAT IS DELIBERATELY LEFT OUT. Three of banking's remaining gaps only LOOK like this one -- a card's
#: verification code, its last four digits, and a verification record's address. They are not dates and
#: no structural rule separates them from a category; they read as enumerable purely because banking has
#: eight debit cards and three verification records. The only rule that would silence them is a wildcard,
#: and a wildcard in a waiver would later absorb a gap in a NEW field of the same collection without
#: anyone being told -- which is the exact property that naming each enumeration exists to buy. So they
#: stay gaps, and a task owner who accepts them names them one by one.
_TEMPORAL = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]|$)|^\d{2}/\d{2}/\d{4}([T ]|$)|^\d{2}:\d{2}(:\d{2})?$")


def _is_identifier_field(name: str) -> bool:
    return name == "id" or name.endswith(_ID_FIELD_ENDINGS)


def _is_temporal(values: set) -> bool:
    strings = [v for v in values if isinstance(v, str)]
    return bool(strings) and len(strings) == len(values) and all(_TEMPORAL.match(v) for v in strings)


def enumerations(full: dict[str, list[dict]]) -> dict[tuple[str, str], set]:
    """(collection, field) -> its full value set, for fields the full store treats as categories.

    A category is inferred, never declared: few distinct values across the full store means tier, cabin,
    status or type; hundreds means an identifier, a date or free text. Identifier-named fields and
    date-valued fields are excluded by name and shape FIRST, because cardinality cannot distinguish them
    from a category when the store is small -- and getting that wrong flags correct carves, which is the
    failure that gets a check switched off.
    """
    out: dict[tuple[str, str], set] = {}
    for coll, rows in full.items():
        seen: dict[str, set] = defaultdict(set)
        for r in rows:
            for k, v in r.items():
                # Categories are LABELS -- strings and flags. Numbers are quantities, and demanding a
                # small build share cover every one of them blocks correct carves: airline records bags
                # per booking as 0-8, and 35 bookings cannot hold all nine values. Measured on the real
                # store, that was the only false positive this rule produced.
                #
                # The trade-off is deliberate. A task that encodes a category as an integer (status 1|2|3)
                # goes unchecked, which is a check that does less than it could. Flagging a correct carve
                # is worse: that is what gets a check switched off by the first person it obstructs.
                if isinstance(v, (str, bool)):
                    seen[k].add(v)
        for field, values in seen.items():
            if _is_identifier_field(field) or _is_temporal(values):
                continue
            if 1 < len(values) <= _MAX_ENUM_VALUES:
                out[(coll, field)] = values
    return out


def check_coverage(build: dict[str, list[dict]], full: dict[str, list[dict]],
                   waiver: CoverageWaiver | None = None) -> CheckResult:
    """Every value the full store enumerates must appear at least once in the build share.

    The build share is small by design, so its proportions cannot match the full store -- but the agent
    must still be able to see one of everything, or it builds against a world missing a category it will
    be scored on. Which fields count as enumerations is inferred from the full store's cardinality, so no
    task declares anything.

    `waiver` is the ONE thing a task declares, and only ever this one: the owner's recorded decision to
    accept a build share that cannot cover NAMED categories (see `coverage_waiver`). It moves the gaps it
    names from `errors` to `findings`, and leaves every other gap failing. It does not shorten the report
    by a single line, deliberately -- the value of a waiver is that the gap is acknowledged, and a waiver
    that also hid the gap would leave a reviewer no better informed than a check nobody ran.

    A waiver entry that no longer corresponds to a gap is reported too, as dead weight rather than as a
    failure: the build share covers it now, or the full store no longer has that enumeration at all.
    Quietly leaving one in place is how a live check becomes a dead one.

    Gaps are computed and reported in `(collection, field)` order, so a waiver regenerated after the data
    moves diffs against the old one minimally.
    """
    res = CheckResult("category coverage")
    if not build or not full:
        res.skipped = "no parseable records for one of the phases"
        return res
    wanted = enumerations(full)
    if not wanted:
        res.skipped = "no low-cardinality fields found, so there are no enumerations to cover"
        return res

    gaps: dict[str, str] = {}
    for (coll, field), values in sorted(wanted.items()):
        name = enumeration_id(coll, field)
        rows = build.get(coll)
        if not rows:
            gaps[name] = (f"{name}: the build share has no {coll} records at all, so it covers none of "
                          f"the {len(values)} value(s) {field} takes in the full store")
            continue
        present = {r[field] for r in rows if field in r
                   and isinstance(r[field], (str, int, float, bool))}
        missing = sorted(map(str, values - present))
        if missing:
            gaps[name] = f"{name} is missing {missing} (full store has {len(values)} values)"

    if waiver is None:
        res.errors = list(gaps.values())
        return res
    _apply_coverage_waiver(res, waiver, gaps,
                           {enumeration_id(coll, field) for coll, field in wanted})
    return res


def _apply_coverage_waiver(res: CheckResult, waiver: CoverageWaiver, gaps: dict[str, str],
                           known: set[str]) -> None:
    """Split `gaps` into the ones the waiver accepts and the ones it does not, and report both.

    THE ONE RULE: a gap the waiver does not name is a FAILURE. Everything else here follows from it.

    `known` is every enumeration the full store has, which is what separates the two ways a waiver entry
    can be dead. An entry naming a real enumeration the build share now covers is STALE -- the carve got
    better, or the data moved, and the entry has done its job. An entry naming nothing the full store
    enumerates is UNKNOWN: a typo, a renamed field, or a field that has grown past the cardinality line and
    is no longer inferred as a category at all.

    NEITHER FAILS, and that is a deliberate asymmetry with the rule above. A failure is reserved for a gap
    that is not accepted, because that is the thing a reviewer must not miss. An entry that matches no gap
    accepts nothing and therefore hides nothing -- it is dead weight, and dead weight in a list this
    churn-prone should cost an author a line in the report, not a red build on a carve that is correct. (If
    the entry was a typo for a gap that is still open, that gap is unnamed and fails on its own account, so
    the mistake is caught either way.)
    """
    res.waived = waiver.reason
    named = set(waiver.enumerations)

    # Every list below is in (collection, field) order -- `gaps` was built that way, and the two derived
    # from the spec are sorted here -- so the report does not shuffle when a spec's entries do, and a
    # regenerated waiver diffs minimally against the one it replaces.
    res.findings = [msg for name, msg in gaps.items() if name in named]
    for name in sorted(n for n in named if n not in gaps and n in known):
        res.findings.append(
            f"{name} is covered by the build share now, so naming it here is no longer doing anything "
            f"-- remove it from {_WAIVER_PATH}.enumerations")
    for name in sorted(n for n in named if n not in known):
        res.findings.append(
            f"{name} is not an enumeration of the full store at all -- misspelt, renamed, or no longer "
            f"low-cardinality -- so naming it here is no longer doing anything: check the spelling, then "
            f"remove it from {_WAIVER_PATH}.enumerations")
    if not gaps:
        res.findings.append(
            "the build share covers every enumeration the full store holds, so this waiver is no longer "
            "doing anything -- remove it, or the next real coverage gap goes unreported")

    unnamed = [name for name in gaps if name not in named]
    if unnamed:
        one = len(unnamed) == 1
        res.errors = [gaps[name] for name in unnamed]
        res.errors.append(
            f"{'1 gap above is' if one else f'{len(unnamed)} gaps above are'} NOT named by this task's "
            f"coverage waiver, so {'it is' if one else 'they are'} not accepted -- a waiver accepts only "
            f"what it names, or a gap appearing by accident later would be absorbed by a reason written "
            f"for a different one. Either carve a build share that covers "
            f"{'it' if one else 'them'}, or add {'this entry' if one else 'these entries'} to "
            f"{_WAIVER_PATH}.enumerations:\n"
            + "\n".join(f"  - {name}" for name in unnamed))


def check_schema_notes(build: dict[str, list[dict]], full: dict[str, list[dict]]) -> CheckResult:
    """Documentation the full store attaches to a collection should survive into the build share.

    REPORTS, NEVER FAILS, and the asymmetry is the point. Some stores carry the task owner's field
    descriptions beside the records -- banking documents 15 of its 17 collections that way, and the agent
    exploring the build env reads those descriptions as the schema. A carve script that copies the records
    and drops the prose therefore costs the agent the schema documentation while leaving every one of the
    three invariants green, so it needs saying out loud.

    It is not an invariant, though. Whether the build share should carry the same prose, different prose or
    none is the task owner's editorial call -- notes are authored text, not data -- and failing a correct
    carve over an editorial call is the thing this module's comments keep warning gets a check switched
    off by the first person it obstructs. So it is a line in the report and nothing more.

    Reported only for a store that carries documentation at all. A store with none cannot lose any, and
    airline's and order_to_cash's output stays exactly the three lines it was.
    """
    res = CheckResult("schema documentation")
    documented = {c: p for c, p in _documented_collections(full).items() if p.strip()}
    if not documented:
        return res
    build_notes = _documented_collections(build)
    dropped = sorted(c for c, p in documented.items() if not build_notes.get(c, "").strip())
    if dropped:
        res.findings.append(
            f"the full store documents {len(documented)} collection(s); the build share carries no "
            f"documentation for {len(dropped)} of them: {dropped[:8]}{' ...' if len(dropped) > 8 else ''}"
            " -- the agent explores the build env, so this is schema documentation it will not see"
        )
    return res


def check_task(task_id: str, tasks_dir: pathlib.Path) -> list[CheckResult]:
    """Every carve check for one task. Returns [] when the task has not split (nothing to check)."""
    if not has_split(task_id, tasks_dir):
        return []
    src = phase_sources(task_id, tasks_dir)
    build = load_records(task_id, tasks_dir, src["build"])
    full = load_records(task_id, tasks_dir, src["eval"])
    results = [
        check_disjoint(task_id, tasks_dir),
        check_closure(build, full),
        # Coverage is the only check a task can waive, and the waiver is read here rather than inside the
        # check so that the check stays a pure function of two stores for its callers in the test suite.
        check_coverage(build, full, waiver=coverage_waiver(task_id, tasks_dir)),
    ]
    if any(p.strip() for p in _documented_collections(full).values()):
        results.append(check_schema_notes(build, full))
    return results


# --------------------------------------------------------------------------------------
# CLI -- so a task owner can check their carve before committing it
# --------------------------------------------------------------------------------------
#
# The checks run in CI either way (tests/appsci_bench/core/test_carve_guards.py). This entry point
# exists because CI is the wrong moment to find out: carving is iterative, and the failure being
# guarded against has no runtime symptom, so an owner needs to run the checks against a share they are
# still editing. Same code, same verdicts -- a green run here is a green run there.

def _print_lines(lines: list[str], indent: str = "      ") -> None:
    """One report line per line, with a multi-line entry's continuations indented with it.

    A failure message can carry a block an author is meant to paste into their spec (the enumerations a
    coverage waiver is missing), and a block whose second line starts at column zero is not pasteable.
    """
    for line in lines:
        for part in line.splitlines():
            print(f"{indent}{part}")


def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Check a task's build/eval data carve. Run this while carving, before committing.",
        epilog="exit status: 0 = every check passed (or the task has not split), 1 = a check failed. "
               "A WAIVED finding does not fail -- it is printed in full, and the task's spec names that "
               "enumeration and carries the owner's reason for accepting it. A coverage gap the spec does "
               "NOT name fails, waiver or no waiver.",
    )
    ap.add_argument("task_id", nargs="*",
                    help="task ids to check; default is every task in tasks/")
    ap.add_argument("--tasks-dir", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parents[1] / "tasks",
                    help="the tasks directory (default: tasks/ beside this script)")
    args = ap.parse_args()

    task_ids = args.task_id or sorted(
        p.name for p in args.tasks_dir.iterdir()
        if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "spec.yaml").exists()
    )

    failed = False
    for task_id in task_ids:
        if not (args.tasks_dir / task_id / "spec.yaml").exists():
            print(f"{task_id}: no spec.yaml under {args.tasks_dir}")
            failed = True
            continue
        results = check_task(task_id, args.tasks_dir)
        if not results:
            print(f"{task_id}: not split -- both phases load the same data, so there is no carve to check")
            continue
        print(f"{task_id}:")
        for r in results:
            if r.skipped:
                # Not a pass. A skipped check means the carve is unverified in that dimension.
                print(f"  ~ {r.name}: NOT MEASURED -- {r.skipped}")
                failed = True
                continue
            if not r.errors and not r.findings:
                print(f"  + {r.name}: ok")
                continue
            # A check can be BOTH failing and waived: the waiver accepts the gaps it names, and a gap it
            # does not name fails. Both blocks print, failures first, because the failure is the thing to
            # act on and the waived findings are the context for why the rest is acceptable.
            if r.errors:
                print(f"  - {r.name}: FAILED")
                _print_lines(r.errors)
                failed = True
            if r.waived:
                # Not a pass and not a failure: an ACKNOWLEDGED gap. Every finding is printed, because a
                # waiver that shortened the report would leave a reviewer no better off than no check.
                print(f"  ! {r.name}: WAIVED -- {r.waived}")
                if r.findings:
                    print(f"      the {len(r.findings)} line(s) below are acknowledged by that reason, "
                          f"not fixed:")
                    _print_lines(r.findings)
                else:
                    # A reason that names nothing. Said plainly, because an author who wrote a waiver and
                    # got a failure needs to be told the waiver is the thing that is incomplete.
                    print("      that reason names no enumerations, so it accepts none of the failures "
                          "above -- the entries it is missing are listed with them")
            elif r.findings:
                # Reported, not judged: the schema-documentation check. Never affects exit status.
                print(f"  ! {r.name}: reported, not a failure")
                _print_lines(r.findings)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_cli())
