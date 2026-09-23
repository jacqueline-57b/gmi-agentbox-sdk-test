"""B-4 Deletion and expiry — one test per case row.

A sandbox must delete from any state, and billing must stop when it does.
Expiry arrives on its own; the expiry time moves only when someone extends it
explicitly, so reading a sandbox or working in it must never renew it quietly.

| ID   | P  | Action                                              | Expected                                              |
|------|----|-----------------------------------------------------|-------------------------------------------------------|
| D-01 | P0 | delete while running and mid-upload; delete right   | all accepted; the upload errors, what finished stays; |
|      |    | after launch; delete after a failure; delete x3     | it never returns to running; three deletes answer the |
|      |    |                                                     | same, and usage holds one record                      |
| D-02 | P0 | a five-minute expiry, then: leave it alone / read   | each expires on time with the expiry time unmoved; a  |
|      |    | every 10s / execute and upload partway through      | command still running ends "cancelled by expiry"      |
| D-03 | P0 | extend the expiry explicitly (if it exists):        | later moves it; earlier does not shorten it; absurd   |
|      |    | longer / shorter / absurdly large                   | is refused or clamped                                 |
| D-04 | P1 | list after a delete, by default and with status     | the default omits it; a status filter finds it again  |
| D-05 | P1 | the usage record in the Console                     | one record, ending at the delete, flat afterwards.    |
|      |    |                                                     | The SDK has no usage entry point                      |

Every row launches a real sandbox, so every row is `billable`. D-01 is four
rows here — one per action — because each ends its sandbox a different way and
a single row would hide which of the four broke.

Measured against the live service on 2026-09-21, and the numbers decide what
these rows can honestly assert:

  * a sandbox is handed a **rolling 24-hour** expiry and `launch()` has no
    lifetime, ttl or expiry parameter, so the five minutes D-02 asks for cannot
    be requested through the SDK. Measured 2026-09-23, the window is an idle
    timeout rather than a lifetime: every `execute()`, upload and download
    pushes `expires_at` to exactly 24h from that moment, while `refresh()` and
    `sandboxes.get()` leave it alone. D-02b (read every 10s) therefore asks a
    question the service answers correctly, and **D-02c — "working in a sandbox
    does not renew its expiry" — asks one the service already contradicts**;
    whoever re-enables these rows should expect D-02c to fail, and that failure
    to be the real behaviour rather than a flake. The runtime's own control plane once accepted a
    TTL reset against the `container_id` the task carries, which is how these
    rows used to get a five-minute sandbox; that route is not usable here, so
    **D-02a, D-02b and D-02c are skipped** with a TODO rather than rewritten to
    watch the default 24h window. The machinery they need is left in place for
    whenever an expiry can be set again;
  * `Sandbox` exposes no `update`, `extend` or `renew`, so D-03 has nothing to
    call;
  * after a delete, `sandboxes.get(id)` answers 404 and every `list(status=...)`
    combination comes back empty, so D-04's "find it again" has no mechanism.

Those are recorded as gaps with the measurement behind them rather than
skipped, because "cannot be tested" and "is not implemented" read the same in a
report unless the row says which it is.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import allure
import pytest
from agentbox_sdk import APIError, NotFoundError, Sandbox
from agentbox_sdk.client import SandboxCollection

from helpers.report import _payload, known_gap, show

FEATURE = "B-4 Deletion and expiry"
P0 = allure.severity_level.BLOCKER
P1 = allure.severity_level.CRITICAL

pytestmark = [pytest.mark.slow, pytest.mark.billable]

RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))

# A deleted sandbox is not required to vanish the instant delete returns, so
# the rows that check what is left afterwards give it this long to settle.
SETTLE_SECONDS = float(os.getenv("GMI_TEST_DELETE_SETTLE", "10"))

# D-01a. Big enough that the upload is still in flight when the delete lands.
UPLOAD_BYTES = int(os.getenv("GMI_TEST_UPLOAD_BYTES", str(24 * 1024 * 1024)))

# D-01c. B-0 T-04b measured that a start command exiting at once leaves the
# sandbox reporting 'creating' well past 300s, so this row does not wait for a
# 'failed' that never comes — it waits a little, records the state it got, and
# deletes from there. The point of the row is that delete works regardless.
FAILED_WAIT = float(os.getenv("GMI_TEST_FAILED_WAIT", "60"))

# D-02. The row wants a five-minute sandbox; `launch()` cannot ask for one, so
# the runtime's TTL is reset against the task's `container_id` before the
# variant starts. Left alone, `expires_at` is 24 hours from the last thing that
# touched the container.
D02_EXPIRY_SECONDS = int(os.getenv("GMI_TEST_EXPIRY_SECONDS", "300"))
# TODO(D-02): re-enable once a sandbox's expiry can be set. `launch()` has no
# ttl/expiry/lifetime parameter and `Sandbox` has no update, so the only way
# these rows ever got a five-minute sandbox was the runtime's own
# `POST /sandboxes/{container_id}/timeout`, which is not usable here. Without
# it the rows would have to watch the default 24h window, so they are skipped
# rather than left to fail for a reason that is not what they measure.
D02_SKIP = (
    "TODO: no way to set a sandbox expiry — launch() has no ttl parameter and the runtime "
    "TTL endpoint is unavailable, so a five-minute sandbox cannot be created"
)
# How long after `expires_at` the sandbox may still answer before "on time"
# is a lie: expiry is asynchronous on the backend.
D02_GRACE = float(os.getenv("GMI_TEST_EXPIRY_GRACE", "60"))
# A sandbox that is gone before its deadline minus this much expired early.
D02_SKEW = float(os.getenv("GMI_TEST_EXPIRY_SKEW", "5"))
D02_READ_INTERVAL = float(os.getenv("GMI_TEST_EXPIRY_INTERVAL", "10"))
# How long after the reset the deadline is re-read before a row commits to
# waiting it out. Measured 2026-09-23: the runtime accepts the new deadline and
# it is written back to now+24h within seven seconds — the same rolling window
# that every execute, upload and download resets — so a row that does not look
# again spends five minutes waiting for a deadline that is gone.
D02_REVERT_CHECK = float(os.getenv("GMI_TEST_EXPIRY_REVERT_CHECK", "10"))
D02_POLL_INTERVAL = float(os.getenv("GMI_TEST_EXPIRY_POLL", "2"))
# D-02c starts watching the sandbox and its command this long before the
# deadline, so the transition to cancelled is caught without polling for five
# minutes.
D02_WATCH_WINDOW = float(os.getenv("GMI_TEST_EXPIRY_WATCH_WINDOW", "30"))

# The runtime's own control plane. AgentBox tasks are GMI Sandbox instances;
# the service says so itself when an upstream create is refused ("upstream
# create container: business-cloud-api .../api/v2/sandboxes returned ...").
SANDBOX_RUNTIME_PATH = "/api/v2"

# Statuses a sandbox must never come back to once it has been deleted (and the
# live statuses that mean "not yet expired").
ALIVE = ("running", "pending", "creating", "starting")

# What a sandbox reports before it is usable: B-2 E-09 measured `pending` from
# launch, and B-0 T-04b `creating` for a start command that exits at once.
ALIVE_BUT_NOT_RUNNING = tuple(status for status in ALIVE if status != "running")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def launch_sandbox(session_agent, tracker, instance_type):
    """Launch a sandbox this row owns and ends itself."""

    def _launch(*, wait: bool = True) -> Sandbox:
        if not session_agent.launchable:
            pytest.skip(f"agent {session_agent.slug} is not launchable")
        sandbox = tracker.track_sandbox(session_agent.launch(instance_type=instance_type))
        if wait:
            sandbox.wait_until_running(timeout=RUN_TIMEOUT)
        return sandbox

    return _launch


@pytest.fixture
def doomed_agent(test_client, tracker, run_id, sandbox_target, image_url, instance_type):
    """An agent whose start command exits at once, for D-01c."""
    agent = tracker.track_agent(
        test_client.agents.create(
            title=f"sdk-test-b4-doomed-{run_id}",
            image_url=image_url,
            idc=sandbox_target.idc,
            instance_type=instance_type,
            runtime="sandbox",
            start_cmd="exit 1",
        )
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        agent.refresh()
        if agent.template_build_status in (None, "ready"):
            return agent
        if agent.template_build_status == "error":
            pytest.fail(f"the doomed agent's image failed to build: {agent.template_build_error}")
        time.sleep(2)
    pytest.fail(f"the doomed agent's image is still {agent.template_build_status!r}")


def delete_sandbox(sandbox: Sandbox, *, title: str) -> Dict[str, Any]:
    """Delete, and describe what the call answered rather than raising."""
    started = time.monotonic()
    try:
        sandbox.delete()
        outcome: Dict[str, Any] = {"accepted": True}
    except APIError as exc:
        outcome = {"accepted": False, **_payload(exc)}
    except Exception as exc:  # noqa: BLE001 - a transport failure is a finding too
        outcome = {"accepted": False, "raised": type(exc).__name__, "message": str(exc)}
    outcome["elapsed_s"] = round(time.monotonic() - started, 2)
    show(
        title,
        method="sandbox.delete() (-> DELETE /tasks/{id})",
        args={"sandbox_id": sandbox.id},
        returns=outcome,
    )
    return outcome


def look_for(client, sandbox_id: str) -> Dict[str, Any]:
    """Every way the SDK offers to find one sandbox, tried on the same id."""
    found: Dict[str, Any] = {}
    try:
        found["get"] = {"status": client.sandboxes.get(sandbox_id).status}
    except APIError as exc:
        found["get"] = {"status_code": exc.status_code, "message": exc.message}

    found["list_default"] = [
        item.status for item in client.sandboxes.list(page_size=50).items if item.id == sandbox_id
    ]
    # `failed`, `all` and `*` are not statuses this API has. They are asked for
    # anyway because an unrecognised value is ignored rather than rejected —
    # measured 2026-09-23, each returns the whole live list — and a row that
    # only tried real values would never see that.
    for status in (
        "running", "creating", "stopped", "stopping", "deleted", "terminated",
        "error", "failed", "all", "*",
    ):
        try:
            page = client.sandboxes.list(status=status, page_size=50)
            found[f"list_status_{status}"] = [
                item.status for item in page.items if item.id == sandbox_id
            ]
        except APIError as exc:
            found[f"list_status_{status}"] = f"{exc.status_code} {exc.message}"

    return found


def live_keys(found: Dict[str, Any]) -> List[str]:
    """Which lookups in a `look_for` result still show the sandbox alive.

    `look_for` mixes shapes: `get` is a dict (`{"status": "running"}`) while the
    `list_*` entries are lists of statuses (or an error string). Comparing a dict
    against a one-element list — what the first version did — matched nothing, so
    a `get` that came back running slipped past the check. Every row that asks
    "is it really gone" asks through here, so no row checks a narrower set of
    lookups than its neighbour.
    """

    def alive(value: Any) -> bool:
        if isinstance(value, dict):
            return value.get("status") in ALIVE
        if isinstance(value, list):
            return any(item in ALIVE for item in value)
        return False

    return [key for key, value in found.items() if alive(value)]


def watch_status(
    read: Any, *, for_seconds: float, stop: Any, interval: float = 2.0
) -> List[Tuple[float, Optional[str]]]:
    """Poll one status until `stop` says so, recording only the changes.

    Returns `[(seconds_since_start, status), ...]`. Rows that watch a sandbox
    after a delete all want the same shape of evidence — when it changed, not
    how many times it was asked.
    """
    timeline: List[Tuple[float, Optional[str]]] = []
    started = time.monotonic()
    while True:
        status = read()
        elapsed = round(time.monotonic() - started, 1)
        if not timeline or timeline[-1][1] != status:
            timeline.append((elapsed, status))
        if stop(status) or time.monotonic() - started >= for_seconds:
            return timeline
        time.sleep(interval)


def surface_scan(
    hints: Tuple[str, ...], *, callables_only: bool = False, **surfaces: Any
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Every public name on each surface, and the ones a hint matches.

    Two rows here answer "does the SDK offer this at all?" by reading its
    surface; they read it the same way so their evidence is comparable.
    `callables_only` is what keeps D-03 honest: `expires_at` is a read-only
    property, and counting it would answer that row "yes, extending exists"
    because the value can be read.
    """
    listed = {
        where: [
            name
            for name in dir(surface)
            if not name.startswith("_")
            and (not callables_only or callable(getattr(surface, name, None)))
        ]
        for where, surface in surfaces.items()
    }
    matched = {
        where: [name for name in names if any(hint in name for hint in hints)]
        for where, names in listed.items()
    }
    return listed, matched


