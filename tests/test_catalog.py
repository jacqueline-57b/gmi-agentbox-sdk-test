"""Read-only checks on `client.idcs` and `client.products`.

Nothing here creates a resource. Nothing here calls an agent or a sandbox
method — `agents.*` and `sandboxes.*` have their own files — and nothing here
asks whether the key is any good, which is `test_client.py`'s question, along
with `health()` and `eligibility()` themselves. What is left is the catalogue a
caller reads before creating anything: which data centers serve each runtime,
and which SKUs each data center sells.

`runtime` is the axis the whole catalogue turns on, and `"sandbox"` is only one
of its values. `eligibility()` names the set — measured 2026-09-24 against this
account, `{"container": {"available": true}, "sandbox": {"available": true}}` —
and every catalogue call answers differently for each of them:

| call | `runtime="sandbox"` | `runtime="container"` | `runtime` omitted |
|---|---|---|---|
| `idcs.list(...)` | 1 center, `sandbox-runloop-us` | 6 centers | the same 6 |
| `products.list(idc_name=<sandbox center>, ...)` | 5 SKUs, all priced 0 | `[]` | `[]` |
| `products.list(idc_name=<container center>, ...)` | `[]` | that center's SKUs | the same SKUs |
| `products.list(...)` with no center | **422**, `IDCName` required | the whole container catalogue | the same catalogue |
| `products.list(idc_name="no-such-center", ...)` | **404** `IDC not found` | `[]` | `[]` |

Read down the last column: omitting `runtime` is not "every runtime", it is
`container`. A caller shopping for a sandbox who forgets it is handed an empty
list rather than an error. Read across the last two rows: the two runtimes do
not even fail alike — the sandbox path insists on a data center and checks that
it exists, the container path does neither.

A runtime the service does not know is refused with `UnprocessableError`, 422,
`runtime_invalid` — but only after case and surrounding whitespace have been
normalised away, so `"SANDBOX"` and `"container "` both work.

`helpers.catalogue.RUNTIMES` is what this account serves today, and every row
below is parametrized over it. The tripwire that keeps it honest lives with the
call that can answer it — `test_eligibility_names_the_runtimes_it_serves`, in
`test_client.py` — so a runtime the service gains or loses fails there, and the
fix is one edit in `helpers/catalogue.py` plus the table above. Every row below
skips a runtime the account cannot use rather than failing for it.
"""

from __future__ import annotations

from typing import Dict, List, Mapping

import allure
import pytest
from agentbox_sdk import (
    AgentBoxClient,
    APIError,
    Eligibility,
    Idc,
    UnprocessableError,
)

from helpers.catalogue import RUNTIMES
from helpers.report import _payload, show

FEATURE = "Catalogue read-only checks"
NORMAL = allure.severity_level.NORMAL

# Values the service does not know. `gmi-ce` is deliberate: it is the
# `deployment_type` the SDK derives for `runtime="sandbox"`, so it is the value
# a caller who confuses the two fields would send.
UNKNOWN_RUNTIMES = ("vm", "gmi-ce", "sdk-test-no-such-runtime")

NO_SUCH_IDC = "sdk-test-no-such-idc-9d41f0"


@pytest.fixture(scope="session")
def data_centers(test_client: AgentBoxClient) -> Dict[str, List[Idc]]:
    """`idcs.list` once per runtime, shared by every row that needs a center."""
    return {runtime: test_client.idcs.list(runtime=runtime) for runtime in RUNTIMES}


def require(runtime: str, runtimes: Mapping[str, Mapping]) -> None:
    """Skip a runtime this organization is not entitled to, rather than fail on it."""
    if not (runtimes.get(runtime) or {}).get("available"):
        pytest.skip(f"this organization has no {runtime!r} runtime: {dict(runtimes)}")


def first_center(runtime: str, data_centers: Mapping[str, List[Idc]]) -> str:
    """The first data center serving `runtime`, or skip: there is nothing to ask about."""
    centers = [idc.idc_id for idc in data_centers[runtime] if idc.idc_id]
    if not centers:
        pytest.skip(f"no {runtime!r} data center is visible to this organization")
    return centers[0]


def skus(products) -> set:
    return {product.instance_type for product in products}


