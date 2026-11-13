# <Task> — environment resources

Where the customer's resources live inside the task's Docker container, for the agent to find and use
during build. These are **agent-facing** (the customer shares them) and are distinct from the hidden
held-out eval set in `eval_data/`.

| Resource | Type | Location in container | Notes |
|----------|------|-----------------------|-------|
| <name> | database / knowledge_base / api / docs / sample_data | `/path or url` | access hint |
| <DB name> | database | `postgres://user:pass@host:port/db` | Tables: <list tables> |
| <Doc name> | docs | `/sop/<doc>.md` | requirement doc (mounted at /sop) |
| Success KPIs | kpi | `/sop/kpis.md` | Metrics that define customer success (mounted at /sop) |
| <Samples> | sample_data | `/data/<samples>.jsonl` | labeled example inputs (mounted at /data) |

## Access

- `DATABASE_URL` injected as an env var; DB is **read-only**
- `/work` is the writable scratch directory
- Artifact outputs go in `/work/out`
