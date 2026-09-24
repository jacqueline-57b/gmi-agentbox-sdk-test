"""The client object itself: what it reports locally, and what the key buys.

Two questions live here, and nothing else does. **What does the client say
about itself without asking anyone** — that is `health()`, the only method on
`AgentBoxClient` that sends no request. And **what is this key entitled to** —
that is `eligibility()`, the cheapest call the API has and the one every other
file's fixtures fail on first, so the rows that decide whether a key is any
good belong in one place rather than at the head of a catalogue file.

Everything `eligibility()` then *implies* — which data centers serve which
runtime, which SKUs each center sells — is `tests/test_catalog.py`.

`health()` is worth the three rows it gets, because it looks like a readiness
check and is not one. Measured 2026-09-24 against `0.1.0b2`, on a client built
with an unroutable `base_url` and a transport that raises on contact:

    {"status": "ok", "baseUrl": "https://example.invalid", "authenticated": true}

Both flags are constants. `status` is the literal `"ok"`. `authenticated` is
`bool(self.api_key)` on a client that cannot exist without one, because
`_require_api_key` raises `ValueError` on an empty or blank key before
`__init__` returns. So `health()` answers identically for a key the service
rejects and for a working one, and `test_an_invalid_key_is_rejected` is the
only row here that settles the question.
"""

from __future__ import annotations

from typing import Dict, Mapping, Tuple

import allure
import pytest
from agentbox_sdk import (
    AgentBoxClient,
    AuthenticationError,
    Eligibility,
    PermissionDeniedError,
)

from helpers.catalogue import RUNTIMES
from helpers.fakes import FakeTransport
from helpers.report import _payload, known_gap, show

FEATURE = "Client and entitlement"
NORMAL = allure.severity_level.NORMAL

# Unroutable on purpose: every offline row below is built on it, so a row that
# starts making requests fails on the transport rather than quietly going live.
OFFLINE_URL = "https://example.invalid"


def offline_client(
    api_key: str = "sdk-test-offline-key",
) -> Tuple[AgentBoxClient, FakeTransport]:
    """A real client wired to a transport that raises on any request.

    `FakeTransport` has no stubs here, so it answers an unstubbed call with an
    `AssertionError` naming the request. That turns "this method touched the
    network" into a failure instead of a slow pass. The transport is handed
    back alongside the client so a row can assert on what it recorded.
    """
    transport = FakeTransport()
    client = AgentBoxClient(api_key=api_key, base_url=OFFLINE_URL, transport=transport)
    return client, transport


# --------------------------------------------------------------------------
# health(): local, and only local
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("health answers from local state and sends nothing")
@allure.severity(NORMAL)
def test_health_answers_from_local_state_and_sends_nothing():
    client, transport = offline_client()

    with allure.step("1. health() reports the configuration the client was built with"):
        health = client.health()
        show(
            "client.health()",
            method="AgentBoxClient(api_key=..., base_url=...).health()",
            args={"base_url": OFFLINE_URL},
            returns=health,
        )
        assert health["status"] == "ok"
        assert health["baseUrl"] == client.base_url == OFFLINE_URL
        assert health["authenticated"] is True

    with allure.step("2. nothing went out on the wire"):
        show(
            "the requests health() made",
            method="(none) - the transport's record after health()",
            returns={"requests": [repr(request) for request in transport.requests]},
        )
        assert transport.requests == [], (
            f"health() is documented as local, but it sent {transport.requests}"
        )


