"""The hand-authored SYNTHETIC records added to the banking BUILD-phase store.

WHY THIS FILE EXISTS. The strict carve leaves 8 real records — 6 customers and 2 bank accounts, both
belonging to one of them — and 15 of the 17 collections empty. That is the correct answer for a
carve-out (see `generate_banking_build_share.py`), and it also leaves the build environment unable to
support the thing the build phase is for. Measured, not assumed: on run
`banking_support_b1_l3_20260910_023628` the agent's own eval generator authored a debit-card freeze case
against an invented customer (`usr_marcus_reilly_demo`), because the store holds no card to freeze and no
customer who owns one. Its judge prompt says so in as many words — "the persona uses plausible demo
identifiers rather than a shared mutable record" — so it graded the conversation instead of the outcome,
scored itself 0.9175, and the artifact then scored 0.021 on the real eval. An agent cannot build a
state-based test of its own work against an empty store, so it builds a politeness test.

WHAT THESE RECORDS ARE. Fabricated customers' holdings for the six customers the carve LEFT, so the
agent can exercise a request end to end: verify an identity against a real record, read a real account,
freeze a real card, dispute a real transaction, judge a real limit-increase against a real payment
history. They are not a relaxation of the withholding rule — nothing here comes out of the eval store —
which is why the augmentation is a separate stage with its own validations rather than a looser carve.

THE FIVE RULES ANY ADDITION HERE MUST OBEY. All five are enforced by `validate_synthetic` in the
generator, so a breach is a refusal to write rather than a review comment:

  1. NO KEY MAY EXIST IN THE FULL STORE. This is what "belongs to the eval environment" means
     operationally: not merely that a record is not scored, but that its id is not the eval store's to
     reuse. A shared key would make a build record and an eval record indistinguishable by id.
  2. NO KEY MAY BE NAMED BY AN EVAL CASE, on whole-value matching, the same test the carve applies to
     what it keeps.
  3. NO KEY MAY COLLIDE WITH AN ID A GOLDEN ACTION MINTS. The replay creates records; a synthetic row
     keyed the same as one of them would make a live creation indistinguishable from the starting state.
  4. EVERY REFERENCE MUST RESOLVE INSIDE THE BUILD SHARE. A synthetic record may point at a carved
     record or another synthetic one, never at anything withheld — the same closure rule, extended.
  5. NOTHING MAY BE DATED AFTER THE ENVIRONMENT'S FIXED "NOW" (2025-11-14 03:40:00 EST). The clock is
     frozen, the agent is told to read it from the tool rather than the host, and a card issued next
     week teaches it the opposite. Card EXPIRY dates are the deliberate exception, and are checked to be
     in the future instead.

WHY THE STORIES ARE SHAPED THE WAY THEY ARE. Each of the six customers carries one situation an internal
eval case can be built from, chosen to reach past verification into the part of the task that is actually
scored — the specialist-tool path. Mia Smith is a six-day-old thin file whose card has not been activated
yet; Alex Riviera is the clean happy path; Liu Xiaoming has a frozen card and a replacement in flight;
Emily Thompson has a duplicated charge, an open dispute and a payment history with a late and a missed
month; Bei Chen is the business customer with an approved limit increase and a fee-waiver flag; Maria
Elena Rodríguez has closed a card, ordered a replacement for another, and holds referrals in every state
that is not the happy one.

WHAT IS DELIBERATELY NOT HERE. The runtime bookkeeping collections — `agent_discoverable_tools`,
`user_discoverable_tools`, `user_discoverable_tool_calls`, `task_config`, `human_transfer_requests` —
stay empty. The first is the read-log table whose rows the hash comparison counts, and its keys are a
deterministic function of a tool name, so a seeded row there would both collide with rule 3 and move the
state hash. Those tables are written by the run, not by the fixture.

BALANCES DO NOT RECONCILE TO TRANSACTIONS, except where they can. The transaction lists are a recent
window (10/2025 onward), not an account's whole history, which is the shape the full store already has —
an account opened in 2021 does not carry four years of rows. For an account opened INSIDE the window the
sum is exact and is checked: Mia's two accounts, opened 11/08/2025, reconcile to the cent.
"""
from __future__ import annotations

# The environment's frozen clock. Every authored date is on or before this day; see rule 5.
REFERENCE_DATE = "11/14/2025"

# The six customers the carve left, by the id they are keyed under. Repeated here as a readable index
# rather than re-derived: every record below hangs off one of these six, and a reviewer needs the names
# to check that a cardholder_name, a dispute's full_name and a verification record agree with the
# customer row they point at.
CUSTOMERS = {
    "123": "Mia Smith",
    "126": "Alex Riviera",
    "3d91a520be": "Liu Xiaoming",
    "4db6fc36e8": "Emily Thompson",
    "601e851551": "Bei Chen",
    "d97fb90fe8": "Maria Elena Rodríguez",
}

# Addresses, phones and dates of birth as the carved customer rows hold them. Any synthetic record that
# restates one of these (a dispute's contact block, a shipping address, a verification record) must match
# its customer row exactly, and the generator checks it — a customer whose address differs between two
# collections teaches the agent that the store disagrees with itself.
_ADDRESS = {
    "123": "742 Maple Street, Austin, TX 78701",
    "126": "24 Beacon Street, Boston, MA, 02114",
    "3d91a520be": "1456 University Avenue, Irvine, CA 92612",
    "4db6fc36e8": "892 Willow Creek Road, Charlotte, NC 28202",
    "601e851551": "2890 Pacific Boulevard, Cupertino, CA 95014",
    "d97fb90fe8": "4455 Mission Street, San Francisco, CA 94112",
}
_PHONE = {
    "123": "512-555-0147",
    "126": "930-102-1332",
    "3d91a520be": "949-555-0418",
    "4db6fc36e8": "704-555-0183",
    "601e851551": "408-555-0932",
    "d97fb90fe8": "415-555-0158",
}
_EMAIL = {
    "123": "mia.smith8574@gmail.com",
    "126": "alex_alex@gmail.com",
    "3d91a520be": "pixel_dragon2000@gmail.com",
    "4db6fc36e8": "pancake_enthusiast@outlook.com",
    "601e851551": "noodle_wizard_88@hotmail.com",
    "d97fb90fe8": "sunflower_dreams2024@hotmail.com",
}
_DOB = {
    "123": "03/15/1992",
    "126": "09/21/1996",
    "3d91a520be": "04/09/1996",
    "4db6fc36e8": "08/15/1990",
    "601e851551": "10/18/1988",
    "d97fb90fe8": "09/28/1995",
}


