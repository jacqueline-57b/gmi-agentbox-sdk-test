"""In-process doubles for the transports AgentBoxClient accepts.

The SDK takes `transport`, `stream_transport`, `shell_transport` and `sleep`
callables in its constructor, so unit tests can exercise the real client code
end to end without a socket, a mock library, or a local HTTP server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Iterable, Iterator, List, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

# Every SDK call is issued under this prefix; strip it so assertions can talk
# about "/deployments" instead of the full URL.
SERVICE_PATH = "/api/v1/ie/container"


@dataclass(frozen=True)
class RecordedRequest:
    """One outbound request captured by :class:`FakeTransport`."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: Optional[bytes]
    timeout: Optional[float]

    @property
    def path(self) -> str:
        path = urlsplit(self.url).path
        if path.startswith(SERVICE_PATH):
            return path[len(SERVICE_PATH) :]
        return path

    @property
    def query(self) -> dict:
        """Query string as {name: [values]} — repeated keys are preserved."""
        return parse_qs(urlsplit(self.url).query)

    @property
    def json_body(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def __repr__(self) -> str:  # pragma: no cover - failure output only
        return f"<{self.method} {self.path}{'?' + urlsplit(self.url).query if urlsplit(self.url).query else ''}>"


@dataclass
class FakeResponse:
    """Duck-types the SDK's internal RawResponse."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass
class FakeStreamResponse:
    """Duck-types the SDK's internal RawStream."""

    status_code: int
    headers: Mapping[str, str]
    lines: Iterator[bytes]


@dataclass
class _Rule:
    method: str
    path_glob: str
    status: int
    body: bytes
    headers: dict
    remaining: float

    def matches(self, request: RecordedRequest) -> bool:
        return (
            self.remaining > 0
            and self.method == request.method.upper()
            and fnmatch(request.path, self.path_glob)
        )


class FakeTransport:
    """A scripted HTTP transport that records what the SDK sent.

    Stub responses in the order they should be consumed. Stubbing the same
    route twice lets a polling test return different payloads per call::

        transport.stub("GET", "/tasks/*", json_body={"task_status": "creating"})
        transport.stub("GET", "/tasks/*", json_body={"task_status": "running"})
    """

    def __init__(self) -> None:
        self.requests: List[RecordedRequest] = []
        self._rules: List[_Rule] = []

    def stub(
        self,
        method: str,
        path_glob: str,
        *,
        status: int = 200,
        json_body: Any = None,
        body: bytes = b"",
        headers: Optional[Mapping[str, str]] = None,
        times: int = 1,
        repeat: bool = False,
    ) -> "FakeTransport":
        """Queue one response. `repeat=True` serves it for every later match."""
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        self._rules.append(
            _Rule(
                method=method.upper(),
                path_glob=path_glob,
                status=status,
                body=body,
                headers=dict(headers or {}),
                remaining=float("inf") if repeat else times,
            )
        )
        return self

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: Optional[float],
    ) -> FakeResponse:
        request = RecordedRequest(method, url, dict(headers), body, timeout)
        self.requests.append(request)
        for rule in self._rules:
            if rule.matches(request):
                rule.remaining -= 1
                return FakeResponse(rule.status, rule.headers, rule.body)
        raise AssertionError(
            f"unstubbed request {request!r}; stubs were: "
            + ", ".join(f"{r.method} {r.path_glob} (x{r.remaining} left)" for r in self._rules)
        )

    @property
    def last_request(self) -> RecordedRequest:
        assert self.requests, "no request was made"
        return self.requests[-1]

    def requests_for(self, method: str, path_glob: str) -> List[RecordedRequest]:
        return [
            request
            for request in self.requests
            if request.method.upper() == method.upper() and fnmatch(request.path, path_glob)
        ]


class FakeStreamTransport:
    """Serves a canned SSE body to `client._stream` / `sandbox.stream()`."""

    def __init__(
        self,
        lines: Iterable[bytes] = (),
        *,
        status: int = 200,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.lines = list(lines)
        self.status = status
        self.headers = dict(headers or {})
        self.calls: List[RecordedRequest] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: Optional[float],
    ) -> FakeStreamResponse:
        self.calls.append(RecordedRequest(method, url, dict(headers), body, timeout))
        return FakeStreamResponse(self.status, self.headers, iter(self.lines))


class FakeShellConnection:
    """Minimal stand-in for the websocket-client connection."""

    def __init__(self, replies: Sequence[str] = ()) -> None:
        self.sent: List[str] = []
        self.closed = False
        self._replies = list(replies)

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self) -> str:
        return self._replies.pop(0) if self._replies else ""

    def close(self) -> None:
        self.closed = True


class FakeShellTransport:
    """Captures the websocket URL/headers the SDK would have dialled."""

    def __init__(self, replies: Sequence[str] = ()) -> None:
        self.replies = list(replies)
        self.url: Optional[str] = None
        self.headers: dict = {}
        self.timeout: Optional[float] = None
        self.connection: Optional[FakeShellConnection] = None

    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: Optional[float]
    ) -> FakeShellConnection:
        self.url = url
        self.headers = dict(headers)
        self.timeout = timeout
        self.connection = FakeShellConnection(self.replies)
        return self.connection


class RecordingSleep:
    """Replaces time.sleep in polling loops: records instead of waiting."""

    def __init__(self) -> None:
        self.calls: List[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


def sse_lines(*events: Mapping[str, Any]) -> List[bytes]:
    """Build an SSE byte stream from {"event": ..., "data": ...} dicts."""
    lines: List[bytes] = []
    for event in events:
        if "event" in event:
            lines.append(f"event: {event['event']}\n".encode("utf-8"))
        data = event.get("data")
        if data is not None:
            payload = data if isinstance(data, str) else json.dumps(data)
            lines.append(f"data: {payload}\n".encode("utf-8"))
        lines.append(b"\n")
    return lines
