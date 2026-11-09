"""Quick visual demo: python demo.py"""
import asyncio, tempfile, os, logging
from router import *
logging.basicConfig(level=logging.WARNING)

class P:
    def __init__(s, n, mode): s.name, s.mode = n, mode
    async def complete(s, r):
        if s.mode == "429": raise UpstreamRateLimited("vendor 429")
        if s.mode == "slow": await asyncio.sleep(5)
        return CompletionResult(f"answer from {s.name}", 120, s.name)

async def main():
    db = os.path.join(tempfile.mkdtemp(), "demo.db")
    lim = SlidingWindowLimiter(db, limit=2000)
    for label, prim in [("primary healthy", "ok"), ("primary 429", "429"), ("primary slow (>3s)", "slow")]:
        r = ModelRouter(P("primary", prim), P("backup", "ok"), lim)
        res = await r.complete(CompletionRequest("tenant-1", "Hello there", 200))
        print(f"{label:22} -> {res.provider}")
    try:
        await ModelRouter(P("p", "ok"), P("b", "ok"), lim).complete(CompletionRequest("tenant-1", "x", 5000))
    except GatewayError as e:
        print("limit exceeded ->", e.status, e.payload())
asyncio.run(main())
