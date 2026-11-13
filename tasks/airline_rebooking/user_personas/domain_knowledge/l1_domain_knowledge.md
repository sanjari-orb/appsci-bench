# airline_rebooking — L1 domain knowledge (agent-facing extract)

Extracted verbatim from the persona of the same name. The simulator still answers out of
that YAML; this file exists so the no-user arm has something to mount for the half of a
stakeholder's knowledge that was never a document. Keep the two in step: a fact here that
is not in the persona would be handed to one arm and withheld from the other.

Role: Applied-science manager overseeing the airline reservations-agent build

## task

an agent that books, modifies, cancels reservations and handles refunds/compensation over the airline DB + tools

## policy_is_unenforced

the tools do NOT enforce airline policy; the agent must check eligibility/rules itself before any write

## refusals_matter

a large share of cases are correctly handled by refusing or transferring to a human, not by acting

## confirmation_gate

every DB-updating action needs the action details listed + explicit user 'yes' first

## scoring

each case scored as DB x COMMUNICATE: final DB must match the golden actions AND required facts must be said to the user

## customers_are_unreliable

customers are often wrong about their own membership level, eligibility, or what they're owed

## test_records_note

the build environment is a small stand-in airline, not production; if they need real records to try the tools against, the accounts below are the ones the team uses. get_user_details on any of them returns that user's reservation ids, which is how you reach the reservations.

## test_record_ids

nora_castillo_9412, owen_delgado_9137, priya_venkat_9268, quinn_okafor_9503, rafael_moreau_9821, TRN001
