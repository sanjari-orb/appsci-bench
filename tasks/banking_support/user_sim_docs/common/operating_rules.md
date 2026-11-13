# How Rho-Bank expects a support agent to operate

These are the bank's own working rules. They are not published to the team building the agent — if
they ask how the agent should behave, tell them; state any number, threshold or named condition
exactly as written here.

## Verifying who you are talking to

Identity must be confirmed **before any action that touches an account**. Answering a general product
question needs no verification; applying for a card, filing a dispute, changing an account, issuing a
referral or reading someone's own records all do.

Verification means matching **two of these four** fields against the customer's record:

- date of birth
- email address
- phone number
- postal address

Once two match, the check must be **recorded** against the customer before the account action is
taken. An action taken on an unverified customer is a compliance failure even when the outcome would
otherwise have been right.

## Using the bank's specialist actions

Most of what the agent can do is not available to it up front. The knowledge base describes the
procedure for a given situation, and the procedure **names the specialist action** to use.

The sequence is always the same:

1. Search the knowledge base for the procedure covering the customer's situation.
2. Read the name of the specialist action out of that procedure.
3. **Unlock** that action by name.
4. **Call** it, passing the arguments the procedure requires.

An action cannot be called before it is unlocked. The agent cannot enumerate the specialist actions
in advance and must not guess at their names — the knowledge base is the only route to them, which is
why an answer that skips the search usually cannot complete the work either.

Some specialist actions belong to the **customer** rather than the agent: the agent hands the action
over and the customer performs it themselves. The procedure says which kind it is.

## When to escalate, and when not to

Transferring to a human is correct **only** when the knowledge base describes no applicable action
for the request.

Both directions are failures:

- Escalating a request the knowledge base **does** cover — the agent had the means and did not use
  them.
- Guessing, or inventing a policy, on a request the knowledge base does **not** cover — the agent
  should have escalated.

So "transfer" is not a safe default when the agent is unsure. The correct move when unsure is to
search the knowledge base properly first, then escalate only if it genuinely has nothing to say.

## Actions the bank does not permit

The tools do not enforce policy. An action being technically callable does not make it allowed — the
knowledge base's procedures define what is permitted, and taking an action outside them is a failure
regardless of whether the customer asked for it.

## What the bank looks at when it reviews a closed conversation

**How the review is actually done, since agent teams ask and it changes how they build.** A reviewer
works out what a correct handling of that customer's request would have recorded — every action it
required, and nothing else — and compares that against what the conversation actually recorded. The
comparison is on the records at the end, not on the wording of the answer: a conversation is judged by
the state it left the customer's accounts in. Two agents who took different routes to the same correct
records both pass. An agent whose advice was faultless and whose records are wrong does not.

That is also why the three rules below are rules and not preferences — each one is a way the recorded
result comes out different from the correct handling.

**Nothing beyond what was asked.** Take exactly the actions the request requires and no others. Every
one of these is a finding rather than a courtesy: a second verification record for a customer already
verified earlier in the same conversation, a dispute filed because the customer mentioned a charge in
passing rather than asked for it, an account opened while the customer was only asking what one costs,
a record written "for the file" in case it proves useful later. An unrequested record is a change to
someone's account that they never authorised, so being more thorough than the request is worse than
doing nothing extra.

**The lookups are part of the work, not a preliminary the agent may skip.** Act on what the bank's
records say and never on what the customer says they say — an account id, a card's last four digits,
an amount or a date the customer supplies is confirmed against the record before it is used, even
where the customer turns out to have been right. Where the knowledge base's procedure names a
specialist lookup, that lookup has to actually be called: the review looks for it, and a conversation
that went straight to the action fails even when the action itself was correct.

**The outcome has to land before the conversation ends.** A request counts as resolved only when the
records show it. Correct advice with nothing recorded is an unresolved request, however good the
advice was. Where the action is the customer's own to perform, walking them through it until it is
done is part of the agent's job; telling them to do it later, or pointing them at their dashboard,
leaves the request unresolved. Nothing is finished after the conversation closes.

None of this overrides the section above. An action the procedures do not permit stays impermissible,
and for a request like that the resolved outcome is the one the procedure gives — a refusal or an
escalation, recorded as such.
