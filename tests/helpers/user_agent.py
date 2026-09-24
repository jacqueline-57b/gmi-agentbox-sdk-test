"""Give the SDK's requests a User-Agent the edge will accept.

The SDK sets no User-Agent of its own, so its stdlib transports go out as
`Python-urllib/<version>` — a signature Cloudflare blocks in front of
`ce-tot.gmicloud-dev.com`. The block is at the edge, before any auth check:
every call comes back 403 with a `text/plain` body of `error code: 1010`,
which the SDK surfaces as `PermissionDeniedError: Request failed` and so reads
like a revoked API key. Any other User-Agent is let through, including none
at all — only the `Python-urllib` signature is refused.

`AgentBoxClient` has no client-level default headers, and the suite builds
clients in a few dozen places, most of them `AgentBoxClient()` with no
transport argument. So rather than thread a wrapper through every call site,
`install()` replaces the module-level transports the SDK falls back to; see
tests/conftest.py, which calls it once at import.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

import agentbox_sdk

USER_AGENT = f"agentbox-sdk/{agentbox_sdk.__version__} (gmi-sdk-test)"

_installed = False


def with_user_agent(transport: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a transport so every request carries `USER_AGENT`.

    A User-Agent already on the request wins, so a test that wants to probe
    the edge's behaviour can still set its own.
    """

    def wrapped(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: Optional[float],
    ) -> Any:
        merged = dict(headers)
        merged.setdefault("User-Agent", USER_AGENT)
        return transport(method, url, merged, body, timeout)

    wrapped.__name__ = getattr(transport, "__name__", "transport")
    wrapped.__doc__ = transport.__doc__
    return wrapped


def install() -> None:
    """Point the SDK's default transports at User-Agent-setting wrappers.

    Idempotent, so importing it from more than one place cannot double-wrap.
    `agentbox_sdk.client` binds the transports by name at import time, so both
    modules have to be patched for `AgentBoxClient()` to pick the wrapper up.
    """
    global _installed
    if _installed:
        return

    from agentbox_sdk import _transport, client

    for name in ("urlopen_transport", "urlopen_stream_transport"):
        wrapped = with_user_agent(getattr(_transport, name))
        setattr(_transport, name, wrapped)
        setattr(client, name, wrapped)

    _installed = True
