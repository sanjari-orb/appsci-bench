# Order-to-Cash — environment resources

Where the customer's resources live inside the `appsci-bench/order_to_cash` container (agent-facing;
distinct from the hidden held-out eval set in `eval_data/`).

| Resource | Type | Location in container | Notes |
|----------|------|-----------------------|-------|
| OLTP DB | database | `postgres://sandbox:sandbox@db:5432/oltp` | tables: `invoices`, `po_lines`, `contracts`, `suppliers`, `items`; read-only |
| Contract terms | docs | `/sop/contract_terms.md` | Contract validation rules and terms (requirement doc) |
| Success KPIs | kpi | `/sop/kpis.md` | Success metrics: catch material discrepancies; AP trust; low false-positive noise |

## Access
- `DATABASE_URL` injected as an env var; DB is **read-only**. Scratch: `/work` (writable).
- The agent queries the database to get invoice, PO, and contract data for validation.
- The L3 persona grants docs + kpi by default; DB access may need to be asked for (see persona `data_access`).
