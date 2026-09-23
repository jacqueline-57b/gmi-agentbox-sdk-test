"""Transports that cut a transfer in half, for the rows that need a broken one.

B-3 asks what survives a connection that dies mid-transfer, and no SDK call
can produce one — a request either completes or is refused. `AgentBoxClient`
does take a `transport` callable, though, and a transport sees the finished
URL, headers and body, so the break can be staged there without the test
reaching into anything private.

The two that cut a transfer short use a real socket and a real reset:
`SO_LINGER` set to zero makes `close()` send RST rather than FIN, so the peer
sees the transfer abandoned rather than ended. A simulated exception would
prove only what the client does with it; these prove what the *service* is
left holding. `abandon_response_transport` is the deliberate exception — it
sends everything and closes politely, because the point there is that the
service *does* finish the work while the caller never learns the outcome.

They are deliberately not clients. Build one with
`AgentBoxClient(transport=truncating_transport(0.5))` and talk to the same
sandbox id as the healthy client does.
"""

from __future__ import annotations

import socket
import ssl
import struct
import time
from http.client import HTTPConnection, HTTPSConnection
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from agentbox_sdk._transport import RawResponse
from agentbox_sdk.errors import TransportError


def _reset(connection: HTTPConnection) -> None:
    """Close the socket with RST instead of a polite FIN, if it is still open."""
    sock = connection.sock
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass  # already gone; the close below is enough
    connection.close()


def _connect(url: str, timeout: float) -> tuple[HTTPConnection, str]:
    parts = urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    if parts.scheme == "https":
        connection: HTTPConnection = HTTPSConnection(
            parts.hostname, parts.port or 443, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        connection = HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    return connection, target


def truncating_transport(fraction: float = 0.5) -> Callable[..., RawResponse]:
    """Send `fraction` of the request body, then reset the connection.

    The full `Content-Length` is announced first, so the service is told to
    expect a whole file and then never gets one — which is the shape of an
    upload interrupted by a dropped network, not of a short file.

    Always raises `TransportError`; the answer this is asked for is not the
    return value but what the destination path holds afterwards.
    """

    def _transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> RawResponse:
        if not body:
            raise ValueError("truncating_transport needs a request body to cut")
        cut = max(1, int(len(body) * fraction))
        connection, target = _connect(url, timeout)
        try:
            connection.putrequest(method, target, skip_accept_encoding=True)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders()
            connection.send(body[:cut])
        finally:
            _reset(connection)
        raise TransportError(
            f"connection reset after sending {cut} of {len(body)} bytes"
        )

    return _transport


def abandon_response_transport(settle: float = 1.0) -> Callable[..., RawResponse]:
    """Deliver the whole request, wait, then leave without reading the answer.

    The opposite end of `truncating_transport`. The service receives a
    complete, valid request and acts on it; the caller is left with a
    transport error and no way to know whether it happened. That gap is the
    subject of every retry-idempotency question — the client that retries here
    is a client that cannot tell it is retrying.

    `settle` is the pause between the request going out and the socket
    closing. It is not cosmetic: closing the instant the last byte is written
    can abort the request before the service has finished reading it, which
    would produce no side effect at all and prove nothing. A second is far
    longer than this API takes to read a small JSON body.

    The close is an ordinary FIN rather than the RST the other two send: the
    service must be left free to finish what it started, and only the reply
    is thrown away.
    """

    def _transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> RawResponse:
        connection, target = _connect(url, timeout)
        try:
            connection.request(method, target, body=body, headers=dict(headers))
            time.sleep(settle)
        finally:
            connection.close()  # FIN, not RST: let the service keep going
        raise TransportError(
            f"the request was sent in full and the connection was closed {settle}s later "
            f"without reading the answer"
        )

    return _transport


def short_read_transport(fraction: float = 0.5) -> Callable[..., RawResponse]:
    """Read `fraction` of the response body, then reset the connection.

    Unlike the upload side this one *returns*, handing the SDK a body shorter
    than the `Content-Length` that came with it. That is the question the row
    is asking: given a download that demonstrably did not finish, does the SDK
    say so, or does it pass the fragment back as if it were the file?
    """

    def _transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> RawResponse:
        connection, target = _connect(url, timeout)
        try:
            connection.request(method, target, body=body, headers=dict(headers))
            response = connection.getresponse()
            declared = response.getheader("Content-Length")
            full = int(declared) if declared and declared.isdigit() else None
            want = max(1, int(full * fraction)) if full else 1024
            partial = response.read(want)
            return RawResponse(response.status, dict(response.getheaders()), partial)
        finally:
            _reset(connection)

    return _transport
