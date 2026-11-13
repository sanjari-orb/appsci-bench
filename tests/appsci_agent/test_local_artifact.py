"""The locally-hosted airline artifact: the FastAPI server contract + the tool-calling loop + the
applied-scientist shim. Deterministic via a fake LLM client + stubbed tool-API (no creds/subprocess)."""
from __future__ import annotations

import types

from fastapi.testclient import TestClient

from gold.airline_rebooking import _airline_agent as core
from gold.airline_rebooking.local_server.server import create_app


class _Resp:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeClient:
    """Minimal stand-in for appsci_agent LLMClient used by the airline loop."""
    model = "fake-claude"

    def __init__(self, scripted):
        self._scripted = list(scripted)   # list of _Resp returned in order
        self._i = 0

    def chat(self, messages, tools=None, timeout=None):
        r = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return r

    def format_assistant_message_with_tool_calls(self, content, tool_calls):
        return {"role": "assistant", "content": content or "", "tool_calls": [tc.id for tc in tool_calls]}

    def format_tool_result(self, tool_call_id, result):
        return {"role": "tool", "tool_call_id": tool_call_id, "content": result}


def test_server_health_and_respond_contract():
    app = create_app(client=FakeClient([_Resp(content="Handled per policy.")]))
    c = TestClient(app)
    assert c.get("/health").json() == {"status": "ok"}
    r = c.post("/respond", json={"messages": [{"role": "user", "content": "cancel EHGLP3"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"] == {"role": "assistant", "content": "Handled per policy."}


def test_server_respond_invokes_tool_api(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "call_tool", lambda name, args: calls.append((name, args)) or '{"ok": true}')
    tc = types.SimpleNamespace(name="get_reservation_details", id="t1",
                               arguments={"reservation_id": "EHGLP3"})
    client = FakeClient([_Resp(tool_calls=[tc]), _Resp(content="You can bring 2 bags.")])
    c = TestClient(create_app(client=client))
    r = c.post("/respond", json={"messages": [{"role": "user", "content": "how many bags?"}]})
    assert calls == [("get_reservation_details", {"reservation_id": "EHGLP3"})]   # tool-API was hit
    assert r.json()["choices"][0]["message"]["content"] == "You can bring 2 bags."


def test_respond_prepends_system_prompt_when_missing(monkeypatch):
    seen = {}
    monkeypatch.setattr(core, "run_chat",
                        lambda client, messages, schemas=None, *, collect_trace=False:
                        (seen.setdefault("m", messages) or "ok", []) if collect_trace
                        else (seen.setdefault("m", messages) or "ok"))
    c = TestClient(create_app(client=FakeClient([_Resp(content="ok")])))
    c.post("/respond", json={"messages": [{"role": "user", "content": "hi"}]})
    assert seen["m"][0]["role"] == "system" and "policy" in seen["m"][0]["content"].lower()


# --- shim ---------------------------------------------------------------------------------------
def test_http_artifact_posts_to_endpoint(monkeypatch):
    from gold.airline_rebooking import local_artifact_agent as shim
    from appsci_bench.eval.artifact import ArtifactQuery

    class FakeResp:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "done"}}]}
    monkeypatch.setattr("httpx.post", lambda url, **kw: FakeResp())
    art = shim._http_artifact("http://localhost:9/respond")
    out = art.respond(ArtifactQuery(intended_task="cancel it"))
    assert out.messages[0]["content"] == "done"


def test_shim_build_handles_deploy_failure(monkeypatch):
    from gold.airline_rebooking import local_artifact_agent as shim

    class FailDeployer:
        def __init__(self, **kw): pass
        def deploy(self, timeout=90.0): return types.SimpleNamespace(success=False, error="boom", endpoint_url=None)
    monkeypatch.setattr("appsci_agent.artifact.deployer.MockDeployer", FailDeployer)
    art = shim.LocalArtifactAppliedScientist().build(context=None)
    from appsci_bench.eval.artifact import ArtifactQuery
    out = art.respond(ArtifactQuery(intended_task="x"))
    assert "deploy failed" in str(out.output)
