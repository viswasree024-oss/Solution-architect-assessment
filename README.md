# Solution Architect Assessment - Viswasree M

Four tasks, all in Python 3.10+. Every task has automated tests (45 total, all passing).

| Task | Folder | What it is | Run tests |
|---|---|---|---|
| 1 | `task1_mcp_server` | MCP server over stdio, Pydantic validation, JSON-RPC `-32602` errors, stdout isolation | `pytest` |
| 2 | `task2_mcp_gateway` | MCP gateway proxy: JWT role auth, `admin_*` tool blocking (`-32001`) | `pytest` |
| 3 | `task3_llm_stream_guardrail` | Streaming SSE gateway with PII redaction (email/SSN/Luhn-checked cards) | `pytest` |
| 4 | `task4_router_fallback` | Token sliding-window limiter (SQLite on disk) + 429/timeout fallback + sanitized errors | `pytest` |

## Setup
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run
```bash
# all tests (from repo root)
cd task1_mcp_server && pytest -p anyio && cd ..
cd task2_mcp_gateway && pytest && cd ..
cd task3_llm_stream_guardrail && pytest && cd ..
cd task4_router_fallback && pytest && python demo.py && cd ..
```
Task 2 live: `uvicorn mock_downstream:app --port 9000` + `uvicorn gateway:app --port 8000`, then `python make_token.py viewer`.
Task 3 live: `uvicorn app:app --port 8001` then `curl -N -X POST localhost:8001/v1/stream -H "content-type: application/json" -d '{"prompt":"hi"}'`.

## Design decisions

**Task 1** - Registered the `tools/call` handler directly because the SDK's `@call_tool` decorator converts every exception into a
"successful" response with `isError=true`; the assessment wants real JSON-RPC errors. Invalid/unknown input -> `-32602`.
Business errors (customer not found) are tool results with `isError`, not protocol errors. Logging is configured to stderr;
a subprocess test asserts every stdout line is valid JSON-RPC. Validation: strict types (no `"5"` as float), regex anchored with
`\d` + pydantic (rejects trailing newline trick), `NaN/inf` rejected, whitespace-only reasons rejected, extra fields forbidden.

**Task 2** - JWT (HS256, algorithm pinned, expiry checked) rather than trusting an unsigned role string. No/invalid token -> HTTP 401.
Unauthorized `admin_*` call -> JSON-RPC `-32001` with the caller's request id, downstream never contacted (tests assert this).
Prefix check is case-insensitive to block `ADMIN_x` bypasses. Notifications get 202, batches are rejected (not in current MCP spec).
Limitation: forwards buffered JSON responses; SSE streaming passthrough would be the next step.

**Task 3** - Rolling-buffer redactor: emits everything except the last 48 chars, moves the cut point if a match straddles it.
Memory is O(holdback + chunk), TTFT penalty is at most 48 chars. Property-style tests prove streamed output == whole-text output for any chunking.
Credit cards use a Luhn check so order numbers are not falsely redacted. Trade-off: an email local-part >48 chars could leak a prefix (tunable).

**Task 4** - `BEGIN IMMEDIATE` transaction makes check-and-reserve atomic, so concurrent requests cannot exceed the limit (test: 20 parallel
requests, exactly 9 admitted). Estimate is reserved up front, replaced by real token usage after the response, and refunded on failure.
SQLite calls run in `asyncio.to_thread` so the event loop is never blocked. Primary has a 3000 ms `asyncio.wait_for`; 429, timeout or
upstream error -> fallback. Clients only ever see `{error:{code,message,request_id}}`; raw errors are logged server-side.
