"""Task 3 - LLM gateway endpoint that streams (SSE) and redacts PII in flight.

Run:  uvicorn app:app --port 8001
Try:  curl -N -X POST localhost:8001/v1/stream -H 'content-type: application/json' -d '{"prompt":"hi"}'
"""
import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from redactor import StreamRedactor

app = FastAPI(title="LLM Gateway - PII guardrail")


class Req(BaseModel):
    prompt: str


async def mock_llm(prompt: str) -> AsyncIterator[str]:
    """Stand-in for a real provider. Emits tiny deltas, deliberately splitting PII mid-token.
    Replace with an httpx streaming call to OpenAI/Anthropic/etc. - same interface: async iterator of str."""
    text = ("Sure! Contact john.doe@example.com or call billing. Your SSN 123-45-6789 and card "
            "4111 1111 1111 1111 are on file. Order 12345 ships Monday. Have a great day!")
    for i in range(0, len(text), 5):
        await asyncio.sleep(0.01)  # simulate network
        yield text[i:i + 5]


async def guarded_stream(prompt: str) -> AsyncIterator[bytes]:
    red = StreamRedactor()
    async for delta in mock_llm(prompt):
        safe = red.feed(delta)
        if safe:  # emit as soon as it's provably safe
            yield f"data: {json.dumps({'delta': safe})}\n\n".encode()
    tail = red.flush()
    if tail:
        yield f"data: {json.dumps({'delta': tail})}\n\n".encode()
    yield b"data: [DONE]\n\n"


@app.post("/v1/stream")
async def stream(req: Req):
    return StreamingResponse(guarded_stream(req.prompt), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