# =====================================================================================================
# accounts
#
# Eleven bank accounts, so that five of the six customers own something (before this, five of the six
# owned nothing at all and the sixth owned both accounts in the store). Keys follow the full store's own
# two live conventions -- `chk_<user_id>` for checking, `biz_chk_<user_id>` for business checking, and a
# 16-hex key for a savings account -- all three of which the full store uses.
#
# The LEVELS are chosen to cover, between them, every one of the twelve account levels the full store
# uses, and the CLASSES to cover business_checking. That is not padding for the coverage checker's sake:
# an agent advising on products needs to have seen more than one tier, and before this the build share
# carried two levels out of twelve. Multiple checking accounts per customer is native to this store --
# the full store has customers with three and four.
# =====================================================================================================
ACCOUNTS = {
    # -- 126 Alex Riviera: the established, well-behaved customer. Two checking tiers and a savings.
    "chk_126": {
        "account_id": "chk_126", "user_id": "126", "class": "checking",
        "level": "Green Account", "date_opened": "03/12/2023", "status": "OPEN",
        "current_holdings": "2418.77",
    },
    "chk_126_2": {
        "account_id": "chk_126_2", "user_id": "126", "class": "checking",
        "level": "Bluest Account", "date_opened": "11/04/2024", "status": "OPEN",
        "current_holdings": "742.10",
    },
    "61f7a2c4d8e93016": {
        "account_id": "61f7a2c4d8e93016", "user_id": "126", "class": "saving",
        "level": "Silver Account", "date_opened": "05/02/2023", "status": "OPEN",
        "current_holdings": "6150.00",
    },
    # -- 3d91a520be Liu Xiaoming: the customer whose card is frozen.
    "chk_3d91a520be": {
        "account_id": "chk_3d91a520be", "user_id": "3d91a520be", "class": "checking",
        "level": "Light Blue Account", "date_opened": "07/19/2024", "status": "OPEN",
        "current_holdings": "836.40",
    },
    "chk_3d91a520be_2": {
        "account_id": "chk_3d91a520be_2", "user_id": "3d91a520be", "class": "checking",
        "level": "Dark Green Account", "date_opened": "02/03/2025", "status": "OPEN",
        "current_holdings": "1204.15",
    },
    # -- 4db6fc36e8 Emily Thompson: the disputes customer.
    "chk_4db6fc36e8": {
        "account_id": "chk_4db6fc36e8", "user_id": "4db6fc36e8", "class": "checking",
        "level": "Silver Account", "date_opened": "09/06/2022", "status": "OPEN",
        "current_holdings": "3892.61",
    },
    "chk_4db6fc36e8_2": {
        "account_id": "chk_4db6fc36e8_2", "user_id": "4db6fc36e8", "class": "checking",
        "level": "Purple Account", "date_opened": "04/18/2024", "status": "OPEN",
        "current_holdings": "517.09",
    },
    # -- 601e851551 Bei Chen: the business customer.
    "chk_601e851551": {
        "account_id": "chk_601e851551", "user_id": "601e851551", "class": "checking",
        "level": "Evergreen Account", "date_opened": "01/23/2021", "status": "OPEN",
        "current_holdings": "5744.28",
    },
    "biz_chk_601e851551": {
        "account_id": "biz_chk_601e851551", "user_id": "601e851551", "class": "business_checking",
        "level": "Navy Blue", "date_opened": "02/09/2021", "status": "OPEN",
        "current_holdings": "18430.55",
    },
    # -- d97fb90fe8 Maria Elena Rodríguez: the churn customer.
    "chk_d97fb90fe8": {
        "account_id": "chk_d97fb90fe8", "user_id": "d97fb90fe8", "class": "checking",
        "level": "Light Green Account", "date_opened": "06/14/2023", "status": "OPEN",
        "current_holdings": "1962.83",
    },
    "biz_chk_d97fb90fe8": {
        "account_id": "biz_chk_d97fb90fe8", "user_id": "d97fb90fe8", "class": "business_checking",
        "level": "Cobalt Blue", "date_opened": "08/30/2023", "status": "OPEN",
        "current_holdings": "7215.40",
    },
}


# =====================================================================================================
# debit_cards
#
# Eight cards. Before this the build share had NONE, which is what made a freeze case unbuildable.
# Between them they cover both card statuses the full store uses (ACTIVE, PENDING) and all five of its
# issue reasons (new_account, expired, lost, damaged, stolen), plus FROZEN -- a status the full store
# happens not to hold but the schema documents, and the one an unfreeze case needs to start from.
#
# `cardholder_name` is the customer's own name uppercased, `last_4_digits` and `cvv` are invented and
# checked to be unique across the file. Card expiry is in the FUTURE by design (rule 5's exception).
# =====================================================================================================
DEBIT_CARDS = {
    # Mia opened her accounts six days ago: the card is issued but not yet activated.
    "dbc_123_blue": {
        "card_id": "dbc_123_blue", "account_id": "01", "user_id": "123",
        "cardholder_name": "MIA SMITH", "last_4_digits": "4407", "cvv": "319",
        "status": "PENDING", "issue_date": "11/08/2025", "expiration_date": "11/30/2029",
        "issue_reason": "new_account",
    },
    "dbc_126_green": {
        "card_id": "dbc_126_green", "account_id": "chk_126", "user_id": "126",
        "cardholder_name": "ALEX RIVIERA", "last_4_digits": "5192", "cvv": "418",
        "status": "ACTIVE", "issue_date": "03/12/2023", "expiration_date": "03/31/2027",
        "issue_reason": "new_account",
    },
    # Liu's story: the Light Blue card is frozen because he mislaid it, a replacement was ordered on
    # 11/12 and is not active yet, and his second account's card is unaffected. This is the shape the
    # agent's own generator had to invent a customer for.
    "dbc_3d91a520be_lightblue": {
        "card_id": "dbc_3d91a520be_lightblue", "account_id": "chk_3d91a520be",
        "user_id": "3d91a520be", "cardholder_name": "LIU XIAOMING",
        "last_4_digits": "3067", "cvv": "902", "status": "FROZEN",
        "issue_date": "07/19/2024", "expiration_date": "07/31/2028",
        "issue_reason": "new_account",
    },
    "dbc_3d91a520be_lightblue_2": {
        "card_id": "dbc_3d91a520be_lightblue_2", "account_id": "chk_3d91a520be",
        "user_id": "3d91a520be", "cardholder_name": "LIU XIAOMING",
        "last_4_digits": "8214", "cvv": "573", "status": "PENDING",
        "issue_date": "11/12/2025", "expiration_date": "11/30/2029",
        "issue_reason": "lost",
    },
    "dbc_3d91a520be_darkgreen": {
        "card_id": "dbc_3d91a520be_darkgreen", "account_id": "chk_3d91a520be_2",
        "user_id": "3d91a520be", "cardholder_name": "LIU XIAOMING",
        "last_4_digits": "7745", "cvv": "236", "status": "ACTIVE",
        "issue_date": "02/03/2025", "expiration_date": "02/28/2029",
        "issue_reason": "new_account",
    },
    # Emily's card was reissued when the previous one expired.
    "dbc_4db6fc36e8_silver": {
        "card_id": "dbc_4db6fc36e8_silver", "account_id": "chk_4db6fc36e8",
        "user_id": "4db6fc36e8", "cardholder_name": "EMILY THOMPSON",
        "last_4_digits": "6118", "cvv": "574", "status": "ACTIVE",
        "issue_date": "09/22/2025", "expiration_date": "09/30/2029",
        "issue_reason": "expired",
    },
    # Bei's was reissued after physical damage.
    "dbc_601e851551_evergreen": {
        "card_id": "dbc_601e851551_evergreen", "account_id": "chk_601e851551",
        "user_id": "601e851551", "cardholder_name": "BEI CHEN",
        "last_4_digits": "8830", "cvv": "147", "status": "ACTIVE",
        "issue_date": "06/30/2025", "expiration_date": "06/30/2029",
        "issue_reason": "damaged",
    },
    # Maria's was reissued after a theft in August.
    "dbc_d97fb90fe8_lightgreen": {
        "card_id": "dbc_d97fb90fe8_lightgreen", "account_id": "chk_d97fb90fe8",
        "user_id": "d97fb90fe8", "cardholder_name": "MARIA ELENA RODRIGUEZ",
        "last_4_digits": "2954", "cvv": "681", "status": "ACTIVE",
        "issue_date": "08/21/2025", "expiration_date": "08/31/2029",
        "issue_reason": "stolen",
    },
}


