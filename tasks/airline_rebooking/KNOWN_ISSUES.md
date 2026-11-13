# Airline task — known issues

## 1. Benchmark contamination: the model already knows the tau2 airline domain

**What:** The airline task is offline-cast from **tau2-bench** (`sierra-research/tau2-bench`), a
**public** benchmark. Current frontier models have the tau2 airline **policy and tool signatures in
their training data**, so the appsci-agent can reproduce them from memory rather than learning them
from the task.

**Evidence (run `airline_rebooking_20260727_162733`, on the redacted spec):** the requirement docs
were mounted **nowhere** at the time, yet the agent still produced a working artifact. The trace shows
exactly how:

- The built `app.py` embeds the tau2 airline policy **verbatim** (e.g. "Basic economy flights cannot
  be modified", "one tool call at a time") — byte-for-byte matches `user_sim_docs/common/policy.md`.
- mini-swe first *tried* to find the docs and failed: it probed the tool-API for `docs`/`policy`/
  `tools_reference` endpoints (none), searched the whole filesystem (`find / -iname '*tau2*'`,
  `grep -rl 'transfer_to_human_agents' /`), and checked for a `pip`-installed `tau2` package — all
  empty.
- It then fell back to memory. Its own commands are annotated:
  `# tau2 airline uses user_ids like mia_li_3668, reservation_id like HATHAT` and
  `# book_reservation - full known tau2 signature`, then it probed the live tool-API to confirm the
  14 tool names.

**This is recall, not retrieval.** The agent has no web-search/browse tool (its tools are
`talk_to_user`, `mini_swe_agent`, `submit_artifact`), and the only external URLs in the whole trace
are its model gateway, the docker CLI download, and its own deployed artifact — nothing fetches tau2
from the internet. The policy/tool knowledge is parametric (training data).

**Why it matters:**
- The score partly measures **tau2 recall**, not "build a compliant agent from the provided
  requirements."
- It is **not general**: a private / non-public task the model has never seen would **fail** under
  the same setup — the docs are reachable nowhere and the tool-API is only partly self-documenting
  (it validates the request *wrapper*, not per-tool argument schemas), so without recall the agent
  would be brute-forcing argument names.
- Possible deeper contamination: if the model also memorized the tau2 **test cases** (the per-case
  oracle in `eval_data/`), scores would be inflated beyond "knows the policy." **Not yet verified —
  worth checking.**

**Mitigations to consider (task-owner decisions, not yet done):**
- Perturb / paraphrase the public policy so verbatim recall can't substitute for using the provided
  requirements.
- Confirm the held-out `eval_data/` cases aren't reproducible from model memory.
- Add a `GET /tools` schema-discovery endpoint to the tool-API so tool *discovery* is a legitimate,
  recall-independent channel (without leaking the policy).

## 2. The policy and the tool signatures are both elicited

The agent is given **no policy document at all**. Every operating rule -- booking, modifying,
cancelling, refunds and compensation -- lives in `user_sim_docs/common/policy.md`, mounted only into
the user-sim, and reaches all three personas. Relative to the previous state this is a plain file
move: `environment/sop/policy.md` became `user_sim_docs/common/policy.md`, byte for byte.

The success KPIs moved with it and are held by the L1 and L3 personas. Gone from `spec.yaml`: the
`technical_docs` list (it named the policy file), the `rebooking_policy` resource, and the `kpis`
resource -- each named something the agent can no longer reach, and each carried a description that
restated the material it was hiding, which the redacted agent spec keeps.

The **tool argument schemas are now elicited too**, after review pushback on treating the policy and
the tool reference differently (PR 100). `environment/sop/tools_reference.json` moved to
`user_sim_docs/common/tools_reference.json`, byte for byte, so the agent gets no `/sop` mount at all.

The earlier position -- that tool discovery is not what this task measures, and that forcing fourteen
argument schemas through conversation spends the elicitation budget on secretarial work -- is what
this change reverses. Whether it costs more in wasted turns than it buys in validity is the open
question the next runs answer.

The tool NAMES went too, in a follow-up step: `interface_design` no longer enumerates the fourteen
tools, only that a fixed read/write/transfer tool set exists and that its names and argument schemas
are not listed in the spec. The artifact's HTTP contract stays in that field, since the build needs
it.

**Measured result: withholding tool information does not force elicitation.** In run
`airline_rebooking_20260819_125722`, with names and schemas both withheld, the agent recovered the
names from memory in its eighth shell command -- annotating the loop itself
`# tau-bench airline tools common names` -- and confirmed them by probing the tool API for 404s. The
list it wrote from memory included names this domain does not have (`think`,
`find_user_id_by_name_zip`, `find_user_id_by_email`) and one near-miss
(`update_reservation_baggage`), which is what marks it as recall rather than discovery. It then spent
all three of its stakeholder questions on policy and never asked about tools at all.

Argument schemas are similarly recoverable without asking: a single `get_reservation_details` read
returns `cabin`, `flight_type`, `total_baggages`, `nonfree_baggages`, `insurance`, `passengers` and
the rest -- essentially the booking argument list, delivered by a read tool.

So the policy numbers remain the only material this task genuinely elicits, because they are the only
material the model cannot recall correctly. That is the case for paraphrasing rather than relocating:
contamination is defeated by making recall WRONG, not by making files unreadable.

What is still open:

- **The agent does not ask.** Measured, twice, on the L2 persona (runs
  `airline_rebooking_20260812_093557` and `..._094444`, on `claude-opus-4-8`): zero stakeholder
  exchanges in either, an empty user-sim event log, and not one of its messages noting anything
  unclear, missing or assumed. In the second run it declared *"I have everything I need"* after
  reading `/sop`, and self-identified the domain as *"tau-bench style"*. Those runs predate this
  change -- the agent then still had booking and cancellation rules to read. Whether removing the
  document entirely provokes a question is untested.
- **Verbatim recall still substitutes for elicitation.** Section 1 above is unfixed. Neither run
  reproduced the withheld rules, but the policy is public and in training data, so a run that
  *appears* to elicit may be recalling. Paraphrasing is the fix and is a separate change; this one
  froze every number so all 50 eval cases and their expected DB states stay valid.
- ~~**Two copies of the policy, hand-synced, unguarded.**~~ **RESOLVED.** The duplication is gone:
  `tasks/airline_rebooking/sop/` was removed and `user_sim_docs/common/` is now the single copy of the
  policy and tool reference. The gold reference reads them from there too, so nothing has to be
  hand-synced and the guard that policed the drift is no longer needed.
- **The policy is written as a build specification, not as staff knowledge.** Its header says the
  document "is fed to the meta applied-science agent" and its preamble opens "**Build an agent** that
  can help users book, modify, or cancel...". That voice was right when the agent read it. Now a
  persona reads it, so the sim is holding a document addressed to the builder rather than a
  description of how the airline works.

### `environment/sop/` and the "nothing but requirement docs" rule

`environment/sop/` no longer holds any document. Git does not track empty directories, so a fresh
clone has no such directory at all and the orchestrator's existence check skips the `/sop` mount; in a
working checkout where the empty directory lingers, `/sop` is still mounted but empty (confirmed in run
`airline_rebooking_20260819_105148`). Either way the agent is handed no documents. Because the whole
directory is bind-mounted, **every file in it is agent-visible** -- so no
READMEs or dev-docs go here (an earlier stray README leaked the `eval_data/` and `gold/` paths to the
agent and was removed; `slides_generator` had the same bug in its `/data`). This is guarded by
`test_every_task_agent_data_dir_is_clean`, which scans every task's `/data` **and** `/sop` for
ground-truth path markers. The rule still applies if anything is ever put back here: nothing but
requirement docs, and copied rather than symlinked -- a Docker bind-mount cannot follow a symlink
pointing outside the mounted directory.

The sync burden is now gone rather than moved: `user_sim_docs/common/` holds the only copy of the
policy and the tool reference, and `user_sim_docs/success_kpis.md` the only copy of the KPIs. The
`sop/` tree that used to hold the originals was removed once nothing read it -- the vendored upstream
provenance moved to `provenance/airline/` and the ground-truth task definition to `TASK_SOP.md` at the
task root.
