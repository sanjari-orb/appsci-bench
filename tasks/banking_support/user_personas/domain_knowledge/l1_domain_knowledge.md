# banking_support — L1 domain knowledge (agent-facing extract)

Extracted verbatim from the persona of the same name. The simulator still answers out of
that YAML; this file exists so the no-user arm has something to mount for the half of a
stakeholder's knowledge that was never a document. Keep the two in step: a fact here that
is not in the persona would be handed to one arm and withheld from the other.

Role: Applied-science manager overseeing the Rho-Bank customer-service-agent build

## task

a knowledge-grounded agent that advises customers and drives correct account actions (applications, referrals, disputes, servicing) over the banking DB + tools

## corpus_too_large

the rules that make an answer correct live across ~700 documents; the agent searches them with the PROVIDED retrieval tools (KB_search_bm25/KB_search_dense/shell) rather than building its own -- you cannot stuff the corpus into a prompt

## grounding_gates_action

correct handling -- including WHICH specialized (discoverable) tool to use -- is gated on retrieving the right knowledge-base doc; the doc names the tool to unlock/call

## no_fabrication

the agent must ground every answer/action in the retrieved docs and must never invent policies, fees, eligibility, or capabilities; if the KB doesn't cover it, say so / transfer

## verify_before_acting

identity must be verified (2 of 4 of dob/email/phone/address, then log_verification) before any account action

## refusals_and_transfers

transferring to a human is correct only when the KB describes no applicable action -- not as a shortcut

## how_we_judge_it

what counts is whether the conversation reached the outcome the bank's rules require -- the customer's records end up in the right state, or the right action is taken and handed over -- not how fluent the reply reads; an action the CUSTOMER performs themselves counts just as much as one the agent takes

## customers_are_unreliable

customers are often wrong or vague about products, eligibility, or what they're owed; the agent should check the KB, not take their word