# =====================================================================================================
# credit_card_accounts
#
# Seven card accounts across five customers. Mia has none -- a six-day-old customer with no card is the
# thin file an "am I eligible" question needs. Two carry the optional `credit_limit` / `account_status` /
# `past_due_amount` fields the full store uses on some records and not others; that unevenness is copied
# deliberately, because an agent that assumes `credit_limit` is always present will break on the real
# store. Both limit values the full store holds appear here, including its inconsistent thousands
# formatting ("$5000.00" and "$15,000.00") -- copied rather than tidied, for the same reason.
# =====================================================================================================
CREDIT_CARD_ACCOUNTS = {
    "cc_126_silver": {
        "account_id": "cc_126_silver", "user_id": "126", "card_type": "Silver Rewards Card",
        "status": "ACTIVE", "date_of_account_open": "06/04/2023",
        "current_balance": "$1,284.53", "credit_limit": "$15,000.00",
        "reward_points": "9,410 points", "account_status": "ACTIVE",
        "past_due_amount": "$0.00",
    },
    # Closed when Alex consolidated cards -- the closure reason below points at this account.
    "cc_126_bronze": {
        "account_id": "cc_126_bronze", "user_id": "126", "card_type": "Bronze Rewards Card",
        "status": "CLOSED", "date_of_account_open": "08/22/2021", "closed_date": "07/30/2025",
        "current_balance": "$0.00", "reward_points": "0 points",
    },
    # Emily: at the $5,000 limit with a past-due balance, which is what makes her limit-increase
    # request deniable for a documented reason rather than arbitrarily.
    "cc_4db6fc36e8_gold": {
        "account_id": "cc_4db6fc36e8_gold", "user_id": "4db6fc36e8",
        "card_type": "Gold Rewards Card", "status": "ACTIVE",
        "date_of_account_open": "01/14/2021", "current_balance": "$2,047.88",
        "credit_limit": "$5000.00", "reward_points": "15,830 points",
        "account_status": "ACTIVE", "past_due_amount": "$78.00",
    },
    "cc_601e851551_bizsilver": {
        "account_id": "cc_601e851551_bizsilver", "user_id": "601e851551",
        "card_type": "Business Silver Rewards Card", "status": "ACTIVE",
        "date_of_account_open": "03/17/2021", "current_balance": "$3,662.41",
        "reward_points": "21,405 points",
    },
    "cc_601e851551_bizgold": {
        "account_id": "cc_601e851551_bizgold", "user_id": "601e851551",
        "card_type": "Business Gold Rewards Card", "status": "ACTIVE",
        "date_of_account_open": "05/06/2024", "current_balance": "$1,109.30",
        "reward_points": "4,860 points",
    },
    # Maria closed the EcoCard in October; the logged closure reason points at it.
    "cc_d97fb90fe8_eco": {
        "account_id": "cc_d97fb90fe8_eco", "user_id": "d97fb90fe8", "card_type": "EcoCard",
        "status": "CLOSED", "date_of_account_open": "05/09/2022", "closed_date": "10/28/2025",
        "current_balance": "$0.00", "reward_points": "0 points",
    },
    "cc_d97fb90fe8_crypto": {
        "account_id": "cc_d97fb90fe8_crypto", "user_id": "d97fb90fe8",
        "card_type": "Crypto-Cash Back", "status": "ACTIVE",
        "date_of_account_open": "11/02/2024", "current_balance": "$418.62",
        "reward_points": "1,204 points",
    },
}


