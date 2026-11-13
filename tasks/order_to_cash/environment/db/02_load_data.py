#!/usr/bin/env python3
"""Load fixture CSV/JSON data into the OLTP database.

This script reads the p2p_seed fixture files and inserts them into PostgreSQL.
Run after 01_schema.sql has created the tables.
"""
import csv
import json
import os
import sys

# Add path for imports if needed
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def load_data(cursor):
    """Load all fixture data into the database."""
    fixture_dir = "/fixture"

    # Load PO lines first (to get supplier and item info)
    po_lines_file = os.path.join(fixture_dir, "po_lines_used.csv")
    suppliers = {}
    items = {}
    pos = {}

    with open(po_lines_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            supplier_id = int(row["SUPPLIER_ID"])
            suppliers[supplier_id] = row["SUPPLIER_NAME"]

            item_id = int(row["ITEM_ID"])
            items[item_id] = row["ITEM_NUMBER"]

            po_number = row["PO_NUMBER"]
            if po_number not in pos:
                pos[po_number] = supplier_id

    # Insert suppliers
    for supplier_id, name in suppliers.items():
        cursor.execute(
            "INSERT INTO suppliers (supplier_id, supplier_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (supplier_id, name)
        )

    # Insert items
    for item_id, item_number in items.items():
        cursor.execute(
            "INSERT INTO items (item_id, item_number) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (item_id, item_number)
        )

    # Insert purchase orders
    for po_number, supplier_id in pos.items():
        cursor.execute(
            "INSERT INTO purchase_orders (po_number, supplier_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (po_number, supplier_id)
        )

    # Insert PO lines
    with open(po_lines_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            received_qty = row["RECEIVED_QTY"] if row["RECEIVED_QTY"] else None
            cursor.execute("""
                INSERT INTO po_lines (po_line_id, po_number, item_id, item_number, item_uom,
                                      quantity, unit_price, received_qty, pay_terms, currency)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (
                int(row["PO_LINE_ID"]),
                row["PO_NUMBER"],
                int(row["ITEM_ID"]),
                row["ITEM_NUMBER"],
                row["ITEM_UOM"],
                float(row["QUANTITY"]),
                float(row["UNIT_PRICE"]),
                float(received_qty) if received_qty else None,
                row["PAY_TERMS"],
                row["VEN_CURRENCY"]
            ))

    # Load contracts
    contracts_file = os.path.join(fixture_dir, "contracts.json")
    with open(contracts_file, "r") as f:
        contracts = json.load(f)

    for supplier_id_str, contract in contracts.items():
        supplier_id = int(supplier_id_str)
        agreed_prices = contract.get("agreed_unit_price", {})
        allowed_fees = contract.get("allowed_fees", [])

        for item_id_str, price in agreed_prices.items():
            item_id = int(item_id_str)
            cursor.execute("""
                INSERT INTO contracts (supplier_id, item_id, agreed_unit_price, allowed_fees)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (supplier_id, item_id, price, json.dumps(allowed_fees)))

    # Load invoices
    invoices_file = os.path.join(fixture_dir, "invoices.csv")

    # Some invoices reference items that were never part of a PO line
    # (e.g. off-contract items) - seed those into items too so the FK holds.
    with open(invoices_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            item_id = int(row["item_id"])
            if item_id not in items:
                items[item_id] = row["item_number"]
                cursor.execute(
                    "INSERT INTO items (item_id, item_number) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (item_id, row["item_number"])
                )

    with open(invoices_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Insert item if not already present (handle orphan items in invoice data)
            item_id = int(row["item_id"])
            if item_id not in items:
                cursor.execute(
                    "INSERT INTO items (item_id, item_number) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (item_id, row["item_number"])
                )
                items[item_id] = row["item_number"]

            extra_fees = row["extra_fees"] if row["extra_fees"] else "[]"
            duplicate_of = row["duplicate_of"] if row["duplicate_of"] else None
            cursor.execute("""
                INSERT INTO invoices (invoice_number, po_line_id, po_number, supplier_id, supplier_name,
                                      item_id, item_number, uom, currency, invoiced_qty, unit_price,
                                      amount, extra_fees, duplicate_of)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (
                row["invoice_number"],
                int(row["po_line_id"]),
                row["po_number"],
                int(row["supplier_id"]),
                row["supplier_name"],
                int(row["item_id"]),
                row["item_number"],
                row["uom"],
                row["currency"],
                float(row["invoiced_qty"]),
                float(row["unit_price"]),
                float(row["amount"]),
                extra_fees,
                duplicate_of
            ))


def verify_load(cursor, fixture_dir="/fixture"):
    """Assert this phase's data actually landed, and abort the run if it did not.

    The same loader serves both phases (build gets the carved share, eval gets the full store), so
    this cannot check for particular records -- it checks that every row the bundled fixture carries
    is in the database. That is the assertion the carve spec asks each phase's setup to end with, and
    it is strictly stronger than a non-zero count: it catches a PARTIAL load, which is the failure
    that otherwise surfaces only as a run of zeros with nothing to explain it.

    Why counting is the right check here. Every insert uses ON CONFLICT DO NOTHING -- correct for
    re-running a load, but it drops a row in silence on a key collision. The fixture is generated free
    of duplicate invoice numbers precisely so that cannot happen (KNOWN_ISSUES.md section 2); this
    turns that intention into a checked fact. A short load means rows were silently skipped, and on
    the eval phase that means a scored case may be reading a different invoice than its answer key
    describes -- a plausible wrong number rather than a crash.

    The answer key is deliberately not available here: seed_data/labels.csv is host-side and enters no
    container (KNOWN_ISSUES.md section 8), so "are the scored invoices present" is not a question this
    can ask. Row-count parity covers it: if every bundled row landed, the scored ones did too.
    """
    def csv_rows(name):
        path = os.path.join(fixture_dir, name)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return sum(1 for _ in csv.DictReader(f))

    counts = {}
    for table in ("invoices", "po_lines", "contracts", "suppliers", "items"):
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        counts[table] = cursor.fetchone()[0]
    print("  loaded: " + "  ".join(f"{t}={n}" for t, n in counts.items()))

    for table, count in counts.items():
        if count == 0:
            raise SystemExit(
                f"FATAL: {table} is empty after load. This phase's fixture did not land, and there "
                f"is nothing worth measuring against an empty store."
            )

    # Row-count parity against what was actually bundled, per file that maps 1:1 onto a table.
    for name, table in (("invoices.csv", "invoices"), ("po_lines_used.csv", "po_lines")):
        expected = csv_rows(name)
        if expected is None:
            raise SystemExit(f"FATAL: {name} missing from {fixture_dir} after bundle extraction")
        if counts[table] != expected:
            raise SystemExit(
                f"FATAL: {name} carries {expected} row(s) but {table} holds {counts[table]}. "
                f"Rows were silently dropped at load; the store is not what the fixture declares."
            )
        print(f"  {table}: all {expected} bundled row(s) present")

    # A duplicate invoice number would let two rows answer to one key. The only way the parity check
    # above could pass while the data is still wrong.
    cursor.execute(
        "SELECT invoice_number, COUNT(*) FROM invoices GROUP BY invoice_number HAVING COUNT(*) > 1"
    )
    dupes = cursor.fetchall()
    if dupes:
        raise SystemExit(f"FATAL: duplicate invoice numbers in the database: {dupes[:10]}")


if __name__ == "__main__":
    import psycopg2

    # Connect to database
    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "oltp"),
        user=os.environ.get("POSTGRES_USER", "sandbox"),
        password=os.environ.get("POSTGRES_PASSWORD", "sandbox")
    )

    try:
        with conn.cursor() as cursor:
            load_data(cursor)
            conn.commit()
            print("Data loaded successfully")
            verify_load(cursor)
    finally:
        conn.close()
