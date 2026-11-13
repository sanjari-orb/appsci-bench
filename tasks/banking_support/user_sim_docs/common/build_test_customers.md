# BUILD-phase customers for internal tests

These six customers exist in the BUILD task environment. If the team building the
agent asks for real customers to use in internal tests, give them the relevant
names and user IDs below (or the full list if requested). These are BUILD fixtures
and are not targets of the held-out evaluation cases. The team should use the
banking read tools to inspect current account and transaction records before
writing a test. The hints below are starting points, not a substitute for those
records. Do not invent verification fields or additional customer identities.

| Customer | User ID | Scenario hint |
| --- | --- | --- |
| Mia Smith | `123` | Recently opened accounts, a debit card awaiting activation, and a savings deposit to reconcile. |
| Alex Riviera | `126` | An active card, clean payment history, a completed referral, and a closed card. |
| Liu Xiaoming | `3d91a520be` | A frozen card, a replacement order in progress, and an ATM fee with a courtesy rebate. |
| Emily Thompson | `4db6fc36e8` | A duplicate charge and open dispute, missed payments, and a denied limit increase. |
| Bei Chen | `601e851551` | Business checking and cards, an approved limit increase, and an annual-fee waiver. |
| Maria Elena Rodríguez | `d97fb90fe8` | A closed card, replacement order, cash-back dispute, pending application, and referrals in several states. |
