# airline_rebooking — L3 domain knowledge (agent-facing extract)

Extracted verbatim from the persona of the same name. The simulator still answers out of
that YAML; this file exists so the no-user arm has something to mount for the half of a
stakeholder's knowledge that was never a document. Keep the two in step: a fact here that
is not in the persona would be handed to one arm and withheld from the other.

Role: Customer-experience product owner for the reservations channel

## goal

callers get a correct, policy-compliant outcome for booking, changing, cancelling, and refund/compensation requests

## pain

customers are often wrong about their status or what they're owed, and bad agents either break policy or give wrong answers

## non_negotiable

the agent must never do something the airline doesn't allow; when a request can't be honored it should say no or hand off to a human

## records_correct

whatever the agent does must leave the reservation records correct

## success_signal

requests resolved the way a great human agent would, with no policy violations and accurate information to the customer

## test_records_note

if they want something real to try it on, these are the test customers in the practice environment. Looking up a customer shows you their bookings.

## test_record_ids

nora_castillo_9412, owen_delgado_9137, priya_venkat_9268, quinn_okafor_9503, rafael_moreau_9821, TRN001
