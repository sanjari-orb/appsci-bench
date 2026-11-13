## Case Routing Policy
The case routing policy determines the best agent to assign the given new case based on the
following criteria:
- **Issue Expertise**: The agent who has closed the most cases associated with the issue most
  similar to the given case.
- **Product Expertise**: If there is a tie in issue expertise, the best agent is the one who has
  solved the most cases associated with the product most relevant to the given case.
- **Workload**: If there is still a tie, the best agent is the one that has the least cases with
  Status not 'Closed'.
