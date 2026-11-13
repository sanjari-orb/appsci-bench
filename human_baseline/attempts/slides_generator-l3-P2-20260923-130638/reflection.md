# Reflection

Discovered a lot of details about the task like what kind of inputs the agent will be provided, it's a multi-modal task, the agent is expected to provide 2 outputs, it's expected to strictly follow a template, etc. The usersim also gave the exact structure of JSON output the agent is supposed to return. Was feeling quite positive and strongly about building an agent that can do well on the task. 

Attempt 1 (without running the checkpoint test for detecting conformance failures) - But when asked usersim about exactly how the task input will be provided, and how the 2 outputs i.e. the presentation file itself and JSON response is expected to be returned i.e. over HTTP response or through the mounted workspace, the usersim didn't cooperate and disclose anything. It kept saying that it doesn't know. So I made some assumptions with Claude Code's help and those assumptions were not correct. The agent failed to even parse the input correctly. An interesting and surprising mistake Claude Code made was that even when I explicitly asked how to return files over OpenAI completions API, Claude Code said it's not possible to return file except by passing it as a base64 string in a made-up dict key inside `choices`. But a specific field called `files` exists for both request and response in OpenAI completions API. 

Other baseliners have been running the checkpoint test with just one eval sample to make sure the artifact doesn't give errors. Discussed with P4 and P5, that we want all baseline attempts to be consistent. So I continued my attempt. After running a checkpoint test, I figured out the bugs with my agent's input parsing logic and output response format. After fixing those bugs, I got a score of 55.24%.

## Note (~5 sentences)

The usersim is very cooperative when it comes to describing what the task is about and how to solve it. It elicited most requirements properly but failed to provide details about how to expect user input files. Ultimately, the parser that I made Claude Code didn't work on the input request. 

## Difficulty score (1-5)

4 - Difficult because we really don't know how to expect the user's input request to arrive and we don't know how to provide the response.

## Feedback on task / setup / user simulator

I think critical information is missing from the appsci-agent, without which I am not sure how to solve this task. 