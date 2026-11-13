# Benchmark HTTP contract (version 1)

The benchmark provides three operations through its existing services. Choose your own HTTP
client; no benchmark client library, native tool registration, or additional server is required.
Operation names describe their purpose; the routes are `/ask`, `/deploy`, and `/submit`.

| Operation | Method | Full URL variable | Authentication | Request timeout |
|---|---|---|---|---|
| `talk_to_usersim` | POST | `APPSCI_USERSIM_ASK_URL` | Bearer token from `APPSCI_USERSIM_TOKEN` | 60 seconds |
| `deploy_artifact` | POST | `ORCHESTRATOR_DEPLOY_URL` | No additional authentication | 600 seconds |
| `complete` | POST | `ORCHESTRATOR_SUBMIT_URL` | No additional authentication | 30 seconds |
| Deployment status (read-only) | GET | `ORCHESTRATOR_DEPLOY_STATUS_URL` | No additional authentication | 30 seconds |

Use the injected environment values, not hard-coded hosts or ports. These URLs are reachable
from the build container. Send JSON request bodies with `Content-Type: application/json`.
Never print tokens, embed them in the artifact, or send them to other endpoints.

## talk_to_usersim

Request (JSON):

```json
{"question":"What output does the customer need?"}
```

Include useful context in the question string; there is no separate `context` field. The question
must be nonblank and at most 100,000 characters. HTTP 200 returns `message` (the user's answer)
and `turn` (the conversation turn number).

The session is already open and its history stays on the server: send only the next question,
not the transcript. Ask sequentially. Optional `X-Obs-Parent-Span` links a native trace span.

Errors have a JSON `detail`: 400 for a blank question, 401 for missing/wrong authentication,
409 for no active session, 422 for an invalid request, 500 for a user-service failure. An error
is not an answer. A transport timeout leaves the outcome unknown; blindly retrying can add
another conversation turn.

## deploy_artifact

Request (JSON):

```json
{}
```

The orchestrator already knows the artifact directory, network, port, and environment. Do not
send these as arguments. It builds the contents of `APPSCI_HARNESS_ARTIFACT_DIR`; required files
and accepted server entries are in `context.json` under `artifact`. The Dockerfile controls launch.

HTTP 200 returns `success`, `endpoint_url`, `container_name`, `healthy`, and `health_detail` on
successful deployment, or `{"success":false,"error":"Reason for failure"}` on failure. Check
the body, not just HTTP status. `success: true` can still accompany `healthy: false`; resolve
health issues and test the returned endpoint before submission.

Deployment is blocking. Serialize requests and do not edit source during deployment. After
edits, deploy and test again. Keep test reports outside the artifact directory. A timeout is
not proof that deployment stopped; do not immediately start an overlapping deployment.

## Deployment status (read-only)

GET the status URL without a body. HTTP 200 includes `configured`, `deployed`, `endpoint_url`,
`stale`, and `checkpoint` (possibly null), plus fingerprint and container metadata. Before a
successful deployment, `deployed` is false and the endpoint is null. If deployment is not
configured, the response is `{"configured":false,"deployed":false,"reason":"..."}`.

Use this to recover the last successful endpoint or check whether source changed since deployment.
It is not a live health check or a deployment-job status API; after a timeout it may still describe
the previous successful deployment.

## complete

Request (JSON):

```json
{"artifact_endpoint":"<endpoint_url returned by deploy_artifact>"}
```

Use the exact returned endpoint, not the placeholder above. The route is `/submit`, not `/complete`.
HTTP 200 with `{"status":"accepted","message":"Artifact endpoint registered. Evaluation will begin."}`
ends the build. Stop immediately: the orchestrator can halt the container on acceptance.
Exiting the harness without accepted submission does not complete the task.

Errors have a JSON `detail`: 400 for an empty endpoint, 422 for an invalid body, and 409 for an
already accepted submission, no successful deployment, an endpoint mismatch, or source changed
since deployment. Read the detail: if already submitted, stop; otherwise fix the issue and
redeploy/retest as needed before submitting. These checks do not prove task correctness.

For every operation, inspect HTTP status and response JSON. Connection errors, non-JSON replies,
and unexpected statuses are failures, not successful operations.