def sold_or_refused(client: AgentBoxClient, center: str, runtime: str):
    """`products.list`, with a refusal recorded instead of raised.

    The two rows at the bottom of this file each ask the same question of both
    runtimes, and one runtime refusing must not stop the other from being
    asked. It used to: against production the container center answers
    `runtime="sandbox"` with a 422, which aborted the row on its first call and
    left the sandbox side of the comparison — the half the row is named after —
    never sent and nothing at all in the report.

    Returns the SKUs sold, or the `APIError` the service answered with. Only
    `APIError` is caught; a `TransportError` from the flaky staging host is not
    a refusal and still fails the row.
    """
    try:
        return sorted(skus(client.products.list(idc_name=center, runtime=runtime)))
    except APIError as exc:
        return exc


# --------------------------------------------------------------------------
# Data centers, per runtime
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("eligibility and idcs.list do not agree on the data centers")
@allure.severity(NORMAL)
def test_eligibility_and_idcs_list_do_not_agree(
    entitlement: Eligibility, data_centers: Dict[str, List[Idc]]
):
    """Two endpoints, two answers to "where may this account launch".

    Reported, not asserted. `eligibility().data_centers` is a flat list of
    names with no runtime on it; `idcs.list(runtime=...)` is the per-runtime
    answer. Which names each one carries is the account's shape on the day,
    not a rule the SDK owes anyone, so this row prints both lists and the two
    differences and leaves the reading to whoever opens the report. The rows
    that follow are where the catalogue is actually held to something.
    """
    listed = {
        runtime: {idc.idc_id for idc in centers} for runtime, centers in data_centers.items()
    }
    everywhere = set().union(*listed.values()) if listed else set()
    advertised = set(entitlement.data_centers)

    with allure.step("1. compare the two lists"):
        show(
            "eligibility().data_centers vs idcs.list(runtime=...)",
            method="test_client.eligibility(), test_client.idcs.list(runtime=...)",
            returns={
                "eligibility().data_centers": sorted(advertised),
                **{f"idcs.list(runtime={rt!r})": sorted(ids) for rt, ids in listed.items()},
                "only in eligibility": sorted(advertised - everywhere) or "none",
                "only in idcs.list": sorted(everywhere - advertised) or "none",
                "the same set": advertised == everywhere,
            },
        )


@allure.feature(FEATURE)
@allure.story("data centers are listed for each runtime")
@allure.severity(NORMAL)
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_data_centers_are_listed_for_each_runtime(
    runtime: str, runtimes: Dict[str, Mapping], data_centers: Dict[str, List[Idc]]
):
    require(runtime, runtimes)

    with allure.step(f"1. every {runtime} data center has an id, and none repeats"):
        centers = data_centers[runtime]
        show(
            f"client.idcs.list(runtime={runtime!r})",
            method="test_client.idcs.list(runtime=...)",
            args={"runtime": runtime},
            returns=[idc.data for idc in centers],
        )
        assert centers, f"no {runtime} data center is visible to this organization"
        ids = [idc.idc_id for idc in centers]
        assert all(ids), f"a {runtime} data center came back without an id: {ids}"
        assert len(ids) == len(set(ids)), f"idcs.list repeated a data center: {ids}"


@allure.feature(FEATURE)
@allure.story("no data center serves two runtimes")
@allure.severity(NORMAL)
def test_each_runtime_has_its_own_data_centers(
    runtimes: Dict[str, Mapping], data_centers: Dict[str, List[Idc]]
):
    for runtime in RUNTIMES:
        require(runtime, runtimes)

    with allure.step("1. the per-runtime lists do not overlap"):
        listed = {
            runtime: {idc.idc_id for idc in centers}
            for runtime, centers in data_centers.items()
        }
        shared = set.intersection(*listed.values())
        show(
            "idcs.list(runtime=...) for every runtime",
            method="test_client.idcs.list(runtime=...)",
            returns={
                **{runtime: sorted(ids) for runtime, ids in listed.items()},
                "in more than one": sorted(shared) or "none",
            },
        )
        assert not shared, (
            f"{sorted(shared)} is listed under more than one runtime, so a caller "
            f"cannot tell from a data center id which runtime it belongs to"
        )


