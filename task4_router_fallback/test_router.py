import asyncio
import json

import pytest

from router import (CompletionRequest, CompletionResult, GatewayError, ModelRouter, SlidingWindowLimiter,
                    UpstreamError, UpstreamRateLimited)


class Fake:
    def __init__(self, name, mode="ok", delay=0.0, tokens=100):
        self.name, self.mode, self.delay, self.tokens, self.calls = name, mode, delay, tokens, 0

    async def complete(self, req):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.mode == "429":
            raise UpstreamRateLimited("429 from vendor, key=sk-SECRET")
        if self.mode == "boom":
            raise UpstreamError("Traceback (most recent call last): File /srv/internal/x.py sk-SECRET")
        return CompletionResult("hi", self.tokens, self.name)


class Clock:
    t = 1000.0
    def __call__(self): return self.t


def make(tmp_path, primary, fallback, limit=50_000, clock=None, **kw):
    lim = SlidingWindowLimiter(str(tmp_path / "rl.db"), limit=limit, clock=clock or Clock())
    return ModelRouter(primary, fallback, lim, **kw), lim


def req(key="k1", prompt="hello", max_tokens=100):
    return CompletionRequest(key, prompt, max_tokens)


def run(c): return asyncio.run(c)


def test_primary_used_normally(tmp_path):
    p, f = Fake("p"), Fake("f")
    r, _ = make(tmp_path, p, f)
    assert run(r.complete(req())).provider == "p" and f.calls == 0


def test_fallback_on_429(tmp_path):
    p, f = Fake("p", "429"), Fake("f")
    r, _ = make(tmp_path, p, f)
    assert run(r.complete(req())).provider == "f"


def test_fallback_on_timeout(tmp_path):
    p, f = Fake("p", delay=1.0), Fake("f")
    r, _ = make(tmp_path, p, f, primary_timeout_s=0.1)  # 0.1s instead of 3s to keep tests fast
    assert run(r.complete(req())).provider == "f"


def test_both_fail_sanitized(tmp_path):
    r, _ = make(tmp_path, Fake("p", "boom"), Fake("f", "boom"))
    with pytest.raises(GatewayError) as e:
        run(r.complete(req()))
    body = json.dumps(e.value.payload())
    assert e.value.status == 503
    for leak in ("Traceback", "SECRET", "internal", ".py"):
        assert leak not in body


def test_rate_limit_blocks_and_evicts(tmp_path):
    clk = Clock()
    r, lim = make(tmp_path, Fake("p", tokens=900), Fake("f"), limit=1000, clock=clk)
    big = req(max_tokens=900, prompt="x" * 40)           # estimate ~910
    run(r.complete(big))
    with pytest.raises(GatewayError) as e:
        run(r.complete(big))
    assert e.value.status == 429 and e.value.payload()["error"]["retry_after_seconds"] > 0
    clk.t += 61                                           # window slides past the first request
    assert run(r.complete(big)).provider == "p"


def test_actual_tokens_replace_estimate(tmp_path):
    r, lim = make(tmp_path, Fake("p", tokens=42), Fake("f"))
    run(r.complete(req()))
    assert run(lim.used("k1")) == 42


def test_failed_request_not_charged(tmp_path):
    r, lim = make(tmp_path, Fake("p", "boom"), Fake("f", "boom"))
    with pytest.raises(GatewayError):
        run(r.complete(req()))
    assert run(lim.used("k1")) == 0


def test_tenants_isolated(tmp_path):
    r, _ = make(tmp_path, Fake("p", tokens=0), Fake("f"), limit=1000)
    run(r.complete(req("a", max_tokens=900)))
    assert run(r.complete(req("b", max_tokens=900))).provider == "p"


def test_concurrency_never_overshoots(tmp_path):
    """20 simultaneous requests of ~5000 est. tokens, limit 50000 -> exactly 10 admitted."""
    r, _ = make(tmp_path, Fake("p", delay=0.05, tokens=5000), Fake("f"))

    async def go():
        rs = await asyncio.gather(*[r.complete(req(max_tokens=5000, prompt="x" * 4)) for _ in range(20)],
                                  return_exceptions=True)
        return rs
    rs = run(go())
    ok = [x for x in rs if isinstance(x, CompletionResult)]
    limited = [x for x in rs if isinstance(x, GatewayError) and x.status == 429]
    assert len(ok) == 9 and len(limited) == 11  # est=5001 each -> 9 fit in 50000
