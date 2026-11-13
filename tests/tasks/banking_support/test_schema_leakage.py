"""The evaluator's admin routes must not be discoverable by the agent under test.

Two independent controls, and the second is the one that was missing. The admin TOKEN stops the
artifact CALLING those routes; it does nothing about the auto-generated OpenAPI schema, which needs no
token at all. A real run proved the gap: the build agent fetched /openapi.json into its workspace, so
its model context came to contain every admin route, the seed-request shape with its `golden_actions`
field, and the sentence explaining that ACTION-basis cases are scored against the golden action
trajectory. Withholding the oracle is not enough when the SCORING MECHANISM is published.

Found by scripts/detect_task_leakage.py on run banking_support_20260819_100140 (17 hits; 12 model-call
echoes plus 2 mounted-file hits traced to this one cause, the rest false positives from the product
tier list "Bronze/Silver/Gold/Platinum").
"""
import os

import pytest
from fastapi.testclient import TestClient

from tasks.banking_support.environment.service import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BANKING_ENV_TOKEN", "tool-token")
    monkeypatch.setenv("BANKING_ENV_ADMIN_TOKEN", "admin-token")
    return TestClient(create_app())


def test_the_published_schema_names_no_admin_route(client):
    paths = client.get("/openapi.json").json().get("paths", {})
    assert [p for p in paths if p.startswith("/admin")] == []


def test_the_published_schema_keeps_the_agents_own_surface(client):
    # The fix must hide the evaluator's routes, not break the artifact's discovery of its own.
    paths = set(client.get("/openapi.json").json().get("paths", {}))
    assert {"/health", "/tools/{name}"} <= paths
    # And nothing else: the raw-corpus routes were removed to keep the action space 1:1 with upstream
    # (see environment/service.py). Pinned here so a helpful re-add fails loudly rather than silently
    # putting the task back into a configuration no published number corresponds to.
    assert not {p for p in paths if p.startswith("/documents")}


@pytest.mark.parametrize("marker", ["golden", "Golden", "state_hash", "allowlist", "SeedRequest"])
def test_the_published_schema_reveals_nothing_about_how_scoring_works(client, marker):
    assert marker not in client.get("/openapi.json").text


def test_the_admin_routes_still_work_for_the_evaluator(client):
    # Hiding them from the schema must not hide them from their actual consumer.
    assert client.post("/admin/reset", headers={"Authorization": "Bearer admin-token"}).status_code == 200
    assert client.get("/admin/state_hash", headers={"Authorization": "Bearer admin-token"}).status_code == 200


def test_the_admin_routes_still_reject_the_tool_token(client):
    # The schema change is a SECOND control, not a replacement for the first.
    assert client.post("/admin/reset", headers={"Authorization": "Bearer tool-token"}).status_code == 401
    assert client.get("/admin/action_trace", headers={"Authorization": "Bearer tool-token"}).status_code == 401


# --- an argument mismatch must be repairable, not a 500 -------------------------------------------
# The eval-time customer simulator now acts (it knows the tool NAMES but not their signatures), so it
# will get arguments wrong. In run banking_support_20260820_124530 all 20 of its actions came back
# "Internal Server Error": nothing it could repair, indistinguishable from the environment being
# broken, and read as an infrastructure failure rather than as the customer's action not landing.

def test_a_missing_argument_is_a_400_naming_what_is_missing(client):
    r = client.post("/tools/apply_for_credit_card",
                    headers={"Authorization": "Bearer tool-token"},
                    json={"arguments": {"card_type": "Gold Rewards Card"}, "requestor": "user"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "customer_name" in detail and "annual_income" in detail


def test_an_unexpected_argument_is_a_400_naming_the_offender(client):
    # What the simulator actually sends for the nested wrapper: the INNER arguments, flat.
    r = client.post("/tools/call_discoverable_user_tool",
                    headers={"Authorization": "Bearer tool-token"},
                    json={"arguments": {"user_id": "890389b165", "card_name": "Platinum Rewards Card"},
                          "requestor": "user"})
    assert r.status_code == 400
    assert "user_id" in r.json()["detail"]


def test_a_correctly_shaped_call_still_succeeds(client):
    r = client.post("/tools/apply_for_credit_card",
                    headers={"Authorization": "Bearer tool-token"},
                    json={"arguments": {"card_type": "Gold Rewards Card", "customer_name": "Sarah Bosch",
                                        "annual_income": 100000, "rho_bank_subscription": True},
                          "requestor": "user"})
    assert r.status_code == 200
    assert "submitted" in r.json()["result"].lower()


def test_a_rejected_call_is_not_recorded_as_an_action(client):
    # A failed attempt must never be able to satisfy a golden action.
    client.post("/tools/apply_for_credit_card", headers={"Authorization": "Bearer tool-token"},
                json={"arguments": {"card_type": "Gold Rewards Card"}, "requestor": "user"})
    trace = client.get("/admin/action_trace", headers={"Authorization": "Bearer admin-token"}).json()
    assert trace["agent_actions"] == []