@allure.feature(FEATURE)
@allure.story("omitting the runtime lists the container data centers")
@allure.severity(NORMAL)
def test_omitting_the_runtime_lists_the_container_data_centers(
    test_client: AgentBoxClient, runtimes: Dict[str, Mapping], data_centers: Dict[str, List[Idc]]
):
    """`runtime` is optional in the signature; leaving it out is not neutral."""
    for runtime in RUNTIMES:
        require(runtime, runtimes)

    container = {idc.idc_id for idc in data_centers["container"]}
    sandbox = {idc.idc_id for idc in data_centers["sandbox"]}

    with allure.step("1. no runtime at all answers with the container centers"):
        default = {idc.idc_id for idc in test_client.idcs.list()}
        show(
            "client.idcs.list() with no runtime",
            method="test_client.idcs.list()",
            returns={
                "returned": sorted(default),
                "runtime='container'": sorted(container),
                "runtime='sandbox'": sorted(sandbox),
            },
        )
        assert default == container, (
            f"omitting `runtime` used to mean `container`; it now returns "
            f"{sorted(default)} against {sorted(container)} for container"
        )
        assert not (default & sandbox), (
            f"the default listing now includes the sandbox centers {sorted(default & sandbox)}"
        )

    with allure.step("2. the empty string is read as no runtime, not as a bad one"):
        empty = {idc.idc_id for idc in test_client.idcs.list(runtime="")}
        show(
            "client.idcs.list(runtime='')",
            method="test_client.idcs.list(runtime='')",
            returns=sorted(empty),
        )
        assert empty == container, (
            f"runtime='' answered {sorted(empty)}, which is neither the container "
            f"listing nor a refusal"
        )


@allure.feature(FEATURE)
@allure.story("a runtime is matched without regard to case or surrounding space")
@allure.severity(NORMAL)
def test_a_runtime_is_matched_loosely(
    test_client: AgentBoxClient, runtimes: Dict[str, Mapping], data_centers: Dict[str, List[Idc]]
):
    require("sandbox", runtimes)
    expected = {idc.idc_id for idc in data_centers["sandbox"]}

    with allure.step("1. spellings that differ only in case or padding are accepted"):
        answers = {
            spelling: sorted(idc.idc_id for idc in test_client.idcs.list(runtime=spelling))
            for spelling in ("SANDBOX", "Sandbox", " sandbox ")
        }
        show(
            "client.idcs.list(runtime=<same word, spelled differently>)",
            method="test_client.idcs.list(runtime=...)",
            args=sorted(answers),
            returns={**answers, "runtime='sandbox'": sorted(expected)},
        )
        assert all(set(ids) == expected for ids in answers.values()), (
            f"the runtime is no longer normalised before it is matched: {answers} "
            f"against {sorted(expected)} for the exact spelling"
        )


@allure.feature(FEATURE)
@allure.story("an unknown runtime is rejected")
@allure.severity(NORMAL)
@pytest.mark.parametrize("runtime", UNKNOWN_RUNTIMES)
def test_an_unknown_runtime_is_rejected(
    test_client: AgentBoxClient, runtime: str, data_centers: Dict[str, List[Idc]]
):
    with allure.step("1. idcs.list refuses it"):
        with pytest.raises(UnprocessableError) as caught:
            test_client.idcs.list(runtime=runtime)
        show(
            f"client.idcs.list(runtime={runtime!r})",
            method="test_client.idcs.list(runtime=...)",
            args={"runtime": runtime},
            returns=_payload(caught.value),
        )
        assert caught.value.status_code == 422
        # `.code` is null on this refusal, so "was the runtime the problem?" can
        # only be answered from `.message`.
        assert caught.value.code is None
        assert "runtime_invalid" in (caught.value.message or "")

    with allure.step("2. products.list refuses it the same way"):
        center = first_center("container", data_centers)
        with pytest.raises(UnprocessableError) as caught:
            test_client.products.list(idc_name=center, runtime=runtime)
        show(
            f"client.products.list(idc_name=..., runtime={runtime!r})",
            method="test_client.products.list(idc_name=..., runtime=...)",
            args={"idc_name": center, "runtime": runtime},
            returns=_payload(caught.value),
        )
        assert caught.value.status_code == 422
        assert "runtime_invalid" in (caught.value.message or "")


