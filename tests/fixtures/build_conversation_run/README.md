# A real build conversation, recorded

`events-usersim.jsonl` is copied verbatim from the user-sim event log of a real benchmark run
(`order_to_cash`, 2026-08-11) -- four exchanges between the applied-science agent and the customer
simulator.

It is here because `runs/` is not in the repository, so a golden check against a run on someone's disk
passes for them and fails everywhere else.

**Do not tidy it.** The `model.request` / `model.response` lines interleaved between the halves of each
exchange are the point: they are why the reader pairs a question to its answer by turn number rather
than by file order. A test asserts they are still present.

Checked for credentials before committing: no keys, tokens or addresses.
