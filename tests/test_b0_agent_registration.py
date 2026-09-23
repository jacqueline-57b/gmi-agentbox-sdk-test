"""B-0 Agent registration and build — one test per case row.

| ID   | P  | Action                               | Expected                                       |
|------|----|--------------------------------------|------------------------------------------------|
| T-01 | P0 | `agents.create` -> poll until ready  | id at once, no sandbox, build progress readable|
| T-02 | P0 | launch the agent x5 while building   | every attempt refused, nothing created         |
| T-03 | P0 | create again with identical fields   | refused: the title already exists              |
| T-04 | P1 | bad image / agent that exits at once | create is accepted; the launch fails with a    |
|      |    |                                      | reason, and both are deletable afterwards      |
| T-05 | P1 | change only the title / start command| no rebuild                                     |
| T-06 | P0 | `agent.delete()` with a live sandbox | PRD says refuse                                |

`show()` logs every SDK call to stdout and to Allure; each row asserts the
Expected column above, so a gap fails the row rather than being annotated.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import allure
import pytest
from agentbox_sdk import (
    Agent,
    APIError,
    NotFoundError,
    SandboxFailed,
    SandboxWaitTimeout,
)

from helpers.report import _payload, show

FEATURE = "B-0 Agent registration and build"
P0 = allure.severity_level.BLOCKER
P1 = allure.severity_level.CRITICAL

# alpine:3.20 builds in ~8-10s in this data center; a heavier image needs more.
BUILD_TIMEOUT = float(os.getenv("GMI_TEST_BUILD_TIMEOUT", "60"))
BUILD_POLL = 1.0

LAUNCH_ATTEMPTS = 5
# The server leaves `code` null, so a build refusal is only matchable as `message`.
BUILD_REFUSAL = (409, "template_not_ready")

# T-03. Observed 2026-09-23: a second create with the same title is refused
# with the upstream 409 wrapped in a 422, and the SDK's own `code` left null.
DUPLICATE_STATUS = 422
DUPLICATE_CODE = "Resource.AlreadyExists"
DUPLICATE_MESSAGE = "Template name already exists"

BAD_IMAGE = "docker.io/library/gmi-sdk-test-no-such-image:0.0.0"
BAD_IMAGE_BUILD_TIMEOUT = 240.0

# A healthy sandbox runs in ~10s, so a start command that dies is long dead by 90s.
EXIT_AT_ONCE_TIMEOUT = float(os.getenv("GMI_TEST_EXIT_AT_ONCE_TIMEOUT", "90"))
RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))

# A build status of None means the backend reports none, which the SDK treats as ready.
_READY = (None, "ready")
_FAILED = ("error", "failed")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def register(test_client, tracker, run_id, sandbox_target, image_url, instance_type):
    """Register an agent, track it for teardown, log the call."""

    def _register(tag: str, **overrides: Any) -> Agent:
        request: Dict[str, Any] = {
            "title": f"sdk-test-b0-{tag}-{run_id}",
            "image_url": image_url,
            "idc": sandbox_target.idc,
            "instance_type": instance_type,
            "runtime": "sandbox",
            **overrides,
        }

        started = time.monotonic()
        agent = test_client.agents.create(**request)
        elapsed = time.monotonic() - started

        tracker.track_agent(agent)
        signature = ", ".join(f"{name}=..." for name in request)
        show(
            f"agents.create [{tag}] -> {elapsed:.2f}s",
            method=f"test_client.agents.create({signature})",
            args=request,
            returns=agent,
        )
        return agent

    return _register


def wait_build(agent: Agent, *, timeout: float = BUILD_TIMEOUT) -> str:
    """Poll to a terminal build state: 'ready', the failure status, or 'timeout'."""
    started = time.monotonic()
    timeline: List[Tuple[float, Optional[str]]] = []

    while True:
        status = agent.refresh().template_build_status
        elapsed = round(time.monotonic() - started, 1)
        if not timeline or timeline[-1][1] != status:
            timeline.append((elapsed, status))

        outcome = "ready" if status in _READY else status if status in _FAILED else None
        if outcome is None and elapsed >= timeout:
            outcome = "timeout"
        if outcome is not None:
            show(
                f"build finished [{agent.slug}]",
                method="agent.refresh() in a loop (-> test_client.agents.get(slug))",
                args={"poll_interval_s": BUILD_POLL, "timeout_s": timeout},
                returns={
                    "timeline_s": timeline,
                    "template_build_status": status,
                    "template_build_error": agent.template_build_error,
                    "launchable": agent.launchable,
                    "outcome": outcome,
                },
            )
            return outcome
        time.sleep(BUILD_POLL)


def attempt_launch(agent: Agent, tracker, instance_type: str, number: int) -> Dict[str, Any]:
    """One launch attempt: `refusal` is `(status_code, message)`, or None if accepted."""
    record = {"attempt": number, "build_status": agent.refresh().template_build_status}
    try:
        sandbox = agent.launch(instance_type=instance_type)
    except APIError as exc:
        return {**record, "refusal": (exc.status_code, exc.message), "code": exc.code}
    tracker.track_sandbox(sandbox)
    return {**record, "refusal": None, "sandbox_id": sandbox.id}


def upstream_error(exc: APIError) -> Dict[str, Any]:
    """The upstream JSON body the SDK wraps into `APIError.message`."""
    start = exc.message.find("{")
    if start == -1:
        return {}
    try:
        return json.loads(exc.message[start:])
    except json.JSONDecodeError:
        return {}


def fingerprint(agent: Agent) -> Dict[str, Any]:
    """What a rebuild would change: which image is attached, and its build state."""
    return {
        "revision": agent.revision,
        "upstream_template_id": agent.data.get("upstream_template_id"),
        "template_build_status": agent.template_build_status,
        "updated_at": agent.data.get("updated_at"),
    }


# --------------------------------------------------------------------------
# T-01 register, then poll the build to ready
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-01 register, then poll the build to ready")
@allure.severity(P0)
@pytest.mark.slow
def test_t01_create_returns_an_id_at_once_and_the_image_builds(test_client, register):
    with allure.step("1. register the image as an agent; the id comes back at once"):
        agent = register("t01")
        assert agent.id and agent.slug, "create returned no identity"

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
        assert wait_build(agent) == "ready", (
            f"the image did not build within {BUILD_TIMEOUT:.0f}s: status "
            f"{agent.template_build_status!r}, error {agent.template_build_error!r}"
        )
        assert agent.launchable is True, "the built agent does not report itself launchable"

    with allure.step("4. build progress is readable, but the build itself has no log"):
        show(
            "what the SDK exposes about a build",
            method="(none) - the SDK exposes no build-log call on Agent at all",
            returns={
                "observed": "logs() exists on Sandbox only; an agent's build has no log",
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
    with allure.step("1. register the agent; the image starts building in the background"):
        agent = register("t02")

    with allure.step("2. launch the agent five times while the image is still building"):
        attempts = [
            attempt_launch(agent, tracker, instance_type, number)
            for number in range(1, LAUNCH_ATTEMPTS + 1)
        ]
        during_build = [item for item in attempts if item["build_status"] == "building"]
        show(
            "agent.launch while the image builds",
            method=f"agent.refresh() then agent.launch(instance_type=...), x{LAUNCH_ATTEMPTS}",
            args={"instance_type": instance_type, "attempts": LAUNCH_ATTEMPTS},
            returns={"attempts": attempts, "made_while_building": len(during_build)},
        )
        if not during_build:
            pytest.skip("the build finished before the first launch — nothing to assert")

    with allure.step("3. every launch during the build was refused, with the build as the reason"):
        accepted = [item for item in during_build if item["refusal"] is None]
        assert not accepted, f"launch was accepted while the image was still building: {accepted}"

        wrong = [item for item in during_build if item["refusal"] != BUILD_REFUSAL]
        assert not wrong, (
            f"expected every refusal to be {BUILD_REFUSAL[0]} {BUILD_REFUSAL[1]!r}, got: {wrong}"
        )

    with allure.step("4. the refused launches created nothing"):
        after = agent.sandboxes()
        show("agent.sandboxes() after the refusals", method="agent.sandboxes()", returns=after)
        assert after.items == [], "a refused launch still created something"


# --------------------------------------------------------------------------
# T-03 creating twice with identical fields
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-03 creating twice with identical fields")
@allure.severity(P0)
@pytest.mark.slow
def test_t03_creating_the_same_agent_twice_is_refused_as_a_duplicate(register):
    with allure.step("1. register the agent"):
        first = register("t03")

    with allure.step("2. creating again with byte-identical fields is refused"):
        refusal: Optional[APIError] = None
        try:
            second = register("t03")
        except APIError as exc:
            refusal = exc
            show(
                "the second create was refused",
                method="test_client.agents.create(...) - the second call, identical fields",
                returns=exc,
            )
        assert refusal is not None, (
            f"identical fields registered a second agent ({first.slug} then {second.slug}) "
            f"instead of being refused as a duplicate"
        )

    with allure.step("3. the refusal carries the duplicate-name code and the title that collided"):
        upstream = upstream_error(refusal)
        show(
            "the refusal, decoded",
            method="(none) - the upstream JSON body the SDK wraps into APIError.message",
            returns={
                "status_code": refusal.status_code,
                "sdk_code": refusal.code,
                "upstream": upstream,
            },
        )
        assert refusal.status_code == DUPLICATE_STATUS, (
            f"expected {DUPLICATE_STATUS}, got {refusal.status_code} {refusal.message}"
        )
        assert upstream.get("code") == DUPLICATE_CODE, (
            f"expected the upstream code {DUPLICATE_CODE!r}, got {upstream.get('code')!r} "
            f"out of {refusal.message}"
        )
        assert upstream.get("message") == f"{DUPLICATE_MESSAGE}: {first.title}", (
            f"expected {DUPLICATE_MESSAGE!r} naming {first.title!r}, "
            f"got {upstream.get('message')!r}"
        )


# --------------------------------------------------------------------------
# T-04a a bad image fails the build with a reason
# T-04b an agent that exits immediately
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-04a a bad image fails the build with a reason")
@allure.severity(P1)
@pytest.mark.slow
def test_t04a_a_bad_image_fails_the_build_and_cannot_be_launched(
    test_client, register, tracker, instance_type
):
    with allure.step("1. registering an agent whose image does not exist is accepted"):
        agent = register("t04a", image_url=BAD_IMAGE)
        assert agent.id and agent.slug, "create returned no identity"

    with allure.step("2. the build reaches a terminal state and reports failure"):
        outcome = wait_build(agent, timeout=BAD_IMAGE_BUILD_TIMEOUT)
        assert outcome in _FAILED, (
            f"the build of {BAD_IMAGE} reported {outcome!r} — a missing image was not detected"
        )

    with allure.step("3. launching it fails, and says why"):
        refusal: Optional[APIError] = None
        sandbox = None
        try:
            sandbox = tracker.track_sandbox(agent.launch(instance_type=instance_type))
        except APIError as exc:
            refusal = exc
        show(
            "agent.launch after a failed build",
            method="agent.launch(instance_type=...)",
            args={
                "instance_type": instance_type,
                "template_build_status": agent.template_build_status,
                "template_build_error": agent.template_build_error,
                "launchable": agent.launchable,
            },
            returns=refusal if refusal is not None else sandbox,
        )
        assert refusal is not None, (
            f"launch was accepted for an agent whose build reported "
            f"{agent.template_build_status!r} (launchable={agent.launchable})"
        )
        assert refusal.message, "the refusal carried no reason"

    with allure.step("4. the agent whose build failed is deletable"):
        agent.delete()
        with pytest.raises(NotFoundError):
            test_client.agents.get(agent.slug)
        show(
            "agent.delete after a failed build",
            method="agent.delete() then test_client.agents.get(slug)",
            args={"slug": agent.slug},
            returns={"deleted": True, "then_get": "404 NotFoundError"},
        )


@allure.feature(FEATURE)
@allure.story("T-04b an agent that exits immediately")
@allure.severity(P1)
@pytest.mark.slow
def test_t04b_an_agent_that_exits_at_once_never_runs_and_says_why(
    register, tracker, instance_type
):
    with allure.step("1. register an agent whose start command exits at once, and build it"):
        agent = register("t04b", start_cmd="exit 1")
        assert wait_build(agent) == "ready", "the image never built, so nothing can be launched"

    with allure.step("2. the launch is accepted, but the sandbox never reaches running"):
        sandbox = tracker.track_sandbox(agent.launch(instance_type=instance_type))
        failure: Optional[BaseException] = None
        try:
            sandbox.wait_until_running(timeout=EXIT_AT_ONCE_TIMEOUT)
        except (SandboxFailed, SandboxWaitTimeout) as exc:
            failure = exc
        sandbox.refresh()
        can_log = bool(sandbox.capabilities.get("logs"))
        show(
            "the sandbox after the start command exited",
            method="agent.launch(instance_type=...) then "
            f"sandbox.wait_until_running(timeout={EXIT_AT_ONCE_TIMEOUT:.0f})",
            args={"instance_type": instance_type, "start_cmd": "exit 1"},
            returns={
                "status": sandbox.status,
                "last_error": sandbox.last_error,
                "wait_raised": _payload(failure) if failure else None,
                "logs": sandbox.logs()[-2000:] if can_log else "capabilities.logs is false",
            },
        )
        assert failure is not None, "a sandbox whose start command exits at once reached running"

    with allure.step("3. the sandbox says why it failed"):
        assert sandbox.status in _FAILED, (
            f"the sandbox never reached a failed state: it is still {sandbox.status!r} "
            f"{EXIT_AT_ONCE_TIMEOUT:.0f}s after the start command exited"
        )
        assert sandbox.last_error, "the sandbox failed without a reason"

    with allure.step("4. the dead sandbox is deletable, and so is the agent after it"):
        sandbox.delete()
        refused: Optional[Dict[str, Any]] = None
        try:
            agent.delete()
        except APIError as exc:
            refused = _payload(exc)
        show(
            "sandbox.delete then agent.delete",
            method="sandbox.delete() then agent.delete()",
            args={"sandbox_id": sandbox.id, "slug": agent.slug},
            returns={"sandbox_deleted": sandbox.id, "agent_delete_refused": refused},
        )
        assert refused is None, (
            f"the agent cannot be deleted after its sandbox was "
            f"({refused['status_code']} {refused['message']}) — it stays in the account"
        )


# --------------------------------------------------------------------------
# T-05 a metadata-only change must not rebuild
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-05 a metadata-only change must not rebuild")
@allure.severity(P1)
@pytest.mark.slow
def test_t05_changing_the_title_or_start_command_does_not_rebuild(register, run_id):
    with allure.step("1. register the agent and build the image once"):
        agent = register("t05")
        assert wait_build(agent) == "ready", (
            "the image never built, so a rebuild cannot be told from a first build"
        )
        before = fingerprint(agent)

    with allure.step("2. changing only the title reused the built image"):
        agent.update(title=f"sdk-test-b0-t05-renamed-{run_id}")
        after_title = fingerprint(agent.refresh())
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

    with allure.step("3. change only the start command"):
        refusal: Optional[APIError] = None
        try:
            agent.update(start_cmd="sleep 30")
        except APIError as exc:
            refusal = exc
        after_cmd = fingerprint(agent.refresh())
        show(
            "agents.update(start_cmd=...)",
            method="agent.update(start_cmd=...) then agent.refresh()",
            args={"start_cmd": "sleep 30"},
            returns={
                "raised": _payload(refusal) if refusal else None,
                "before": after_title,
                "after": after_cmd,
            },
        )
        assert refusal is None, (
            f"the start command cannot be changed after registration "
            f"({refusal.status_code} {refusal.message}) — it takes a new agent"
        )

    with allure.step("4. changing the start command reused the built image"):
        assert after_cmd["template_build_status"] in _READY, (
            "changing the start command kicked off a rebuild"
        )
        assert after_cmd["upstream_template_id"] == after_title["upstream_template_id"], (
            "changing only the start command replaced the built image "
            f"({after_title['upstream_template_id']} -> {after_cmd['upstream_template_id']}), "
            "so a one-line command change costs a full build"
        )


# --------------------------------------------------------------------------
# T-06 deleting an agent that still has a running sandbox
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("T-06 deleting an agent that still has a running sandbox")
@allure.severity(P0)
@pytest.mark.slow
def test_t06_deleting_an_agent_with_a_running_sandbox_is_refused(
    test_client, register, tracker, instance_type
):
    with allure.step("1. register the agent and build the image"):
        agent = register("t06")
        assert wait_build(agent) == "ready", "the image never built, so nothing can be launched"

    with allure.step("2. launch it and wait until the sandbox is running"):
        sandbox = tracker.track_sandbox(agent.launch(instance_type=instance_type))
        sandbox.wait_until_running(timeout=RUN_TIMEOUT)
        show(
            "agent.launch -> running",
            method="agent.launch(instance_type=...) then sandbox.wait_until_running()",
            args={"instance_type": instance_type},
            returns=sandbox,
        )
        assert sandbox.status == "running", f"the sandbox is {sandbox.status!r}, not running"

    with allure.step("3. deleting the agent is refused while the sandbox runs"):
        refusal: Optional[APIError] = None
        orphan: Any = None
        try:
            agent.delete()
        except APIError as exc:
            refusal = exc
        else:
            try:
                orphan = _payload(test_client.sandboxes.get(sandbox.id))
            except APIError as exc:
                orphan = _payload(exc)

        show(
            "agent.delete with a running sandbox",
            method="agent.delete()" if refusal is not None
            else "agent.delete() (accepted) then test_client.sandboxes.get(sandbox_id)",
            args={"slug": agent.slug},
            returns=refusal if refusal is not None
            else {"deleted": True, "sandbox_after_delete": orphan},
        )
        assert refusal is not None, (
            f"PRD violation: the agent was deleted while sandbox {sandbox.id} was running; "
            f"the attachment says whether it is still up and billing"
        )
        assert refusal.message, "the refusal carried no reason"