# --------------------------------------------------------------------------
# Products, per runtime
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("products are listed for each runtime")
@allure.severity(NORMAL)
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_products_are_listed_for_each_runtime(
    test_client: AgentBoxClient,
    runtime: str,
    runtimes: Dict[str, Mapping],
    data_centers: Dict[str, List[Idc]],
):
    require(runtime, runtimes)
    center = first_center(runtime, data_centers)

    with allure.step(f"1. the {runtime} center sells SKUs, and only its own"):
        products = test_client.products.list(idc_name=center, runtime=runtime)
        show(
            f"client.products.list(idc_name=..., runtime={runtime!r})",
            method="test_client.products.list(idc_name=..., runtime=...)",
            args={"idc_name": center, "runtime": runtime},
            returns=[product.data for product in products],
        )
        assert products, f"the {runtime} data center {center} sells nothing"
        assert all(product.instance_type for product in products), (
            f"a {runtime} product came back without an instance type: "
            f"{[product.data for product in products]}"
        )
        stamped = {product.data.get("dataCenter") for product in products}
        assert stamped == {center}, (
            f"products.list(idc_name={center!r}) returned SKUs stamped {stamped}"
        )


@allure.feature(FEATURE)
@allure.story("the SKU the live fixtures launch on is on sale")
@allure.severity(NORMAL)
def test_the_target_sandbox_sku_is_on_sale(test_client: AgentBoxClient, sandbox_target):
    """`sandbox_target` and `instance_type` in conftest pick from this listing;
    if the SKU they settled on is gone, every billable row fails later and here."""
    with allure.step("1. the discovered SKU is among the products the center sells"):
        products = test_client.products.list(idc_name=sandbox_target.idc, runtime="sandbox")
        show(
            "client.products.list(idc_name=..., runtime='sandbox')",
            method="test_client.products.list(idc_name=..., runtime='sandbox')",
            args={"idc_name": sandbox_target.idc, "wanted": sandbox_target.instance_type},
            returns=[product.data for product in products],
        )
        assert products
        assert sandbox_target.instance_type in skus(products), (
            f"{sandbox_target.instance_type} is no longer sold in {sandbox_target.idc}: "
            f"{sorted(skus(products))}"
        )


@allure.feature(FEATURE)
@allure.story("a data center sells nothing under another runtime")
@allure.severity(NORMAL)
def test_a_data_center_sells_nothing_under_another_runtime(
    test_client: AgentBoxClient, runtimes: Dict[str, Mapping], data_centers: Dict[str, List[Idc]]
):
    """Asking the right center for the wrong runtime is empty, not an error."""
    for runtime in RUNTIMES:
        require(runtime, runtimes)

    with allure.step("1. every center, asked for a runtime it does not serve"):
        crossed = {}
        for runtime in RUNTIMES:
            center = first_center(runtime, data_centers)
            for other in RUNTIMES:
                if other == runtime:
                    continue
                crossed[f"{center} asked for {other!r}"] = sorted(
                    skus(test_client.products.list(idc_name=center, runtime=other))
                )
        show(
            "client.products.list(idc_name=<one runtime>, runtime=<another>)",
            method="test_client.products.list(idc_name=..., runtime=...)",
            returns=crossed,
        )
        assert all(not found for found in crossed.values()), (
            f"a data center answered for a runtime it does not serve: {crossed}"
        )