# =====================================================================================================
# bank_account_transaction_history
#
# A recent window (10/01/2025 - 11/13/2025), covering all eleven transaction types the full store uses.
# `amount` is a FLOAT, positive for credits and negative for debits, matching the full store -- unlike
# every other monetary field in this schema, which is a formatted string. That inconsistency is real and
# is copied.
#
# MIA'S ROWS RECONCILE EXACTLY, and are the only ones that can: her accounts opened 11/08/2025, inside
# this window, so the sum of her transactions must equal the balance the carved account record states.
# 01 nets to 0.00 (a deposit and a transfer out of the same size); 02 holds a single 129.02 deposit. The
# generator checks both. The other accounts opened between 2021 and 2025, so their window is a slice of a
# longer history and no sum is claimed.
# =====================================================================================================
BANK_TRANSACTIONS = {
    # -- Mia Smith, account 01: nets to exactly 0, the balance the carved record states.
    "btxn_3f81c04a92de": {
        "transaction_id": "btxn_3f81c04a92de", "account_id": "01", "date": "11/10/2025",
        "description": "DIRECT DEPOSIT - NORTHGATE CONSULTING PAYROLL",
        "amount": 250.00, "type": "direct_deposit", "status": "posted",
    },
    "btxn_7d29b6e105fa": {
        "transaction_id": "btxn_7d29b6e105fa", "account_id": "01", "date": "11/12/2025",
        "description": "ACH TRANSFER OUT - RENT SHARE",
        "amount": -250.00, "type": "ach_transfer_out", "status": "posted",
    },
    # -- Mia Smith, account 02: a single opening deposit equal to the stated balance.
    "btxn_bc540e7a13d9": {
        "transaction_id": "btxn_bc540e7a13d9", "account_id": "02", "date": "11/08/2025",
        "description": "MOBILE DEPOSIT - CHECK 1042",
        "amount": 129.02, "type": "mobile_deposit", "status": "posted",
    },
    # -- Alex Riviera, chk_126: an ordinary month.
    "btxn_a05e9d31c748": {
        "transaction_id": "btxn_a05e9d31c748", "account_id": "chk_126", "date": "10/31/2025",
        "description": "DIRECT DEPOSIT - BEACON ANALYTICS PAYROLL",
        "amount": 3120.44, "type": "direct_deposit", "status": "posted",
    },
    "btxn_1e6f8b0a2c54": {
        "transaction_id": "btxn_1e6f8b0a2c54", "account_id": "chk_126", "date": "11/03/2025",
        "description": "BILL PAY - EVERSOURCE ENERGY",
        "amount": -142.18, "type": "bill_pay", "status": "posted",
    },
    "btxn_59c2a7f4be80": {
        "transaction_id": "btxn_59c2a7f4be80", "account_id": "chk_126", "date": "11/06/2025",
        "description": "DEBIT CARD PURCHASE - STAR MARKET BOSTON MA",
        "amount": -87.63, "type": "debit_card_purchase", "status": "posted",
    },
    "btxn_e740b19c26af": {
        "transaction_id": "btxn_e740b19c26af", "account_id": "chk_126", "date": "11/09/2025",
        "description": "EVERYONEPAY SENT - J. OKONKWO",
        "amount": -45.00, "type": "everyonepay", "status": "posted",
    },
    "btxn_2b8d5c03fa61": {
        "transaction_id": "btxn_2b8d5c03fa61", "account_id": "chk_126", "date": "11/11/2025",
        "description": "ATM WITHDRAWAL - RHO-BANK #1183 BOSTON MA",
        "amount": -120.00, "type": "atm_withdrawal", "status": "posted",
    },
    "btxn_9c31e6b8407d": {
        "transaction_id": "btxn_9c31e6b8407d", "account_id": "61f7a2c4d8e93016",
        "date": "11/01/2025", "description": "INTEREST CREDIT - SILVER SAVINGS",
        "amount": 4.61, "type": "interest_credit", "status": "posted",
    },
    # -- Liu Xiaoming, chk_3d91a520be: an out-of-network ATM fee that was later rebated, which is the
    #    fee-reversal shape a "why was I charged" case needs.
    "btxn_4a7e2c95d183": {
        "transaction_id": "btxn_4a7e2c95d183", "account_id": "chk_3d91a520be",
        "date": "10/24/2025", "description": "ATM WITHDRAWAL - 7-ELEVEN #2288 IRVINE CA",
        "amount": -80.00, "type": "atm_withdrawal", "status": "posted",
    },
    "btxn_d18b3f6ae052": {
        "transaction_id": "btxn_d18b3f6ae052", "account_id": "chk_3d91a520be",
        "date": "10/24/2025", "description": "OUT-OF-NETWORK ATM FEE",
        "amount": -3.50, "type": "atm_fee", "status": "posted",
    },
    "btxn_6e0c9a41b752": {
        "transaction_id": "btxn_6e0c9a41b752", "account_id": "chk_3d91a520be",
        "date": "10/28/2025", "description": "FEE REBATE - COURTESY REVERSAL ATM FEE",
        "amount": 3.50, "type": "fee_rebate", "status": "posted",
    },
    "btxn_f395c8206a41": {
        "transaction_id": "btxn_f395c8206a41", "account_id": "chk_3d91a520be",
        "date": "11/02/2025", "description": "MOBILE DEPOSIT - CHECK 3391",
        "amount": 640.00, "type": "mobile_deposit", "status": "posted",
    },
    "btxn_08c5b2e7f149": {
        "transaction_id": "btxn_08c5b2e7f149", "account_id": "chk_3d91a520be",
        "date": "11/07/2025", "description": "DEBIT CARD PURCHASE - TRADER JOES IRVINE CA",
        "amount": -64.19, "type": "debit_card_purchase", "status": "posted",
    },
    "btxn_5d9f1a86c374": {
        "transaction_id": "btxn_5d9f1a86c374", "account_id": "chk_3d91a520be_2",
        "date": "11/05/2025", "description": "WIRE TRANSFER OUT - TUITION PAYMENT UCI",
        "amount": -1500.00, "type": "wire_transfer_out", "status": "posted",
    },
    # -- Emily Thompson, chk_4db6fc36e8.
    "btxn_c62e08b4f9a1": {
        "transaction_id": "btxn_c62e08b4f9a1", "account_id": "chk_4db6fc36e8",
        "date": "10/31/2025", "description": "DIRECT DEPOSIT - CAROLINA HEALTH PAYROLL",
        "amount": 2840.19, "type": "direct_deposit", "status": "posted",
    },
    "btxn_71b3d5c9e802": {
        "transaction_id": "btxn_71b3d5c9e802", "account_id": "chk_4db6fc36e8",
        "date": "11/04/2025", "description": "MONTHLY MAINTENANCE FEE",
        "amount": -20.00, "type": "monthly_fee", "status": "posted",
    },
    "btxn_ae147b0293cd": {
        "transaction_id": "btxn_ae147b0293cd", "account_id": "chk_4db6fc36e8",
        "date": "11/10/2025", "description": "BILL PAY - DUKE ENERGY",
        "amount": -164.55, "type": "bill_pay", "status": "posted",
    },
    "btxn_b8027e5a3f16": {
        "transaction_id": "btxn_b8027e5a3f16", "account_id": "chk_4db6fc36e8_2",
        "date": "11/12/2025", "description": "ACH TRANSFER IN - SAVINGS SWEEP",
        "amount": 300.00, "type": "ach_transfer_in", "status": "posted",
    },
    # -- Bei Chen, personal and business.
    "btxn_3c96e1d47b58": {
        "transaction_id": "btxn_3c96e1d47b58", "account_id": "chk_601e851551",
        "date": "11/01/2025", "description": "DIRECT DEPOSIT - CHEN DESIGN STUDIO DRAW",
        "amount": 4200.00, "type": "direct_deposit", "status": "posted",
    },
    "btxn_0f52b8a6c913": {
        "transaction_id": "btxn_0f52b8a6c913", "account_id": "chk_601e851551",
        "date": "11/08/2025", "description": "ATM WITHDRAWAL - RHO-BANK #0442 CUPERTINO CA",
        "amount": -200.00, "type": "atm_withdrawal", "status": "posted",
    },
    "btxn_82e4c07b1d95": {
        "transaction_id": "btxn_82e4c07b1d95", "account_id": "biz_chk_601e851551",
        "date": "11/03/2025", "description": "ACH TRANSFER IN - CLIENT INVOICE 20411",
        "amount": 9800.00, "type": "ach_transfer_in", "status": "posted",
    },
    "btxn_d5a71c9e6403": {
        "transaction_id": "btxn_d5a71c9e6403", "account_id": "biz_chk_601e851551",
        "date": "11/06/2025", "description": "ACH TRANSFER OUT - CONTRACTOR PAYMENT",
        "amount": -3400.00, "type": "ach_transfer_out", "status": "posted",
    },
    "btxn_1a4f8e026b7d": {
        "transaction_id": "btxn_1a4f8e026b7d", "account_id": "biz_chk_601e851551",
        "date": "11/13/2025", "description": "WIRE TRANSFER IN - RETAINER GRANDVIEW LLC",
        "amount": 5000.00, "type": "wire_transfer_in", "status": "pending",
    },
    # -- Maria Elena Rodríguez.
    "btxn_ec3806a1b5f2": {
        "transaction_id": "btxn_ec3806a1b5f2", "account_id": "chk_d97fb90fe8",
        "date": "10/30/2025", "description": "DIRECT DEPOSIT - MISSION CREATIVE PAYROLL",
        "amount": 2610.00, "type": "direct_deposit", "status": "posted",
    },
    "btxn_47b9d2f8c063": {
        "transaction_id": "btxn_47b9d2f8c063", "account_id": "chk_d97fb90fe8",
        "date": "11/05/2025", "description": "EVERYONEPAY RECEIVED - R. ESPINOZA",
        "amount": 75.00, "type": "everyonepay", "status": "posted",
    },
    "btxn_9e58c1740ab3": {
        "transaction_id": "btxn_9e58c1740ab3", "account_id": "chk_d97fb90fe8",
        "date": "11/09/2025", "description": "CHECK DEPOSIT - CHECK 8820",
        "amount": 410.00, "type": "check_deposit", "status": "posted",
    },
    "btxn_2d70f6b39e14": {
        "transaction_id": "btxn_2d70f6b39e14", "account_id": "biz_chk_d97fb90fe8",
        "date": "11/07/2025", "description": "DEBIT CARD PURCHASE - ADOBE SYSTEMS",
        "amount": -59.99, "type": "debit_card_purchase", "status": "posted",
    },
    "btxn_5f1c8b40d267": {
        "transaction_id": "btxn_5f1c8b40d267", "account_id": "biz_chk_d97fb90fe8",
        "date": "11/11/2025", "description": "OVERDRAFT PROTECTION TRANSFER FEE",
        "amount": -12.50, "type": "overdraft_fee", "status": "posted",
    },
}


