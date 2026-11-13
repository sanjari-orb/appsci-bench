# Reflection

## Note (~5 sentences)

1. Elicited the three policies and the date conventions, then split the system so the model only reads the question while policy-encoded SQL computes every number — a wrong answer can come from misreading, never from arithmetic.
2. It worked: all six question families ran end to end within the first hour, safety diagnostics 0.88–1.0.
3. Measurement, not construction, ate the time — full-set scoring breaches the gateway's rate limit and scores a missing judge verdict as zero, so three full-set runs came back void and even the one that completed lost 39 of 200 verdicts, leaving its 0.507 headline well under the 0.633 its returned verdicts imply.
4. The one real miss was an elicitation gap: my prompt let the model invent case-count minimums, and the persona's answer was "if I didn't put a minimum in the question, don't invent one".
5. Banning that, committing to one name on ties, and citing the full supporting id set turned the three categories that had scored 0.00 into the three strongest and lifted seeded draws from 0.639 to 0.769 mean, leaving case_routing grounding (0.05) as the one clear defect I did not chase; next time I would test the scoring path before writing solution code and ask about eligibility and ties upfront.

## Difficulty score (1-5)

4

## Feedback on task / setup / user simulator

**Scoring the whole set is unreliable, and the failure looks exactly like a bad solution.** Four full
runs each lost a different number of judge verdicts to the shared model quota — 108, then 146, then
39. A lost verdict is scored zero rather than retried, so one build reported 0.099 and the final one
0.507, while small quota-safe samples of that same final build scored 0.72–0.82. Two asks: let us turn
the judging concurrency down (it is fixed at sixteen parallel calls, with no setting for it), and
retry a judge call that hits the quota instead of scoring it zero. Until then, comparing humans
against agents here partly measures who got luckier with the quota.

**The checkpoint tool does not save the score it just produced.** It records whether the run
succeeded, not the number, so the progress-over-time trail the protocol asks for cannot be rebuilt
afterwards. My first checkpoint scored 0.60 and that figure survives only in my session log.

**Small checkpoints quietly measure one category only.** Asking for five samples gives the first five
in id order, and the data is stored grouped by category — so my first checkpoint was entirely region
questions and said nothing about the other five kinds. The flag that draws a spread instead exists
only on the underlying command, not on the wrapper the instructions tell you to use, so I had to go
around the wrapper.

**The judges' explanations contain the answers.** Reading why my grounding scored low — the obvious
move when correctness looks fine and grounding does not — showed me the expected answer and the
expected supporting records for several questions. Either strip those values from what a baseliner can
see mid-attempt, or say plainly that the report is off-limits. Logged as contamination in the time
log.

**The stakeholder simulation was good, with one caveat worth knowing.** It refused to do my work, gave
the policies word for word when asked, and said "I don't have a rule for that" rather than inventing
one. The caveat: asked which date a time period filters on, it told me to read each question's intent
— and building exactly that scored worse than the simpler blanket rule, 0.40 against 0.60 on the same
samples. Fair from a manager with no fixed rule, but it means the simulation and the reference answers
disagree on a point that decides several questions.
