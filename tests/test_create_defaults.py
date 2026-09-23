"""What `agents.create` fills in when `runtime` is left out.

The SDK sends no default of its own: `runtime` is `Optional[str] = None` and
`_compact()` drops every None, so omitting it leaves the key out of
`POST /deployments` altogether. The reference calls the parameter optional and
says it "defaults to the service default" without naming that default, so the
only way to know what a caller gets is to create one and read it back.

Omitting `runtime` drops a second field with it: the SDK derives
`deployment_type` as `"gmi-ce" if runtime == "sandbox" else None`, so a create
without a runtime also sends no deployment type. Both are recorded here.

Observed on 2026-09-22 against this account: both a minimal create (title and
image only) and a B-0-shaped one (idc and instance_type, no runtime) came back
`runtime="container"`, with the service filling in `deployment_type="gmi-ce"`
by itself — the SDK sends that field only for `runtime="sandbox"`, so it is the
`runtime` field, not the deployment type, that says what was registered. Such
an agent reports `template_build_status: null`, which B-0's `_READY` reads as
ready: a create that loses its `runtime` looks built without ever building.

This is an observation, not a PRD row — no case in the B-0 table covers it, so
it asserts only what the reference itself promises: that omitting the parameter
is accepted, that the answer says which runtime was chosen, and that a later
`get` agrees with the create.

OBS-02 takes the next step and launches such an agent, because that is where a
lost `runtime` actually bites: the launch is refused — `Inventory.NotConfigured`
from upstream, observed 2026-09-22 — and each refusal still leaves a task record
behind. The SDK raised, so the caller was handed no id to delete, yet
`agent.sandboxes()` lists them; the account-wide listing reports the same
records with `deployment_slug: null`. Finding them takes a list call the caller
has no reason to make.

OBS-01 registers two agents and launches nothing. OBS-02 attempts five launches
and carries the `billable` marker: they are refused in this organization today,
but an attempt is what the marker is for.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

import allure
import pytest
from agentbox_sdk import Agent, APIError

from helpers.report import _payload, known_gap, show

FEATURE = "B-0 Agent registration and build"
P2 = allure.severity_level.NORMAL


def chosen_runtime(agent: Agent) -> Dict[str, Any]:
    """The two fields a create without `runtime` leaves for the service to fill."""
    return {
        "runtime": agent.runtime,
        "deployment_type": agent.data.get("deployment_type"),
    }


LAUNCH_ATTEMPTS = 5


def all_sandbox_ids(test_client) -> Set[str]:
    """Every task id in the account, not only the ones an agent owns.

    A launch the backend refuses can still leave a task with no agent attached,
    and no per-agent listing will ever return it — so the count that answers
    "did this create anything?" has to be the unfiltered one.
    """
    return {sandbox.id for sandbox in test_client.sandboxes.list(page_size=100).items}


def create_without_runtime(
    test_client, tracker, title: str, **fields: Any
) -> Tuple[Optional[Agent], Optional[APIError]]:
    """Create with no `runtime` key at all; return the agent, or the refusal."""
    request: Dict[str, Any] = {"title": title, **fields}
    try:
        agent = test_client.agents.create(**request)
    except APIError as exc:
        show(
            f"agents.create without runtime [{title}]",
            method=f"test_client.agents.create({', '.join(f'{n}=...' for n in request)})",
            args=request,
            returns=exc,
        )
        return None, exc

    tracker.track_agent(agent)
    show(
        f"agents.create without runtime [{title}]",
        method=f"test_client.agents.create({', '.join(f'{n}=...' for n in request)})",
        args=request,
        returns=agent,
    )
    return agent, None


@allure.feature(FEATURE)
@allure.story("OBS-01 create without runtime: what the service defaults to")
@allure.severity(P2)
@pytest.mark.slow
def test_create_without_runtime_reports_the_runtime_it_chose(
    test_client, tracker, run_id, image_url, sandbox_target, instance_type
):
    with allure.step("1. create with nothing but a title and an image"):
        minimal, minimal_refusal = create_without_runtime(
            test_client, tracker, f"sdk-test-runtime-min-{run_id}", image_url=image_url
        )

    with allure.step("2. create the way B-0 does, minus `runtime`"):
        agent, refusal = create_without_runtime(
            test_client,
            tracker,
            f"sdk-test-runtime-idc-{run_id}",
            image_url=image_url,
            idc=sandbox_target.idc,
            instance_type=instance_type,
        )
        assert agent is not None, (
            f"the reference lists `runtime` as optional, but omitting it was rejected: "
            f"{_payload(refusal)}"
        )

    with allure.step("3. get it back: the service says which runtime it chose"):
        fetched = test_client.agents.get(agent.slug)
        show(
            "the runtime the service filled in",
            method="test_client.agents.get(slug)",
            args={"slug": agent.slug},
            returns={
                "with idc + instance_type, no runtime": {
                    "on_create": chosen_runtime(agent),
                    "on_get": chosen_runtime(fetched),
                    "template_build_status": fetched.template_build_status,
                    "launchable": fetched.launchable,
                },
                "title + image only": _payload(minimal_refusal)
                if minimal is None
                else chosen_runtime(minimal),
            },
        )

        assert fetched.runtime == agent.runtime, (
            f"create and get disagree about the runtime: create said {agent.runtime!r}, "
            f"get says {fetched.runtime!r}"
        )
        if not fetched.runtime:
            known_gap(
                "KNOWN GAP: neither create nor get names a runtime when the parameter is "
                "omitted, so a caller cannot tell what kind of agent they registered"
            )
        assert fetched.runtime, "the service did not say which runtime it picked"


@allure.feature(FEATURE)
@allure.story("OBS-02 launching an agent that was registered without runtime")
@allure.severity(P2)
@pytest.mark.slow
@pytest.mark.billable
def test_launching_an_agent_without_runtime_leaves_nothing_behind(
    test_client, tracker, run_id, image_url, sandbox_target, instance_type
):
    with allure.step("1. register an agent without `runtime`"):
        agent, refusal = create_without_runtime(
            test_client,
            tracker,
            f"sdk-test-runtime-launch-{run_id}",
            image_url=image_url,
            idc=sandbox_target.idc,
            instance_type=instance_type,
        )
        assert agent is not None, (
            f"the reference lists `runtime` as optional, but omitting it was rejected: "
            f"{_payload(refusal)}"
        )
        show(
            "what the service registered",
            method="(none) - reading the create response above",
            returns={
                **chosen_runtime(agent),
                "template_build_status": agent.template_build_status,
                "launchable": agent.launchable,
            },
        )

    with allure.step(f"2. launch it {LAUNCH_ATTEMPTS} times"):
        before = all_sandbox_ids(test_client)
        attempts: List[Dict[str, Any]] = []
        for number in range(1, LAUNCH_ATTEMPTS + 1):
            try:
                sandbox = agent.launch(instance_type=instance_type)
            except APIError as exc:
                attempts.append(
                    {"attempt": number, "refusal": (exc.status_code, exc.message)}
                )
            else:
                tracker.track_sandbox(sandbox)  # never leave a billable resource behind
                attempts.append(
                    {"attempt": number, "refusal": None, "sandbox_id": sandbox.id}
                )

        handed_back = {item.get("sandbox_id") for item in attempts} - {None}
        show(
            f"agent.launch x{LAUNCH_ATTEMPTS} on an agent with no runtime",
            method="agent.launch(instance_type=...) (-> test_client.sandboxes.launch(slug, ...))",
            args={"instance_type": instance_type, "attempts": LAUNCH_ATTEMPTS},
            returns={
                "attempts": attempts,
                "refused": sum(1 for item in attempts if item["refusal"] is not None),
                "ids handed back to the caller": sorted(handed_back),
            },
        )

    with allure.step("3. the agent's own list, next to the account's"):
        owned = agent.sandboxes()
        # Anything the account gained that the caller was never told about. The
        # ids the caller did get are excluded: those are tracked for teardown
        # and are not what this row is about.
        leaked = sorted(all_sandbox_ids(test_client) - before - handed_back)

        # Deleted here rather than after the assertion: a row that goes red must
        # not be the reason a record is left in the account.
        removed: Dict[str, Any] = {}
        for sandbox_id in leaked:
            try:
                test_client.sandboxes.delete(sandbox_id)
                removed[sandbox_id] = "deleted"
            except APIError as exc:
                removed[sandbox_id] = f"{exc.status_code} {exc.message}"

        show(
            "what the refused launches left behind",
            method="agent.sandboxes() then test_client.sandboxes.list(page_size=100), "
            "then test_client.sandboxes.delete(id) for anything the caller never saw",
            returns={
                "agent.sandboxes()": {
                    "total": owned.total,
                    "ids": sorted(item.id for item in owned.items),
                },
                "records the account gained": leaked,
                "cleaning them up": removed or "nothing to clean up",
            },
        )

    with allure.step("4. a refused launch must leave nothing behind"):
        refused = [item for item in attempts if item["refusal"] is not None]
        if leaked:
            known_gap(
                f"KNOWN GAP: {len(refused)} refused launch(es) left {len(leaked)} task record(s). "
                f"`agent.launch()` raised, so the caller was handed no id to delete, yet "
                f"`agent.sandboxes()` lists {owned.total} of them — a caller only finds out it "
                f"owns them by listing. The account-wide listing shows the same records with "
                f"`deployment_slug: null`"
            )
        assert not leaked, (
            f"a refused launch still created {len(leaked)} record(s) the caller was never "
            f"handed: {leaked}"
        )
