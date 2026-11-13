"""Database access: connection, runtime schema discovery, and record loaders.

The benchmark supplies a PostgreSQL DSN in DATABASE_URL.  Only the `contracts`
table and its four columns are documented (sop/contract_terms.md); every other
table and column name is discovered at runtime by name alias AND by column
signature, so a differently-named table is still found by its shape.

Two DB-API backends sit behind one interface: psycopg for the production
PostgreSQL path, and the standard library's sqlite3 for the local self-test
fixture.  That way selftest.py drives the real loaders rather than a parallel
implementation, with no third-party driver needed to run the tests.

Initialisation is lazy, cached and thread-safe: connecting at import would let a
database failure kill the container and take /health down with it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse

from models import (
    Contract,
    DatabaseUnavailable,
    Fee,
    Invoice,
    InvoiceLine,
    POLine,
    norm_key,
    to_decimal,
)

log = logging.getLogger("invoice_review.db")


def _norm_name(name: str) -> str:
    """Normalised identifier key: lowercase, punctuation stripped."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class _Backend:
    """Minimal DB-API surface the loaders need."""

    placeholder = "?"

    def connect(self) -> Any:
        raise NotImplementedError

    def list_columns(self, conn: Any) -> dict[str, list[str]]:
        """{physical table name: [column names]} for every readable table."""
        raise NotImplementedError

    def quote(self, table: str) -> str:
        return table


class Sqlite(_Backend):
    placeholder = "?"

    def __init__(self, path: str) -> None:
        self.path = path

    def connect(self) -> Any:
        import sqlite3

        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def list_columns(self, conn: Any) -> dict[str, list[str]]:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        out: dict[str, list[str]] = {}
        for table in tables:
            info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            out[table] = [row[1] for row in info]
        return out

    def quote(self, table: str) -> str:
        return f'"{table}"'


class Postgres(_Backend):
    placeholder = "%s"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row

        # autocommit: a failed statement must not poison the whole session.
        return psycopg.connect(
            self.dsn, autocommit=True, connect_timeout=10, row_factory=dict_row
        )

    def list_columns(self, conn: Any) -> dict[str, list[str]]:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT table_schema, table_name, column_name "
                "FROM information_schema.columns "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY table_schema, table_name, ordinal_position"
            )
            rows = cursor.fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            schema = row["table_schema"] if isinstance(row, dict) else row[0]
            table = row["table_name"] if isinstance(row, dict) else row[1]
            column = row["column_name"] if isinstance(row, dict) else row[2]
            key = table if schema == "public" else f"{schema}.{table}"
            out.setdefault(key, []).append(column)
        return out

    def quote(self, table: str) -> str:
        return ".".join(f'"{part}"' for part in table.split("."))


def _make_backend(url: str) -> _Backend:
    scheme = urlparse(url).scheme.lower().split("+")[0]
    if scheme in ("postgres", "postgresql", "pg"):
        return Postgres(url)
    if scheme == "sqlite":
        # sqlite:///relative.db  /  sqlite:////absolute.db
        path = url[len("sqlite://"):]
        while path.startswith("//"):
            path = path[1:]
        return Sqlite(path or ":memory:")
    raise DatabaseUnavailable(
        "database_unavailable",
        f"unsupported DATABASE_URL scheme {scheme!r}; expected postgresql",
    )


# --------------------------------------------------------------------------
# Logical schema: what we need, and every name it might go by
# --------------------------------------------------------------------------

@dataclass
class TableSpec:
    key: str
    names: tuple[str, ...]
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    # Resolving these fields suggests we matched the wrong table.
    negative: tuple[str, ...] = ()


FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "invoices": {
        "invoice_number": ("invoice_number", "invoice_no", "invoice_num", "invoice_id",
                           "invoicenumber", "inv_number", "inv_no", "inv_num", "inv_id",
                           "number", "invoice", "id", "doc_number", "document_number"),
        "supplier_id": ("supplier_id", "supplier", "vendor_id", "vendor",
                        "supplier_code", "supplier_number", "supplier_name"),
        "supplier_name": ("supplier_name", "vendor_name", "supplier_label",
                          "supplier_description"),
        "po_number": ("po_number", "po_no", "po_num", "po_id", "purchase_order_number",
                      "purchase_order_no", "purchase_order_id", "purchase_order", "po"),
        "total": ("invoice_total", "total_amount", "total", "amount_total", "grand_total",
                  "total_due", "invoiced_total", "gross_total", "amount"),
        "duplicate_of": ("duplicate_of", "duplicate_of_invoice", "duplicate_of_invoice_number",
                         "duplicate_invoice", "duplicate_invoice_number", "duplicate_of_id",
                         "is_duplicate_of", "dup_of", "duplicate"),
        "currency": ("currency", "currency_code", "curr"),
        "doc_type": ("doc_type", "document_type", "invoice_type", "type", "kind",
                     "document_kind"),
    },
    "invoice_lines": {
        "invoice_ref": ("invoice_number", "invoice_no", "invoice_num", "invoice_id",
                        "invoicenumber", "inv_number", "inv_no", "inv_num", "inv_id",
                        "invoice"),
        "item_id": ("item_id", "item", "item_code", "item_number", "sku", "product_id",
                    "product_code", "material_id", "material"),
        "quantity": ("invoiced_qty", "invoice_qty", "qty_invoiced", "invoice_quantity",
                     "invoiced_quantity", "quantity_invoiced", "quantity", "qty", "units",
                     "line_quantity"),
        "unit_price": ("invoice_unit_price", "invoiced_unit_price", "unit_price",
                       "unit_cost", "price_per_unit", "price", "rate", "unit_amount"),
        "line_no": ("line_no", "line_number", "line_num", "line", "seq", "sequence",
                    "position", "line_id", "id"),
        "po_ref": ("po_number", "po_no", "po_num", "po_id", "purchase_order_number",
                   "purchase_order_id", "purchase_order", "po"),
        "po_line_id": ("po_line_id", "po_line_key", "po_line_ref", "poline_id",
                       "purchase_order_line_id"),
    },
    "invoice_fees": {
        "invoice_ref": ("invoice_number", "invoice_no", "invoice_num", "invoice_id",
                        "invoicenumber", "inv_number", "inv_no", "inv_num", "inv_id",
                        "invoice"),
        "label": ("fee_label", "fee_type", "fee_name", "fee_code", "label", "fee",
                  "charge_type", "charge_label", "type", "name", "description"),
        "amount": ("fee_amount", "amount", "charge_amount", "fee_value", "value", "total"),
    },
    "po_lines": {
        "po_ref": ("po_number", "po_no", "po_num", "po_id", "purchase_order_number",
                   "purchase_order_no", "purchase_order_id", "purchase_order", "po"),
        "item_id": ("item_id", "item", "item_code", "item_number", "sku", "product_id",
                    "product_code", "material_id", "material"),
        "quantity": ("po_quantity", "ordered_quantity", "quantity_ordered", "order_quantity",
                     "quantity", "qty", "units"),
        "unit_price": ("po_unit_price", "agreed_unit_price", "ordered_unit_price",
                       "unit_price", "unit_cost", "price_per_unit", "price", "rate"),
        "po_line_id": ("po_line_id", "po_line_key", "poline_id",
                       "purchase_order_line_id"),
        "line_no": ("po_line_no", "po_line_number", "line_no", "line_number", "line_num",
                    "line", "seq", "position", "id"),
        "currency": ("currency", "currency_code", "curr"),
    },
    "contracts": {
        # These four names are documented in sop/contract_terms.md.
        "supplier_id": ("supplier_id", "supplier", "vendor_id", "vendor", "supplier_code"),
        "item_id": ("item_id", "item", "item_code", "sku", "product_id", "material_id"),
        "agreed_unit_price": ("agreed_unit_price", "agreed_price", "contract_price",
                              "negotiated_price", "unit_price", "price"),
        "allowed_fees": ("allowed_fees", "permitted_fees", "authorized_fees",
                         "authorised_fees", "allowed_fee_labels", "fees",
                         "allowed_charges", "permitted_charges"),
        "currency": ("currency", "currency_code", "curr"),
    },
}

TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        "contracts",
        ("contracts", "contract", "supplier_contracts", "supplier_contract",
         "contract_terms", "contract_lines", "contract_items", "vendor_contracts"),
        required=("supplier_id", "item_id"),
        optional=("agreed_unit_price", "allowed_fees", "currency"),
    ),
    TableSpec(
        "invoices",
        ("invoices", "invoice", "invoice_headers", "invoice_header", "ap_invoices",
         "ap_invoice", "supplier_invoices", "supplier_invoice"),
        required=("invoice_number",),
        optional=("supplier_id", "supplier_name", "po_number", "total", "duplicate_of",
                  "currency", "doc_type"),
        # No negatives: a one-row-per-invoice schema legitimately carries the item,
        # quantity and unit price of its single line on the invoice row itself.
    ),
    TableSpec(
        "invoice_lines",
        ("invoice_lines", "invoice_line", "invoice_line_items", "invoice_items",
         "invoice_item", "invoice_details", "invoice_detail", "ap_invoice_lines"),
        required=("invoice_ref", "item_id"),
        optional=("quantity", "unit_price", "line_no", "po_ref"),
    ),
    TableSpec(
        "invoice_fees",
        ("invoice_fees", "invoice_fee", "invoice_charges", "invoice_charge",
         "invoice_surcharges", "fees", "fee_lines", "additional_charges"),
        required=("invoice_ref", "label", "amount"),
        negative=("item_id", "quantity", "duplicate_of"),
    ),
    TableSpec(
        "po_lines",
        ("po_lines", "po_line", "purchase_order_lines", "purchase_order_line",
         "po_line_items", "po_items", "purchase_order_items", "order_lines"),
        required=("po_ref", "item_id"),
        optional=("quantity", "unit_price", "po_line_id", "line_no", "currency"),
        # A PO-line table does not carry an invoice number or a duplicate marker; a
        # one-row-per-invoice table does, and must not win this spec.
        negative=("invoice_number", "invoice_no", "invoice_id", "duplicate_of"),
    ),
)

# Invoice lines / fees are sometimes embedded as JSON on the invoice row instead.
JSON_LINE_COLUMNS = ("lines", "line_items", "items", "invoice_lines", "details",
                     "line_detail", "detail")
JSON_FEE_COLUMNS = ("extra_fees", "fees", "charges", "additional_charges", "surcharges",
                    "fee_lines", "extra_charges", "fee_detail")

# A one-row-per-invoice ("flat") schema keeps the invoice's single line on the invoice
# row rather than in a child table.  These are the columns that line is read from.
FLAT_LINE_ALIASES: dict[str, tuple[str, ...]] = {
    "item_id": FIELD_ALIASES["invoice_lines"]["item_id"],
    "quantity": FIELD_ALIASES["invoice_lines"]["quantity"],
    "unit_price": FIELD_ALIASES["invoice_lines"]["unit_price"],
    "po_line_id": FIELD_ALIASES["invoice_lines"]["po_line_id"],
    "line_no": ("line_no", "line_number", "line_num", "line", "seq", "position"),
}
FLAT_LINE_REQUIRED = ("item_id", "quantity", "unit_price")

# Required for the service to answer at all.  A missing one is a schema-level
# problem, reported as 503 rather than as a per-invoice 422 -- and never as a
# business verdict built on data we could not read.
# invoice_lines is absent here on purpose: line items have three legal sources
# (child table, JSON column, or the invoice row itself), checked separately below.
REQUIRED_TABLES = ("invoices", "po_lines", "contracts")
REQUIRED_FIELDS = {
    "invoices": ("invoice_number", "total", "duplicate_of"),
    "invoice_lines": ("invoice_ref", "item_id", "quantity", "unit_price"),
    "po_lines": ("po_ref", "item_id", "quantity", "unit_price"),
    "contracts": ("supplier_id", "item_id", "allowed_fees"),
}


@dataclass
class ResolvedTable:
    key: str
    table: str
    columns: dict[str, str]          # logical field -> physical column
    all_columns: tuple[str, ...] = ()

    def has(self, *fields: str) -> bool:
        return all(f in self.columns for f in fields)


@dataclass
class Schema:
    tables: dict[str, ResolvedTable] = field(default_factory=dict)
    json_lines_column: Optional[str] = None
    json_fees_column: Optional[str] = None
    # Set when the invoice row is itself the invoice's single line item.
    flat_line_columns: Optional[dict[str, str]] = None

    def get(self, key: str) -> Optional[ResolvedTable]:
        return self.tables.get(key)

    def line_source(self) -> Optional[str]:
        """Where invoice line items come from: a child table, a JSON column on the
        invoice row, or the invoice row itself."""
        if self.get("invoice_lines") is not None:
            return "table"
        if self.json_lines_column:
            return "json"
        if self.flat_line_columns:
            return "flat"
        return None

    def describe(self) -> str:
        parts = []
        for key, resolved in sorted(self.tables.items()):
            fields = ",".join(f"{k}={v}" for k, v in sorted(resolved.columns.items()))
            parts.append(f"{key}->{resolved.table}({fields})")
        if self.json_lines_column:
            parts.append(f"json_lines={self.json_lines_column}")
        if self.json_fees_column:
            parts.append(f"json_fees={self.json_fees_column}")
        if self.flat_line_columns:
            flat = ",".join(f"{k}={v}" for k, v in sorted(self.flat_line_columns.items()))
            parts.append(f"flat_line({flat})")
        return "; ".join(parts)


