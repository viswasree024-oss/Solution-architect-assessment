"""Task 2 - MCP security gateway: JWT auth + method/tool-level authorization.

  client --(Bearer JWT, JSON-RPC)--> [gateway] --(JSON-RPC)--> downstream MCP server

Run:  uvicorn gateway:app --port 8000     (downstream at DOWNSTREAM_URL)
"""
import json
import logging
import os

import httpx
import jwt  # PyJWT
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("mcp-gateway")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me-dev-secret-change-me")
DOWNSTREAM_URL = os.getenv("DOWNSTREAM_URL", "http://127.0.0.1:9000/mcp")
ADMIN_PREFIX = "admin_"

# JSON-RPC codes
PARSE_ERROR, INVALID_REQUEST = -32700, -32600
UNAUTHORIZED_TOOL_CALL = -32001
# headers worth passing through for MCP over HTTP
FORWARD_REQ_HEADERS = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version"}
FORWARD_RESP_HEADERS = {"content-type", "mcp-session-id"}


def rpc_error(rid, code: int, message: str, http_status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
                        status_code=http_status)


def role_from_header(auth: str | None) -> str | None:
    """Return role from 'Bearer <jwt>' or None if missing/invalid/expired."""
    if not auth or not auth.lower().startswith("bearer "):
        return None
    try:
        claims = jwt.decode(auth[7:].strip(), JWT_SECRET, algorithms=["HS256"])  # algorithm pinned
    except jwt.PyJWTError:
        return None
    role = claims.get("role")
    return role if isinstance(role, str) else None


def create_app(downstream: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="MCP Gateway")
    client = downstream or httpx.AsyncClient(timeout=30)

    @app.post("/mcp")
    async def gateway(request: Request):
        # 1. Authenticate (HTTP-level: no/bad token -> 401)
        role = role_from_header(request.headers.get("authorization"))
        if role is None:
            return JSONResponse({"error": "missing or invalid bearer token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})

        # 2. Parse JSON-RPC envelope
        raw = await request.body()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return rpc_error(None, PARSE_ERROR, "Parse error", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            # (batches are not part of current MCP spec -> rejected)
            return rpc_error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "Invalid Request", 400)

        # 3. Method-level authorization
        if msg["method"] == "tools/call":
            params = msg.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if not isinstance(name, str):
                return rpc_error(msg.get("id"), -32602, "Invalid params: 'name' must be a string")
            if name.strip().lower().startswith(ADMIN_PREFIX) and role != "admin":
                log.warning("blocked role=%s tool=%s", role, name)
                if "id" not in msg:   # notification: nobody to answer
                    return Response(status_code=202)
                return rpc_error(msg["id"], UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call")
        # tools/list and everything else: forward transparently

        # 4. Forward to downstream, return its response as-is
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQ_HEADERS}
        try:
            up = await client.post(DOWNSTREAM_URL, content=raw, headers=headers)
        except httpx.HTTPError:
            log.exception("downstream failure")  # details only in server logs
            return rpc_error(msg.get("id"), -32603, "Downstream server unavailable", 502)
        out_headers = {k: v for k, v in up.headers.items() if k.lower() in FORWARD_RESP_HEADERS}
        return Response(content=up.content, status_code=up.status_code, headers=out_headers)

    return app


app = create_app()