# =====================================================================================================
# credit_card_transaction_history
#
# `transaction_amount` is a FORMATTED STRING here ("$156.78"), unlike the bank-transaction float above.
# Covers seven of the eleven card types the full store uses; the remaining four are premium and business
# tiers none of these six customers plausibly holds, and stay in the spec's coverage waiver rather than
# being padded in.
#
# EMILY'S DUPLICATE PAIR is the point of this collection: two identical charges at the same merchant on
# the same day, one of which her dispute below contests. A duplicate-charge case cannot be built without
# a real duplicate to point at.
# =====================================================================================================
CREDIT_CARD_TRANSACTIONS = {
    # -- Alex Riviera, Silver Rewards Card.
    "txn_6b19e5c04a7d": {
        "transaction_id": "txn_6b19e5c04a7d", "user_id": "126",
        "credit_card_type": "Silver Rewards Card", "merchant_name": "Legal Sea Foods",
        "transaction_amount": "$118.40", "transaction_date": "11/02/2025",
        "category": "Dining", "status": "COMPLETED", "rewards_earned": "296 points",
    },
    "txn_c40a72e8d95b": {
        "transaction_id": "txn_c40a72e8d95b", "user_id": "126",
        "credit_card_type": "Silver Rewards Card", "merchant_name": "Delta Air Lines",
        "transaction_amount": "$412.00", "transaction_date": "11/04/2025",
        "category": "Travel", "status": "COMPLETED", "rewards_earned": "1030 points",
    },
    "txn_e85d13b7f0a2": {
        "transaction_id": "txn_e85d13b7f0a2", "user_id": "126",
        "credit_card_type": "Silver Rewards Card", "merchant_name": "Star Market",
        "transaction_amount": "$96.22", "transaction_date": "11/08/2025",
        "category": "Groceries", "status": "COMPLETED", "rewards_earned": "240 points",
    },
    "txn_1f7c9a24e63b": {
        "transaction_id": "txn_1f7c9a24e63b", "user_id": "126",
        "credit_card_type": "Silver Rewards Card", "merchant_name": "Apple",
        "transaction_amount": "$249.00", "transaction_date": "11/11/2025",
        "category": "Electronics", "status": "PENDING", "rewards_earned": "622 points",
    },
    # A refunded charge on the closed Bronze card, before it was closed.
    "txn_a29e04c8b571": {
        "transaction_id": "txn_a29e04c8b571", "user_id": "126",
        "credit_card_type": "Bronze Rewards Card", "merchant_name": "Wayfair",
        "transaction_amount": "$310.75", "transaction_date": "07/12/2025",
        "category": "Shopping", "status": "REFUNDED", "rewards_earned": "0 points",
    },
    # -- Emily Thompson, Gold Rewards Card. The first two are the duplicate pair.
    "txn_4c8e1b7d9a20": {
        "transaction_id": "txn_4c8e1b7d9a20", "user_id": "4db6fc36e8",
        "credit_card_type": "Gold Rewards Card", "merchant_name": "Blue Ridge Outfitters",
        "transaction_amount": "$189.99", "transaction_date": "11/05/2025",
        "category": "Shopping", "status": "COMPLETED", "rewards_earned": "475 points",
    },
    "txn_9f2d6a1c4b83": {
        "transaction_id": "txn_9f2d6a1c4b83", "user_id": "4db6fc36e8",
        "credit_card_type": "Gold Rewards Card", "merchant_name": "Blue Ridge Outfitters",
        "transaction_amount": "$189.99", "transaction_date": "11/05/2025",
        "category": "Shopping", "status": "COMPLETED", "rewards_earned": "475 points",
    },
    "txn_70b3e9c15d48": {
        "transaction_id": "txn_70b3e9c15d48", "user_id": "4db6fc36e8",
        "credit_card_type": "Gold Rewards Card", "merchant_name": "Harris Teeter",
        "transaction_amount": "$143.87", "transaction_date": "11/01/2025",
        "category": "Groceries", "status": "COMPLETED", "rewards_earned": "359 points",
    },
    "txn_b5107e2a8c96": {
        "transaction_id": "txn_b5107e2a8c96", "user_id": "4db6fc36e8",
        "credit_card_type": "Gold Rewards Card", "merchant_name": "Duke Energy",
        "transaction_amount": "$164.55", "transaction_date": "11/10/2025",
        "category": "Utilities", "status": "COMPLETED", "rewards_earned": "411 points",
    },
    "txn_08d4c7b1e359": {
        "transaction_id": "txn_08d4c7b1e359", "user_id": "4db6fc36e8",
        "credit_card_type": "Gold Rewards Card", "merchant_name": "Regal Cinemas",
        "transaction_amount": "$34.50", "transaction_date": "11/09/2025",
        "category": "Entertainment", "status": "DECLINED", "rewards_earned": "0 points",
    },
    # -- Bei Chen, business cards.
    "txn_2e7a4d09b6f1": {
        "transaction_id": "txn_2e7a4d09b6f1", "user_id": "601e851551",
        "credit_card_type": "Business Silver Rewards Card", "merchant_name": "Amazon Web Services",
        "transaction_amount": "$742.16", "transaction_date": "11/01/2025",
        "category": "Software", "status": "COMPLETED", "rewards_earned": "1855 points",
    },
    "txn_c193b8e50a76": {
        "transaction_id": "txn_c193b8e50a76", "user_id": "601e851551",
        "credit_card_type": "Business Silver Rewards Card", "merchant_name": "Staples",
        "transaction_amount": "$218.94", "transaction_date": "11/06/2025",
        "category": "Operations", "status": "COMPLETED", "rewards_earned": "547 points",
    },
    "txn_5a06f2c8d413": {
        "transaction_id": "txn_5a06f2c8d413", "user_id": "601e851551",
        "credit_card_type": "Business Gold Rewards Card", "merchant_name": "FedEx",
        "transaction_amount": "$88.20", "transaction_date": "11/07/2025",
        "category": "Shipping", "status": "COMPLETED", "rewards_earned": "220 points",
    },
    "txn_f28c6b0794ad": {
        "transaction_id": "txn_f28c6b0794ad", "user_id": "601e851551",
        "credit_card_type": "Business Gold Rewards Card", "merchant_name": "United Airlines",
        "transaction_amount": "$1,043.60", "transaction_date": "10/29/2025",
        "category": "Travel", "status": "COMPLETED", "rewards_earned": "2609 points",
    },
    # -- Maria Elena Rodríguez: EcoCard (before closure) and the active Crypto-Cash Back card.
    "txn_9d3b17e6c825": {
        "transaction_id": "txn_9d3b17e6c825", "user_id": "d97fb90fe8",
        "credit_card_type": "EcoCard", "merchant_name": "Rainforest Alliance",
        "transaction_amount": "$60.00", "transaction_date": "10/14/2025",
        "category": "Green", "status": "COMPLETED", "rewards_earned": "150 points",
    },
    "txn_37e0a9c142db": {
        "transaction_id": "txn_37e0a9c142db", "user_id": "d97fb90fe8",
        "credit_card_type": "EcoCard", "merchant_name": "Bay Area Bike Share",
        "transaction_amount": "$29.00", "transaction_date": "10/20/2025",
        "category": "Transportation", "status": "COMPLETED", "rewards_earned": "72 points",
    },
    "txn_6c1e58b03fa9": {
        "transaction_id": "txn_6c1e58b03fa9", "user_id": "d97fb90fe8",
        "credit_card_type": "Crypto-Cash Back", "merchant_name": "Philz Coffee",
        "transaction_amount": "$18.75", "transaction_date": "11/10/2025",
        "category": "Dining", "status": "COMPLETED", "rewards_earned": "$0.37 cash back",
    },
    "txn_04a7c9e2b168": {
        "transaction_id": "txn_04a7c9e2b168", "user_id": "d97fb90fe8",
        "credit_card_type": "Crypto-Cash Back", "merchant_name": "Netflix",
        "transaction_amount": "$22.99", "transaction_date": "11/12/2025",
        "category": "Media", "status": "COMPLETED", "rewards_earned": "$0.46 cash back",
    },
}