def _resolve_columns(
    spec_fields: dict[str, tuple[str, ...]], physical: Sequence[str]
) -> dict[str, str]:
    by_key = {}
    for column in physical:
        by_key.setdefault(_norm_name(column), column)
    resolved: dict[str, str] = {}
    taken: set[str] = set()
    for logical, aliases in spec_fields.items():
        for alias in aliases:
            column = by_key.get(_norm_name(alias))
            if column is not None and column not in taken:
                resolved[logical] = column
                taken.add(column)
                break
    return resolved


def _score_table(
    spec: TableSpec, table: str, columns: dict[str, str], physical: Sequence[str] = ()
) -> int:
    if not all(f in columns for f in spec.required):
        return -1
    name_key = _norm_name(table)
    physical_keys = {_norm_name(c) for c in physical}
    score = 0
    for position, alias in enumerate(spec.names):
        alias_key = _norm_name(alias)
        if name_key == alias_key:
            score += 200 - position
            break
        if alias_key in name_key:
            score += 80 - position
            break
    score += 20 * len(spec.required)
    score += 4 * sum(1 for f in spec.optional if f in columns)
    # Negatives name physical columns whose presence means this is the wrong table.
    score -= 60 * sum(1 for name in spec.negative if _norm_name(name) in physical_keys)
    return score


def discover_schema(columns_by_table: dict[str, list[str]]) -> Schema:
    """Map each logical table to a physical one by name alias and column signature."""
    schema = Schema()
    for spec in TABLE_SPECS:
        aliases = FIELD_ALIASES[spec.key]
        best: Optional[tuple[int, str, dict[str, str]]] = None
        for table, physical in columns_by_table.items():
            resolved = _resolve_columns(aliases, physical)
            score = _score_table(spec, table, resolved, physical)
            if score < 0:
                continue
            if best is None or score > best[0] or (score == best[0] and table < best[1]):
                best = (score, table, resolved)
        if best is not None:
            schema.tables[spec.key] = ResolvedTable(
                spec.key, best[1], best[2], tuple(columns_by_table[best[1]])
            )

    invoices = schema.get("invoices")

    # A logical invoice_lines resolution that lands on the invoice table itself is not a
    # child table -- it is the flat one-row-per-invoice shape, handled below.  Keeping it
    # as a "table" would make the loader re-query the invoice by its own number and read
    # header columns as line columns.
    lines = schema.get("invoice_lines")
    if lines is not None and invoices is not None and lines.table == invoices.table:
        del schema.tables["invoice_lines"]

    if invoices is not None:
        by_key = {_norm_name(c): c for c in invoices.all_columns}
        for candidate in JSON_LINE_COLUMNS:
            if _norm_name(candidate) in by_key:
                schema.json_lines_column = by_key[_norm_name(candidate)]
                break
        for candidate in JSON_FEE_COLUMNS:
            if _norm_name(candidate) in by_key:
                schema.json_fees_column = by_key[_norm_name(candidate)]
                break

        # Last resort for line items: the invoice row carries its own single line.
        if schema.get("invoice_lines") is None and not schema.json_lines_column:
            flat = _resolve_columns(FLAT_LINE_ALIASES, invoices.all_columns)
            # The header's own total must not be re-read as the line's unit price.
            taken = {invoices.columns.get(f) for f in ("total", "invoice_number",
                                                       "supplier_id", "po_number",
                                                       "duplicate_of")}
            flat = {k: v for k, v in flat.items() if v not in taken}
            if all(f in flat for f in FLAT_LINE_REQUIRED):
                schema.flat_line_columns = flat
    return schema


