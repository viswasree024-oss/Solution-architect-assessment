"""Task 1 - MCP server (stdio) with strict validation.

RULES THAT MATTER:
  * stdout is ONLY for JSON-RPC. Never use print(). All logs go to stderr.
  * Invalid input -> JSON-RPC error -32602 (Invalid params), not a "successful" reply.
"""
import asyncio
import json
import logging
import sys
from typing import Annotated

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# --- logging: stderr ONLY (stdout is the protocol channel) -------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("customer-mcp")

# --- input schemas -------------------------------------------------------------
CustomerId = Annotated[str, Field(pattern=r"^CUST-\d{5}$", description="Format CUST-XXXXX (5 digits)")]


class GetCustomerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # unknown fields / wrong types rejected
    customer_id: CustomerId


class RefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: CustomerId
    amount: float = Field(gt=0, allow_inf_nan=False, description="Positive amount")
    reason: str = Field(min_length=10, description="At least 10 characters")

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        if len(v.strip()) < 10:  # "          " must not pass
            raise ValueError("reason must contain at least 10 non-whitespace-padded characters")
        return v.strip()


TOOLS = {
    "get_customer_record": (GetCustomerArgs, "Fetch a customer record by customer_id (CUST-XXXXX)."),
    "trigger_refund": (RefundArgs, "Issue a refund. amount > 0, reason >= 10 chars."),
}

# --- fake data store -------------------------------------------------------------
CUSTOMERS = {
    "CUST-00001": {"customer_id": "CUST-00001", "name": "Asha Rao", "tier": "gold"},
    "CUST-00002": {"customer_id": "CUST-00002", "name": "Li Wei", "tier": "silver"},
}

server = Server("customer-support-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=n, description=d, inputSchema=model.model_json_schema())
        for n, (model, d) in TOOLS.items()
    ]


def _invalid(msg: str) -> McpError:
    return McpError(types.ErrorData(code=types.INVALID_PARAMS, message=msg))


async def handle_call_tool(req: types.CallToolRequest) -> types.ServerResult:
    """Registered directly (instead of @server.call_tool) so protocol-level problems
    surface as real JSON-RPC errors. The @call_tool decorator would turn every exception
    into a 'successful' response with isError=true."""
    name = req.params.name
    if name not in TOOLS:
        raise _invalid(f"Unknown tool: {name}")  # spec: unknown tool -> -32602
    model, _ = TOOLS[name]
    try:
        args = model.model_validate(req.params.arguments or {})
    except ValidationError as e:
        # Report field + reason only (no input echo => no accidental data leak)
        problems = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'arguments'}: {x['msg']}" for x in e.errors())
        log.warning("validation failed for %s: %s", name, problems)
        raise _invalid(f"Invalid arguments for {name}: {problems}") from None

    if name == "get_customer_record":
        rec = CUSTOMERS.get(args.customer_id)
        if rec is None:  # business error: tool ran, result says so (isError), NOT a protocol error
            return _result({"error": f"{args.customer_id} not found"}, is_error=True)
        return _result(rec)

    # trigger_refund
    if args.customer_id not in CUSTOMERS:
        return _result({"error": f"{args.customer_id} not found"}, is_error=True)
    log.info("refund %.2f for %s", args.amount, args.customer_id)
    return _result({"status": "refund_queued", "customer_id": args.customer_id,
                    "amount": round(args.amount, 2), "reason": args.reason})


def _result(payload: dict, is_error: bool = False) -> types.ServerResult:
    return types.ServerResult(types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))], isError=is_error))


server.request_handlers[types.CallToolRequest] = handle_call_tool


async def main() -> None:
    async with stdio_server() as (read, write):
        log.info("server starting on stdio")
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
