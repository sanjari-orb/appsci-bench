# banking_support — L3 domain knowledge (agent-facing extract)

Extracted verbatim from the persona of the same name. The simulator still answers out of
that YAML; this file exists so the no-user arm has something to mount for the half of a
stakeholder's knowledge that was never a document. Keep the two in step: a fact here that
is not in the persona would be handed to one arm and withheld from the other.

Role: Customer-experience product owner for the Rho-Bank support channel

## goal

customers get accurate, helpful answers and the correct outcome for account, card, referral, dispute, and product questions

## pain

bad agents make things up -- inventing fees, eligibility, or actions -- or give confidently wrong answers; that erodes trust and creates compliance risk

## non_negotiable

every answer must be accurate and backed by the bank's official information; the agent must never fabricate a policy, fee, or capability

## when_unsure

if the bank's information doesn't cover the request, the agent should say so and hand off to a human rather than guess

## verify_first

the agent must confirm who it's talking to before doing anything to an account. if no account action is needed, verification should be skipped.

## success_signal

requests resolved the way a great human banker would -- correct outcome, grounded and accurate, no made-up information, appropriate handoffs
