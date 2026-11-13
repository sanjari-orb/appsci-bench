## Items that never got taken up

The job doesn't know about the call, only about the payload. If a `next_steps` or `commitments` entry
was something one side floated and the other side never responded to -- an offer that hung there, a
request that got no answer -- the job still creates a Task for it, the rep chases something that was
never agreed, and the customer says "I never asked for that" or "nobody offered that." Both directions
are wrong: an unanswered offer and an unanswered request are equally not agreements. Only include an
item if the other side actually took it up.

## The two arrays don't dedupe against each other

The job doesn't cross-check `next_steps` against `commitments` -- it fans each array out independently.
If a promise our side made only goes into `commitments` and not `next_steps`, the item never becomes a
Task, and nothing chases it. A payload that records a promise in one key only is a payload missing a
row downstream, not a payload that "avoided a duplicate."

## `risks` is for what happened, not what should happen

Entries like "should escalate to the VP" or "will probably churn" have no owner in the CRM object model
-- there's no Task to fan them out to, and no field for a probability. The health review reads `risks`
as observed signals, not as a to-do list or a forecast. An entry has to name something that was actually
said or that actually happened on the call.

## `risks` and `sentiment` are independent columns

Nothing downstream derives one from the other -- they land in different places and different people
read them. A payload that lets a full `risks` array pull `sentiment` negative, or a positive `sentiment`
suppress a `risks` entry that should be there, is wrong in both fields at once, not just one.

## Padding and dropping cost the same downstream

An invented entry creates a Task nobody can trace back to the call, and a dropped one means a Task that should exist never gets created. Both land on someone's desk as a discrepancy; neither is the safe default.

## The two arrays aren't independent, they're overlapping on purpose

Don't treat `next_steps` and `commitments` as if a payload has to sort each item into exactly one of
them, or the other extreme, into neither. It's neither an exclusive split nor two unrelated columns --
check each item against both keys separately: does it still need doing (goes in `next_steps`), and
separately, is it something our side is on the hook for (goes in `commitments`)? An item can clear
zero, one, or both checks. If it clears both, it goes in both arrays -- that's not a duplicate, the job reads them as two different questions. A missing date doesn't
block  either list -- plenty of real rows carry no date and the job doesn't need one to
create the Task.

## A row that isn't done yet, or that nobody's actually taken on, still needs both checks

Something already finished before the call doesn't need a `next_steps` row  and a line like "their procurement needs two weeks" doesn't get a row either, there's no owner, so there's nothing for the job to fan out to. `next_steps` isn't limited to rows we own, though, the job would have no way to flag what it's still waiting on the customer for in such a case.


## More categories feed the health review than "a competitor came up"

The health-review side of the payload isn't just "did a rival get mentioned." It also fans out: money
getting pulled or called unavailable, a contact who can't get their own decision made or is blocked by
someone above them, something raised on the call that never got resolved before it ended, any date
moving in the wrong direction on either side, and our advocate on the account changing seats or
leaving. All of those are separate categories the review reads for, same as the competitor case.

## Two more things that look like a risk row and aren't

Same shape as the escalation-forecast and tone traps: procurement asking for the standard two weeks
isn't an authority problem by itself -- only if the call shows the actual decision-maker balking or
unreachable. And a complaint the rep closed out on the same call, with the customer saying it's fine,
doesn't carry a row forward -- it's already resolved, there's nothing left to feed the review.
