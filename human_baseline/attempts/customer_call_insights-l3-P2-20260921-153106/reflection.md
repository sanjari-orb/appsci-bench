# Reflection

Submitted and got a final score of 0.455. Was surprised to get a low score because 

## Note (~5 sentences)

My approach was to ask Claude Code to explore the task environment and resources available in the directory which was only a kpis.md file. CC did a good job of listing out the seed data from the DB and that became fodder for early smoke tests. Simultaneously, I elicited requirements from usersim and got a couple of very important points from it like
1. what is the task about
2. what is the expected output
3. What values can sentiment take and what's the rubric to classify those values
4. what is the difference between next steps and commitment. 

Then, I made Claude Code implement an agent for the task after providing all the information gathered. I did initial smoke tests with 3 seed samples from the DB and then also asked CC to write an eval dataset of 20 samples, create eval metrics and generate an eval harness. I did 4 optimization cycles using internal evals and mostly improved the prompt in those cycles.

## Difficulty score (1-5)

I give it a score of 2. I found the task to be quite easy but it was very hard to get critical information about the task from the usesim like format of input request - text or structured. Also, because my score was low, I might have missed some hidden rules about the task.

## Feedback on task / setup / user simulator

We should be careful that critical information necessary to do well on a task is reachable. I worry that L3 is not eliciting all the information it should and the appsci-agent is of course, not provided everything directly. 