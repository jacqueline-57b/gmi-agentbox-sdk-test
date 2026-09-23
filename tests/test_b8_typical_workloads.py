"""B-8 Typical workloads — one test per case row.

| ID   | P  | Action                                              | Expected                                              |
|------|----|-----------------------------------------------------|-------------------------------------------------------|
| W-01 | P1 | one sandbox: write -> run -> edit, 200 times        | never throttled; P50/P95 recorded                     |
| W-02 | P1 | 10 sandboxes in parallel for 10 minutes, mixing an  | they do not affect each other; solo and loaded P95    |
|      |    | infinite loop, large output and `rm -rf`            | recorded                                              |
| W-03 | P1 | 200MB and a 15-minute async job, the client exits   | the deadline is never taken back; the result is       |
|      |    | and comes back                                      | complete; nothing accrues past the delete             |

Every row is `billable`. W-01 shares the session sandbox; W-02 and W-03 launch
their own and delete them as soon as they are done.

Two measurements from earlier chapters shape what these rows can claim. This
organization admits somewhere between five and eight concurrent sandboxes of
the smallest instance type (B-1 C-01, B-5 R-02), so W-02 fires the ten its case
row asks for and reports how many it actually got rather than assuming ten. And
`capabilities.metrics` is false on this runtime (B-1 C-03), so W-03's usage leg
records what the SDK can answer instead of asserting a number it cannot read.
`expires_at` is a rolling 24h idle window rather than a fixed lifetime — every
`execute()` pushes it to now + 24h, a `refresh()` does not — so W-03 holds the
service to a deadline that never moves *earlier*, which is the promise a caller
can actually plan around.

Percentiles are recorded, not judged: what they should be depends on the data
center's load that minute, and a build turning red on it would be noise. What
each row asserts is the behaviour — no refusals, no crossed effects, a result
that survives.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import allure
import pytest
from agentbox_sdk import AgentBoxClient, APIError, Sandbox

from helpers.clock import parse_timestamp
from helpers.report import _payload, show
from helpers.stats import summarise

FEATURE = "B-8 Typical workloads"
P1 = allure.severity_level.CRITICAL

pytestmark = [pytest.mark.slow, pytest.mark.billable]

RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))

# W-01. Two hundred write/run/edit cycles is what the case row asks for; lower
# it for a cheap smoke run.
W01_CYCLES = int(os.getenv("GMI_TEST_W01_CYCLES", "200"))
W01_PATH = "/home/user/w01.txt"

# W-02. Ten sandboxes for ten minutes. The fleet size is what the row asks for,
# not what the quota grants — the row reads back how many it got.
W02_SANDBOXES = int(os.getenv("GMI_TEST_W02_SANDBOXES", "10"))
W02_DURATION = float(os.getenv("GMI_TEST_W02_DURATION", "600"))
# Long enough for a stable baseline, short enough not to pad the row.
W02_SOLO_SECONDS = float(os.getenv("GMI_TEST_W02_SOLO", "60"))
W02_PROBE_EVERY = 2.0
# The probe is deliberately trivial: it measures the platform's round trip, not
# the sandbox's ability to compute.
W02_PROBE = "echo w02-probe"
# One per neighbour, round-robin. Each runs until its sandbox is deleted.
W02_WORKLOADS = {
    "cpu": "sh -c 'while true; do :; done'",
    "output": "sh -c 'while true; do tr \"\\0\" \"x\" < /dev/zero | head -c 2000000; done'",
    "churn": (
        "sh -c 'while true; do mkdir -p /tmp/w02/a/b/c; "
        "dd if=/dev/zero of=/tmp/w02/a/b/c/f bs=1M count=50 2>/dev/null; rm -rf /tmp/w02; done'"
    ),
}

# W-03. 200MB and a quarter of an hour, with the client thrown away in between.
W03_MB = int(os.getenv("GMI_TEST_W03_MB", "200"))
W03_DURATION = int(os.getenv("GMI_TEST_W03_DURATION", "900"))
W03_PATH = "/home/user/w03-payload.bin"
W03_RESULT = "/home/user/w03-result.txt"
W03_POLL = 15.0

LIVE_STATUSES = ("running", "creating")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def launch_sandbox(session_agent, tracker, instance_type):
    """A sandbox this row owns, tracked for teardown."""

    def _launch(*, wait: bool = True) -> Sandbox:
        if not session_agent.launchable:
            pytest.skip(f"agent {session_agent.slug} is not launchable")
        sandbox = tracker.track_sandbox(session_agent.launch(instance_type=instance_type))
        if wait:
            sandbox.wait_until_running(timeout=RUN_TIMEOUT)
        return sandbox

    return _launch


def timed(call) -> Dict[str, Any]:
    """Run one SDK call, and report how long it took and how it answered."""
    started = time.monotonic()
    try:
        result = call()
    except APIError as exc:
        return {"seconds": time.monotonic() - started, "ok": False, **_payload(exc)}
    except Exception as exc:  # noqa: BLE001 - a transport failure is a finding too
        return {
            "seconds": time.monotonic() - started,
            "ok": False,
            "raised": type(exc).__name__,
            "message": str(exc),
        }
    return {"seconds": time.monotonic() - started, "ok": True, "result": result}


def probe(sandbox: Sandbox, seconds: float) -> List[Dict[str, Any]]:
    """Run the same trivial command for `seconds`, timing every round trip."""
    samples: List[Dict[str, Any]] = []
    stop = time.monotonic() + seconds
    while time.monotonic() < stop:
        record = timed(lambda: sandbox.execute(W02_PROBE))
        execution = record.pop("result", None)
        record["exit_code"] = execution.exit_code if execution is not None else None
        samples.append(record)
        time.sleep(W02_PROBE_EVERY)
    return samples


def phase_ok(record: Dict[str, Any]) -> bool:
    """A phase is only done if the call returned *and* the command exited 0."""
    if not record["ok"]:
        return False
    return getattr(record.get("result"), "exit_code", 0) in (0, None)


def latencies(samples: List[Dict[str, Any]]) -> List[float]:
    return [item["seconds"] for item in samples if item["ok"]]


def still_live(sandbox: Sandbox) -> Dict[str, Any]:
    """The sandbox's status now, with a 404 reported rather than raised."""
    try:
        sandbox.refresh()
        return {"id": sandbox.id, "status": sandbox.status, "last_error": sandbox.last_error}
    except APIError as exc:
        return {"id": sandbox.id, "status": "unreadable", **_payload(exc)}