def apply_overrides(schema: Schema, raw: str) -> Schema:
    """SCHEMA_OVERRIDES env escape hatch, so a surprising live schema needs no redeploy
    of new code:

        {"invoices": {"table": "ap_inv", "columns": {"invoice_number": "inv_no"}}}
    """
    try:
        overrides = json.loads(raw)
    except (TypeError, ValueError) as exc:
        log.warning("ignoring unparseable SCHEMA_OVERRIDES: %s", exc)
        return schema
    if not isinstance(overrides, dict):
        return schema
    for key, spec in overrides.items():
        if not isinstance(spec, dict):
            continue
        existing = schema.tables.get(key)
        table = spec.get("table") or (existing.table if existing else None)
        if not table:
            continue
        columns = dict(existing.columns) if existing else {}
        supplied = spec.get("columns")
        if isinstance(supplied, dict):
            columns.update({k: v for k, v in supplied.items() if isinstance(v, str)})
        schema.tables[key] = ResolvedTable(
            key, table, columns, existing.all_columns if existing else ()
        )
    if "json_lines_column" in overrides:
        schema.json_lines_column = overrides["json_lines_column"] or None
    if "json_fees_column" in overrides:
        schema.json_fees_column = overrides["json_fees_column"] or None
    if "flat_line_columns" in overrides:
        supplied = overrides["flat_line_columns"]
        if isinstance(supplied, dict):
            schema.flat_line_columns = {
                k: v for k, v in supplied.items() if isinstance(v, str)
            }
        else:
            schema.flat_line_columns = None
    return schema


def validate_schema(schema: Schema) -> None:
    """A schema that cannot answer the checks is an infrastructure fault, not a verdict."""
    problems: list[str] = []
    for key in REQUIRED_TABLES:
        resolved = schema.get(key)
        if resolved is None:
            problems.append(f"no table found for {key}")
            continue
        missing = [f for f in REQUIRED_FIELDS.get(key, ()) if f not in resolved.columns]
        if missing:
            problems.append(f"{resolved.table} is missing {', '.join(missing)}")

    # Line items may live in a child table, in a JSON column, or on the invoice row.
    lines = schema.get("invoice_lines")
    if lines is not None:
        missing = [f for f in REQUIRED_FIELDS["invoice_lines"] if f not in lines.columns]
        if missing:
            problems.append(f"{lines.table} is missing {', '.join(missing)}")
    elif schema.line_source() is None:
        problems.append("no source found for invoice line items")

    if schema.get("invoice_fees") is None and not schema.json_fees_column:
        problems.append("no source found for invoice fees")
    if problems:
        raise DatabaseUnavailable(
            "schema_unsupported",
            "the database does not expose the data this review needs: "
            + "; ".join(problems),
        )


# --------------------------------------------------------------------------
# Connection + schema cache
# --------------------------------------------------------------------------

class Database:
    """Lazily initialised, cached, thread-safe handle."""

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url
        self._backend: Optional[_Backend] = None
        self._schema: Optional[Schema] = None
        self._lock = threading.Lock()
        self._local = threading.local()

    # -- connection ------------------------------------------------------
    def _resolve_backend(self) -> _Backend:
        if self._backend is not None:
            return self._backend
        url = self._url if self._url is not None else os.environ.get("DATABASE_URL", "")
        if not url or not url.strip():
            raise DatabaseUnavailable(
                "database_unavailable", "DATABASE_URL is not set"
            )
        self._backend = _make_backend(url.strip())
        return self._backend

    def _connection(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        backend = self._resolve_backend()
        try:
            conn = backend.connect()
        except DatabaseUnavailable:
            raise
        except Exception as exc:  # driver missing, DSN bad, host unreachable
            raise DatabaseUnavailable(
                "database_unavailable", f"could not connect to the database: {exc}"
            ) from exc
        self._local.conn = conn
        return conn

    def _drop_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        for attempt in (1, 2):
            conn = self._connection()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute(sql, tuple(params))
                    rows = cursor.fetchall()
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                return [_row_to_dict(row) for row in rows]
            except Exception as exc:
                self._drop_connection()
                if attempt == 1:
                    log.warning("query failed, retrying on a fresh connection: %s", exc)
                    continue
                raise DatabaseUnavailable(
                    "database_unavailable", f"database query failed: {exc}"
                ) from exc
        return []

    # -- schema ----------------------------------------------------------
    def schema(self) -> Schema:
        if self._schema is not None:
            return self._schema
        with self._lock:
            if self._schema is not None:
                return self._schema
            backend = self._resolve_backend()
            conn = self._connection()
            try:
                columns_by_table = backend.list_columns(conn)
            except Exception as exc:
                self._drop_connection()
                raise DatabaseUnavailable(
                    "database_unavailable", f"could not read the database schema: {exc}"
                ) from exc
            schema = discover_schema(columns_by_table)
            override = os.environ.get("SCHEMA_OVERRIDES", "").strip()
            if override:
                schema = apply_overrides(schema, override)
            validate_schema(schema)
            log.info("resolved schema: %s", schema.describe())
            self._schema = schema
            return schema

    def placeholder(self) -> str:
        return self._resolve_backend().placeholder

    def quote(self, table: str) -> str:
        return self._resolve_backend().quote(table)

    def close(self) -> None:
        self._drop_connection()

    def reset(self) -> None:
        """Drop the cached schema and connection (used by the self-test)."""
        self._drop_connection()
        with self._lock:
            self._schema = None
            self._backend = None


def _row_to_dict(row: Any) -> dict:
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return {index: value for index, value in enumerate(row)}


# --------------------------------------------------------------------------
# Value normalisation
# --------------------------------------------------------------------------

def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_fee_labels(value: Any) -> frozenset[str]:
    """allowed_fees may be a JSON array, a native array, a Postgres array literal, or a
    comma-separated string.  All three normalise to a set of comparison keys."""
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(norm_key(item) for item in value if _text(item))
    if isinstance(value, dict):
        return frozenset(norm_key(item) for item in value.keys() if _text(item))
    text = str(value).strip()
    if not text:
        return frozenset()
    looks_like_json = text.startswith("[") or (text.startswith("{") and '"' in text)
    if looks_like_json:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (list, tuple)):
            return frozenset(norm_key(item) for item in parsed if _text(item))
        if isinstance(parsed, dict):
            return frozenset(norm_key(item) for item in parsed.keys() if _text(item))
    if text.startswith("{") and text.endswith("}"):
        # Postgres array literal: {freight,handling}
        inner = text[1:-1]
        return frozenset(
            norm_key(part.strip().strip('"')) for part in inner.split(",") if part.strip()
        )
    separator = ";" if ";" in text and "," not in text else ","
    return frozenset(
        norm_key(part.strip().strip('"')) for part in text.split(separator) if part.strip()
    )


