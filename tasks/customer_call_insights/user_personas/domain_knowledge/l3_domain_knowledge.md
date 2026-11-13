# customer_call_insights — L3 domain knowledge (agent-facing extract)

Role: Revenue-operations director (product owner for this effort)

## monthly_call_volume

~4k recorded calls a month across the sales and CS teams

## current_pain

Post-call notes are inconsistent or missing; what we promised on a call gets lost and a customer
has to remind us.

## who_reads_it

Reps and CS managers read the CRM before their next touch; the CS health review runs off it weekly.

## final_consumer

A downstream job writes the output into the CRM automatically -- no human retypes it.

## trust_is_the_metric

If a rep has to pull the recording to check what was agreed, this has not worked.

## no_invented_detail

Everything recorded has to be something actually said on the call -- we cannot have entries nobody
can trace, because then reps stop trusting the whole record.

## nothing_agreed_gets_dropped

The flip side, and just as damaging: if we told a customer we would do something and it never
reaches the record, they are the one who reminds us. That is how a relationship ends up on the
fence, and it is the reason we started this. Missing something real is not the safe option -- it is
the other half of the same problem.

## quiet_calls_stay_quiet

Plenty of calls are a five-minute check-in where nothing was agreed; those should come back with
nothing in them rather than padded out. That is not licence to be sparse on a call that did have
things in it.

## success_signal

Reps stop re-listening to calls, and nothing we promised goes missing.

## preloaded_examples

IT tells me they've preloaded some example calls in the database already, if that's useful to you.

## what_we_want_captured

Four things, in the business terms we use internally: what needs doing after the call, what we
specifically promised the customer, warning signs that the account or deal could go badly, and how
the customer came across overall. That's the ask. Exactly how that gets encoded -- the field names,
the format -- is an engineering call, not mine; I care about the four things being right, not about
the shape they're written in.
