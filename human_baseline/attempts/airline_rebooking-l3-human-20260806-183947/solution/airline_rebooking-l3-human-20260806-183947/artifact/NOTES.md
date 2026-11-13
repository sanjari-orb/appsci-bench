# Airline rebooking task: 
## Description of the task: 
We run a contact channel for airline reservations, and we want an agent that handles customer requests end to end -- booking, changing, cancelling, plus refunds and compensation -- the way a good human agent would. The customer gets a valid outcome and we never break airline policy. The big pain today is that customers are often wrong about their status or what they're owed, and bad agents either break policy or just give wrong answers. So the non-negotiable is: never do something the airline doesn't allow, and when a request can't be honored, say no or hand it to a human. And whatever the agent does has to leave the reservation records correct. 

## Notes from the interview


* Users are usually wrong, an agent should double check always. He is allowed to contradict a user but he should explain accurately. 
* Accurancy over latency and other non-functional metrics. 
* Aconversation finishes when: 
    * customer's request has been resolved correctly
    * the agent has told them it can't be done and either explained why or handed off to a human. 
* Failure taxoomy: 
   * policy violations 
   * wrong/inaccurate info to the customer 
   * incorrect reservation records 
   * acting without confirmation or when it should've refused/transferred.
* Human transference: 
    * when a request cannot be honored within the policy it should be transfered. 
    * when the agent is not allowed to resolve the request it should be transferred. 
    * the agent should never improvise. 