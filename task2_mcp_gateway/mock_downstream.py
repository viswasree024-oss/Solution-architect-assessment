"""A fake downstream MCP server over HTTP (JSON-RPC). Records every call it receives."""
from fastapi import FastAPI, Request

app = FastAPI()
app.state.calls = []  # tests use this to prove the gateway did / did not forward

TOOLS = [
    {"name": "get_status", "description": "Public status", "inputSchema": {"type": "object"}},
    {"name": "admin_reset_key", "description": "Reset API key", "inputSchema": {"type": "object"}},
]


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    app.state.calls.append(body)
    rid, method = body.get("id"), body.get("method")
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = body["params"]["name"]
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": f"ran {name}"}], "isError": False}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
