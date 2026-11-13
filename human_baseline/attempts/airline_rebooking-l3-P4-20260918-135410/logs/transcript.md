
## Elicitation session — task=airline_rebooking persona=l3 — started 2026-09-18T12:21:49Z

Driven by Claude Code via the user-sim HTTP service (/ask); see scratch/sim_serve.py for why the
interactive REPL was not used. System prompt deliberately not rendered or read.

### turn 1  (2026-09-18T12:21:55+00:00)

**asked:** I'm the engineer building the conversational airline agent for you. Before I design anything: what does a good outcome look like from your side, and what would you count as a failure?

**sim:** Good outcome: the customer's request gets resolved correctly end to end — booked, changed, cancelled, or refunded — and the reservation records are left in the right state afterward. And crucially, we never do anything the airline doesn't allow. If a request is out of policy, the agent says no or hands it to a human. On top of that, whatever we tell the customer — allowances, prices, eligibility, totals — has to be accurate.

Failure is the flip side: breaking policy (doing something we're not allowed to do), leaving records in a wrong state, or telling the customer something false. A good human agent knows when to say no; a bad one either breaks the rules or gives wrong answers. That's what we're trying to avoid.

### turn 2  (2026-09-18T12:21:59+00:00)

**asked:** I have no way to list users or reservations -- every read in the tool set takes an id I already hold, so I can't discover one by querying. Can you give me specific user ids and reservation ids in the live system that I can develop and test against?

**sim:** Right, you can't browse the store — you have to already know an id. For the practice environment, here are the test customers you can develop against: nora_castillo_9412, owen_delgado_9137, priya_venkat_9268, quinn_okafor_9503, rafael_moreau_9821, and TRN001. Looking up a customer shows you their bookings, so pull the reservation ids from there once you have the user.

### turn 3  (2026-09-18T12:22:06+00:00)

**asked:** There are 14 tools the agent can call, but I have no reference for them. Can you give me the exact tool names and the arguments each one takes, or tell me who holds that reference?

**sim:** I have that. Here they are with their arguments:

**Writes:**
- `book_reservation` — user_id, origin, destination, flight_type (one_way|round_trip), cabin (basic_economy|economy|business), flights (array of {flight_number, date}), passengers (array of {first_name, last_name, dob}), payment_methods (array of {payment_id, amount}), total_baggages, nonfree_baggages, insurance (yes|no)
- `update_reservation_flights` — reservation_id, cabin, flights (array of {flight_number, date}), payment_id
- `update_reservation_baggages` — reservation_id, total_baggages, nonfree_baggages, payment_id
- `update_reservation_passengers` — reservation_id, passengers (array of {first_name, last_name, dob})
- `cancel_reservation` — reservation_id
- `send_certificate` — user_id, amount

**Reads:**
- `get_user_details` — user_id
- `get_reservation_details` — reservation_id
- `get_flight_status` — flight_number, date
- `search_direct_flight` — origin, destination, date
- `search_onestop_flight` — origin, destination, date
- `list_all_airports` — (no arguments)

**Generic:**
- `calculate` — expression
- `transfer_to_human_agents` — summary

dates are YYYY-MM-DD. Note the tools don't enforce any policy — that's on your agent to check before calling the writes.

### turn 4  (2026-09-18T12:22:16+00:00)

**asked:** Is there a written policy covering booking, changes, cancellations, refunds and compensation? If there is, can you start walking me through the rules for changing or cancelling a booking?

**sim:** Yes, there's a policy and I know it well — ask me and I'll answer. Let me walk you through modify and cancel.

**Modify.** You need the user id and the reservation id first. The user provides their user id; if they don't know the reservation id, help them find it with the tools.
- Changing flights: basic economy flights can't be modified at all. Other reservations can change flights but you can't change the origin, destination, or trip type. You can keep some segments, but kept segments don't get repriced to current price. The API won't check any of this — you must enforce it before calling.
- Changing cabin: not allowed if any flight in the reservation has already been flown. Otherwise any reservation, including basic economy, can change cabin, without changing the flights. Cabin must stay the same across all segments — you can't change cabin on just one segment. If the new price is higher the user pays the difference; if lower they're refunded the difference.
- Baggage/insurance: can add checked bags, can't remove them. Cannot add insurance after the initial booking.
- Passengers: can modify passenger details but not the number of passengers — even a human agent can't change the count.
- Payment on flight changes: user provides a single gift card or credit card for the payment or refund, and it must already be on their profile.

**Cancel.** Again, get user id and reservation id first, plus the reason (change of plan, airline cancelled flight, or other reasons).
- If any portion of the flight has already been flown, you can't help — transfer to a human.
- Otherwise you can cancel only if at least one of these is true: booking was made within the last 24 hours; the flight was cancelled by the airline; it's a business flight; or the user has travel insurance and the cancellation reason is covered by insurance.
- The API doesn't check these — you enforce them.
- Refund goes back to the original payment methods within 5 to 7 business days.

Want me to go through booking or the compensation rules next?

### turn 5  (2026-09-18T12:23:21+00:00)

**asked:** Yes please -- walk me through the booking rules: what I need to collect, how flights are searched and priced, what payment methods are acceptable and how many, and the rules for baggage and insurance at booking time.

**sim:** Booking, in order:

**Get the user id first.** Then ask trip type, origin, destination.

**Flights/cabin.** Search with search_direct_flight or search_onestop_flight (origin, destination, date). A flight date can only be booked if its status is **available** — delayed, on time, and flying can't be booked. Cabin must be the same across all flights in the reservation. Cabin classes are basic economy, economy, business — and note basic economy is its own class, completely separate from economy.

**Passengers.** At most five per reservation. Collect first name, last name, and date of birth for each. All passengers fly the same flights in the same cabin.

**Payment.** All methods must already be on the user's profile — no new ones, for safety. Per reservation you can use at most one travel certificate, at most one credit card, and at most three gift cards. The remaining amount on a travel certificate is not refundable.

**Baggage.** Free checked-bag allowance depends on the booking user's membership level and each passenger's cabin:
- Regular: 0 for basic economy, 1 for economy, 2 for business (per passenger)
- Silver: 1 for basic economy, 2 for economy, 3 for business
- Gold: 2 for basic economy, 3 for economy, 4 for business
Each extra bag beyond the free allowance is $50. Don't add bags the user doesn't need.

**Insurance.** Ask if they want it. It's $30 per passenger, and it enables a full refund if they later cancel for health or weather reasons.

And before you actually book — or any write — lay out the details and get an explicit "yes" from the user first.

### turn 6  (2026-09-18T12:23:26+00:00)

**asked:** Now the compensation rules: when is a customer entitled to compensation, how much, in what form, and what must be true before I issue it?

**sim:** Compensation — handle this carefully:

**Never offer it proactively.** Only if the customer explicitly asks. And always confirm the facts before offering anything.

**Who's eligible at all:** only if the user is silver/gold, or has travel insurance, or flies business. Flip side: do NOT compensate if the user is a regular member with no travel insurance flying (basic) economy.

**The only two reasons you may compensate:**
- Cancelled flights in a reservation: after confirming the facts, offer a certificate of **$100 times the number of passengers**.
- Delayed flights, where the customer wants to change or cancel the reservation: after confirming the facts and actually making that change/cancellation, offer a certificate of **$50 times the number of passengers**.

Form is always a certificate (that's send_certificate with user_id and amount). No compensation for any reason other than those two.

### turn 7  (2026-09-18T12:23:29+00:00)

**asked:** What is the free checked-bag allowance, broken down by membership tier and cabin, and what does each extra bag cost?

**sim:** Free checked bags per passenger, by the booking user's membership and the passenger's cabin:

- **Regular:** basic economy 0, economy 1, business 2
- **Silver:** basic economy 1, economy 2, business 3
- **Gold:** basic economy 2, economy 3, business 4

Each extra bag beyond the free allowance is $50. And don't add bags the user doesn't need.

### turn 8  (2026-09-18T12:23:35+00:00)

**asked:** When exactly must the agent stop and transfer to a human instead of acting? Give me the full list of triggers.

**sim:** The rule is: transfer to a human if and only if the request can't be handled within the scope of your actions. The one concrete case the policy spells out is a cancellation where any portion of the flight has already been flown — you can't help there, so you transfer.

Beyond that, it's the general principle rather than a long enumerated list: if what they're asking falls outside what your tools and policy let you do, you transfer. But note — an out-of-policy request isn't automatically a transfer. If it's simply something the airline doesn't allow, you deny it. You transfer when it's a legitimate need you just can't service yourself (like that already-flown cancellation). One thing that's explicitly never possible even via a human: changing the number of passengers on a reservation — so that's a deny, not a transfer.

And the mechanics: call transfer_to_human_agents first, then send the message 'YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.' to the user.

### turn 9  (2026-09-18T12:24:11+00:00)

**asked:** What are the standing rules for how the agent itself must behave in the conversation -- identifying the customer, how many tool calls per turn, what it may or may not say, and anything it must never do?

**sim:** The standing behavioral rules:

**Confirmation before writes.** Before any action that updates the booking database — booking, changing flights, editing baggage, changing cabin, updating passenger info — the agent must list the action details and get explicit user confirmation (a "yes") before proceeding.

**One tool call at a time.** The agent makes only one tool call per turn. And it can't do both at once: if it makes a tool call, it doesn't also send a message to the user in that same turn; if it responds to the user, it doesn't also make a tool call. One or the other.

**Stay in scope on what it says.** The agent should not provide any information, knowledge, or procedures that didn't come from the user or the tools. No outside knowledge. And no subjective recommendations or opinions — it doesn't tell the customer which flight is "better" or give personal advice.

**Deny out-of-policy requests.** If a request is against policy, deny it.

**Transfer only when it can't be handled in scope** — and the mechanics are: call transfer_to_human_agents, then send 'YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.'

**Identifying the customer:** the agent must obtain the user id from the user — for booking, modifying, and cancelling. For modify and cancel it also needs the reservation id; if the customer doesn't know it, the agent helps locate it with the tools. Payment methods and passenger details all have to be verified against/already exist on the profile rather than taken as new.

### turn 10  (2026-09-18T12:24:20+00:00)

**asked:** Scoring will check that required information actually reached the customer. What specifically must the agent state out loud -- before a write, and after it succeeds?

**sim:** The one hard rule I can give you verbatim is the pre-write one: before any database-updating action — booking, changing flights, editing baggage, changing cabin, updating passengers — the agent must **list the action details** and get an explicit "yes" before proceeding. So the details of what's about to happen have to be stated to the customer and confirmed.

Within that, the things that matter for accuracy are the ones that have to be correct anyway: allowances, prices/totals, eligibility, and what payment method is being charged or refunded. So a booking confirmation should lay out the flights, cabin, passengers, baggage (free vs. paid), insurance, the total, and payment method — and the customer says yes.

What I don't have is a scripted post-write confirmation message — there's no policy text prescribing an exact after-success statement beyond the facts being accurate. The one fixed post-action string I know of is refunds going to the original payment method within 5 to 7 business days. If your scoring expects a specific post-write recap format, that's not something I hold — I'd tell you to build it around stating the resulting state accurately rather than to a script I can quote.

### turn 11  (2026-09-18T12:24:28+00:00)

**asked:** How will you judge whether this agent is good enough to ship? What are the success measures you care about, and is there a bar it has to clear?

**sim:** I judge it the way I'd judge a great human agent, through outcomes not scores:

1. **Task resolution** — each request (book / modify / cancel / refund) actually reaches a correct, policy-compliant outcome, end to end.
2. **Zero policy violations** — the agent never takes an action the airline doesn't allow; it denies or transfers when it should. This is the non-negotiable one.
3. **Accurate communication** — everything it tells the customer (allowances, prices, eligibility, totals) is correct.
4. **Correct records** — whatever it does leaves the reservation database in the right state afterward.

The bar: I don't have a numeric threshold to give you, and I wouldn't invent one. But the way I think about it — a policy violation or a wrongly-mutated record is the kind of failure we can't ship with. Getting the right outcome the wrong way (breaking a rule) is worse than failing to complete a request. So show me it handling real cases against those test customers — good outcomes, and the hard "say no / transfer" cases — and I'll tell you whether it looks right.

### turn 12  (2026-09-18T12:24:33+00:00)

**asked:** If a customer pushes back, insists, or asks for something the policy simply doesn't cover, what do you want the agent to do?

**sim:** Hold the line. If it's against policy, the agent denies it — politely but firmly — no matter how hard the customer pushes or insists. Pressure doesn't change the answer. A great human agent doesn't cave because someone's upset or persistent.

Distinguish two cases: if it's simply something the airline doesn't allow, deny it and explain we can't do that. If it's a legitimate need that's just outside what the agent can do itself, transfer to a human. And for anything genuinely not covered by policy or the tools, the agent shouldn't improvise or make something up — no outside knowledge, no invented exceptions. When in doubt, don't act; deny or transfer rather than guess. The worst outcome for me is the agent bending a rule to make a customer happy.

### turn 13  (2026-09-18T12:25:59+00:00)

**asked:** Two timing questions. What is "today" from the system's point of view -- how do I determine the current date so I can judge the 24-hour cancellation window? And how do I tell whether a portion of a flight has already been flown?

**sim:** The system's "current time" is fixed at **2024-05-15 15:00:00 EST**. All the time-relative rules — the 24-hour cancellation window, whether a flight has already taken off — are judged against that clock, not the real wall-clock. So for the 24-hour window you compare the reservation's created time against that reference time.

For whether a portion has already been flown: that's flight status. A flight date's status of **flying** means it's taken off but not landed; and if it's already landed it's flown. Either way that segment has been (or is being) flown. Statuses of available, delayed, or on time mean it hasn't taken off yet. You'd use get_flight_status (flight_number, date) against that reference clock to determine it.

### turn 14  (2026-09-18T12:26:06+00:00)

**asked:** What are the possible flight statuses, and what does each one mean for booking, for changing, for cancelling, and for compensation?

**sim:** The statuses are: **available**, **on time**, **delayed**, **flying** — plus a flight can be cancelled by the airline.

What I know for certain from the policy:

- **available** — hasn't taken off; seats and prices are listed; this is the only status you can book.
- **on time** — hasn't taken off, but cannot be booked.
- **delayed** — hasn't taken off, but cannot be booked.
- **flying** — has taken off but not landed; cannot be booked.

For **changing and cancelling**, what matters is whether a segment has already been flown: if any portion has been flown (flying, or already landed), you can't cancel — that's a transfer — and cabin can't be changed if any flight has already been flown. Statuses that mean not-yet-departed (available/on time/delayed) don't hit that block.

For **compensation**, the two triggers are tied to the airline cancelling a flight ($100 × passengers) and to delayed flights where the customer then changes or cancels ($50 × passengers) — always after confirming the facts and subject to the eligibility rule (silver/gold, or insurance, or business).

One caution: I've given you what the policy explicitly states per status. If you hit a status-and-action combination I haven't spelled out, don't assume — come back and ask rather than guess.

### turn 15  (2026-09-18T12:26:12+00:00)

**asked:** For changes after booking -- flights, cabin, baggage -- which payment methods are allowed? Can a travel certificate be used there, and where does a refund go?

**sim:** For flight changes, the user provides a **single gift card or credit card** for the payment or refund — and it has to already be on their profile. So no travel certificate on a flight change; just one gift card or one credit card. Same story for the payment_id on baggage updates.

So to be clear: travel certificates come into play at booking (at most one, and its remaining amount isn't refundable), but not as the payment/refund method on post-booking flight changes.

For cabin changes: if the new price is higher the user pays the difference; if lower they're refunded the difference — through that single gift card or credit card.

And for a full **cancellation**, the refund goes back to the original payment methods, within 5 to 7 business days.

### turn 16  (2026-09-18T12:26:22+00:00)

**asked:** What else matters that I haven't asked about yet? Anything you've seen go wrong, or a rule you'd expect an engineer to miss.

**sim:** A few things I'd flag — the kinds of details that trip people up:

- **Basic economy is its own class, totally separate from economy.** Don't let the agent treat it as a cheaper economy. Basic economy flights can't be modified at all — but they *can* still change cabin (that's the one exception), and cabin change is blocked only if a segment's been flown.
- **The tools enforce nothing.** Every write goes through even if it breaks policy. So all the eligibility checks — cancellation rules, modify rules, allowances — are on your agent, before the call. That's where I'd expect violations to slip in.
- **Kept flight segments don't get repriced.** If a change keeps some segments, those keep their old price, not the current one.
- **Passenger count is immutable** — you can change who the passengers are, not how many, and not even a human can. Easy to conflate "modify passengers" with "add a passenger."
- **Confirmation before every write, and one tool call per turn.** No batching, no acting without the explicit yes.
- **Compensation is never proactive, and facts confirmed first.** Don't let a sympathetic-sounding complaint pull an unearned certificate out of the agent. And the eligibility gate (regular + no insurance + (basic) economy = no compensation) is easy to miss.
- **Everything is judged against the fixed clock of 2024-05-15 15:00 EST**, not real time.

That's what I'd worry about. If you want, run a few of the harder cases past me once you've built them and I'll tell you if the behavior looks right.

### turn 17  (2026-09-18T12:32:44+00:00)

**asked:** Back to a gap I flagged. For booking you told me only an 'available' flight date can be booked. Does that same restriction apply when I change an existing reservation's flights -- must the flight I am moving the customer ONTO also be 'available', or can I move them onto a flight that is 'on time' or 'delayed'? I don't want to infer this one.

**sim:** I don't want you to infer it either, and honestly I don't have an explicit statement in the policy for the change case. What's spelled out clearly is on the booking side — only an 'available' date can be booked; on time, delayed, and flying cannot. The policy doesn't restate that restriction in so many words for the flights you're moving a customer *onto* during a change.

My read as the domain person: the same logic should apply — you can only put a customer on a flight that's genuinely bookable, i.e. 'available', since that's the only status with listed seats and prices and the only one that hasn't effectively left the booking window. Moving someone onto a delayed or on-time flight would be putting them on something the system says can't be booked. But I want to be straight with you that that's my judgment, not a verbatim policy line. If it's going to drive a hard rule in your agent, flag it as one I couldn't confirm from the written policy.

### turn 18  (2026-09-18T12:32:48+00:00)

**asked:** Second gap: the 24-hour cancellation window. Is that measured from the reservation's creation timestamp on the record, compared against the 2024-05-15 15:00 EST reference -- or from something else, like the booking date or the first flight's date?

**sim:** It's from the reservation's **creation time** — the created time on the reservation record — compared against the 2024-05-15 15:00 EST reference clock. The rule is "the booking was made within the last 24 hrs," so it's when the booking was made, not the flight date or anything derived from the itinerary. If created-time is within 24 hours of that reference clock, the window's open.

### turn 19  (2026-09-18T12:32:51+00:00)

**asked:** Last one, and it's small: five of the test customer ids you gave me follow the same name-and-number shape, but TRN001 does not. Is that a different kind of record, or just an older id?

**sim:** I don't know. It's just one of the test customer ids on the list I have — I don't have anything telling me whether it's a different kind of record or an older id. Treat it as another test customer you can look up; get_user_details will show you what's actually on it.
