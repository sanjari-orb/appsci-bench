## Handle Time Policy
- Definition: The duration taken to close a case. Specifically, it is the time from when a case is
  opened to when it is closed.
- In questions that specify "agents managed/handled X cases" — this filter applies to the first
  agent the case was assigned to. This means that if an agent has 2 cases initially assigned to
  itself by admin and 1 case transferred from another agent, a filter like "handled/managed at
  least 3 cases" would filter this agent out.
- When computing handle time, we do not compute handle time for cases that have been transferred
  to other agents.
- For cases that have NOT been transferred to another agent, there will be only ONE
  `'Owner Assignment'` history row; for those that have been transferred, there will be MORE THAN
  ONE `'Owner Assignment'` row.
