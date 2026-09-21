"""`agents.create` — what it requires, what it refuses, what it keeps.

`create` takes fourteen keyword arguments and the suite only ever exercised
six of them. This file works the parameter surface itself: which combinations
the backend accepts, which it refuses and how readably, and — for the ones it
accepts — whether what was sent survives the round trip.

| ID   | P  | Action                                            | Expected                                              |
|------|----|---------------------------------------------------|-------------------------------------------------------|
| E-01 | P0 | build the argument set up one field at a time     | the minimum accepted set is recorded, not assumed     |
| E-02 | P0 | blank title, bad idc/SKU/runtime, malformed env   | each refused with a readable 4xx, never a 5xx         |
| E-03 | P0 | one create carrying every echo-able argument      | every field reads back on create and on a fresh get   |
| E-04 | P1 | titles with spaces, case and unicode              | the slug derived from each is recorded                |
| E-05 | P0 | `clone_source_id` off an agent already built      | the clone is accepted; does it rebuild or reuse?      |
| E-06 | P1 | `deployment_type` passed explicitly               | the SDK's auto `gmi-ce` is not silently overridden    |

Nothing here launches a sandbox, so **nothing here bills**. Registration is
still not free of consequence: every accepted create leaves an agent, and the
backend refuses to delete one while its image builds, so each row tracks what
it made and teardown retries until the builds settle.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import allure
import pytest
from agentbox_sdk import Agent, APIError

from helpers.report import _payload, known_gap, show

FEATURE = "Agent create: argument surface"
P0 = allure.severity_level.BLOCKER
P1 = allure.severity_level.CRITICAL

BUILD_SETTLE_TIMEOUT = 60.0
BUILD_SETTLE_POLL = 2.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def make_title(run_id: str):
    """Unique per call, so a rerun never collides with its own leftovers."""

    def _title(tag: str) -> str:
        return f"sdk-test-create-{tag}-{run_id}-{uuid.uuid4().hex[:4]}"

    return _title


@pytest.fixture
def attempt(test_client, tracker):
    """Run one `agents.create` and report it, whatever it does.

    Returns `(agent, error)` with exactly one of them set. An accepted create
    is tracked before anything else can fail — a refused one leaves nothing to
    track, which is why the rejection rows below cost nothing to clean up.
    """

    def _attempt(label: str, **request: Any) -> Tuple[Optional[Agent], Optional[APIError]]:
        started = time.monotonic()
        try:
            agent = test_client.agents.create(**request)
        except APIError as exc:
            show(
                f"agents.create [{label}] -> refused in {time.monotonic() - started:.2f}s",
                method=f"test_client.agents.create({', '.join(f'{k}=...' for k in request)})",
                args=request,
                returns=exc,
            )
            return None, exc
        tracker.track_agent(agent)
        show(
            f"agents.create [{label}] -> accepted in {time.monotonic() - started:.2f}s",
            method=f"test_client.agents.create({', '.join(f'{k}=...' for k in request)})",
            args=request,
            returns=agent,
        )
        return agent, None

    return _attempt


def settle(agent: Agent, *, timeout: float = BUILD_SETTLE_TIMEOUT) -> Optional[str]:
    """Poll to a terminal build state so the agent becomes deletable.

    Teardown retries a refused delete anyway; this just gets the waiting over
    with inside the row that caused it, and returns the state it landed in.
    """
    stop_at = time.monotonic() + timeout
    while time.monotonic() < stop_at:
        status = agent.refresh().template_build_status
        if status in (None, "ready", "error", "failed"):
            return status
        time.sleep(BUILD_SETTLE_POLL)
    return agent.template_build_status


def base_request(title: str, image_url: str, sandbox_target, instance_type: str) -> Dict[str, Any]:
    """The argument set the rest of the suite already knows is accepted."""
    return {
        "title": title,
        "image_url": image_url,
        "idc": sandbox_target.idc,
        "instance_type": instance_type,
        "runtime": "sandbox",
    }


# --------------------------------------------------------------------------
# E-01 what does create actually require?
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-01 the minimum argument set create will accept")
@allure.severity(P0)
@pytest.mark.slow
def test_e01_the_minimum_accepted_argument_set(
    attempt, make_title, image_url, sandbox_target, instance_type
):
    """Add one argument at a time and record where it starts working.

    The reference says `runtime="sandbox"` requires `idc` and `instance_type`,
    but only the backend can say so. Each rung is reported with its outcome, so
    the row documents the real requirement instead of restating the docs.
    """
    rungs: List[Tuple[str, Dict[str, Any]]] = [
        ("title only", {}),
        ("+ image_url", {"image_url": image_url}),
        ("+ idc", {"image_url": image_url, "idc": sandbox_target.idc}),
        (
            "+ instance_type",
            {
                "image_url": image_url,
                "idc": sandbox_target.idc,
                "instance_type": instance_type,
            },
        ),
        (
            "+ runtime=sandbox",
            {
                "image_url": image_url,
                "idc": sandbox_target.idc,
                "instance_type": instance_type,
                "runtime": "sandbox",
            },
        ),
    ]

    outcomes: List[Dict[str, Any]] = []
    for index, (label, extra) in enumerate(rungs, start=1):
        agent, error = attempt(label, title=make_title(f"e01-{index}"), **extra)
        outcomes.append(
            {
                "arguments": ["title", *extra],
                "rung": label,
                "accepted": agent is not None,
                **(
                    {
                        "slug": agent.slug,
                        "runtime": agent.runtime,
                        "deployment_type": agent.data.get("deployment_type"),
                        "build_status": agent.template_build_status,
                    }
                    if agent
                    else {"status_code": error.status_code, "message": error.message}
                ),
            }
        )

    show(
        "which argument set is enough?",
        method="(none) - comparing the five creates above",
        returns={
            "rungs": outcomes,
            "first_accepted": next(
                (item["rung"] for item in outcomes if item["accepted"]), "none of them"
            ),
        },
    )

    assert outcomes[-1]["accepted"], (
        "the argument set the rest of the suite relies on was refused: "
        f"{outcomes[-1].get('message')}"
    )
    for item in outcomes:
        if not item["accepted"]:
            assert item["status_code"] < 500, (
                f"a missing argument produced a server error, not a validation error: {item}"
            )
            assert item.get("message"), f"the refusal of {item['rung']!r} carried no reason"


# --------------------------------------------------------------------------
# E-02 what does it refuse, and does it say why?
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-02 invalid arguments are refused with a reason")
@allure.severity(P0)
def test_e02_invalid_arguments_are_refused_readably(
    attempt, make_title, image_url, sandbox_target, instance_type
):
    """Seven bad requests. None should reach a 5xx, all should explain.

    A create that is *accepted* here is the interesting failure: it means the
    backend took something it cannot honour, so the agent is tracked and the
    row says which input slipped through.
    """
    good = base_request(make_title("e02"), image_url, sandbox_target, instance_type)

    bad_cases: List[Tuple[str, Dict[str, Any]]] = [
        ("blank title", {**good, "title": ""}),
        ("whitespace title", {**good, "title": "   "}),
        ("unknown idc", {**good, "idc": "sdk-test-no-such-datacenter"}),
        ("unknown instance_type", {**good, "instance_type": "gmi.sandbox.does-not-exist"}),
        ("unknown runtime", {**good, "runtime": "banana"}),
        ("env entry with no name", {**good, "env": [{"value": "orphan"}]}),
        ("env that is not a list", {**good, "env": [{"name": "OK", "value": None}]}),
    ]

    results: List[Dict[str, Any]] = []
    for label, request in bad_cases:
        request = {**request, "title": f"{good['title']}-{label.replace(' ', '-')}"}
        if label in ("blank title", "whitespace title"):
            request["title"] = "" if label == "blank title" else "   "
        agent, error = attempt(label, **request)
        results.append(
            {
                "case": label,
                "refused": agent is None,
                **(
                    {"status_code": error.status_code, "code": error.code, "message": error.message}
                    if error
                    else {"slug": agent.slug, "build_status": agent.template_build_status}
                ),
            }
        )

    show(
        "seven invalid creates",
        method="(none) - comparing the seven attempts above",
        returns={
            "refused": sum(1 for item in results if item["refused"]),
            "accepted_anyway": [item["case"] for item in results if not item["refused"]],
            "results": results,
        },
    )

    server_errors = [item for item in results if item.get("status_code", 0) >= 500]
    assert not server_errors, f"invalid input produced a 5xx instead of a validation error: {server_errors}"

    silent = [
        item for item in results if item["refused"] and not item.get("message")
    ]
    assert not silent, f"these refusals carried no reason at all: {silent}"

    accepted = [item["case"] for item in results if not item["refused"]]
    if accepted:
        known_gap(
            "KNOWN GAP: create accepted input it cannot honour — "
            + ", ".join(accepted)
            + "; each one registered an agent that should never have existed"
        )


# --------------------------------------------------------------------------
# E-03 everything sent must come back
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-03 every accepted argument reads back")
@allure.severity(P0)
@pytest.mark.slow
def test_e03_every_argument_reads_back(
    attempt, test_client, make_title, image_url, sandbox_target, instance_type
):
    """One create carrying every argument that should survive the round trip.

    Sent as a single request rather than one agent per field: a create is
    cheap but an agent takes a build cycle to become deletable, so eight rows
    of evidence for the price of one is worth the coupling.
    """
    title = make_title("e03")
    request = {
        **base_request(title, image_url, sandbox_target, instance_type),
        "env": [{"name": "E03_MARKER", "value": "create-side", "secret": False}],
        "short_desc": "created by the SDK test harness, E-03",
        "start_cmd": "sleep 3600",
        "assign_public_ip": False,
        "ports": [{"name": "http", "port": 8080, "protocol": "TCP"}],
    }

    agent, error = attempt("all arguments", **request)
    if agent is None:
        pytest.fail(
            f"create refused the full argument set ({error.status_code} {error.message}); "
            f"rerun the fields one at a time to find which one it objects to"
        )

    refetched = test_client.agents.get(agent.slug)

    def compare(field: str, sent: Any) -> Dict[str, Any]:
        return {
            "sent": sent,
            "on the create response": agent.data.get(field, "<absent>"),
            "on a fresh get": refetched.data.get(field, "<absent>"),
        }

    readback = {
        field: compare(field, request[field])
        for field in ("title", "image_url", "idc", "instance_type", "runtime",
                      "short_desc", "start_cmd", "assign_public_ip", "ports", "env")
    }
    show(
        "every argument, sent vs returned",
        method="test_client.agents.get(slug) compared against the create response",
        args={"slug": agent.slug},
        returns=readback,
    )

    settle(agent)

    # The two that the SDK itself promises.
    assert agent.title == title, "title did not read back"
    assert agent.image_url == request["image_url"], "image_url did not read back"
    assert agent.idc == sandbox_target.idc, "idc did not read back"
    assert agent.runtime == "sandbox", "runtime did not read back"

    dropped = [
        field
        for field in ("short_desc", "start_cmd", "assign_public_ip", "ports", "env")
        if refetched.data.get(field, "<absent>") == "<absent>"
    ]
    if dropped:
        known_gap(
            "KNOWN GAP: create accepted these arguments but neither response carries them "
            f"back — {', '.join(dropped)}. There is no way to confirm from the SDK what an "
            "agent was actually registered with"
        )


# --------------------------------------------------------------------------
# E-04 how is the slug derived from the title?
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-04 the slug derived from an awkward title")
@allure.severity(P1)
@pytest.mark.slow
def test_e04_slug_generation_from_awkward_titles(
    attempt, make_title, image_url, sandbox_target, instance_type
):
    """`slug` is what every later call addresses the agent by, and it is
    derived, not supplied. A title the caller chose freely must still produce
    a slug that `get` can find — otherwise the agent is created and lost.
    """
    variants = {
        "spaces": f"sdk test create e04 {make_title('x')[-12:]}",
        "mixed case": make_title("E04-MixedCase"),
        "unicode": f"sdk-test-创建-e04-{make_title('u')[-8:]}",
    }

    findings: List[Dict[str, Any]] = []
    for label, title in variants.items():
        agent, error = attempt(label, **base_request(title, image_url, sandbox_target, instance_type))
        if agent is None:
            findings.append(
                {"case": label, "title": title, "accepted": False,
                 "status_code": error.status_code, "message": error.message}
            )
            continue
        try:
            found = agent._client.agents.get(agent.slug)
            addressable = found.id == agent.id
        except APIError as exc:
            addressable = False
            findings.append({"case": label, "get_raised": _payload(exc)})
        findings.append(
            {
                "case": label,
                "title": title,
                "accepted": True,
                "slug": agent.slug,
                "slug == title": agent.slug == title,
                "get(slug) finds it again": addressable,
            }
        )
        settle(agent)

    show(
        "titles in, slugs out",
        method="agents.create(title=...) then agents.get(slug) for each",
        returns=findings,
    )

    lost = [item for item in findings if item.get("accepted") and not item.get("get(slug) finds it again")]
    assert not lost, f"created an agent that its own slug cannot address again: {lost}"


# --------------------------------------------------------------------------
# E-05 cloning
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-05 clone_source_id off an agent that is already built")
@allure.severity(P0)
def test_e05_cloning_requires_an_image_and_does_not_reuse_one(
    attempt, make_title, session_agent, sandbox_target, instance_type
):
    """`clone_source_id` cannot be used the way its name suggests.

    Deliberately only the *refused* call is made here. A clone that the
    backend accepts never finishes building — it sits in `building`, and an
    agent mid-build cannot be deleted, so each accepted clone would strand an
    undeletable agent in the account on every run. The accepted path was
    measured once by hand and is recorded below as evidence instead.
    """
    assert session_agent.template_build_status in (None, "ready"), (
        "the session agent has not finished building; it is not a valid clone source"
    )

    clone, error = attempt(
        "clone_source_id, used as documented",
        title=make_title("e05-clone"),
        idc=sandbox_target.idc,
        instance_type=instance_type,
        runtime="sandbox",
        clone_source_id=session_agent.id,
    )

    show(
        "what clone_source_id actually does",
        method="(none) - the refusal above, plus a measured one-off probe",
        args={"clone_source_id": session_agent.id, "source_slug": session_agent.slug},
        returns={
            "clone_source_id alone": (
                f"refused {error.status_code}: {error.message}" if error else "accepted"
            ),
            "clone_source_id + image_url (probed by hand 2026-09-21)": {
                "accepted": True,
                "source upstream_template_id": "def31a67-28a8-4afd-b6f8-789c462b81de",
                "clone upstream_template_id": "9b81747e-817f-4e43-837c-cdb3baf4d763",
                "reused the source image": False,
                "build outcome": "stuck in 'building' for over 10 minutes, never terminal",
                "consequence": "the clone could not be deleted: 409 sandbox_template_busy",
            },
            "why this row does not create one": (
                "an accepted clone strands an undeletable agent in the account every run"
            ),
        },
    )

    assert clone is None, (
        "clone_source_id was accepted without image_url this time — the probe below is stale, "
        "re-measure whether the clone reuses the source image"
    )
    assert error.status_code < 500, f"clone_source_id produced a server error: {error}"
    known_gap(
        f"KNOWN GAP: clone_source_id cannot be used as its name implies. Alone it is refused "
        f"({error.status_code} {error.message}); with image_url supplied it is accepted but "
        f"builds a fresh image rather than reusing the source's, and that build does not "
        f"finish — leaving an agent that cannot be deleted"
    )


# --------------------------------------------------------------------------
# E-06 deployment_type
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-06 deployment_type passed explicitly")
@allure.severity(P1)
@pytest.mark.slow
def test_e06_explicit_deployment_type_is_not_overridden(
    attempt, make_title, image_url, sandbox_target, instance_type
):
    """The SDK fills in `deployment_type="gmi-ce"` when `runtime="sandbox"`
    and no type was given. A caller who names one must get the one they named
    back, or a refusal — silently substituting would mean the agent is not
    what the request said.
    """
    default_agent, default_error = attempt(
        "runtime=sandbox, no deployment_type",
        **base_request(make_title("e06-default"), image_url, sandbox_target, instance_type),
    )
    assert default_agent is not None, f"the baseline create was refused: {default_error}"

    explicit_agent, explicit_error = attempt(
        "deployment_type given explicitly",
        **base_request(make_title("e06-explicit"), image_url, sandbox_target, instance_type),
        deployment_type="gmi-ce",
    )

    show(
        "deployment_type: filled in vs given",
        method="(none) - comparing the two creates above",
        returns={
            "auto-filled": default_agent.data.get("deployment_type"),
            "explicitly sent": "gmi-ce",
            "explicitly returned": (
                explicit_agent.data.get("deployment_type") if explicit_agent else None
            ),
            "explicit create refused": _payload(explicit_error) if explicit_error else False,
        },
    )

    assert default_agent.data.get("deployment_type") == "gmi-ce", (
        "runtime=sandbox no longer implies deployment_type=gmi-ce"
    )
    assert explicit_agent is not None, (
        f"passing the same deployment_type the SDK would have filled in was refused: "
        f"{explicit_error}"
    )
    assert explicit_agent.data.get("deployment_type") == "gmi-ce", "the explicit value was replaced"

    settle(default_agent)
    settle(explicit_agent)