# =====================================================================================================
# payment_history
#
# Keyed `pay_<user_id>_<card abbreviation>_<YYYYMM>`, the full store's own convention. Nine months across
# two cardholders. Alex is clean; Emily runs a late month then a missed one, which is what leaves her
# card past due by $78.00 and gives a limit-increase refusal a real reason to cite. The full store holds
# only ON_TIME rows, so LATE and MISSED are shapes the agent could not otherwise have seen.
# =====================================================================================================
PAYMENT_HISTORY = {
    "pay_126_silver_202508": {
        "payment_id": "pay_126_silver_202508", "credit_card_account_id": "cc_126_silver",
        "user_id": "126", "payment_date": "08/15/2025", "amount": "$620.00", "status": "ON_TIME",
    },
    "pay_126_silver_202509": {
        "payment_id": "pay_126_silver_202509", "credit_card_account_id": "cc_126_silver",
        "user_id": "126", "payment_date": "09/15/2025", "amount": "$540.00", "status": "ON_TIME",
    },
    # Alex's card bills on the 15th. There is no November row for the same reason Emily has none: on
    # 11/14 that payment is not yet due, and a fixture that records it would be dating itself forward.
    "pay_126_silver_202510": {
        "payment_id": "pay_126_silver_202510", "credit_card_account_id": "cc_126_silver",
        "user_id": "126", "payment_date": "10/15/2025", "amount": "$710.00", "status": "ON_TIME",
    },
    "pay_4db6fc36e8_gold_202508": {
        "payment_id": "pay_4db6fc36e8_gold_202508",
        "credit_card_account_id": "cc_4db6fc36e8_gold", "user_id": "4db6fc36e8",
        "payment_date": "08/20/2025", "amount": "$310.00", "status": "ON_TIME",
    },
    "pay_4db6fc36e8_gold_202509": {
        "payment_id": "pay_4db6fc36e8_gold_202509",
        "credit_card_account_id": "cc_4db6fc36e8_gold", "user_id": "4db6fc36e8",
        "payment_date": "09/28/2025", "amount": "$295.00", "status": "LATE",
    },
    # Due 10/20 and never paid, which is what leaves the card $78.00 past due as of the frozen clock.
    # There is deliberately no November row: on 11/14 that payment is not due yet, so a MISSED or LATE
    # row for it would be a claim about the future.
    "pay_4db6fc36e8_gold_202510": {
        "payment_id": "pay_4db6fc36e8_gold_202510",
        "credit_card_account_id": "cc_4db6fc36e8_gold", "user_id": "4db6fc36e8",
        "payment_date": "10/20/2025", "amount": "$0.00", "status": "MISSED",
    },
    "pay_601e851551_bizsilver_202510": {
        "payment_id": "pay_601e851551_bizsilver_202510",
        "credit_card_account_id": "cc_601e851551_bizsilver", "user_id": "601e851551",
        "payment_date": "10/18/2025", "amount": "$1,200.00", "status": "ON_TIME",
    },
}


