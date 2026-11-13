## Manual integration results

Fill one block per manual run. Work as the customer through the sim web app
(`python -m sim.app`), then use the record inspector to compare before/after state. Judge on
end-state **records**, not the wording of the reply (V-1).

For each run: read the customer's live records first (V-3), build a realistic request from that
state (V-5), use only real verification values (V-4), then check the end state equals the required
actions and **nothing extra** (V-2, R-18).

### Run template (copy per scenario)

- **Customer / user_id:**
- **Records before (key facts):**
- **Request posed (as the customer):**
- **Verification fields used (two, from the record):**
- **KB procedure found (doc id / action tool it names):**
- **Records after:**
- **Required actions (from the procedure):**
- **Match?** required present · nothing extra · named lookups performed  → PASS / FAIL
- **Notes / findings:**

### Fixtures (starting points — inspect live records first)

| Customer | user_id | Hint |
|---|---|---|
| Mia Smith | 123 | New accounts; debit card awaiting activation; savings deposit to reconcile |
| Alex Riviera | 126 | Active card; clean history; completed referral; closed card |
| Liu Xiaoming | 3d91a520be | Frozen card; replacement in progress; ATM fee w/ courtesy rebate |
| Emily Thompson | 4db6fc36e8 | Duplicate charge + open dispute; missed payments; denied limit increase |
| Bei Chen | 601e851551 | Business checking + cards; approved limit increase; annual-fee waiver |
| Maria Elena Rodríguez | d97fb90fe8 | Closed card; replacement; cash-back dispute; pending application; referrals |