def _json_rows(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        rows = list(value)
    elif isinstance(value, dict):
        rows = [value]
    else:
        try:
            parsed = json.loads(str(value))
        except ValueError:
            return []
        rows = parsed if isinstance(parsed, list) else [parsed]
    return [row for row in rows if isinstance(row, dict)]


def _pick(row: dict, aliases: Iterable[str]) -> Any:
    by_key = {_norm_name(k): v for k, v in row.items() if isinstance(k, str)}
    for alias in aliases:
        if _norm_name(alias) in by_key:
            return by_key[_norm_name(alias)]
    return None


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def _select(database: Database, resolved: ResolvedTable, where_field: str,
            value: str, order_fields: Sequence[str] = ()) -> list[dict]:
    """Exact match first, then a case-insensitive retry for id spelling drift."""
    table = database.quote(resolved.table)
    column = f'"{resolved.columns[where_field]}"'
    mark = database.placeholder()
    order = ""
    order_columns = [
        f'"{resolved.columns[f]}"' for f in order_fields if f in resolved.columns
    ]
    if order_columns:
        order = " ORDER BY " + ", ".join(order_columns)
    rows = database.fetch_all(f"SELECT * FROM {table} WHERE {column} = {mark}{order}",
                              (value,))
    if rows:
        return rows
    return database.fetch_all(
        f"SELECT * FROM {table} WHERE LOWER(CAST({column} AS VARCHAR)) = LOWER({mark})"
        f"{order}",
        (value,),
    )


def load_invoice_header(database: Database, invoice_number: str) -> Optional[Invoice]:
    """Pipeline step 2.  Header fields only: lines and fees are deliberately not read
    here, so the DUPLICATE short-circuit at step 4 cannot be influenced by a
    line-level or contract-level data problem."""
    schema = database.schema()
    resolved = schema.get("invoices")
    assert resolved is not None, "validate_schema() guarantees this"
    rows = _select(database, resolved, "invoice_number", invoice_number)
    if not rows:
        return None
    row = rows[0]
    columns = resolved.columns

    def value(logical: str) -> Any:
        column = columns.get(logical)
        return row.get(column) if column else None

    invoice = Invoice(
        invoice_number=_text(value("invoice_number")) or invoice_number,
        supplier_id=_text(value("supplier_id")),
        supplier_name=_text(value("supplier_name")),
        po_number=_text(value("po_number")),
        total=to_decimal(value("total")),
        duplicate_of=_text(value("duplicate_of")),
        currency=_text(value("currency")),
        doc_type=_text(value("doc_type")),
    )
    invoice.raw_row = row  # type: ignore[attr-defined]

    # A duplicate flag stored as a boolean carries no invoice number; keep the
    # signal but do not invent a reference.
    if isinstance(value("duplicate_of"), bool):
        invoice.duplicate_of = "an earlier invoice" if value("duplicate_of") else None

    # If the PO reference lives only on the line rows, one extra lookup is needed at
    # step 2 for linkage validation.  duplicate_of remains the sole input to step 4.
    if invoice.po_number is None:
        invoice.po_number = _po_number_from_lines(database, invoice)
    return invoice


def _po_number_from_lines(database: Database, invoice: Invoice) -> Optional[str]:
    """Some schemas hang the PO reference off the line rows rather than the header."""
    resolved = database.schema().get("invoice_lines")
    if resolved is None or "po_ref" not in resolved.columns:
        return None
    column = resolved.columns["po_ref"]
    for row in _select(database, resolved, "invoice_ref", invoice.invoice_number):
        text = _text(row.get(column))
        if text:
            return text
    return None


def load_invoice_details(database: Database, invoice: Invoice) -> None:
    """Pipeline step 5.  Fills invoice.lines and invoice.fees -- non-duplicates only."""
    schema = database.schema()
    invoice.lines = _load_lines(database, schema, invoice)
    invoice.fees = _load_fees(database, schema, invoice)


def _load_lines(database: Database, schema: Schema, invoice: Invoice) -> list[InvoiceLine]:
    resolved = schema.get("invoice_lines")
    lines: list[InvoiceLine] = []
    if resolved is not None:
        rows = _select(database, resolved, "invoice_ref", invoice.invoice_number,
                       order_fields=("line_no",))
        columns = resolved.columns
        for index, row in enumerate(rows, start=1):
            line_no = row.get(columns["line_no"]) if "line_no" in columns else None
            lines.append(
                InvoiceLine(
                    item_id=_text(row.get(columns.get("item_id", ""))),
                    quantity=to_decimal(row.get(columns.get("quantity", ""))),
                    unit_price=to_decimal(row.get(columns.get("unit_price", ""))),
                    line_no=_as_int(line_no, index),
                    po_line_id=_text(row.get(columns.get("po_line_id", ""))),
                )
            )
    if lines:
        return lines

    raw_row = getattr(invoice, "raw_row", None) or {}

    if schema.json_lines_column:
        aliases = FIELD_ALIASES["invoice_lines"]
        for index, row in enumerate(
            _json_rows(raw_row.get(schema.json_lines_column)), start=1
        ):
            lines.append(
                InvoiceLine(
                    item_id=_text(_pick(row, aliases["item_id"])),
                    quantity=to_decimal(_pick(row, aliases["quantity"])),
                    unit_price=to_decimal(_pick(row, aliases["unit_price"])),
                    line_no=_as_int(_pick(row, aliases["line_no"]), index),
                    po_line_id=_text(_pick(row, aliases["po_line_id"])),
                )
            )
        return lines

    if schema.flat_line_columns and raw_row:
        # One row per invoice: the invoice row is its own single line item.
        flat = schema.flat_line_columns
        lines.append(
            InvoiceLine(
                item_id=_text(raw_row.get(flat.get("item_id", ""))),
                quantity=to_decimal(raw_row.get(flat.get("quantity", ""))),
                unit_price=to_decimal(raw_row.get(flat.get("unit_price", ""))),
                line_no=_as_int(raw_row.get(flat.get("line_no", "")), 1),
                po_line_id=_text(raw_row.get(flat.get("po_line_id", ""))),
            )
        )
    return lines


def _looks_like_fee_record(row: dict, aliases: dict[str, tuple[str, ...]]) -> bool:
    """A fee record names its own label and amount columns; a {label: amount} mapping
    does not.  Both shapes turn up in JSON-embedded fee lists."""
    keys = {_norm_name(k) for k in row if isinstance(k, str)}
    has_label = any(_norm_name(alias) in keys for alias in aliases["label"])
    has_amount = any(_norm_name(alias) in keys for alias in aliases["amount"])
    return has_label and has_amount


def _fees_from_json(raw: Any, aliases: dict[str, tuple[str, ...]]) -> list[Fee]:
    value = raw
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except ValueError:
            return []
    if isinstance(value, dict):
        if _looks_like_fee_record(value, aliases):
            value = [value]
        else:
            return [Fee(label=_text(k), amount=to_decimal(v)) for k, v in value.items()]
    if not isinstance(value, (list, tuple)):
        return []
    fees: list[Fee] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        if _looks_like_fee_record(row, aliases):
            fees.append(
                Fee(
                    label=_text(_pick(row, aliases["label"])),
                    amount=to_decimal(_pick(row, aliases["amount"])),
                )
            )
        else:
            fees.extend(Fee(label=_text(k), amount=to_decimal(v)) for k, v in row.items())
    return fees


def _load_fees(database: Database, schema: Schema, invoice: Invoice) -> list[Fee]:
    resolved = schema.get("invoice_fees")
    fees: list[Fee] = []
    if resolved is not None:
        rows = _select(database, resolved, "invoice_ref", invoice.invoice_number)
        columns = resolved.columns
        for row in rows:
            fees.append(
                Fee(
                    label=_text(row.get(columns.get("label", ""))),
                    amount=to_decimal(row.get(columns.get("amount", ""))),
                )
            )
    if fees or not schema.json_fees_column:
        return fees
    raw_row = getattr(invoice, "raw_row", None) or {}
    return _fees_from_json(raw_row.get(schema.json_fees_column), FIELD_ALIASES["invoice_fees"])


def load_po_lines(database: Database, po_number: str) -> list[POLine]:
    schema = database.schema()
    resolved = schema.get("po_lines")
    assert resolved is not None, "validate_schema() guarantees this"
    rows = _select(database, resolved, "po_ref", po_number, order_fields=("line_no",))
    columns = resolved.columns
    po_lines: list[POLine] = []
    for index, row in enumerate(rows, start=1):
        line_no = row.get(columns["line_no"]) if "line_no" in columns else None
        po_lines.append(
            POLine(
                po_number=_text(row.get(columns.get("po_ref", ""))) or po_number,
                item_id=_text(row.get(columns.get("item_id", ""))),
                quantity=to_decimal(row.get(columns.get("quantity", ""))),
                unit_price=to_decimal(row.get(columns.get("unit_price", ""))),
                line_no=_as_int(line_no, index),
                currency=_text(row.get(columns.get("currency", ""))),
                po_line_id=_text(row.get(columns.get("po_line_id", ""))),
            )
        )
    return po_lines


def load_contract(database: Database, supplier_id: str, *fallbacks: Optional[str]
                  ) -> Optional[Contract]:
    """Allowed items are the distinct item_id values across this supplier's own contract
    rows; allowed fees come from the documented allowed_fees column.  Never generalised
    from another supplier.

    The invoice record may identify its supplier in more than one column, and only one of
    them joins to the contract table.  Each candidate the request itself named is tried in
    turn; the first with rows wins.  This cannot widen the contract -- every candidate
    refers to the same supplier on the same invoice -- it only avoids reporting a missing
    contract because the wrong column was used to look it up."""
    schema = database.schema()
    resolved = schema.get("contracts")
    assert resolved is not None, "validate_schema() guarantees this"

    candidates: list[str] = []
    for candidate in (supplier_id, *fallbacks):
        text = _text(candidate)
        if text and not any(norm_key(text) == norm_key(seen) for seen in candidates):
            candidates.append(text)

    rows: list[dict] = []
    for candidate in candidates:
        rows = _select(database, resolved, "supplier_id", candidate)
        if rows:
            supplier_id = candidate
            break
    if not rows:
        return None
    columns = resolved.columns
    allowed_items: set[str] = set()
    allowed_fees: set[str] = set()
    agreed_prices: dict[str, Decimal] = {}
    for row in rows:
        item = _text(row.get(columns.get("item_id", "")))
        if item:
            allowed_items.add(norm_key(item))
            price = to_decimal(row.get(columns.get("agreed_unit_price", "")))
            if price is not None:
                agreed_prices.setdefault(norm_key(item), price)
        if "allowed_fees" in columns:
            allowed_fees |= parse_fee_labels(row.get(columns["allowed_fees"]))
    # Only read an allowed_items column if the real schema genuinely has one.
    if "allowed_items" in columns:
        for row in rows:
            allowed_items |= parse_fee_labels(row.get(columns["allowed_items"]))
    return Contract(
        supplier_id=_text(rows[0].get(columns.get("supplier_id", ""))) or supplier_id,
        allowed_items=frozenset(allowed_items),
        allowed_fees=frozenset(allowed_fees),
        agreed_prices=agreed_prices,
        row_count=len(rows),
    )


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
