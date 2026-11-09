"""Runs the server as a real subprocess and checks: stdout purity + error codes."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

SERVER = str(Path(__file__).with_name("server.py"))
PARAMS = StdioServerParameters(command=sys.executable, args=[SERVER])


async def call(tool, args):
    """Returns the tool result, or the McpError (caught INSIDE the session so the
    SDK's task group does not wrap it in an ExceptionGroup)."""
    async with stdio_client(PARAMS) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            try:
                return await s.call_tool(tool, args)
            except McpError as e:
                return e


@pytest.mark.anyio
async def test_valid_get():
    res = await call("get_customer_record", {"customer_id": "CUST-00001"})
    assert not res.isError and "Asha" in res.content[0].text


@pytest.mark.anyio
async def test_valid_refund():
    res = await call("trigger_refund", {"customer_id": "CUST-00001", "amount": 10.5, "reason": "Item arrived damaged"})
    assert "refund_queued" in res.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("tool,args", [
    ("get_customer_record", {"customer_id": "cust-00001"}),
    ("get_customer_record", {"customer_id": "CUST-1234"}),
    ("get_customer_record", {"customer_id": "CUST-12345\n"}),   # trailing newline trick
    ("get_customer_record", {"customer_id": 12345}),
    ("get_customer_record", {}),
    ("get_customer_record", {"customer_id": "CUST-00001", "extra": 1}),
    ("trigger_refund", {"customer_id": "CUST-00001", "amount": 0, "reason": "long enough reason"}),
    ("trigger_refund", {"customer_id": "CUST-00001", "amount": -5, "reason": "long enough reason"}),
    ("trigger_refund", {"customer_id": "CUST-00001", "amount": "5", "reason": "long enough reason"}),
    ("trigger_refund", {"customer_id": "CUST-00001", "amount": float("inf"), "reason": "long enough reason"}),
    ("trigger_refund", {"customer_id": "CUST-00001", "amount": 5, "reason": "short"}),
    ("trigger_refund", {"customer_id": "CUST-00001", "amount": 5, "reason": "          "}),
    ("nonexistent_tool", {}),
])
async def test_invalid_inputs_return_jsonrpc_invalid_params(tool, args):
    err = await call(tool, args)
    assert isinstance(err, McpError), "expected a JSON-RPC error"
    assert err.error.code == -32602


def test_stdout_is_pure_jsonrpc():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_customer_record", "arguments": {"customer_id": "bad"}}},
    ]
    # Keep stdin open until the last reply arrives (closing early would cancel in-flight work).
    p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    p.stdin.write("\n".join(map(json.dumps, msgs)) + "\n")
    p.stdin.flush()
    lines = []
    while True:
        line = p.stdout.readline()
        if not line:
            break
        if line.strip():
            lines.append(line)
            if json.loads(line).get("id") == 3:
                break
    p.stdin.close()
    err = p.stderr.read()
    p.wait(timeout=10)
    assert lines, "no output"
    for l in lines:
        assert json.loads(l)["jsonrpc"] == "2.0", f"non JSON-RPC on stdout: {l!r}"
    assert "server starting" in err          # logs went to stderr
    assert any(json.loads(l).get("error", {}).get("code") == -32602 for l in lines)