# --------------------------------------------------------------------------
# W-01 a write / run / edit loop is never throttled
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("W-01 a write / run / edit loop is never throttled")
@allure.severity(P1)
def test_w01_a_write_run_edit_loop_is_never_throttled(session_sandbox):
    with allure.step("1. run the write / run / edit cycle the case row asks for"):
        cycles: List[Dict[str, Any]] = []
        for index in range(1, W01_CYCLES + 1):
            started = time.monotonic()
            wrote = timed(
                lambda: session_sandbox.upload_file(
                    path=W01_PATH, file=f"cycle {index}\n".encode("utf-8")
                )
            )
            ran = timed(lambda: session_sandbox.execute(f"wc -c < {W01_PATH}"))
            edited = timed(
                lambda: session_sandbox.execute(f"sh -c 'echo edit-{index} >> {W01_PATH}'")
            )
            phases = {"write": wrote, "run": ran, "edit": edited}
            record: Dict[str, Any] = {
                "cycle": index,
                "cycle_s": round(time.monotonic() - started, 3),
                "ok": all(phase_ok(phase) for phase in phases.values()),
            }
            for name, phase in phases.items():
                record[f"{name}_s"] = round(phase["seconds"], 3)
                if not phase_ok(phase):
                    record[f"{name}_failed"] = {
                        key: value for key, value in phase.items() if key != "result"
                    } | {"exit_code": getattr(phase.get("result"), "exit_code", None)}
            cycles.append(record)

        done = [item for item in cycles if item["ok"]]
        # A service that throttles shows it either as a refusal or as latency
        # that climbs with the call count, so both are reported.
        quarter = max(1, len(done) // 4)
        show(
            "the write / run / edit loop",
            method="sandbox.upload_file(...), sandbox.execute('wc -c < ...'), "
            "sandbox.execute('echo ... >> ...'), in a loop",
            args={"cycles": W01_CYCLES, "path": W01_PATH},
            returns={
                "completed": len(done),
                "failed": [item for item in cycles if not item["ok"]],
                "cycle": summarise([item["cycle_s"] for item in done]),
                "write": summarise([item["write_s"] for item in done]),
                "run": summarise([item["run_s"] for item in done]),
                "edit": summarise([item["edit_s"] for item in done]),
                "first_quarter_cycle": summarise([item["cycle_s"] for item in done[:quarter]]),
                "last_quarter_cycle": summarise([item["cycle_s"] for item in done[-quarter:]]),
            },
        )

    with allure.step("2. nothing in the loop was refused"):
        failed = [item for item in cycles if not item["ok"]]
        assert not failed, (
            f"{len(failed)} of {W01_CYCLES} cycles did not complete — a loop this shape is "
            f"what an agent does all day, and it was throttled or errored: {failed[:5]}"
        )

    with allure.step("3. the loop really did the work, 200 times over"):
        tail = session_sandbox.execute(f"wc -l < {W01_PATH}")
        show(
            "what the last cycle left behind",
            method=f"sandbox.execute('wc -l < {W01_PATH}')",
            returns={"lines": (tail.data.get("stdout") or "").strip(), "expected": "2"},
        )
        # Each cycle rewrites the file and appends one line, so the file the
        # loop leaves behind is two lines — proof the write and the edit both
        # landed, rather than the calls merely returning 200.
        assert (tail.data.get("stdout") or "").strip() == "2", (
            "the file the loop leaves behind is not what one write plus one edit produces"
        )


# --------------------------------------------------------------------------
# W-02 a loaded fleet does not disturb its neighbours
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("W-02 a loaded fleet does not disturb its neighbours")
@allure.severity(P1)
def test_w02_parallel_sandboxes_do_not_affect_each_other(session_agent, tracker, instance_type):
    with allure.step("1. launch the fleet the case row asks for"):
        def start(_: int) -> Optional[Sandbox]:
            try:
                sandbox = session_agent.launch(instance_type=instance_type)
            except APIError:
                return None
            tracker.track_sandbox(sandbox)
            return sandbox

        with ThreadPoolExecutor(max_workers=W02_SANDBOXES) as pool:
            launched = [item for item in pool.map(start, range(W02_SANDBOXES)) if item]

        def settle(sandbox: Sandbox) -> Optional[Sandbox]:
            try:
                sandbox.wait_until_running(timeout=RUN_TIMEOUT)
                return sandbox
            except Exception:  # noqa: BLE001 - a sandbox that never ran is not a neighbour
                return None

        with ThreadPoolExecutor(max_workers=max(1, len(launched))) as pool:
            fleet = [item for item in pool.map(settle, launched) if item]

        show(
            "the fleet",
            method="agent.launch(instance_type=...) x N, then wait_until_running on each",
            args={"asked_for": W02_SANDBOXES},
            returns={
                "launch_accepted": len(launched),
                "reached_running": len(fleet),
                "shortfall": W02_SANDBOXES - len(fleet),
                "note": "the concurrent-sandbox quota is B-5 R-03's subject, not this row's; "
                "this row reports the fleet it got and measures isolation across it",
                "ids": [sandbox.id for sandbox in fleet],
            },
        )
        assert len(fleet) >= 2, (
            f"only {len(fleet)} sandbox(es) reached running, so there is no neighbour to be "
            f"disturbed by and nothing this row can measure"
        )

    with allure.step("2. measure the probe sandbox while the fleet is idle"):
        measured, neighbours = fleet[0], fleet[1:]
        solo = probe(measured, W02_SOLO_SECONDS)
        show(
            "the probe, with every neighbour idle",
            method=f"sandbox.execute({W02_PROBE!r}) every {W02_PROBE_EVERY:.0f}s",
            args={"sandbox_id": measured.id, "seconds": W02_SOLO_SECONDS},
            returns={
                "latency": summarise(latencies(solo)),
                "failed": [item for item in solo if not item["ok"]],
            },
        )

    with allure.step("3. load every neighbour, and keep measuring"):
        names = list(W02_WORKLOADS)
        assigned: List[Dict[str, Any]] = []
        for index, neighbour in enumerate(neighbours):
            name = names[index % len(names)]
            record = timed(lambda: neighbour.execute(W02_WORKLOADS[name], wait=False))
            record.pop("result", None)
            assigned.append({"sandbox_id": neighbour.id, "workload": name, **record})

        loaded = probe(measured, W02_DURATION)
        show(
            "the probe, with every neighbour loaded",
            method="sandbox.execute(<workload>, wait=False) on each neighbour, then the same "
            "probe on the measured sandbox",
            args={
                "seconds": W02_DURATION,
                "workloads": {item["sandbox_id"]: item["workload"] for item in assigned},
            },
            returns={
                "solo_latency": summarise(latencies(solo)),
                "loaded_latency": summarise(latencies(loaded)),
                "workloads_accepted": [item for item in assigned if not item["ok"]] or "all",
                "failed_probes": [item for item in loaded if not item["ok"]],
            },
        )

    with allure.step("4. the loaded neighbours did not disturb the sandbox next door"):
        broken = [item for item in loaded if not item["ok"]]
        assert not broken, (
            f"{len(broken)} of {len(loaded)} probes failed while the neighbours were loaded, "
            f"so work on one sandbox reaches another: {broken[:3]}"
        )
        wrong_exit = [item for item in loaded if item.get("exit_code") not in (0, None)]
        assert not wrong_exit, f"the probe command started failing under load: {wrong_exit[:3]}"

    with allure.step("5. every sandbox in the fleet survived its own workload"):
        final = [still_live(sandbox) for sandbox in fleet]
        show(
            "the fleet after the run",
            method="sandbox.refresh() on each",
            returns=final,
        )
        dead = [item for item in final if item["status"] != "running"]
        assert not dead, (
            f"{len(dead)} sandbox(es) did not survive: an infinite loop, a flood of output or "
            f"an rm -rf ended the sandbox running it: {dead}"
        )


# --------------------------------------------------------------------------
# W-03 a long job outlives the client that started it
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("W-03 a long job outlives the client that started it")
@allure.severity(P1)
def test_w03_a_long_job_survives_the_client_and_keeps_its_result(launch_sandbox):
    with allure.step("1. lay down the payload and note what the sandbox promised"):
        sandbox = launch_sandbox()
        sandbox_id = sandbox.id
        expiry_at_launch = sandbox.expires_at

        written = sandbox.execute(
            f"sh -c 'dd if=/dev/urandom of={W03_PATH} bs=1M count={W03_MB} 2>/dev/null; "
            f"md5sum {W03_PATH}'",
            wait=True,
            wait_timeout_seconds=300,
        )
        checksum = (written.data.get("stdout") or "").split()[0] if written.exit_code == 0 else ""
        show(
            "the payload",
            method=f"sandbox.execute('dd ... count={W03_MB}; md5sum ...')",
            args={"path": W03_PATH, "megabytes": W03_MB},
            returns={
                "exit_code": written.exit_code,
                "md5": checksum,
                "expires_at": expiry_at_launch,
            },
        )
        assert written.exit_code == 0 and checksum, (
            f"the {W03_MB}MB payload was not written: {written.status!r} / "
            f"{(written.data.get('stderr') or '')!r}"
        )

    with allure.step("2. start the long job asynchronously, then throw the client away"):
        job = sandbox.execute(
            f"sh -c 'sleep {W03_DURATION}; md5sum {W03_PATH} > {W03_RESULT}; "
            f"wc -c < {W03_PATH} >> {W03_RESULT}'",
            wait=False,
        )
        execution_id = job.id
        assert execution_id, "the async execute returned no id to come back with"
        # `expires_at` is a rolling 24h idle window (measured 2026-09-23: every
        # execute pushes it to now + 24h, a refresh does not), so the deadline
        # this row holds the service to is the one set by the last activity —
        # starting the job — not the one read at launch.
        sandbox.refresh()
        expiry_before_the_wait = sandbox.expires_at
        show(
            "the long job",
            method="sandbox.execute('sleep ...; md5sum ...', wait=False)",
            args={"seconds": W03_DURATION, "execution_id": execution_id},
            returns={
                "execution": _payload(job.data),
                "expires_at (at launch)": expiry_at_launch,
                "expires_at (once the job was started)": expiry_before_the_wait,
            },
        )
        del sandbox, job

    with allure.step("3. a new client picks the job up from nothing but the two ids"):
        recovered = AgentBoxClient().sandboxes.get(sandbox_id)
        assert recovered.id == sandbox_id, "the new client got a different sandbox"

        deadline = time.monotonic() + W03_DURATION + 300
        execution = recovered.get_execution(execution_id)
        while execution.status in ("pending", "running") and time.monotonic() < deadline:
            time.sleep(W03_POLL)
            execution = recovered.get_execution(execution_id)
        show(
            "the job, polled from the new client",
            method="AgentBoxClient().sandboxes.get(id), then sandbox.get_execution(id) in a loop",
            args={"sandbox_id": sandbox_id, "execution_id": execution_id},
            returns={"status": execution.status, "exit_code": execution.exit_code},
        )
        assert execution.status == "succeeded", (
            f"the job the discarded client started ended {execution.status!r}"
        )

    with allure.step("4. the result is complete, and the expiry never moved"):
        result = recovered.execute(f"cat {W03_RESULT}")
        lines = (result.data.get("stdout") or "").split()
        recovered.refresh()
        show(
            "the result and the expiry",
            method=f"sandbox.execute('cat {W03_RESULT}'), then sandbox.refresh()",
            returns={
                "result_file": (result.data.get("stdout") or "").strip(),
                "md5_before": checksum,
                "expires_at (at launch)": expiry_at_launch,
                "expires_at (once the job was started)": expiry_before_the_wait,
                "expires_at (now)": recovered.expires_at,
                "note": "the window rolls forward on activity, so these three differ by "
                "design; what the row holds the service to is that it never moves earlier",
            },
        )
        assert lines and lines[0] == checksum, (
            f"the payload changed under the job: {checksum} at the start, {lines[:1]} at the end"
        )
        assert lines[-1] == str(W03_MB * 1024 * 1024), (
            f"the payload is {lines[-1]} bytes, not the {W03_MB}MB it was written as"
        )
        assert parse_timestamp(recovered.expires_at) >= parse_timestamp(expiry_before_the_wait), (
            f"the expiry moved earlier while the job ran ({expiry_before_the_wait} -> "
            f"{recovered.expires_at}), so the deadline a caller was given can be taken back"
        )
        assert parse_timestamp(recovered.expires_at) > datetime.now(timezone.utc), (
            f"the sandbox is already past its expiry ({recovered.expires_at}) with the job "
            "only just finished — a long run outlives the deadline it was granted"
        )

    with allure.step("5. nothing keeps running once the sandbox is deleted"):
        usage: Any
        try:
            now = int(time.time())
            usage = _payload(recovered.metrics(start=now - 3600, end=now).data)
        except APIError as exc:
            usage = _payload(exc)
        metrics_capability = recovered.capabilities.get("metrics")
        recovered.delete()
        time.sleep(3)
        after = still_live(recovered)
        show(
            "usage, and the sandbox after the delete",
            method="sandbox.metrics(start=..., end=...) then sandbox.delete()",
            returns={
                "capabilities.metrics": metrics_capability,
                "metrics": usage,
                "after_delete": after,
            },
        )
        assert after["status"] not in LIVE_STATUSES, (
            "the sandbox still reports a live status after the delete, so the job — and "
            "whatever it bills — is still running"
        )
