"""Streaming PII redactor.

PROBLEM: a secret can be split across chunks ("4111 11" + "11 1111 1111"), so redacting
each chunk alone misses it. Buffering the whole response kills latency + memory.

SOLUTION: keep a small rolling buffer. After each chunk:
  * everything except the last HOLDBACK chars is "safe" (no pattern can still be growing there)
  * emit the safe part (with matches replaced), keep the tail for the next chunk
  * if a match straddles the safe/tail boundary, move the boundary to the match start
Memory is bounded by HOLDBACK + chunk size. Latency cost is at most HOLDBACK chars.
Trade-off: an email whose local-part is longer than HOLDBACK could leak its prefix.
"""
import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")  # 13-19 digits, optional space/dash


def _luhn(s: str) -> bool:
    d = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, x in enumerate(reversed(d)):
        if i % 2:
            x *= 2
            x -= 9 if x > 9 else 0
        total += x
    return total % 10 == 0


PATTERNS = [(EMAIL, None), (SSN, None), (CARD, _luhn)]  # (regex, optional validator)


def _spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for rx, check in PATTERNS:
        for m in rx.finditer(text):
            if check is None or check(m.group()):
                spans.append(m.span())
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:                       # merge overlaps
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def redact(text: str, token: str = "[REDACTED]") -> str:
    """Non-streaming reference implementation (also used to test the streaming one)."""
    out, pos = [], 0
    for s, e in _spans(text):
        out += [text[pos:s], token]
        pos = e
    out.append(text[pos:])
    return "".join(out)


class StreamRedactor:
    def __init__(self, holdback: int = 48, token: str = "[REDACTED]"):
        self.holdback, self.token, self.buf = holdback, token, ""

    def feed(self, chunk: str) -> str:
        """Add a chunk; return text that is safe to send to the client now."""
        self.buf += chunk
        cut = len(self.buf) - self.holdback
        if cut <= 0:
            return ""
        spans = _spans(self.buf)
        for s, e in spans:
            if s < cut < e:       # match straddles boundary -> wait until it's complete
                cut = s
                break
        out, pos = [], 0
        for s, e in spans:
            if e <= cut:
                out += [self.buf[pos:s], self.token]
                pos = e
        out.append(self.buf[pos:cut])
        self.buf = self.buf[cut:]
        return "".join(out)

    def flush(self) -> str:
        """End of stream: redact and release whatever is left."""
        rest, self.buf = redact(self.buf, self.token), ""
        return rest
