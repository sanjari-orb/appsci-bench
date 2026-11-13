# banking_support — L2 domain knowledge (agent-facing extract)

Extracted verbatim from the persona of the same name. The simulator still answers out of
that YAML; this file exists so the no-user arm has something to mount for the half of a
stakeholder's knowledge that was never a document. Keep the two in step: a fact here that
is not in the persona would be handed to one arm and withheld from the other.

Role: Integration engineer who owns the Rho-Bank customer-service backend, data, and tool layer

## db_shape

transactional store (in-memory JSON) with collections for users, accounts, debit/credit cards, credit_card_applications, referrals, verification_history, transaction/dispute records, etc.

## user_record

a user has user_id, name, address, date_of_birth, email, phone_number; identity is confirmed by matching 2 of 4 fields (dob/email/phone/address)

## knowledge_base

~700 official documents (product terms, fee schedules, eligibility, procedures), each {id, title, content}; searchable via the provided retrieval tools (KB_search_bm25/KB_search_dense/shell); searching is the ONLY way in -- there is no endpoint that lists the documents or fetches one by id

## retrieval_provided

retrieval over the corpus is PROVIDED as agent tools -- KB_search_bm25 (BM25 keyword, no key), KB_search_dense (OpenAI embeddings; needs BANKING_EMBED_API_KEY or OPENAI_API_KEY, degrades gracefully without one), and shell (read-only Unix commands -- ls/cat/grep/head/find/awk -- over the corpus as files); all READ-only over the corpus, so they never touch the DB. Whoever builds the agent WIRES these rather than building their own retrieval (the corpus is far too large to put in a prompt)

## base_tools

always-available agent tools: get_user_information_by_{id,name,email}, get_credit_card_accounts_by_user, get_referrals_by_user, get_credit_card_transactions_by_user, log_verification, change_user_email, get_current_time, transfer_to_human_agents

## customer_tools

tools the CUSTOMER executes (not the agent): apply_for_credit_card, submit_referral, submit_transaction, request_human_agent_transfer, call_discoverable_user_tool

## discoverable_tools

specialized actions are NOT listed by default -- the knowledge base names the tool; the agent unlocks it with unlock_discoverable_agent_tool then calls call_discoverable_agent_tool, or gives a customer-side tool over with give_discoverable_user_tool

## no_policy_enforcement

the write tools apply changes blindly; they do NOT validate grounding, eligibility, or identity -- that's on the caller

## reference_time

the environment's 'now' is fixed at 2025-11-14 03:40:00 EST; the agent should call get_current_time rather than assume it
