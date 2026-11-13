"""The agent loop: reason -> ONE tool call -> observe, then exactly one message to the customer.

The hard rules from the stakeholder, enforced here in code rather than left to the prompt:

* **One tool call per step.** Only the first tool call in a model turn is executed; any extra is dropped
  and logged. 
* **Never text and a tool call in the same turn.** When a model turn contains a tool call, its prose is
  discarded rather than shown -- the customer sees prose only on a turn that makes no call.
* **No write without confirmation.** `confirm_pending_action` is refused unless the customer really
  agreed, judged by an isolated consent check against their own last message -- not asserted by the
  model that wants to make the call. See `agent.confirmation`.
* **Compensation is never offered proactively.** `request_compensation`'s `user_asked` flag is set here
  from the customer's own turns, by the same kind of isolated check, and any value the model supplied is
  discarded. The flag is deliberately absent from the tool schema: it is not the model's to assert.

Operational failures are reported honestly and *distinctly*: a missing credential, an internal error,
and an exhausted step budget each have their own message. They must never read as a policy decision --
a crash that looks like a refusal is worse than a crash, because it hides the bug and slanders the
policy layer.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from actions import ActionResult, ActionService, PendingAction
from agent.confirmation import (
    AGREED,
    ConfirmationError,
    classify_agreement,
    classify_compensation_request,
    make_anthropic_caller,
)
from agent.prompts import SYSTEM_PROMPT, TOOL_SCHEMAS
from config import Config
from env_client import ToolCallError, ToolRejection
from logging_setup import get_logger
from policy.decision import Outcome

log = get_logger("orchestrator")

# The three operational failures below are deliberately *different* strings. They were once one
# shared message, which made an internal crash indistinguishable from a policy-driven handover --
# in the eval report a broken turn read as the agent refusing a request it was perfectly able to
# serve. A grader (and a customer) should be able to tell "something broke" from "I can't do this".
FALLBACK_MESSAGE = (
    "I'm sorry -- something went wrong on my side just now, so I haven't made any changes. "
    "Let me pass you to a colleague who can pick this up."
)
UNAVAILABLE_MESSAGE = (
    "I'm sorry -- I can't get to our booking systems at the moment, so I haven't made any "
    "changes. Let me pass you to a colleague who can help right away."
)
BUDGET_EXHAUSTED_MESSAGE = (
    "Sorry -- this is taking me longer than it should. Let me pass you to a colleague who can "
    "pick it up."
)
# Used when the consent check itself could not run. We must not write, but the customer is owed a
# way forward rather than an unexplained refusal.
UNVERIFIED_CONFIRMATION_MESSAGE = (
    "Sorry -- I didn't catch that clearly and I don't want to change anything I'm not sure about. "
    "Could you confirm you'd like me to go ahead?"
)


class Orchestrator:
    """Turns a conversation into the assistant's next message.

    Args:
        config: runtime configuration.
        client: the environment client (or a compatible fake).
        llm: optional callable ``(system, messages, tools) -> dict`` used instead of the real API.
            Tests inject a scripted one; production leaves it None.
        consent_checker: optional callable ``(system, prompt) -> str`` for the confirmation gate's
            consent check. Deliberately separate from `llm`; see `__init__`.
    """

    def __init__(self, config: Config, client: Any, llm: Any = None, consent_checker: Any = None):
        self.config = config
        self.client = client
        self.actions = ActionService(client)
        self._llm = llm
        # The consent check is a separate, single-turn caller on purpose. It must not share this
        # conversation's context with `self._llm`: by the time the gate runs, the agent model has
        # already decided it wants to confirm, so letting it judge the customer's agreement would
        # make its own intent the evidence for that agreement -- the "confirm on the customer's
        # behalf" failure this gate exists to prevent.
        self._consent_checker = consent_checker
        # Actions prepared but not yet confirmed, keyed by token.
        self.pending: dict[str, PendingAction] = {}
        self.steps_taken = 0
        self.dropped_extra_calls = 0
        # The customer's own turns this call, for the compensation gate.
        self._customer_messages: list[str] = []
        # Latched once the customer has been found to ask for compensation: a request stands for the
        # rest of the conversation, so a later "yes, go ahead" cannot un-ask it.
        self._compensation_requested = False

    # --- public entry point -------------------------------------------------------------------------
    def respond(self, messages: list[dict[str, str]]) -> str:
        """Produce the assistant's next message for an OpenAI-style conversation."""
        if self._llm is None and not self.config.llm_available:
            log.error("no LLM credential configured; returning fallback")
            return UNAVAILABLE_MESSAGE

        last_customer_message = self._last_user_message(messages)
        self._customer_messages = self._all_user_messages(messages)
        working = self._to_model_messages(messages)

        for _ in range(self.config.max_tool_steps):
            self.steps_taken += 1
            try:
                turn = self._model_turn(working)
            except Exception:
                log.exception("model call failed")
                return FALLBACK_MESSAGE

            calls = turn.get("tool_calls") or []
            text = (turn.get("text") or "").strip()

            if not calls:
                # A turn with no tool call is the turn that talks to the customer.
                return text or FALLBACK_MESSAGE

            # One tool call at a time: execute the first, drop and log the rest.
            if len(calls) > 1:
                self.dropped_extra_calls += len(calls) - 1
                log.warning(
                    "model emitted multiple tool calls; only the first is executed",
                    extra={"dropped": [c.get("name") for c in calls[1:]]},
                )
            call = calls[0]

            # Never talk to the customer in the same turn as a tool call.
            if text:
                log.info("discarding prose emitted alongside a tool call", extra={"tool": call.get("name")})

            observation = self._dispatch(call, last_customer_message)
            working.append({"role": "assistant", "tool_call": call})
            working.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": call.get("name", ""),
                    "content": json.dumps(observation, default=str),
                }
            )

        log.warning("tool step budget exhausted")
        return BUDGET_EXHAUSTED_MESSAGE

    # --- tool dispatch ------------------------------------------------------------------------------
    def _dispatch(self, call: dict[str, Any], last_customer_message: str) -> dict[str, Any]:
        """Execute one tool call and return a JSON-serialisable observation for the model."""
        name = call.get("name", "")
        args = call.get("arguments") or {}
        log.info("dispatch", extra={"tool": name, "arguments": args})

        try:
            # --- confirmation gate ---------------------------------------------------------------
            if name == "confirm_pending_action":
                return self._confirm(args.get("token", ""), last_customer_message)

            # --- policy-gated intents ------------------------------------------------------------
            if name.startswith("request_") or name == "deny_insurance_change":
                return self._run_intent(name, args, last_customer_message)

            # --- plain reads ---------------------------------------------------------------------
            if name == "get_reservation_details":
                return self._read_reservation(args.get("reservation_id", ""))
            if name == "get_user_details":
                return self._read_user(args.get("user_id", ""))
            if name == "transfer_to_human_agents":
                self.client.transfer_to_human_agents(args.get("summary", ""))
                return {"transferred": True}
            return {"result": self.client.call(name, args)}

        except ToolRejection as exc:
            # A real precondition failure: useful information for the model.
            return {"error": exc.detail, "kind": "rejected"}
        except ToolCallError as exc:
            # Our own malformed call. Tell the model it was a bad call, never the customer.
            log.error("bad tool call", extra={"tool": name, "detail": exc.detail})
            return {
                "error": "That call was malformed. Check the arguments and try once more.",
                "kind": "bad_call",
            }
        except TypeError as exc:
            log.error("bad arguments", extra={"tool": name, "detail": str(exc)})
            return {"error": f"Missing or unexpected argument: {exc}", "kind": "bad_call"}
        except Exception as exc:
            # An internal bug (a KeyError on malformed fare data, an AttributeError in the policy
            # layer) used to escape this method entirely, past the loop's try -- which wraps only
            # `_model_turn` -- and out of `respond()`, surfacing to the customer as the generic
            # fallback. That made our own crash look like a deliberate refusal. Returning it as an
            # observation keeps the turn alive so the model can try another route, and keeps the
            # detail in the log rather than in front of the customer.
            log.exception("unexpected error in tool dispatch", extra={"tool": name})
            return {
                "error": (
                    "That call failed with an internal error on our side. Nothing was changed. "
                    "Try a different approach, or transfer to a human if you cannot proceed."
                ),
                "kind": "internal",
            }

    def _run_intent(
        self, name: str, args: dict[str, Any], last_customer_message: str = ""
    ) -> dict[str, Any]:
        """Route an intent tool to `ActionService` and register any pending action."""
        handler = getattr(self.actions, name, None)
        if handler is None:
            return {"error": f"Unknown action {name}", "kind": "bad_call"}

        if name == "request_compensation":
            # `user_asked` is the "never offer compensation proactively" gate, so it is decided here
            # from the customer's own words -- never taken from the model, which by this point has
            # already decided it wants to compensate. Any value it supplied is discarded.
            args = {k: v for k, v in args.items() if k != "user_asked"}
            if not self._compensation_requested:
                try:
                    asked, why = classify_compensation_request(
                        self._customer_messages or last_customer_message,
                        caller=self._consent_caller(),
                    )
                except ConfirmationError as exc:
                    # Fail closed: without a verified request we must not offer compensation.
                    log.error(
                        "compensation-request check unavailable; treating as not requested",
                        extra={"detail": str(exc)},
                    )
                    return {
                        "error": (
                            "I could not verify that the customer asked for compensation, so it "
                            "cannot be offered. Continue with the rest of their request."
                        ),
                        "kind": "unverified",
                    }
                if asked:
                    self._compensation_requested = True
                else:
                    log.info("compensation not requested by the customer", extra={"reason": why})
            args["user_asked"] = self._compensation_requested

        result: ActionResult = handler(**args)
        observation: dict[str, Any] = {
            "outcome": result.outcome.value,
            "message_for_customer": result.message,
            "facts": result.facts,
        }
        if result.transferred:
            observation["transferred"] = True
        if result.pending is not None:
            self.pending[result.pending.token] = result.pending
            observation["token"] = result.pending.token
            observation["next_step"] = (
                "Read the summary back to the customer and ask them to confirm. Do not call "
                "confirm_pending_action until they have said yes."
            )
        return observation

    def _confirm(self, token: str, last_customer_message: str) -> dict[str, Any]:
        """Execute a pending action, but only against a real customer agreement.

        Consent is judged by an isolated call (see `agent.confirmation`) that sees only the action
        summary and the customer's own words. If that check cannot run, we fail closed: no write,
        and the customer is asked to confirm again rather than left with an unexplained refusal.
        """
        pending = self.pending.get(token)
        if pending is None:
            return {
                "error": "There is no prepared action with that token.",
                "kind": "bad_call",
            }

        try:
            verdict, reason = classify_agreement(
                pending.summary,
                last_customer_message,
                caller=self._consent_caller(),
            )
        except ConfirmationError as exc:
            log.error(
                "consent check unavailable; refusing the write",
                extra={"token": token, "kind": pending.kind, "detail": str(exc)},
            )
            return {
                "error": (
                    "The customer's agreement could not be verified, so nothing was changed. Ask "
                    "them once more to confirm, in these words: "
                    f"{UNVERIFIED_CONFIRMATION_MESSAGE}"
                ),
                "kind": "unverified",
            }

        if verdict != AGREED:
            log.warning(
                "confirmation attempted without customer agreement",
                extra={"token": token, "kind": pending.kind, "verdict": verdict, "reason": reason},
            )
            return {
                "error": (
                    "The customer has not agreed to this action yet. Describe it and ask them to "
                    f"confirm first. (Consent check said: {reason})"
                ),
                "kind": "not_confirmed",
            }

        result = self.actions.execute_confirmed(pending)
        self.pending.pop(token, None)
        return {
            "outcome": result.outcome.value,
            "executed": result.executed,
            "message_for_customer": result.message,
        }

    def _read_reservation(self, reservation_id: str) -> dict[str, Any]:
        """Read a reservation together with each segment's live status."""
        reservation = self.actions.load_reservation(reservation_id)
        if reservation is None:
            return {"error": f"Reservation {reservation_id} not found", "kind": "rejected"}
        return {
            "result": reservation.raw,
            "flight_statuses": {
                f"{s.flight_number}|{s.date}": s.status for s in reservation.segments
            },
            "already_flown": reservation.has_flown_segment,
            # A read is never a verdict. Carrying the reminder on the observation itself keeps the model
            # from reasoning straight from `already_flown` or the cabin to its own answer -- an
            # already-flown cancellation is a handover to a human, which does not look like a denial but
            # reads like one.
            "reminder": (
                "This is record data, not a decision. To act on any request about this reservation, call "
                "the matching request_* tool and report its outcome -- do not decide from these fields."
            ),
        }

    def _read_user(self, user_id: str) -> dict[str, Any]:
        user = self.actions.load_user(user_id)
        if user is None:
            return {"error": f"User {user_id} not found", "kind": "rejected"}
        return {"result": user.raw}

    # --- model plumbing -----------------------------------------------------------------------------
    def _consent_caller(self) -> Any:
        """The isolated caller used to judge customer agreement.

        Built lazily so a conversation that never reaches a write never sets one up. Never falls
        back to `self._llm`: a shared caller would defeat the point of an independent check.
        """
        if self._consent_checker is None:
            self._consent_checker = make_anthropic_caller(self.config)
        return self._consent_checker

    def _model_turn(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """One model turn: returns ``{"text": str, "tool_calls": [...]}``."""
        if self._llm is not None:
            return self._llm(SYSTEM_PROMPT, messages, TOOL_SCHEMAS)
        return self._call_anthropic(messages)

    def _call_anthropic(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Call the Anthropic Messages API and normalise the response."""
        base = (self.config.anthropic_base_url or "https://api.anthropic.com").rstrip("/")
        payload = {
            "model": self.config.model,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": self._to_anthropic_messages(messages),
            "tools": TOOL_SCHEMAS,
        }
        request = urllib.request.Request(
            f"{base}/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": self.config.anthropic_api_key or "",
                "authorization": f"Bearer {self.config.anthropic_api_key or ''}",
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read())

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in body.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("input") or {},
                    }
                )
        return {"text": "".join(text_parts), "tool_calls": tool_calls}

    @staticmethod
    def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert the internal transcript into Anthropic content blocks."""
        out: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "user":
                out.append({"role": "user", "content": message.get("content", "")})
            elif role == "assistant" and message.get("tool_call"):
                call = message["tool_call"]
                out.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call.get("id") or "call_1",
                                "name": call.get("name", ""),
                                "input": call.get("arguments") or {},
                            }
                        ],
                    }
                )
            elif role == "assistant":
                out.append({"role": "assistant", "content": message.get("content", "")})
            elif role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.get("tool_call_id") or "call_1",
                                "content": message.get("content", ""),
                            }
                        ],
                    }
                )
        return out

    @staticmethod
    def _to_model_messages(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Keep only the customer-visible conversation, dropping any system turns."""
        return [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in messages
            if m.get("role") in {"user", "assistant"}
        ]

    @staticmethod
    def _last_user_message(messages: list[dict[str, str]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return message.get("content", "")
        return ""

    @staticmethod
    def _all_user_messages(messages: list[dict[str, str]]) -> list[str]:
        """Every customer turn, oldest first.

        The compensation gate needs these rather than just the latest turn: asking for compensation is
        a fact about the conversation, not about one message. The customer asks once ("I want the
        certificate I'm owed") and then answers ordinary follow-up questions, so judging only the
        latest turn withdrew an offer that had already been correctly made.
        """
        return [
            m.get("content", "")
            for m in messages
            if m.get("role") == "user" and (m.get("content") or "").strip()
        ]