# =====================================================================================================
# referrals, verification_history, applications, disputes, orders, flags, limit-increase requests
#
# The tail collections. Small on purpose -- one or two records each, enough for the agent to read the
# SHAPE of a record the specialist tools create, which is what it needs before it can judge whether its
# own artifact created one correctly.
#
# The four collections that are empty in the full store as well (`cash_back_disputes`,
# `credit_limit_increase_requests`, `credit_card_account_flags`, `debit_card_orders`) get records here
# anyway. They are empty upstream because no eval case STARTS with one pending, not because the schema is
# unused -- each is written at run time by a specialist tool. A past example is the cheapest way for the
# agent to learn the shape it will have to produce.
# =====================================================================================================
REFERRALS = {
    "8c1d5e9f2a7b4306": {
        "referral_id": "8c1d5e9f2a7b4306", "referrer_id": "126",
        "referred_account_type": "Gold Rewards Card", "referral_status": "COMPLETE",
        "date": "09/28/2025",
    },
    "2f6a9d1c8b405e73": {
        "referral_id": "2f6a9d1c8b405e73", "referrer_id": "d97fb90fe8",
        "referred_account_type": "Silver Rewards Card", "referral_status": "IN_PROGRESS",
        "date": "10/30/2025",
    },
    "7b3e5c9a1d264f80": {
        "referral_id": "7b3e5c9a1d264f80", "referrer_id": "d97fb90fe8",
        "referred_account_type": "Bronze Rewards Card", "referral_status": "REJECTED",
        "date": "09/12/2025",
    },
    "c58f0a3e7b9d1246": {
        "referral_id": "c58f0a3e7b9d1246", "referrer_id": "d97fb90fe8",
        "referred_account_type": "Platinum Rewards Card", "referral_status": "ERROR",
        "date": "08/19/2025",
    },
    "e1d47b8c2f6a5930": {
        "referral_id": "e1d47b8c2f6a5930", "referrer_id": "601e851551",
        "referred_account_type": "Gold Rewards Card", "referral_status": "NO_PROGRESS",
        "date": "11/07/2025",
    },
    "b04a6e19c7d3f582": {
        "referral_id": "b04a6e19c7d3f582", "referrer_id": "4db6fc36e8",
        "referred_account_type": "Silver Rewards Card", "referral_status": "APPLIED",
        "date": "11/03/2025",
    },
}

# Keyed `<user_id>_<YYYYMMDD>`, the full store's convention. This is the record the agent WRITES after a
# successful identity check, so seeing three real ones is how it learns what its own write must contain.
# Every field is copied from the customer's carved row; the generator checks that it agrees.
VERIFICATION_HISTORY = {
    "126_20251109": {
        "name": "Alex Riviera", "user_id": "126", "address": _ADDRESS["126"],
        "email": _EMAIL["126"], "phone_number": _PHONE["126"], "date_of_birth": _DOB["126"],
        "time_verified": "2025-11-09 10:41:00 EST",
    },
    "d97fb90fe8_20251111": {
        "name": "Maria Elena Rodríguez", "user_id": "d97fb90fe8",
        "address": _ADDRESS["d97fb90fe8"], "email": _EMAIL["d97fb90fe8"],
        "phone_number": _PHONE["d97fb90fe8"], "date_of_birth": _DOB["d97fb90fe8"],
        "time_verified": "2025-11-11 16:07:00 EST",
    },
    "601e851551_20251113": {
        "name": "Bei Chen", "user_id": "601e851551", "address": _ADDRESS["601e851551"],
        "email": _EMAIL["601e851551"], "phone_number": _PHONE["601e851551"],
        "date_of_birth": _DOB["601e851551"], "time_verified": "2025-11-13 08:52:00 EST",
    },
}

# 16-character hex, the full store's convention. Maria has one decision pending.
CREDIT_CARD_APPLICATIONS = {
    "4e7b1a9c52d8f603": {
        "application_id": "4e7b1a9c52d8f603", "card_type": "Platinum Rewards Card",
        "customer_name": "Maria Elena Rodríguez", "annual_income": 98000.0,
        "rho_bank_subscription": False, "status": "PENDING", "date": "11/11/2025",
    },
}

# Emily's dispute of the second of the two identical Blue Ridge Outfitters charges. Contact fields are
# her carved customer row's, and `card_last_4_digits` is her debit card's -- the full store does the same
# thing, which is itself worth the agent seeing.
TRANSACTION_DISPUTES = {
    "dsp_5b9e2c7a4f18": {
        "dispute_id": "dsp_5b9e2c7a4f18", "transaction_id": "txn_9f2d6a1c4b83",
        "user_id": "4db6fc36e8", "card_action": "keep_active", "card_last_4_digits": "6118",
        "full_name": "Emily Thompson", "phone": _PHONE["4db6fc36e8"],
        "email": _EMAIL["4db6fc36e8"], "address": _ADDRESS["4db6fc36e8"],
        "contacted_merchant": True, "purchase_date": "11/05/2025",
        "issue_noticed_date": "11/07/2025", "dispute_reason": "duplicate_charge",
        "resolution_requested": "full_refund", "partial_refund_amount": None,
        "eligible_for_provisional_credit": True, "provisional_credit_given": True,
        "submitted_at": "11/08/2025", "status": "UNDER_REVIEW",
    },
}

