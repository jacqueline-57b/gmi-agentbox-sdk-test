"""Read-only checks against the real API. Nothing here creates a resource."""

from __future__ import annotations

import pytest
from agentbox_sdk import AgentBoxClient, APIError, NotFoundError


def test_the_api_key_is_accepted(test_client: AgentBoxClient):
    entitlement = test_client.eligibility()
    print("*********")
    print(entitlement)

    assert isinstance(entitlement.eligible, bool)
    assert isinstance(entitlement.data_centers, list)


def test_sandbox_data_centers_are_listed(test_client: AgentBoxClient):
    idcs = test_client.idcs.list(runtime="sandbox")

    assert idcs, "no sandbox data centers visible to this organization"
    assert all(idc.idc_id for idc in idcs)


def test_products_are_listed_for_a_data_center(test_client: AgentBoxClient, sandbox_target):
    products = test_client.products.list(idc_name=sandbox_target.idc, runtime="sandbox")

    assert products
    assert sandbox_target.instance_type in {product.instance_type for product in products}


def test_agents_list_returns_a_page(test_client: AgentBoxClient):
    page = test_client.agents.list(page=1, page_size=5)

    assert page.page == 1
    assert page.page_size == 5
    assert len(page.items) <= 5
    assert isinstance(page.total, int)


def test_sandbox_list_excludes_stopped_and_deleted_by_default(test_client: AgentBoxClient):
    page = test_client.sandboxes.list()

    assert {sandbox.status for sandbox in page.items}.isdisjoint({"stopped", "deleted"})


def test_unknown_agent_raises_not_found(test_client: AgentBoxClient):
    with pytest.raises(NotFoundError) as caught:
        test_client.agents.get("sdk-test-does-not-exist-9d41f0")

    assert caught.value.status_code == 404


def test_an_invalid_key_is_rejected(test_client: AgentBoxClient):
    # test_client is requested only to skip this when no key is configured.
    stranger = AgentBoxClient(api_key="definitely-not-a-valid-key")

    with pytest.raises(APIError) as caught:
        stranger.agents.list()

    assert caught.value.status_code in {401, 403}
