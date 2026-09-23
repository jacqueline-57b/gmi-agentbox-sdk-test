"""Read-only checks against the real API. Nothing here creates a resource."""

from __future__ import annotations

import allure
import pytest
from agentbox_sdk import (
    AgentBoxClient,
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
)

from helpers.report import _payload, show

FEATURE = "Catalogue read-only checks"
NORMAL = allure.severity_level.NORMAL


@allure.feature(FEATURE)
@allure.story("the API key is accepted")
@allure.severity(NORMAL)
def test_the_api_key_is_accepted(test_client: AgentBoxClient):
    with allure.step("1. the organization is eligible and its data centers are listed"):
        entitlement = test_client.eligibility()
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


@allure.feature(FEATURE)
@allure.story("sandbox data centers are listed")
@allure.severity(NORMAL)
def test_sandbox_data_centers_are_listed(test_client: AgentBoxClient):
    with allure.step("1. every data center the organization may launch in has an id"):
        idcs = test_client.idcs.list(runtime="sandbox")
        show(
            "client.idcs.list(runtime='sandbox')",
            method="test_client.idcs.list(runtime='sandbox')",
            returns=[idc.data for idc in idcs],
        )
        assert idcs, "no sandbox data centers visible to this organization"
        assert all(idc.idc_id for idc in idcs)


@allure.feature(FEATURE)
@allure.story("products are listed for a data center")
@allure.severity(NORMAL)
def test_products_are_listed_for_a_data_center(test_client: AgentBoxClient, sandbox_target):
    with allure.step("1. the discovered SKU is among the products the center sells"):
        products = test_client.products.list(idc_name=sandbox_target.idc, runtime="sandbox")
        show(
            "client.products.list(idc_name=..., runtime='sandbox')",
            method="test_client.products.list(idc_name=..., runtime='sandbox')",
            args={"idc_name": sandbox_target.idc},
            returns=[product.data for product in products],
        )
        assert products
        assert sandbox_target.instance_type in {product.instance_type for product in products}


@allure.feature(FEATURE)
@allure.story("agents.list returns a page")
@allure.severity(NORMAL)
def test_agents_list_returns_a_page(test_client: AgentBoxClient):
    with allure.step("1. the page echoes the pagination it was asked for"):
        page = test_client.agents.list(page=1, page_size=5)
        show(
            "client.agents.list(page=1, page_size=5)",
            method="test_client.agents.list(page=1, page_size=5)",
            returns=page,
        )
        assert page.page == 1
        assert page.page_size == 5
        assert len(page.items) <= 5
        assert isinstance(page.total, int)


@allure.feature(FEATURE)
@allure.story("sandboxes.list excludes stopped and deleted by default")
@allure.severity(NORMAL)
def test_sandbox_list_excludes_stopped_and_deleted_by_default(test_client: AgentBoxClient):
    with allure.step("1. no stopped or deleted sandbox leaks into the default listing"):
        page = test_client.sandboxes.list()
        show(
            "client.sandboxes.list()",
            method="test_client.sandboxes.list()",
            returns=page,
        )
        assert {sandbox.status for sandbox in page.items}.isdisjoint({"stopped", "deleted"})


@allure.feature(FEATURE)
@allure.story("an unknown agent raises not found")
@allure.severity(NORMAL)
def test_unknown_agent_raises_not_found(test_client: AgentBoxClient):
    with allure.step("1. agents.get on a slug that does not exist answers 404"):
        with pytest.raises(NotFoundError) as caught:
            test_client.agents.get("sdk-test-does-not-exist-9d41f0")
        show(
            "client.agents.get('sdk-test-does-not-exist-9d41f0')",
            method="test_client.agents.get('sdk-test-does-not-exist-9d41f0')",
            returns=_payload(caught.value),
        )
        assert caught.value.status_code == 404


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
            stranger.agents.list()
        show(
            "AgentBoxClient(api_key=<invalid>, base_url=...).agents.list()",
            method="stranger.agents.list()",
            args={"base_url": test_client.base_url},
            returns=_payload(caught.value),
        )
        assert caught.value.status_code in {401, 403}
