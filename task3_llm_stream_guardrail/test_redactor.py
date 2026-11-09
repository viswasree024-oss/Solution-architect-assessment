import random

import pytest

from redactor import StreamRedactor, redact

SAMPLE = ("Mail me at john.doe@example.com, SSN 123-45-6789, card 4111 1111 1111 1111 "
          "and 4111-1111-1111-1111 also 4111111111111111. Order 12345 and phone 5551234 fine. "
          "Invalid card 4111 1111 1111 1112 stays. x@y.io end")


def stream_it(text, sizes):
    r, out, i = StreamRedactor(), [], 0
    while i < len(text):
        n = random.choice(sizes)
        out.append(r.feed(text[i:i + n]))
        i += n
    out.append(r.flush())
    return "".join(out)


def test_reference():
    out = redact(SAMPLE)
    assert "john.doe" not in out and "123-45-6789" not in out and out.count("4111") == 1
    assert "Order 12345" in out and "1112" in out  # Luhn-invalid number not redacted


@pytest.mark.parametrize("sizes", [[1], [2], [3, 7], [5], [1, 50], [100]])
def test_streaming_equals_reference_for_any_chunking(sizes):
    random.seed(1)
    for _ in range(50):
        assert stream_it(SAMPLE, sizes) == redact(SAMPLE)


def test_memory_bounded_and_incremental():
    r = StreamRedactor(holdback=48)
    emitted, peak = 0, 0
    for _ in range(10_000):
        emitted += len(r.feed("lorem ipsum "))
        peak = max(peak, len(r.buf))
    assert peak <= 48 + 12 and emitted > 100_000  # buffer stays tiny; data flows during the stream


def test_first_text_released_early():
    r = StreamRedactor(holdback=48)
    assert r.feed("x" * 100) == "x" * 52  # TTFT: no need to wait for the end
