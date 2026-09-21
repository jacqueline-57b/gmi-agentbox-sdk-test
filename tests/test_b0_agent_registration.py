"""B-0 Agent registration and build — one test per case row.

Registering an agent is meant to be cheap and repeatable: it returns an id at
once, starts nothing, and builds the image in the background so every later
launch skips the build.

| ID   | P  | Action                               | Expected                                       |
|------|----|--------------------------------------|------------------------------------------------|
| T-01 | P0 | `agents.create` -> poll until ready  | id at once, no sandbox, key shown once,        |
|      |    |                                      | build progress readable                        |
| T-02 | P0 | launch the agent x5 while building    | every attempt refused, nothing created         |
| T-03 | P0 | create again with identical fields   | same agent (no idempotency key -> known gap)   |
| T-04 | P1 | bad image / agent that exits at once | both fail with a reason, logs visible,         |
|      |    |                                      | deletable, not billed                          |
| T-05 | P1 | change only the title / start command| no rebuild                                     |
| T-06 | P0 | `agent.delete()` with a live sandbox | PRD says refuse                                |

Every API call is logged through `show()`, which names the SDK method, its
arguments and its return value, and attaches all of it to the Allure report.
Secrets are reduced to their shape first.

T-04b and T-06 launch a sandbox and carry the `billable` marker; the rest cost
nothing.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import allure
import pytest
from agentbox_sdk import (
    Agent,
    AgentBoxClient,
    APIError,
    NotFoundError,
    Sandbox,
    SandboxFailed,
    SandboxWaitTimeout,
)

from helpers.report import _payload, known_gap, show

FEATURE = "B-0 Agent registration and build"
P0 = allure.severity_level.BLOCKER
P1 = allure.severity_level.CRITICAL

# alpine:3.20 builds in ~8-10s in this data center, so 30s is ample headroom
# without letting a stuck build hold the suite for a quarter of an hour.
# A heavier image needs GMI_TEST_BUILD_TIMEOUT raised.
BUILD_TIMEOUT = float(os.getenv("GMI_TEST_BUILD_TIMEOUT", "30"))
BAD_IMAGE_BUILD_TIMEOUT = 240.0
RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))
# A healthy sandbox in this data center reaches running in ~10s, so a start
# command that dies has long since died by here; waiting the full RUN_TIMEOUT
# only makes the suite slow.
EXIT_AT_ONCE_TIMEOUT = 90.0
BUILD_POLL = 1.0

# `create` registers; it must not block on the build.
CREATE_MUST_RETURN_WITHIN = 10.0
BAD_IMAGE = "docker.io/library/gmi-sdk-test-no-such-image:0.0.0"

_READY = (None, "ready")
_FAILED = ("error", "failed")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def register(test_client, tracker, run_id, sandbox_target, image_url, instance_type):
    """Register an agent, track it for teardown, log the call.

    Returns `(agent, elapsed_seconds)`; the elapsed time is what T-01 asserts on.
    """

    def _register(tag: str, **overrides: Any) -> Tuple[Agent, float]:
        request: Dict[str, Any] = {
            "title": f"sdk-test-b0-{tag}-{run_id}",
            "image_url": image_url,
            "idc": sandbox_target.idc,
            "instance_type": instance_type,
            "runtime": "sandbox",
        }
        request.update(overrides)

        started = time.monotonic()
        agent = test_client.agents.create(**request)
        elapsed = time.monotonic() - started

        tracker.track_agent(agent)
        # Spell the call the way it was made. The keyword names come from the
        # request dict itself, so a row that passes an override (a bad image, a
        # start command) shows that argument instead of a stale hand-typed list.
        signature = ", ".join(f"{name}=..." for name in request)
        show(
            f"agents.create [{tag}] -> {elapsed:.2f}s",
            method=f"test_client.agents.create({signature})",
            args=request,
            returns=agent,
        )
        return agent, elapsed

    return _register


def wait_build(agent: Agent, *, timeout: float = BUILD_TIMEOUT) -> str:
    """Poll to a terminal build state. Returns 'ready', the failure, or 'timeout'."""
    started = time.monotonic()
    timeline: List[Tuple[float, Optional[str]]] = []

    while True:
        agent.refresh()
        status = agent.template_build_status
        elapsed = round(time.monotonic() - started, 1)
        if not timeline or timeline[-1][1] != status:
            timeline.append((elapsed, status))

        terminal = "ready" if status in _READY else status if status in _FAILED else None
        if terminal is None and elapsed >= timeout:
            terminal = "timeout"
        if terminal is not None:
            show(
                f"build finished [{agent.slug}]",
                method="agent.refresh() in a loop (-> test_client.agents.get(slug))",
                args={"poll": "agent.refresh()", "poll_interval_s": BUILD_POLL, "timeout_s": timeout},
                returns={
                    "timeline_s": timeline,
                    "template_build_status": status,
                    "template_build_error": agent.template_build_error,
                    "launchable": agent.launchable,
                    "outcome": terminal,
                },
            )
            return terminal
        time.sleep(BUILD_POLL)


# --------------------------------------------------------------------------
# T-01 register, then poll the build to ready
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-01 register, then poll the build to ready")
@allure.severity(P0)
@pytest.mark.slow
def test_t01_create_returns_an_id_at_once_and_the_image_builds(test_client, register):
    with allure.step("1. register the image as an agent; the id comes back at once"):
        agent, elapsed = register("t01")

        # Asserted against the response itself, before anything else is called:
        # registration either hands back an identity immediately or it does not.
        assert agent.id and agent.slug, "create returned no identity"
        assert elapsed < CREATE_MUST_RETURN_WITHIN, (
            f"create blocked for {elapsed:.1f}s — it must register and return, not build"
        )

    with allure.step("2. registering does not automatically launch a sandbox"):
        listed = test_client.sandboxes.list(agent_id=agent.id)
        show(
            "sandboxes.list",
            method="test_client.sandboxes.list(agent_id=...)",
            args={"agent_id": agent.id},
            returns=listed,
        )
        assert listed.items == [], "registration launched a sandbox"

    with allure.step("3. poll the image build through to ready"):
        assert wait_build(agent) == "ready"
        assert agent.launchable is True

    with allure.step("4. the build itself has no readable log"):
        show(
            "no build log for an agent",
            method="(none) - the SDK exposes no build-log call on Agent at all",
            returns={
                "observed": "the SDK has no build log for an agent; logs() exists on Sandbox only",
                "readable_instead": ["template_build_status", "template_build_error"],
                "workaround": "read the build log in the Console",
            },
        )


# --------------------------------------------------------------------------
# T-02 launching the agent during the build is refused
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-02 launching the agent during the build is refused")
@allure.severity(P0)
@pytest.mark.slow
def test_t02_launch_is_refused_while_the_image_is_still_building(
    register, tracker, instance_type
):
    agent, _ = register("t02")
    attempts: List[Dict[str, Any]] = []

    for attempt in range(1, 6):
        build_status = agent.refresh().template_build_status
        try:
            sandbox = agent.launch(instance_type=instance_type)
        except APIError as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "build_status": build_status,
                    "refused": True,
                    "status_code": exc.status_code,
                    "message": exc.message,
                    "code": exc.code,
                }
            )
        else:
            tracker.track_sandbox(sandbox)  # never leave a billable resource behind
            attempts.append(
                {
                    "attempt": attempt,
                    "build_status": build_status,
                    "refused": False,
                    "sandbox_id": sandbox.id,
                }
            )

    during_build = [item for item in attempts if item["build_status"] == "building"]
    show(
        "agent.launch x5 during the build",
        method="agent.refresh() then agent.launch(instance_type=...), 5 times "
        "(-> test_client.sandboxes.launch(slug, ...))",
        args={"instance_type": instance_type, "attempts": 5},
        returns={"attempts": attempts, "made_while_building": len(during_build)},
    )

    if not during_build:
        pytest.skip("the build finished before the first launch — nothing to assert")

    accepted = [item for item in during_build if not item["refused"]]
    assert not accepted, f"launch was accepted while the image was still building: {accepted}"

    after = agent.sandboxes()
    show(
        "agent.sandboxes() after the refusals",
        method="agent.sandboxes()",
        returns=after,
    )
    assert after.items == [], "a refused launch still created something"


# --------------------------------------------------------------------------
# T-03
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-03 creating twice with identical fields")
@allure.severity(P0)
@pytest.mark.slow
def test_t03_creating_the_same_agent_twice_returns_the_same_agent(register):
    first, _ = register("t03")

    try:
        second, _ = register("t03")
    except APIError as exc:
        show(
            "second agents.create with identical fields",
            method="test_client.agents.create(...) - the second call, identical fields",
            returns=exc,
        )
        known_gap(
            f"KNOWN GAP: no idempotency key — an identical create is rejected "
            f"({exc.status_code} {exc.message}) instead of returning the first agent"
        )

    show(
        "identical create, compared",
        method="(none) - comparing the two create responses above",
        returns={
            "first": {"id": first.id, "slug": first.slug, "title": first.title},
            "second": {"id": second.id, "slug": second.slug, "title": second.title},
            "same_agent": second.id == first.id,
        },
    )

    if second.id != first.id:
        known_gap(
            "KNOWN GAP: no idempotency key — identical fields registered a second agent "
            f"({first.slug} then {second.slug}), so a retried create silently doubles the account"
        )
    assert second.id == first.id


# --------------------------------------------------------------------------
# T-04
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-04a a bad image fails the build with a reason")
@allure.severity(P1)
@pytest.mark.slow
def test_t04a_a_bad_image_fails_the_build_and_the_agent_is_deletable(test_client, register):
    try:
        agent, _ = register("t04a", image_url=BAD_IMAGE)
    except APIError as exc:
        show(
            "agents.create with a bad image",
            method="test_client.agents.create(image_url=<nonexistent>, ...)",
            args={"image_url": BAD_IMAGE},
            returns=exc,
        )
        assert exc.message, "the rejection carried no reason"
        return

    outcome = wait_build(agent, timeout=BAD_IMAGE_BUILD_TIMEOUT)

    if outcome == "timeout":
        pytest.fail(
            f"a nonexistent image never reached a terminal build state in "
            f"{BAD_IMAGE_BUILD_TIMEOUT:.0f}s — the failure is unobservable"
        )
    if outcome == "ready":
        pytest.fail(f"the build of {BAD_IMAGE} reported ready — a missing image was not detected")

    observed = {
        "template_build_status": agent.template_build_status,
        "template_build_error": agent.template_build_error,
        "launchable": agent.launchable,
    }

    agent.delete()
    with pytest.raises(NotFoundError):
        test_client.agents.get(agent.slug)
    show(
        "agent.delete after a failed build",
        method="agent.delete() then test_client.agents.get(slug)",
        args={"slug": agent.slug},
        returns={**observed, "deleted": True, "then_get": "404 NotFoundError"},
    )

    assert observed["template_build_error"], (
        "the build reached 'error' but template_build_error is empty — a wrong image fails "
        "with no reason readable anywhere in the SDK, and the agent still reports "
        f"launchable={observed['launchable']}"
    )


@allure.feature(FEATURE)
@allure.story("T-04b an agent that exits immediately")
@allure.severity(P1)
@pytest.mark.slow
@pytest.mark.billable
def test_t04b_an_agent_that_exits_at_once_reports_a_reason(register, tracker, instance_type):
    agent, _ = register("t04b", start_cmd="exit 1")
    assert wait_build(agent) == "ready"

    sandbox = tracker.track_sandbox(agent.launch(instance_type=instance_type))
    show(
        "agent.launch",
        method="agent.launch(instance_type=...) (-> test_client.sandboxes.launch(slug, ...))",
        args={"instance_type": instance_type, "start_cmd": "exit 1"},
        returns=sandbox,
    )

    failure: Optional[BaseException] = None
    try:
        sandbox.wait_until_running(timeout=EXIT_AT_ONCE_TIMEOUT)
    except (SandboxFailed, SandboxWaitTimeout) as exc:
        failure = exc

    sandbox.refresh()
    show(
        "sandbox state after the start command exited",
        method=f"sandbox.wait_until_running(timeout={EXIT_AT_ONCE_TIMEOUT:.0f}) then sandbox.refresh()",
        returns={
            "status": sandbox.status,
            "last_error": sandbox.last_error,
            "wait_raised": _payload(failure) if failure else None,
            "capabilities": sandbox.capabilities,
        },
    )

    if sandbox.capabilities.get("logs"):
        logs = sandbox.logs()
        show(
            "sandbox.logs()",
            method="sandbox.logs()",
            args={"sandbox_id": sandbox.id},
            returns={"logs": logs[-2000:]},
        )
    else:
        show(
            "sandbox.logs()",
            method="(none) - capabilities.logs is false, so the call was never made",
            returns={"skipped": "the runtime does not advertise the logs capability"},
        )

    sandbox.delete()
    show(
        "sandbox.delete",
        method="sandbox.delete()",
        returns={"deleted": sandbox.id},
    )

    # "deletable" is part of this row: the agent must go once its sandbox has.
    try:
        agent.delete()
        deletion: Dict[str, Any] = {"refused": False}
    except APIError as exc:
        deletion = {"refused": True, **_payload(exc)}
    show(
        "agent.delete once the sandbox is gone",
        method="agent.delete()",
        args={"slug": agent.slug},
        returns=deletion,
    )

    gaps: List[str] = []
    if deletion["refused"]:
        gaps.append(
            f"the agent cannot be deleted after its sandbox was "
            f"({deletion['status_code']} {deletion['message']}) — it stays in the account"
        )
    if not sandbox.capabilities.get("logs"):
        gaps.append("the runtime reports logs=false, so sandbox.logs() cannot show why it died")
    if sandbox.status not in _FAILED:
        gaps.append(
            f"the sandbox never reached a failed state: ended {sandbox.status!r}, "
            f"last_error={sandbox.last_error!r}, wait raised "
            f"{type(failure).__name__ if failure else None}"
        )
    if gaps:
        known_gap("KNOWN GAP: " + "; ".join(gaps))

    assert sandbox.last_error, "the sandbox failed without a reason"


# --------------------------------------------------------------------------
# T-05
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-05 a metadata-only change must not rebuild")
@allure.severity(P1)
@pytest.mark.slow
def test_t05_changing_the_title_or_start_command_does_not_rebuild(register, run_id):
    agent, _ = register("t05")
    assert wait_build(agent) == "ready"

    def fingerprint() -> Dict[str, Any]:
        return {
            "revision": agent.revision,
            "upstream_template_id": agent.data.get("upstream_template_id"),
            "template_build_status": agent.template_build_status,
            "updated_at": agent.data.get("updated_at"),
        }

    before = fingerprint()

    agent.update(title=f"sdk-test-b0-t05-renamed-{run_id}")
    agent.refresh()
    after_title = fingerprint()
    show(
        "agents.update(title=...)",
        method="agent.update(title=...) then agent.refresh()",
        args={"title": "renamed"},
        returns={"before": before, "after": after_title},
    )

    assert after_title["template_build_status"] in _READY, "renaming kicked off a rebuild"
    assert after_title["upstream_template_id"] == before["upstream_template_id"], (
        "renaming replaced the built image"
    )

    try:
        agent.update(start_cmd="sleep 30")
    except APIError as exc:
        show(
            "agents.update(start_cmd=...)",
            method="agent.update(start_cmd=...)",
            args={"start_cmd": "sleep 30"},
            returns=exc,
        )
        known_gap(
            f"KNOWN GAP: the start command cannot be changed after registration "
            f"({exc.status_code} {exc.message}) — it takes a new agent"
        )

    agent.refresh()
    after_cmd = fingerprint()
    show(
        "agents.update(start_cmd=...)",
        method="agent.update(start_cmd=...) then agent.refresh()",
        args={"start_cmd": "sleep 30"},
        returns={"before": after_title, "after": after_cmd},
    )

    if after_cmd["upstream_template_id"] != after_title["upstream_template_id"]:
        known_gap(
            "KNOWN GAP: changing only the start command rebuilt the image "
            f"({after_title['upstream_template_id']} -> {after_cmd['upstream_template_id']}), "
            "so a one-line command change costs a full build"
        )
    assert after_cmd["template_build_status"] in _READY, "changing the start command rebuilt the image"


# --------------------------------------------------------------------------
# T-06
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-06 deleting an agent that still has a running sandbox")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_t06_deleting_an_agent_with_a_running_sandbox_is_refused(
    test_client, register, tracker, instance_type
):
    agent, _ = register("t06")
    assert wait_build(agent) == "ready"

    sandbox = tracker.track_sandbox(agent.launch(instance_type=instance_type))
    sandbox.wait_until_running(timeout=RUN_TIMEOUT)
    show(
        "agent.launch -> running",
        method="agent.launch(instance_type=...) then sandbox.wait_until_running()",
        args={"instance_type": instance_type},
        returns=sandbox,
    )
    assert sandbox.status == "running"

    try:
        agent.delete()
    except APIError as exc:
        show(
            "agent.delete with a running sandbox",
            method="agent.delete()",
            args={"slug": agent.slug},
            returns=exc,
        )
        assert exc.message, "the refusal carried no reason"
        return

    # The delete went through. Record what happened to the sandbox that is
    # still billing before failing the row.
    try:
        orphan = _payload(test_client.sandboxes.get(sandbox.id))
    except APIError as exc:
        orphan = _payload(exc)

    show(
        "FAIL: agent.delete succeeded with a running sandbox",
        method="agent.delete() (accepted) then test_client.sandboxes.get(sandbox_id)",
        args={"slug": agent.slug},
        returns={"sandbox_after_delete": orphan, "prd": "delete must be refused while a sandbox runs"},
    )
    pytest.fail(
        f"PRD violation: the agent was deleted while sandbox {sandbox.id} was running; "
        f"see the attachment for whether it is still up and billing"
    )