@allure.feature(FEATURE)
@allure.story("health cannot report a client as unauthenticated")
@allure.severity(NORMAL)
def test_health_cannot_report_a_client_as_unauthenticated():
    """`authenticated` has no false case, so `health()` is not a key check."""
    with allure.step("1. a key the service rejects still reads as authenticated"):
        stranger, _ = offline_client("definitely-not-a-valid-key")
        health = stranger.health()
        show(
            "health() on a client holding a key the service rejects",
            method="AgentBoxClient(api_key='definitely-not-a-valid-key', ...).health()",
            returns=health,
        )
        assert health["authenticated"] is True

    with allure.step("2. and a client with no key cannot be built at all"):
        refusals = {}
        for blank in ("", "   "):
            with pytest.raises(ValueError) as caught:
                AgentBoxClient(api_key=blank, base_url=OFFLINE_URL, transport=FakeTransport())
            refusals[repr(blank)] = str(caught.value)
        show(
            "AgentBoxClient(api_key=<blank>)",
            method="AgentBoxClient(api_key=...)",
            args={"api_key": sorted(refusals)},
            returns=refusals,
        )
        assert refusals, "an empty api_key is no longer refused at construction"

    with allure.step("3. so the flag has no false case"):
        known_gap(
            "KNOWN GAP: `health()` cannot report an unhealthy client. `status` is the "
            "literal 'ok' and `authenticated` is `bool(self.api_key)` on a client that "
            "cannot be constructed without a non-blank key, so both fields are "
            "constants — the dict is identical for a working key, for a key the "
            "service rejects with 401, and for a base_url that does not resolve. It "
            "sends no request, so it cannot answer 'can I reach the API' either. "
            "eligibility() is the call that answers both, and the only cost is one "
            "round trip."
        )


# --------------------------------------------------------------------------
# eligibility(): what the key is entitled to
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("the API key is accepted")
@allure.severity(NORMAL)
def test_the_api_key_is_accepted(entitlement: Eligibility):
    with allure.step("1. the organization is eligible and its data centers are listed"):
        show(
            "client.eligibility()",
            method="test_client.eligibility()",
            returns=entitlement.data,
        )
        assert entitlement.eligible is True, (
            f"the key is accepted but the organization is not entitled to launch: "
            f"{entitlement.data}"
        )
        assert isinstance(entitlement.data_centers, list)
        assert entitlement.data_centers, "eligibility() named no data center at all"


@allure.feature(FEATURE)
@allure.story("eligibility names the runtimes it serves")
@allure.severity(NORMAL)
def test_eligibility_names_the_runtimes_it_serves(runtimes: Dict[str, Mapping]):
    """The tripwire for `helpers.catalogue.RUNTIMES`, which every row in
    `test_catalog.py` is parametrized over: a runtime the service adds would
    otherwise go untested rather than reported."""
    with allure.step("1. the catalogue says which runtimes exist"):
        show(
            "client.eligibility().data['runtimes']",
            method="test_client.eligibility()",
            returns=runtimes,
        )
        assert runtimes, (
            "eligibility() no longer reports a `runtimes` map, so nothing here can "
            "tell which runtimes the account may ask for"
        )
        assert set(runtimes) == set(RUNTIMES), (
            f"the account's runtimes changed: eligibility reports {sorted(runtimes)}, "
            f"the suite is written for {sorted(RUNTIMES)}. Extend RUNTIMES in "
            f"tests/helpers/catalogue.py (and the table in test_catalog.py's "
            f"docstring) so the new runtime is covered too."
        )

    with allure.step("2. each one says whether it is available"):
        assert all(
            "available" in (detail or {}) for detail in runtimes.values()
        ), f"a runtime was named without an availability flag: {runtimes}"


@allure.feature(FEATURE)
@allure.story("an invalid key is rejected")
@allure.severity(NORMAL)
def test_an_invalid_key_is_rejected(test_client: AgentBoxClient):
    # test_client is requested only to skip this when no key is configured, and
    # to pin the origin: without base_url the stranger would follow
    # GMI_AGENTBOX_BASE_URL or fall back to the production DEFAULT_BASE_URL.
    stranger = AgentBoxClient(
        api_key="definitely-not-a-valid-key", base_url=test_client.base_url
    )

    with allure.step("1. the service refuses the key rather than the transport"):
        with pytest.raises((AuthenticationError, PermissionDeniedError)) as caught:
            stranger.eligibility()
        show(
            "AgentBoxClient(api_key=<invalid>, base_url=...).eligibility()",
            method="stranger.eligibility()",
            args={"base_url": test_client.base_url},
            returns=_payload(caught.value),
        )
        assert caught.value.status_code in {401, 403}
