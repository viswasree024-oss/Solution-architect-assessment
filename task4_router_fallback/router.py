"""Task 4 - Rate limiting + model fallback router for an LLM gateway.

* Token-aware sliding-window limiter, stored in an on-disk SQLite file (WAL mode).
  Each request writes (api_key, timestamp, tokens). Window usage = SUM(tokens) over the last 60s.
  Reservation is atomic (BEGIN IMMEDIATE) so concurrent requests cannot overshoot the limit.
* Primary provider with 3000 ms timeout; on 429 / timeout / upstream failure -> fallback provider.
* Errors returned to clients are a fixed, sanitized payload; raw details only go to server logs.
"""
import asyncio
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger("llm-router")


# ---------------------------------------------------------------- errors
class UpstreamRateLimited(Exception):
    """Provider answered 429."""


class UpstreamError(Exception):
    """Any other provider failure (5xx, connection error...). Message is NEVER shown to clients."""


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after
        self.request_id = uuid.uuid4().hex[:12]

    def payload(self) -> dict:
        err = {"code": self.code, "message": self.message, "request_id": self.request_id}
        if self.retry_after is not None:
            err["retry_after_seconds"] = self.retry_after
        return {"error": err}


# ---------------------------------------------------------------- providers
@dataclass
class CompletionRequest:
    api_key: str
    prompt: str
    max_tokens: int = 256


@dataclass
class CompletionResult:
    text: str
    tokens_used: int
    provider: str


class Provider(Protocol):
    name: str
    async def complete(self, req: CompletionRequest) -> CompletionResult: ...


# ---------------------------------------------------------------- limiter
class SlidingWindowLimiter:
    def __init__(self, db_path: str, limit: int = 50_000, window_s: int = 60,
                 clock: Callable[[], float] = time.time):
        self.limit, self.window, self.clock = limit, window_s, clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS usage("
                         "id INTEGER PRIMARY KEY AUTOINCREMENT, api_key TEXT NOT NULL, "
                         "ts REAL NOT NULL, tokens INTEGER NOT NULL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_usage ON usage(api_key, ts)")

    # --- sync internals (run in a worker thread so the event loop never blocks) ---
    def _reserve(self, key: str, tokens: int):
        with self._lock:
            now = self.clock()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM usage WHERE ts <= ?", (now - self.window,))  # eviction (all tenants)
                rows = self._db.execute("SELECT ts, tokens FROM usage WHERE api_key=? ORDER BY ts", (key,)).fetchall()
                used = sum(t for _, t in rows)
                if used + tokens > self.limit:
                    # when will enough old tokens expire for this request to fit?
                    need, retry = used + tokens - self.limit, self.window
                    freed = 0
                    for ts, t in rows:
                        freed += t
                        if freed >= need:
                            retry = max(1, int(ts + self.window - now) + 1)
                            break
                    self._db.execute("COMMIT")
                    return None, retry
                cur = self._db.execute("INSERT INTO usage(api_key, ts, tokens) VALUES(?,?,?)", (key, now, tokens))
                self._db.execute("COMMIT")
                return cur.lastrowid, 0
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _adjust(self, row_id: int, tokens: int):
        with self._lock:
            self._db.execute("UPDATE usage SET tokens=? WHERE id=?", (tokens, row_id))

    def _used(self, key: str) -> int:
        with self._lock:
            now = self.clock()
            r = self._db.execute("SELECT COALESCE(SUM(tokens),0) FROM usage WHERE api_key=? AND ts>?",
                                 (key, now - self.window)).fetchone()
            return r[0]

    # --- async API ---
    async def reserve(self, key: str, tokens: int):
        return await asyncio.to_thread(self._reserve, key, tokens)

    async def adjust(self, row_id: int, tokens: int):
        await asyncio.to_thread(self._adjust, row_id, tokens)

    async def used(self, key: str) -> int:
        return await asyncio.to_thread(self._used, key)


# ---------------------------------------------------------------- router
def estimate_tokens(req: CompletionRequest) -> int:
    return max(1, len(req.prompt) // 4) + req.max_tokens  # ~4 chars/token + worst-case output


class ModelRouter:
    def __init__(self, primary: Provider, fallback: Provider, limiter: SlidingWindowLimiter,
                 primary_timeout_s: float = 3.0, fallback_timeout_s: float = 10.0):
        self.primary, self.fallback, self.limiter = primary, fallback, limiter
        self.t_primary, self.t_fallback = primary_timeout_s, fallback_timeout_s

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        est = estimate_tokens(req)
        row_id, retry = await self.limiter.reserve(req.api_key, est)  # atomic check+reserve
        if row_id is None:
            raise GatewayError(429, "rate_limit_exceeded", "Token rate limit exceeded for this API key.", retry)
        try:
            try:
                res = await asyncio.wait_for(self.primary.complete(req), self.t_primary)
            except (UpstreamRateLimited, asyncio.TimeoutError, UpstreamError) as e:
                log.warning("primary failed (%s: %s) -> fallback", type(e).__name__, e)
                res = await asyncio.wait_for(self.fallback.complete(req), self.t_fallback)
            await self.limiter.adjust(row_id, res.tokens_used)  # replace estimate with real usage
            return res
        except Exception as e:
            await self.limiter.adjust(row_id, 0)  # failed request shouldn't burn the tenant's quota
            if isinstance(e, GatewayError):
                raise
            log.error("all providers failed: %r", e, exc_info=True)  # raw detail stays in logs
            raise GatewayError(503, "upstream_unavailable",
                               "The model service is temporarily unavailable. Please retry.") from None
