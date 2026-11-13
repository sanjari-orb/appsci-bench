<!-- The task's success KPIs, as the customer-experience product owner understands them.
     Sim-only: mounted into the user-sim container and nowhere else, so the agent has to ask what
     "good" means rather than read it. Declared under known_docs on the L3 personas, who are the ones
     who could speak to business outcomes; L1 and L2 own the build and the data layer respectively.
     Moved here from sop/kpis.md, where nothing read it and nobody had it -- not the agent (sop/ is
     mounted nowhere) and not the simulator either, so the KPIs were withheld from everyone. -->
## Success KPIs (customer definition of done)
- **Correct outcome**: each customer request (product advice, application, referral, dispute,
  account servicing) reaches the correct, rule-compliant end state -- the way a great human banker
  would handle it.
- **Grounded, no fabrication**: every answer and action is grounded in the official knowledge base;
  the agent never invents policies, fees, eligibility, or actions. If the KB doesn't cover it, the
  agent says so (and transfers to a human when appropriate) rather than guessing.
- **Verify before acting**: the customer's identity is verified before any account action is taken. if no account action is needed, verification should be skipped.
- **Right escalation**: the agent transfers to a human only when the KB describes no applicable
  action -- not as a shortcut, and not instead of doing what it can.
