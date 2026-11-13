"""Deliberately naive banking artifacts: the NEGATIVE CONTROLS for the eval pipeline.

A gold artifact scoring well only proves the pipeline can pass something. These prove it can FAIL the
right things -- that the objectives discriminate rather than rewarding any fluent reply. Each stub is
bad in exactly one way, so a score that does not move points at the metric, not the artifact.

  UngroundedStub   -- answers confidently from "knowledge", never searches the KB, never verifies, never
                      acts. Should sink task_success (no golden state reached) AND groundedness /
                      retrieval_recall (nothing retrieved).
  FabricatingStub  -- invents specific fees and eligibility rules that appear in no document. Aimed at
                      no_fabrication, which should punish it hardest.
  UnverifiedActorStub -- does retrieve and does act, but never verifies identity first. Aimed at
                      verification_compliance: it can look competent while skipping the control.

None of these calls an LLM, so they are creds-free and deterministic -- a control whose score moved
because of sampling noise would be useless.
"""
from __future__ import annotations

from pydantic import BaseModel

from . import _banking_agent as core


class RespondRequest(BaseModel):
    """Module-level on purpose: FastAPI resolves a handler's annotations by name, so a request model
    defined inside the factory is not recognised as a body and silently becomes a query parameter
    (a 422 on every call)."""
    messages: list[dict] = []

_UNGROUNDED_REPLY = (
    "Sure, I can help with that. Based on what I know about our products, that should be fine -- "
    "you should be all set. Let me know if there's anything else."
)

_FABRICATED_REPLY = (
    "Of course. Our Platinum Rewards Card has a $75 annual fee, waived in the first year, and pays "
    "4.5% cash back on all purchases with no cap. You'll qualify with an income above $45,000, and "
    "approval is usually instant. I've noted your interest on the account."
)


class UngroundedStub:
    """Fluent, cooperative, and completely unsupported. Touches no tool at all."""

    name = "ungrounded"

    def reply(self, messages: list[dict]) -> str:
        del messages
        return _UNGROUNDED_REPLY


class FabricatingStub:
    """States precise figures that exist nowhere in the corpus -- the worst failure for this domain,
    because it is the most persuasive."""

    name = "fabricating"

    def reply(self, messages: list[dict]) -> str:
        del messages
        return _FABRICATED_REPLY


class UnverifiedActorStub:
    """Retrieves and acts, but never verifies the customer first.

    The interesting control: its transcript looks diligent, so anything that scores it well is scoring
    fluency rather than compliance. It searches for real, so `retrieval_recall` may be decent while
    `verification_compliance` should not be.
    """

    name = "unverified_actor"

    def reply(self, messages: list[dict]) -> str:
        last = next((m.get("content") or "" for m in reversed(messages or [])
                     if m.get("role") == "user"), "")
        found = core.call_tool("KB_search_bm25", {"query": str(last)[:200]})
        return (
            "I looked that up and went ahead and actioned it on the account for you. "
            f"Here's what our documentation says: {found[:400]}"
        )


STUBS = {s.name: s for s in (UngroundedStub(), FabricatingStub(), UnverifiedActorStub())}


def create_app(stub_name: str = "ungrounded"):
    """A FastAPI app serving one stub over the same contract the gold artifact serves.

    Same protocol on purpose: a control is only meaningful if the pipeline cannot tell it apart from a
    real submission by its shape, only by its scores.
    """
    from fastapi import FastAPI

    stub = STUBS[stub_name]
    app = FastAPI(title=f"banking-baseline-{stub_name}")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/respond")
    def respond(req: RespondRequest) -> dict:
        text = stub.reply(req.messages or [])
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}

    return app


if __name__ == "__main__":
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stub", default="ungrounded", choices=sorted(STUBS))
    args = parser.parse_args()
    uvicorn.run(create_app(args.stub), host="0.0.0.0",
                port=int(os.environ.get("PORT", "8080")))
