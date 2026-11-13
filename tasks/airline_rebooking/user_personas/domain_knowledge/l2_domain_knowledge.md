# airline_rebooking — L2 domain knowledge (agent-facing extract)

Extracted verbatim from the persona of the same name. The simulator still answers out of
that YAML; this file exists so the no-user arm has something to mount for the half of a
stakeholder's knowledge that was never a document. Keep the two in step: a fact here that
is not in the persona would be handed to one arm and withheld from the other.

Role: Integration engineer who owns the airline reservations backend + tool layer

## db_shape

the airline DB has three collections: flights, users, reservations

## user_record

a user has user_id, name, address, dob, email, membership level (regular/silver/gold), payment_methods (credit_card/gift_card/certificate), reservation ids

## reservation_record

a reservation has reservation_id, user_id, origin, destination, flight_type (one_way/round_trip), cabin, flights[], passengers[], payment, baggages, insurance, created time

## flight_record

a flight has flight_number, origin, destination, scheduled times, and per-date status (available/delayed/on time/flying) with seats + prices per cabin

## read_tools

get_user_details, get_reservation_details, search_direct_flight, search_onestop_flight, list_all_airports, get_flight_status, calculate

## write_tools

book_reservation, update_reservation_flights, update_reservation_baggages, update_reservation_passengers, cancel_reservation, send_certificate

## transfer_tool

transfer_to_human_agents(summary) for out-of-scope requests

## no_policy_enforcement

the write tools apply changes blindly; they do NOT validate policy/eligibility -- that's on the caller

## confirmation_gate

product requirement: list action details and get explicit user confirmation before any write tool call; one tool call per turn

## reference_time

the environment's 'now' is fixed at 2024-05-15 15:00:00 EST (matters for 24h cancel + flown-segment checks)

## test_records_note

nothing enumerates users or reservations -- get_user_details and get_reservation_details both take an id you already hold -- so the build environment's records are listed below. get_user_details returns a user's reservation ids, so one user id reaches everything of theirs. These are build-environment records only.

## test_record_ids

nora_castillo_9412, owen_delgado_9137, priya_venkat_9268, quinn_okafor_9503, rafael_moreau_9821, TRN001
