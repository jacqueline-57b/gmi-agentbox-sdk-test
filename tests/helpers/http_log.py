"""Transport wrappers that print every request and response.

The SDK has no logging of its own, but `AgentBoxClient` accepts `transport`
and `stream_transport` callables, so a live client can be handed a wrapper
that narrates the traffic on its way through.

Enabled with `--log-http`; see tests/conftest.py. Output goes to stdout,
so pytest needs `-s` (or `--capture=no`) for it to reach the terminal.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterator, Mapping, Optional

# Headers whose value is a credential and must never reach a terminal or a
# CI log. Compared case-insensitively.
_SECRET_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "cookie"})

# Bodies longer than this are cut short; a full payload dump buries the one
# field you are usually looking for.
_MAX_BODY_CHARS = 2000


def _redact(name: str, value: str) -> str:
    if name.lower() not in _SECRET_HEADERS:
        return value
    tail = value[-4:] if len(value) > 4 else ""
    return f"<redacted, {len(value)} chars, ends …{tail}>"


def _render_body(body: Optional[bytes]) -> str:
    """Pretty-print JSON, fall back to text, then to a byte count."""
    if not body:
        return "<empty>"
    try:
        text = json.dumps(json.loads(body.decode("utf-8")), indent=2, ensure_ascii=False)
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return f"<{len(body)} bytes, not utf-8>"
    if len(text) > _MAX_BODY_CHARS:
        return f"{text[:_MAX_BODY_CHARS]}\n… truncated, {len(text)} chars total"
    return text


def _indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in text.splitlines()) or prefix + "<empty>"


def _log_request(sink: Callable[[str], None], method: str, url: str,
                 headers: Mapping[str, str], body: Optional[bytes]) -> None:
    sink(f"\n  ──▶ {method} {url}")
    for name, value in headers.items():
        sink(f"      {name}: {_redact(name, value)}")
    if body:
        sink("      ── request body ──")
        sink(_indent(_render_body(body)))


def logging_transport(inner: Callable[..., Any], sink: Callable[[str], None]) -> Callable[..., Any]:
    """Wrap a unary transport so each call narrates itself."""

    def transport(method: str, url: str, headers: Mapping[str, str],
                  body: Optional[bytes], timeout: float) -> Any:
        _log_request(sink, method, url, headers, body)
        started = time.monotonic()
        try:
            response = inner(method, url, headers, body, timeout)
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000
            sink(f"  ◀── transport raised {type(exc).__name__} after {elapsed:.0f}ms: {exc}")
            raise
        elapsed = (time.monotonic() - started) * 1000
        sink(f"  ◀── {response.status_code} in {elapsed:.0f}ms")
        sink(_indent(_render_body(response.body)))
        return response

    return transport


def logging_stream_transport(inner: Callable[..., Any], sink: Callable[[str], None]) -> Callable[..., Any]:
    """Wrap a streaming transport, narrating the handshake and every line."""

    def stream_transport(method: str, url: str, headers: Mapping[str, str],
                         body: Optional[bytes], timeout: Optional[float]) -> Any:
        _log_request(sink, method, url, headers, body)
        started = time.monotonic()
        stream = inner(method, url, headers, body, timeout)
        elapsed = (time.monotonic() - started) * 1000
        sink(f"  ◀── {stream.status_code} in {elapsed:.0f}ms, streaming")

        inner_lines = stream.lines

        def narrated_lines() -> Iterator[bytes]:
            for index, line in enumerate(inner_lines):
                sink(f"      [{index}] {line.decode('utf-8', 'replace').rstrip()}")
                yield line
            sink("      ── stream closed ──")

        stream.lines = narrated_lines()
        return stream

    return stream_transport
