import time

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

import mock_downstream
from gateway import JWT_SECRET, create_app


def tok(role, exp=3600, secret=JWT_SECRET):
    return jwt.encode({"role": role, "exp": int(time.time()) + exp}, secret, algorithm="HS256")


@pytest.fixture
def setup():
    mock_downstream.app.state.calls.clear()
    down = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_downstream.app), base_url="http://mock")
    import gateway
    gateway.DOWNSTREAM_URL = "http://mock/mcp"
    return TestClient(create_app(down)), mock_downstream.app.state.calls


def rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, **({"params": params} if params else {})}


def post(c, body, token=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post("/mcp", json=body, headers=h)


def test_tools_list_forwarded_for_viewer(setup):
    c, calls = setup
    r = post(c, rpc("tools/list"), tok("viewer"))
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 2 and len(calls) == 1


def test_viewer_blocked_from_admin_tool_without_downstream_call(setup):
    c, calls = setup
    r = post(c, rpc("tools/call", {"name": "admin_reset_key", "arguments": {}}, id=7), tok("viewer"))
    assert r.json() == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32001, "message": "Unauthorized Tool Call"}}
    assert calls == []  # never reached downstream


def test_admin_allowed(setup):
    c, calls = setup
    r = post(c, rpc("tools/call", {"name": "admin_reset_key"}), tok("admin"))
    assert "ran admin_reset_key" in r.text and len(calls) == 1


def test_viewer_normal_tool_allowed(setup):
    c, calls = setup
    assert "ran get_status" in post(c, rpc("tools/call", {"name": "get_status"}), tok("viewer")).text


def test_case_trick_blocked(setup):
    c, calls = setup
    r = post(c, rpc("tools/call", {"name": "ADMIN_reset_key"}), tok("viewer"))
    assert r.json()["error"]["code"] == -32001 and calls == []


@pytest.mark.parametrize("token", [None, "garbage", tok("admin", secret="x" * 40), tok("admin", exp=-10)])
def test_bad_tokens_401(setup, token):
    c, calls = setup
    assert post(c, rpc("tools/list"), token).status_code == 401 and calls == []


def test_malformed_json(setup):
    c, _ = setup
    r = c.post("/mcp", content=b"{not json", headers={"Authorization": f"Bearer {tok('admin')}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


def test_bad_params_type(setup):
    c, calls = setup
    r = post(c, rpc("tools/call", {"name": 123}), tok("admin"))
    assert r.json()["error"]["code"] == -32602 and calls == []