def _parse_timestamp(value: str) -> datetime:
    """Parse an API timestamp, tolerating nanoseconds and a trailing Z.

    `fromisoformat` takes at most microseconds, and this API returns nine
    fractional digits on some fields (B-1 parse_timestamp measured the same),
    so the tail is trimmed before parsing.
    """
    text = re.sub(r"\.(\d{6})\d+", r".\1", value.replace("Z", "+00:00"))
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _expiry_seconds(sandbox: Sandbox) -> Optional[float]:
    raw = sandbox.expires_at
    if not raw:
        return None
    return (_parse_timestamp(raw) - datetime.now(timezone.utc)).total_seconds()


def _seconds_from_now(value: str) -> float:
    return (_parse_timestamp(value) - datetime.now(timezone.utc)).total_seconds()


def _sleep_until(moment: str, *, offset: float = 0.0) -> None:
    """Sleep until `moment` plus `offset` seconds; never sleep backwards."""
    time.sleep(max(0.0, _seconds_from_now(moment) + offset))


def set_runtime_expiry(client, sandbox: Sandbox, *, seconds: int) -> Dict[str, Any]:
    """Reset the sandbox runtime's TTL, which the AgentBox SDK cannot express.

    The task carries the runtime instance id as `container_id`; the runtime's
    own control plane accepts `POST /sandboxes/{id}/timeout` with the same
    organization key. This is setup only — every action D-02 measures is a
    `Sandbox` call.
    """
    container_id = sandbox.data.get("container_id")
    assert container_id, (
        "the task carries no container_id, so the runtime instance the expiry "
        f"belongs to cannot be addressed: {sorted(sandbox.data)}"
    )
    url = f"{client.base_url.rstrip('/')}{SANDBOX_RUNTIME_PATH}/sandboxes/{container_id}/timeout"
    request = urllib.request.Request(
        url,
        data=json.dumps({"timeout": seconds}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {client.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        pytest.fail(f"resetting the runtime expiry to {seconds}s failed: HTTP {exc.code} {detail}")


def wait_for_sdk_expiry(sandbox: Sandbox, expected: str, *, timeout: float = 60) -> List[str]:
    """Wait until the SDK reads back the runtime's new expiry.

    The task's `expires_at` is hydrated from the runtime, so it can trail the
    reset by a reconcile; this returns what it saw instead of guessing.
    """
    deadline = time.monotonic() + timeout
    observed: List[str] = []
    while True:
        sandbox.refresh()
        observed.append(sandbox.expires_at)
        if sandbox.expires_at and abs(_seconds_from_now(sandbox.expires_at) - _seconds_from_now(expected)) <= 5:
            return observed
        if time.monotonic() >= deadline:
            pytest.fail(
                f"the SDK still reads expires_at={sandbox.expires_at!r} {timeout:.0f}s after the "
                f"runtime answered new_end_at={expected!r}; the task never picked the reset up "
                f"(observed {observed})"
            )
        time.sleep(2)


def probe_life(sandbox: Sandbox) -> Dict[str, Any]:
    """One read of the sandbox: is it alive, and what is its expiry now?

    Only a 404 or a status outside `ALIVE` counts as expired; a call that
    errors some other way is recorded as still alive, because "the service
    could not answer" is not evidence the deadline arrived.
    """
    try:
        sandbox.refresh()
        return {
            "at": datetime.now(timezone.utc).isoformat(),
            "alive": sandbox.status in ALIVE,
            "status": sandbox.status,
            "expires_at": sandbox.expires_at,
            "seconds_from_now": (
                round(_expiry_seconds(sandbox) or 0, 1) if sandbox.expires_at else None
            ),
        }
    except NotFoundError as exc:
        return {
            "at": datetime.now(timezone.utc).isoformat(),
            "alive": False,
            "status": "404",
            "status_code": exc.status_code,
            "message": exc.message,
        }
    except APIError as exc:
        return {
            "at": datetime.now(timezone.utc).isoformat(),
            "alive": True,
            "status": f"error {exc.status_code}",
            "message": exc.message,
        }


def probe_execution(sandbox: Sandbox, execution_id: str) -> Dict[str, Any]:
    """One read of the command's state, including whatever it says when it ends."""
    try:
        execution = sandbox.get_execution(execution_id)
        data = execution.data
        return {
            "readable": True,
            "status": execution.status,
            "exit_code": execution.exit_code,
            "reason": data.get("reason") or data.get("error") or data.get("message"),
            "completed_at": data.get("completed_at"),
        }
    except APIError as exc:
        return {"readable": False, "status_code": exc.status_code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001 - a dropped data plane is a finding too
        return {"readable": False, "raised": type(exc).__name__, "message": str(exc)}


def _is_cancelled(entry: Dict[str, Any]) -> bool:
    haystack = f"{entry.get('status') or ''} {entry.get('reason') or ''}".lower()
    return "cancel" in haystack


def _d02_sandbox(launch_sandbox, test_client) -> Tuple[Sandbox, str]:
    """Launch, reset the runtime TTL to five minutes, and read the deadline back.

    The report lives here rather than in each row: three rows start this way,
    and a reader comparing them needs the three setups described identically.
    `wait_for_sdk_expiry` already fails unless the SDK agrees with the runtime
    to within five seconds, so no row has to assert that again.
    """
    sandbox = launch_sandbox()
    reset = set_runtime_expiry(test_client, sandbox, seconds=D02_EXPIRY_SECONDS)
    deadline = reset["data"]["new_end_at"]
    reads = wait_for_sdk_expiry(sandbox, deadline)

    # Read it once more before committing to the wait. The deadline is accepted
    # and then taken away again, so a row that trusts the first read spends its
    # five minutes watching a deadline that no longer exists and fails at the
    # end as though the expiry were merely late.
    time.sleep(D02_REVERT_CHECK)
    sandbox.refresh()
    held = sandbox.expires_at
    drift = (
        round(_seconds_from_now(held) - _seconds_from_now(deadline), 1) if held else None
    )
    show(
        f"a {D02_EXPIRY_SECONDS}s expiry, set on the runtime and read back through the SDK",
        method=(
            "sandboxes.launch(...), POST /api/v2/sandboxes/{container_id}/timeout, "
            "sandbox.refresh()"
        ),
        args={"container_id": sandbox.data.get("container_id"), "timeout": D02_EXPIRY_SECONDS},
        returns={
            "runtime_old_end_at": reset["data"]["old_end_at"],
            "runtime_new_end_at": deadline,
            "sdk_expires_at": sandbox.expires_at,
            "sdk_seconds_from_now": round(_expiry_seconds(sandbox) or 0, 1),
            "reads_until_the_sdk_agreed": reads,
            f"expires_at {D02_REVERT_CHECK:.0f}s later": held,
            "drift_s": drift,
        },
    )
    if drift is None or abs(drift) > D02_SKEW:
        known_gap(
            f"KNOWN GAP: the deadline this row needs does not hold. The runtime accepted "
            f"{D02_EXPIRY_SECONDS}s and answered new_end_at={deadline!r}, the SDK read it "
            f"back, and {D02_REVERT_CHECK:.0f}s later expires_at is {held!r} — "
            f"{drift if drift is None else f'{drift:.0f}s'} away, i.e. back at the rolling "
            "24h window the account hands out. Nothing in the SDK or the runtime control "
            "plane can give a sandbox a short life, so a five-minute expiry cannot be "
            "observed at all"
        )
        pytest.fail(
            f"the {D02_EXPIRY_SECONDS}s deadline reverted to {held!r} within "
            f"{D02_REVERT_CHECK:.0f}s, so there is no short life to measure"
        )
    return sandbox, deadline


def _distinct_expiries(entries: List[Dict[str, Any]]) -> List[str]:
    """Every distinct `expires_at` a run of probes saw, in order."""
    return sorted({entry["expires_at"] for entry in entries if entry.get("expires_at")})


# --------------------------------------------------------------------------
# D-01a delete while running, with an upload in flight
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-01a deleting a running sandbox with an upload in flight")
@allure.severity(P0)
def test_d01a_delete_while_running_and_uploading(launch_sandbox, test_client):
    sandbox = launch_sandbox()
    settled = f"/tmp/b4-settled-{uuid.uuid4().hex[:8]}.bin"
    in_flight = f"/tmp/b4-inflight-{uuid.uuid4().hex[:8]}.bin"

    with allure.step("1. one upload finishes before anything is deleted"):
        sandbox.upload_file(path=settled, file=b"b4-d01a-settled")
        back = sandbox.download_file(path=settled)
        show(
            "an upload that completed",
            method="sandbox.upload_file(...) then sandbox.download_file(...)",
            args={"path": settled},
            returns={"downloaded": back.content.decode(), "filename": back.filename},
        )
        assert back.content == b"b4-d01a-settled", "a completed upload did not read back"

    with allure.step("2. start a large upload and delete the sandbox under it"):
        payload = b"x" * UPLOAD_BYTES
        upload_result: Dict[str, Any] = {}

        def upload() -> None:
            started = time.monotonic()
            try:
                sandbox.upload_file(path=in_flight, file=payload)
                upload_result.update(completed=True)
            except APIError as exc:
                upload_result.update(completed=False, **_payload(exc))
            except Exception as exc:  # noqa: BLE001 - a reset connection is the likely answer
                upload_result.update(
                    completed=False, raised=type(exc).__name__, message=str(exc)
                )
            upload_result["elapsed_s"] = round(time.monotonic() - started, 2)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(upload)
            time.sleep(0.5)  # let the upload get going
            deletion = delete_sandbox(sandbox, title="sandbox.delete() mid-upload")
            future.result(timeout=300)

        show(
            "the upload that was in flight when the delete landed",
            method="sandbox.upload_file(path=..., file=<%d bytes>) on another thread" % UPLOAD_BYTES,
            args={"path": in_flight, "bytes": UPLOAD_BYTES},
            returns=upload_result,
        )

    with allure.step("3. the delete was accepted even though work was in flight"):
        assert deletion["accepted"], (
            f"delete was refused while an upload was running: {deletion}. The PRD wants a "
            "sandbox to delete from any state"
        )

    with allure.step("4. the sandbox does not come back to life"):
        time.sleep(SETTLE_SECONDS)
        after = look_for(test_client, sandbox.id)
        show(
            "looking for the sandbox after the delete",
            method="sandboxes.get(id) and sandboxes.list(...) on the same id",
            args={"sandbox_id": sandbox.id},
            returns=after,
        )
        alive = live_keys(after)
        assert not alive, f"the deleted sandbox still reports a live status: {alive} / {after}"


# --------------------------------------------------------------------------
# D-01b delete immediately after launch
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-01b deleting a sandbox the instant it was launched")
@allure.severity(P0)
def test_d01b_delete_immediately_after_launch(launch_sandbox, test_client):
    with allure.step("1. launch and delete without waiting for running"):
        # No refresh() between the two calls: launch already answers with a
        # status, and the round trip spent re-reading it is time the sandbox
        # spends becoming running — which is the state this row is trying to
        # delete *before*.
        launched = time.monotonic()
        sandbox = launch_sandbox(wait=False)
        state_at_delete = sandbox.status
        deletion = delete_sandbox(
            sandbox, title=f"sandbox.delete() while status is {state_at_delete!r}"
        )
        deletion["status_at_delete"] = state_at_delete
        deletion["launch_to_delete_s"] = round(time.monotonic() - launched, 2)
        show(
            "how soon after the launch the delete went out",
            method="(none) - timing the two calls above",
            returns=deletion,
        )
        assert deletion["accepted"], (
            f"delete was refused for a sandbox still {state_at_delete!r}: {deletion}"
        )
        if state_at_delete not in ALIVE_BUT_NOT_RUNNING:
            known_gap(
                f"KNOWN GAP: this row deletes a sandbox that has not come up yet, but launch "
                f"answered {state_at_delete!r}, so the delete landed on a sandbox that was "
                f"already past creation and the mid-creation case went unasked"
            )

    with allure.step("2. it never reaches running afterwards"):
        # A sandbox deleted mid-creation could still be finishing its start on
        # the backend; the row watches for a while rather than asking once.
        def status_now() -> Optional[str]:
            try:
                return test_client.sandboxes.get(sandbox.id).status
            except NotFoundError:
                return "404"

        observed = watch_status(
            status_now, for_seconds=SETTLE_SECONDS * 3, stop=lambda status: status == "404"
        )
        show(
            "what the sandbox did after being deleted mid-creation",
            method="sandboxes.get(id) in a loop",
            args={"sandbox_id": sandbox.id},
            returns={"status_at_delete": state_at_delete, "timeline_s": observed},
        )
        came_back = [item for item in observed if item[1] == "running"]
        assert not came_back, (
            f"the sandbox reached running after it was deleted: {observed}. A delete during "
            "creation must not leave a machine that starts anyway and bills"
        )


# --------------------------------------------------------------------------
# D-01c delete a sandbox that never came up
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-01c deleting a sandbox whose start command failed")
@allure.severity(P0)
def test_d01c_delete_a_sandbox_that_failed(doomed_agent, tracker, instance_type, test_client):
    with allure.step("1. launch an agent whose start command exits at once"):
        sandbox = tracker.track_sandbox(doomed_agent.launch(instance_type=instance_type))
        observed = watch_status(
            lambda: sandbox.refresh().status,
            for_seconds=FAILED_WAIT,
            stop=lambda status: status not in ALIVE,
            interval=3.0,
        )
        show(
            "where a sandbox with a dying start command ends up",
            method="sandbox.refresh() in a loop",
            args={"start_cmd": "exit 1", "waited_s": FAILED_WAIT},
            returns={"timeline_s": observed, "final_status": sandbox.status},
        )

    with allure.step("2. delete is accepted whatever state it is in"):
        deletion = delete_sandbox(
            sandbox, title=f"sandbox.delete() while status is {sandbox.status!r}"
        )
        assert deletion["accepted"], (
            f"delete was refused for a sandbox in state {sandbox.status!r}: {deletion}"
        )

    with allure.step("3. it is gone and does not return"):
        time.sleep(SETTLE_SECONDS)
        after = look_for(test_client, sandbox.id)
        show(
            "looking for the failed sandbox after the delete",
            method="sandboxes.get(id) and sandboxes.list(...)",
            args={"sandbox_id": sandbox.id},
            returns=after,
        )
        alive = live_keys(after)
        assert not alive, f"the deleted sandbox still reports a live status: {alive} / {after}"

    with allure.step("4. 'failed' was never actually reached"):
        if sandbox.status in ALIVE or observed[-1][1] in ALIVE:
            known_gap(
                f"KNOWN GAP: a start command that exits at once never puts the sandbox into a "
                f"failed state — after {FAILED_WAIT:.0f}s it still reported "
                f"{observed[-1][1]!r}. This row therefore proves delete works on a sandbox "
                "that never came up, not on one the service calls failed, and B-0 T-04b and "
                "B-2 E-09 measured the same thing: no SDK call reaches a terminal failure"
            )


# --------------------------------------------------------------------------
# D-01d deleting three times
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-01d deleting the same sandbox three times")
@allure.severity(P0)
def test_d01d_deleting_three_times_gives_the_same_answer(launch_sandbox):
    sandbox = launch_sandbox()
    outcomes: List[Dict[str, Any]] = []

    with allure.step("1. delete the same sandbox three times"):
        for attempt in range(1, 4):
            outcomes.append(
                {
                    "attempt": attempt,
                    **delete_sandbox(sandbox, title=f"sandbox.delete(), attempt {attempt}"),
                }
            )
            time.sleep(1)
        show(
            "three deletes of one sandbox",
            method="sandbox.delete() x3",
            args={"sandbox_id": sandbox.id},
            returns={"attempts": outcomes},
        )

    with allure.step("2. all three answer the same"):
        # A retrying client cannot tell its own retry from a real error unless
        # repeating the delete is answered the same way every time.
        signatures = [
            (item["accepted"], item.get("status_code")) for item in outcomes
        ]
        distinct = sorted(set(signatures))
        show(
            "the three answers, compared",
            method="(none) - comparing the three deletes above",
            returns={"signatures": signatures, "distinct": distinct},
        )
        if len(distinct) > 1:
            known_gap(
                f"KNOWN GAP: deleting the same sandbox three times answered {len(distinct)} "
                f"different ways ({distinct}). A client that retries a delete after a dropped "
                "connection cannot tell its own successful retry from a real failure, so "
                "every caller has to special-case the second answer"
            )
        assert len(distinct) == 1, f"three identical deletes answered differently: {signatures}"

    with allure.step("3. the first delete was accepted"):
        assert outcomes[0]["accepted"], f"the first delete was refused: {outcomes[0]}"


# --------------------------------------------------------------------------
# D-02 a five-minute expiry
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-02a a sandbox nobody touches expires on time")
@allure.severity(P0)
@pytest.mark.skip(reason=D02_SKIP)
def test_d02a_an_untouched_sandbox_expires_on_time(launch_sandbox, test_client):
    with allure.step("1. give the sandbox a five-minute life and read the deadline back"):
        sandbox, deadline = _d02_sandbox(launch_sandbox, test_client)
        assert sandbox.status in ALIVE, (
            f"the sandbox is {sandbox.status!r} before the idle wait starts, so there is no "
            "five-minute life to watch"
        )
        assert 0 < (_expiry_seconds(sandbox) or 0) <= D02_EXPIRY_SECONDS + D02_GRACE, (
            "the runtime did not install a five-minute deadline"
        )

    with allure.step("2. leave it alone until the deadline"):
        _sleep_until(deadline, offset=2)

    with allure.step("3. the first look after the deadline finds it out of its life"):
        timeline: List[Dict[str, Any]] = []
        started = time.monotonic()
        while time.monotonic() - started < D02_GRACE:
            probe = probe_life(sandbox)
            probe["t_s"] = round(time.monotonic() - started, 1)
            timeline.append(probe)
            if not probe["alive"]:
                break
            time.sleep(D02_POLL_INTERVAL)
        show(
            "the first calls after the deadline",
            method="sandbox.refresh() in a loop, starting after expires_at",
            args={"sandbox_id": sandbox.id, "expires_at": deadline},
            returns={"timeline_s": timeline},
        )

    with allure.step("4. it expired on time"):
        assert not timeline[-1]["alive"], (
            f"the sandbox still answers {timeline[-1].get('status')!r} more than {D02_GRACE:.0f}s "
            f"after its deadline ({deadline}) — five minutes nothing enforces would bill until a "
            "human notices"
        )


# --------------------------------------------------------------------------
# D-02b read it every 10s
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-02b reading a sandbox every 10s does not renew its expiry")
@allure.severity(P0)
@pytest.mark.skip(reason=D02_SKIP)
def test_d02b_reading_every_ten_seconds_does_not_move_the_expiry(launch_sandbox, test_client):
    with allure.step("1. the five-minute deadline read back"):
        sandbox, deadline = _d02_sandbox(launch_sandbox, test_client)
        baseline = sandbox.expires_at

    with allure.step(f"2. read it every {D02_READ_INTERVAL:.0f}s until it expires"):
        reads: List[Dict[str, Any]] = []
        early: Optional[Dict[str, Any]] = None
        started = time.monotonic()
        while True:
            probe = probe_life(sandbox)
            probe["t_s"] = round(time.monotonic() - started, 1)
            reads.append(probe)
            if not probe["alive"]:
                if _seconds_from_now(deadline) > D02_SKEW:
                    early = probe
                break
            remaining = _seconds_from_now(deadline)
            if remaining > 0:
                time.sleep(min(D02_READ_INTERVAL, remaining))
            elif -remaining > D02_GRACE:
                break
            else:
                time.sleep(D02_POLL_INTERVAL)
        show(
            "the expiry across repeated reads",
            method=f"sandbox.refresh() every {D02_READ_INTERVAL:.0f}s until it is gone",
            args={"sandbox_id": sandbox.id, "expires_at": deadline},
            returns={"reads": reads, "distinct_expires_at": _distinct_expiries(reads)},
        )

    with allure.step("3. the deadline never moved, and it arrived on time"):
        distinct = _distinct_expiries(reads)
        assert distinct == [baseline], (
            f"reading the sandbox moved its expiry through {distinct} — polling a sandbox must "
            "not renew it, or a monitoring loop keeps a machine alive for ever"
        )
        assert early is None, (
            f"the sandbox was already gone before its deadline minus {D02_SKEW:.0f}s: {early} "
            f"(deadline {deadline}). An expiry that fires early is as wrong as one that never does"
        )
        assert len(reads) >= 3, "the read loop did not actually repeat"
        assert not reads[-1]["alive"], (
            f"the sandbox still answers {reads[-1].get('status')!r} after its deadline plus "
            f"{D02_GRACE:.0f}s: {reads[-1]}"
        )


# --------------------------------------------------------------------------
# D-02c execute and upload partway through
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-02c working in a sandbox does not renew its expiry")
@allure.severity(P0)
@pytest.mark.skip(reason=D02_SKIP)
def test_d02c_execute_and_upload_do_not_move_the_expiry(launch_sandbox, test_client):
    sandbox, deadline = _d02_sandbox(launch_sandbox, test_client)
    baseline = sandbox.expires_at
    path = f"/tmp/b4-d02-{uuid.uuid4().hex[:8]}.txt"

    with allure.step("1. start a long command and upload a file while it runs"):
        execution = sandbox.execute("sleep 600", wait=False)
        statuses_seen = [execution.status]
        for _ in range(15):
            if execution.status == "running":
                break
            time.sleep(1)
            execution = sandbox.get_execution(execution.id)
            statuses_seen.append(execution.status)
        sandbox.upload_file(path=path, file=b"b4-d02")
        back = sandbox.download_file(path=path)
        sandbox.refresh()
        after_work = sandbox.expires_at
        show(
            "the work done partway through its life",
            method="sandbox.execute('sleep 600', wait=False), sandbox.upload_file(...), "
            "sandbox.download_file(...), sandbox.refresh()",
            args={"command": "sleep 600", "path": path},
            returns={
                "accepted": execution.accepted,
                "execution_id": execution.id,
                "statuses_seen": statuses_seen,
                "downloaded": back.content.decode(),
                "expires_at_before_work": baseline,
                "expires_at_after_work": after_work,
                "moved": after_work != baseline,
            },
        )

    with allure.step("2. the work did not move the deadline"):
        assert execution.id, "the long command was not accepted, so nothing is left running"
        assert back.content == b"b4-d02", "the upload did not land, so the row did not exercise the sandbox"
        assert "running" in statuses_seen, (
            f"the command never reported running ({statuses_seen}); there was nothing to cancel"
        )
        assert after_work == baseline, (
            f"executing a command and uploading a file moved the expiry from {baseline} to "
            f"{after_work} — a busy sandbox would never expire"
        )

    with allure.step("3. watch the deadline and the running command until the sandbox is gone"):
        _sleep_until(deadline, offset=-D02_WATCH_WINDOW)
        timeline: List[Dict[str, Any]] = []
        started = time.monotonic()
        while time.monotonic() - started < D02_WATCH_WINDOW + D02_GRACE:
            control = probe_life(sandbox)
            control["t_s"] = round(time.monotonic() - started, 1)
            command = probe_execution(sandbox, execution.id)
            command["t_s"] = control["t_s"]
            timeline.append({"sandbox": control, "command": command})
            if not control["alive"] and not command["readable"]:
                break
            time.sleep(D02_POLL_INTERVAL)
        show(
            "the deadline and the command's end, read together",
            method="sandbox.refresh() and sandbox.get_execution(...) every few seconds",
            args={"sandbox_id": sandbox.id, "execution_id": execution.id, "expires_at": deadline},
            returns={"timeline_s": timeline},
        )

    with allure.step("4. the deadline never moved and arrived on time"):
        distinct = _distinct_expiries([entry["sandbox"] for entry in timeline])
        assert distinct == [baseline], (
            f"watching the sandbox moved its expiry through {distinct}; it should have stayed "
            f"{baseline}"
        )
        gone = next((entry for entry in timeline if not entry["sandbox"]["alive"]), None)
        assert gone is not None, (
            f"the sandbox still answers after its deadline plus {D02_GRACE:.0f}s: "
            f"{timeline[-1]['sandbox']}"
        )
        late = -_seconds_from_now(deadline)
        assert late <= D02_GRACE, (
            f"the sandbox outlived its deadline by {late:.1f}s, outside the {D02_GRACE:.0f}s "
            "settle window"
        )

    with allure.step("5. the command that was still running ended cancelled by the expiry"):
        commands = [entry["command"] for entry in timeline]
        readable = [entry for entry in commands if entry.get("readable")]
        cancelled = [entry for entry in readable if _is_cancelled(entry)]
        wrong_end = [
            entry["status"]
            for entry in readable
            if entry.get("status") in ("succeeded", "completed", "failed", "error")
        ]
        running_after_gone = [
            entry["command"]
            for entry in timeline
            if not entry["sandbox"]["alive"]
            and entry["command"].get("readable")
            and entry["command"].get("status") in ("running", "pending", "canceling", "cancelling")
        ]
        assert not running_after_gone, (
            f"the sandbox expired but its command still reports running: {running_after_gone}. "
            "A command can only outlive its sandbox by running nowhere and billing nothing"
        )
        assert not wrong_end, (
            f"the command ended {wrong_end} instead of being cancelled by the expiry: {commands}"
        )
        if cancelled:
            show(
                "the command's end state",
                method="(none) - reading the execution probes above",
                returns={
                    "statuses_seen": [entry.get("status") for entry in readable],
                    "cancelled": cancelled[-1],
                    "cancelled_by": "expiry",
                },
            )
        else:
            last = readable[-1] if readable else None
            known_gap(
                "KNOWN GAP: the command's end state could not be read as cancelled. The row "
                "watched the execution while and after the sandbox expired; the last readable "
                f"state was {last}. The API reports `cancelled` for a command a delete or a "
                "cancel ended, and the row cannot tell which of those — if any — the response "
                "would have named for an expiry"
            )


# --------------------------------------------------------------------------
# D-03 extending the expiry
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-03 extending the expiry explicitly")
@allure.severity(P0)
def test_d03_extending_the_expiry(session_sandbox):
    with allure.step("1. look for anything that could move the expiry"):
        hints = ("extend", "renew", "expire", "ttl", "lifetime", "update", "patch", "keepalive")
        surfaces, found = surface_scan(
            hints, callables_only=True, Sandbox=Sandbox, SandboxCollection=SandboxCollection
        )
        show(
            "every public method that could extend an expiry",
            method="(none) - inspecting the SDK surface",
            args={"searched_for": hints},
            returns={"surfaces": surfaces, "matches": found},
        )

    with allure.step("2. record the expiry that cannot be changed"):
        session_sandbox.refresh()
        show(
            "the expiry as it stands",
            method="sandbox.refresh()",
            returns={
                "expires_at": session_sandbox.expires_at,
                "seconds_from_now": round(_expiry_seconds(session_sandbox) or 0),
                "capabilities_expiry": session_sandbox.capabilities.get("expiry"),
            },
        )

    with allure.step("3. there is no way to extend, shorten or overshoot it"):
        assert not any(found.values()), (
            f"something that could move the expiry exists after all: {found} — this row's "
            "Known gap is stale and should drive the three real sub-cases instead"
        )
        known_gap(
            "KNOWN GAP: the SDK cannot extend an expiry. Sandbox and SandboxCollection expose "
            "no extend, renew, update or patch call, and `expires_at` is read-only — so none "
            "of this row's three sub-cases (a later time moves it, an earlier one does not "
            "shorten it, an absurd one is refused or clamped) has anything to call. The "
            f"runtime advertises capabilities.expiry="
            f"{session_sandbox.capabilities.get('expiry')!r}, which promises an expiry exists "
            "but not that a caller can change it"
        )


# --------------------------------------------------------------------------
# D-04 finding a deleted sandbox again
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-04 a deleted sandbox leaves the default list but stays findable")
@allure.severity(P1)
def test_d04_listing_a_deleted_sandbox(launch_sandbox, test_client):
    sandbox = launch_sandbox()
    sandbox_id = sandbox.id

    with allure.step("1. before the delete, the default list contains it"):
        before = look_for(test_client, sandbox_id)
        live_status = before["get"].get("status")
        # Any status filter that returns a *running* sandbox is not selecting on
        # the status it names. Measured here rather than remembered from an
        # earlier run, so the claim in step 4 is this run's own evidence.
        mislabelled = [
            key.replace("list_status_", "")
            for key, value in before.items()
            if key.startswith("list_status_")
            and isinstance(value, list)
            and value
            and key != f"list_status_{live_status}"
        ]
        show(
            "looking for a live sandbox",
            method="sandboxes.get(id) and sandboxes.list(...)",
            args={"sandbox_id": sandbox_id},
            returns={
                **before,
                "status it actually has": live_status,
                "filters that returned it anyway": mislabelled or "none",
            },
        )
        assert before["list_default"], "a running sandbox is missing from the default list"

    with allure.step("2. delete it"):
        deletion = delete_sandbox(sandbox, title="sandbox.delete() before listing again")
        assert deletion["accepted"], f"delete was refused: {deletion}"
        time.sleep(SETTLE_SECONDS)

    with allure.step("3. the default list no longer contains it"):
        after = look_for(test_client, sandbox_id)
        show(
            "looking for the sandbox after the delete",
            method="sandboxes.get(id) and sandboxes.list(...) on the same id",
            args={"sandbox_id": sandbox_id},
            returns=after,
        )
        assert not after["list_default"], (
            f"the deleted sandbox is still in the default list: {after['list_default']}"
        )

    with allure.step("4. a status filter finds it again"):
        recovered = {
            key: value
            for key, value in after.items()
            if key.startswith("list_status_") and value and not isinstance(value, str)
        }
        show(
            "which status filter brings it back",
            method="(none) - reading the lookups above",
            returns={
                "filters_that_found_it": recovered or "none",
                "get_by_id": after["get"],
            },
        )
        if not recovered:
            known_gap(
                "KNOWN GAP: a deleted sandbox cannot be found again by any means the SDK "
                f"offers. sandboxes.get(id) answers {after['get']}, and every "
                "sandboxes.list(status=...) value tried — running, stopped, deleted, "
                "terminated, error — comes back without it. A caller reconciling its own "
                "records against the service has no way to confirm a sandbox it remembers "
                "was deleted rather than never created"
                + (
                    f". Note also that while it was still {live_status!r}, the filters "
                    f"{mislabelled} returned it anyway, so those are not selecting on the "
                    "status they name"
                    if mislabelled
                    else ""
                )
            )
        assert recovered, "no status filter brought the deleted sandbox back"


# --------------------------------------------------------------------------
# D-05 the usage record
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("D-05 one usage record, ending at the delete")
@allure.severity(P1)
def test_d05_usage_is_one_record_that_stops_at_the_delete(launch_sandbox, test_client):
    with allure.step("1. look for a usage or billing entry point"):
        hints = ("usage", "billing", "cost", "invoice", "charge", "spend", "meter")
        surfaces, found = surface_scan(
            hints,
            AgentBoxClient=test_client,
            Sandbox=Sandbox,
            SandboxCollection=SandboxCollection,
        )
        show(
            "every public surface that could carry usage",
            method="(none) - inspecting the SDK surface",
            args={"searched_for": hints},
            returns={"surfaces": surfaces, "matches": found},
        )

    with allure.step("2. the nearest thing the SDK has is metrics, which is not usage"):
        sandbox = launch_sandbox()
        capability = sandbox.capabilities.get("metrics")
        metrics_signature = str(inspect.signature(Sandbox.metrics))
        sample: Any
        if capability:
            now = int(time.time())
            try:
                sample = _payload(sandbox.metrics(start=now - 300, end=now))
            except APIError as exc:
                sample = _payload(exc)
        else:
            sample = "capabilities.metrics is false; the call was not made"
        show(
            "metrics, as a stand-in for usage",
            method="sandbox.metrics(start=..., end=...)" if capability else "(none)",
            returns={
                "capabilities_metrics": capability,
                "signature": metrics_signature,
                "sample": sample,
                "note": "metrics report machine load, not billed time or money",
            },
        )

    with allure.step("3. delete the sandbox — usage should stop here"):
        deletion = delete_sandbox(sandbox, title="sandbox.delete() to stop the meter")
        assert deletion["accepted"], f"delete was refused: {deletion}"
        deleted_at = datetime.now(timezone.utc).isoformat()

    with allure.step("4. whether usage stopped cannot be read from the SDK"):
        known_gap(
            "KNOWN GAP: the SDK has no usage entry point, so this row cannot verify its own "
            f"expectation. Nothing on AgentBoxClient, Sandbox or SandboxCollection matches "
            f"{hints} ({found}), and metrics() reports machine load rather than billed time. "
            f"That one usage record exists, that it ends at the delete ({deleted_at}) and "
            "that it stops growing afterwards are all only checkable by opening the Console "
            "— which also means an automated suite cannot catch a sandbox that keeps billing "
            "after it was deleted"
        )