@allure.feature(FEATURE)
@allure.story("omitting the runtime hides the sandbox SKUs")
@allure.severity(NORMAL)
def test_omitting_the_runtime_hides_the_sandbox_skus(
    test_client: AgentBoxClient, runtimes: Dict[str, Mapping], data_centers: Dict[str, List[Idc]]
):
    """The trap the table in the docstring is really about: a caller who leaves
    `runtime` out while shopping for a sandbox is told the center sells nothing."""
    for runtime in RUNTIMES:
        require(runtime, runtimes)
    center = first_center("sandbox", data_centers)

    with allure.step("1. the sandbox center, asked with and without a runtime"):
        with_runtime = skus(test_client.products.list(idc_name=center, runtime="sandbox"))
        without = skus(test_client.products.list(idc_name=center))
        show(
            "client.products.list(idc_name=<sandbox center>) with and without `runtime`",
            method="test_client.products.list(idc_name=...[, runtime='sandbox'])",
            args={"idc_name": center},
            returns={
                "runtime='sandbox'": sorted(with_runtime),
                "runtime omitted": sorted(without) or "[] - empty, and no error",
            },
        )
        assert with_runtime, f"the sandbox center {center} sells nothing"
        assert not without, (
            f"omitting `runtime` used to hide the sandbox SKUs; it now returns "
            f"{sorted(without)}"
        )

    with allure.step("2. the whole catalogue, with no center and no runtime"):
        everything = skus(test_client.products.list())
        container = skus(test_client.products.list(runtime="container"))
        show(
            "client.products.list() with no arguments",
            method="test_client.products.list()",
            returns={
                "count": len(everything),
                "also in runtime='container'": len(everything & container),
                "also sold by the sandbox center": sorted(everything & with_runtime) or "none",
            },
        )
        assert everything, "products.list() with no arguments returned nothing at all"
        assert everything <= container, (
            f"the bare listing is no longer the container catalogue; "
            f"{sorted(everything - container)} is in one and not the other"
        )
        assert not (everything & with_runtime), (
            f"the bare listing now includes sandbox SKUs: {sorted(everything & with_runtime)}"
        )


@allure.feature(FEATURE)
@allure.story("only the sandbox catalogue insists on a data center")
@allure.severity(NORMAL)
def test_only_the_sandbox_catalogue_insists_on_a_data_center(
    test_client: AgentBoxClient, runtimes: Dict[str, Mapping]
):
    for runtime in RUNTIMES:
        require(runtime, runtimes)

    with allure.step("1. runtime='sandbox' with no center is refused"):
        with pytest.raises(UnprocessableError) as caught:
            test_client.products.list(runtime="sandbox")
        show(
            "client.products.list(runtime='sandbox') with no idc_name",
            method="test_client.products.list(runtime='sandbox')",
            returns=_payload(caught.value),
        )
        # 422 from the SDK, wrapping an upstream 400 whose body names the field:
        # `Field validation for 'IDCName' failed on the 'required' tag`.
        assert caught.value.status_code == 422
        assert "IDCName" in (caught.value.message or "")

    with allure.step("2. runtime='container' with no center answers with everything"):
        products = test_client.products.list(runtime="container")
        show(
            "client.products.list(runtime='container') with no idc_name",
            method="test_client.products.list(runtime='container')",
            returns={"count": len(products), "instance types": sorted(skus(products))},
        )
        assert products, (
            "the container catalogue now needs a data center too, which would make "
            "the refusal above consistent rather than a sandbox-only rule"
        )


@allure.feature(FEATURE)
@allure.story("an unknown data center is a 404 only on the sandbox path")
@allure.severity(NORMAL)
def test_an_unknown_data_center_is_a_404_only_on_the_sandbox_path(
    test_client: AgentBoxClient, runtimes: Dict[str, Mapping]
):
    for runtime in RUNTIMES:
        require(runtime, runtimes)

    with allure.step("1. the sandbox path says the data center does not exist"):
        with pytest.raises(NotFoundError) as caught:
            test_client.products.list(idc_name=NO_SUCH_IDC, runtime="sandbox")
        show(
            f"client.products.list(idc_name={NO_SUCH_IDC!r}, runtime='sandbox')",
            method="test_client.products.list(idc_name=..., runtime='sandbox')",
            args={"idc_name": NO_SUCH_IDC},
            returns=_payload(caught.value),
        )
        assert caught.value.status_code == 404
        assert NO_SUCH_IDC in (caught.value.message or "")

    with allure.step("2. the container path answers as if the name were real"):
        products = test_client.products.list(idc_name=NO_SUCH_IDC, runtime="container")
        show(
            f"client.products.list(idc_name={NO_SUCH_IDC!r}, runtime='container')",
            method="test_client.products.list(idc_name=..., runtime='container')",
            args={"idc_name": NO_SUCH_IDC},
            returns=[product.data for product in products] or "[] - empty, and no error",
        )
        assert not products, (
            f"a data center that does not exist sold {sorted(skus(products))}"
        )