# A cash-back dispute on Maria's Crypto-Cash Back card -- the collection the specialist tool
# `submit_cash_back_dispute_0589` writes.
CASH_BACK_DISPUTES = {
    "dsp_a41c8e60b937": {
        "dispute_id": "dsp_a41c8e60b937", "user_id": "d97fb90fe8",
        "transaction_id": "txn_04a7c9e2b168", "submitted_at": "11/13/2025",
        "status": "SUBMITTED",
    },
}

# Maria closed the EcoCard; the reason was logged two days before the closure took effect.
CREDIT_CARD_CLOSURE_REASONS = {
    "clsr_d97fb90fe8_eco_001": {
        "record_id": "clsr_d97fb90fe8_eco_001",
        "credit_card_account_id": "cc_d97fb90fe8_eco", "user_id": "d97fb90fe8",
        "closure_reason": "not_using_card", "logged_at": "10/26/2025", "status": "LOGGED",
    },
    "clsr_126_bronze_001": {
        "record_id": "clsr_126_bronze_001", "credit_card_account_id": "cc_126_bronze",
        "user_id": "126", "closure_reason": "simplifying_finances",
        "logged_at": "07/28/2025", "status": "LOGGED",
    },
}

# Replacement card orders: Maria's mislaid Crypto card (expedited) and Emily's damaged Gold card.
CREDIT_CARD_ORDERS = {
    "ccord_d97fb90fe8_crypto_001": {
        "order_id": "ccord_d97fb90fe8_crypto_001",
        "credit_card_account_id": "cc_d97fb90fe8_crypto", "user_id": "d97fb90fe8",
        "shipping_address": _ADDRESS["d97fb90fe8"], "reason": "lost",
        "expedited_shipping": True, "order_date": "11/09/2025",
        "expected_delivery": "11/13/2025", "status": "SHIPPED",
    },
    "ccord_4db6fc36e8_gold_001": {
        "order_id": "ccord_4db6fc36e8_gold_001",
        "credit_card_account_id": "cc_4db6fc36e8_gold", "user_id": "4db6fc36e8",
        "shipping_address": _ADDRESS["4db6fc36e8"], "reason": "damaged",
        "expedited_shipping": False, "order_date": "10/12/2025",
        "expected_delivery": "10/22/2025", "status": "DELIVERED",
    },
}

# Liu's replacement debit card order, matching the PENDING card above.
DEBIT_CARD_ORDERS = {
    "dcord_3d91a520be_001": {
        "order_id": "dcord_3d91a520be_001", "account_id": "chk_3d91a520be",
        "user_id": "3d91a520be", "delivery_option": "STANDARD", "card_design": "CLASSIC",
        "shipping_address": _ADDRESS["3d91a520be"], "delivery_fee": "$0",
        "design_fee": "$0", "total_fee": "$0", "order_date": "11/12/2025",
        "expected_delivery": "7-10 business days", "status": "PROCESSING",
    },
}

# One approved and one denied limit increase. The denial cites Emily's past-due balance, which her
# payment history and her card's `past_due_amount` both independently support -- so an agent reasoning
# about eligibility from the data reaches the same answer the record states.
CREDIT_LIMIT_INCREASE_REQUESTS = {
    "cli_601e851551_bizsilver_001": {
        "request_id": "cli_601e851551_bizsilver_001",
        "credit_card_account_id": "cc_601e851551_bizsilver", "user_id": "601e851551",
        "requested_increase_amount": "$5,000.00", "submitted_date": "10/02/2025",
        "decision_date": "10/04/2025", "previous_limit": "$15,000.00",
        "new_limit": "$20,000.00", "increase_amount": "$5,000.00", "status": "APPROVED",
    },
    "cli_4db6fc36e8_gold_001": {
        "request_id": "cli_4db6fc36e8_gold_001",
        "credit_card_account_id": "cc_4db6fc36e8_gold", "user_id": "4db6fc36e8",
        "requested_increase_amount": "$2,500.00", "submitted_date": "11/06/2025",
        "decision_date": "11/07/2025", "denial_reason": "past_due_balance",
        "status": "DENIED",
    },
}

# A retention offer that waived Bei's business card annual fee for one year.
CREDIT_CARD_ACCOUNT_FLAGS = {
    "ccflag_601e851551_bizsilver_001": {
        "flag_id": "ccflag_601e851551_bizsilver_001",
        "credit_card_account_id": "cc_601e851551_bizsilver", "user_id": "601e851551",
        "flag_type": "annual_fee_waived", "effective_date": "04/01/2025",
        "expiration_date": "03/31/2026", "reason": "retention_offer",
        "applied_at": "03/28/2025", "status": "ACTIVE",
    },
}


#: collection -> {record key -> record}. The one thing the generator reads from this module.
SYNTHETIC: dict[str, dict[str, dict]] = {
    "accounts": ACCOUNTS,
    "bank_account_transaction_history": BANK_TRANSACTIONS,
    "cash_back_disputes": CASH_BACK_DISPUTES,
    "credit_card_account_flags": CREDIT_CARD_ACCOUNT_FLAGS,
    "credit_card_accounts": CREDIT_CARD_ACCOUNTS,
    "credit_card_applications": CREDIT_CARD_APPLICATIONS,
    "credit_card_closure_reasons": CREDIT_CARD_CLOSURE_REASONS,
    "credit_card_orders": CREDIT_CARD_ORDERS,
    "credit_card_transaction_history": CREDIT_CARD_TRANSACTIONS,
    "credit_limit_increase_requests": CREDIT_LIMIT_INCREASE_REQUESTS,
    "debit_card_orders": DEBIT_CARD_ORDERS,
    "debit_cards": DEBIT_CARDS,
    "payment_history": PAYMENT_HISTORY,
    "referrals": REFERRALS,
    "transaction_disputes": TRANSACTION_DISPUTES,
    "verification_history": VERIFICATION_HISTORY,
}

#: The accounts whose transaction window covers their whole life, so their rows MUST sum to the balance
#: the carved account record states. Checked by the generator; see the module docstring.
RECONCILING_ACCOUNTS = {"01": "0", "02": "129.02"}
