# Do not quote this attempt's elicitation score

`elicitation.json` here reads `status: scored`, `elicited_rate: 0.0`. Taken at face value that says a
person asked for nineteen facts and got none. It does not mean that.

This conversation is dated **2026-08-06**. The airline policy did not move behind the customer simulator
until **2026-08-19** (PR #100, commit `9c0a1f9`, which created `tasks/airline_rebooking/user_sim_docs/`).
On the day of this attempt the policy document was mounted for the agent to read, and the persona held
none of the facts the rubric asks about. Zero is the only score this transcript could have produced, and
it is a fact about the setup rather than about the participant.

The rubric was authored later, from `user_sim_docs/common/policy.md`. Scoring an older conversation
against it is still worth doing -- it is how you see that the score responds to the material actually
being withheld -- but the number is not a human baseline for elicitation and must not be read as the
reference point a machine score is compared against.

The same caution applies more weakly to the `order_to_cash` attempt (2026-08-12): it predates the
persona carve-out of PR #114 (2026-08-19), so its 0.5 is a floor rather than a measurement. Its
threshold items did come back, so that conversation was at least scoring something real.

A post-2026-08-19 attempt is what would give a quotable human number for airline. There is not one yet.
