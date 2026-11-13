# crm_insights — L3 domain knowledge (agent-facing extract)

Role: Manager or analyst asking a business question about the CRM

## problem_shape

I have a question about our accounts, cases, or agents (e.g. which agent handles cases fastest,
what's the top complaint about a product, which state closes cases quickest) and I want a direct
answer with the records behind it, not a data dump.

## data_shape

I don't know the database schema or how to query it -- I just ask the question in plain language,
the way I'd ask a colleague.

## what_counts_as_problem

An answer is only useful if it's both correct and backed by specific records I could go check
myself (case numbers, not just a number). A confident-sounding answer with nothing behind it isn't
something I can act on or defend in a meeting.

## what_doesnt_count

I don't care about the SQL/query mechanics, table names, or how the system got the answer -- only
that it's right and I can see what it's based on if I ask.

## expected_output

A direct answer to my question (an agent name/id, a state, a month, an issue) plus a short list of
the specific case records that support it.

## priority

Getting it right matters more than getting it fast -- these numbers sometimes go into reports or
performance conversations, so a wrong answer is worse than a slightly slower one.
