"""B-1 Sandbox startup and readiness — one test per case row.

| ID   | P  | Action                                      | Expected                                            |
|------|----|---------------------------------------------|-----------------------------------------------------|
| C-01 | P0 | launch x20, time the id and time to running | id: P95 <= 1s, P99 <= 5s; time to running recorded  |
| C-02 | P0 | upload + execute x10 the instant it runs    | all ten succeed                                     |
| C-03 | P0 | read back the expiry, launch config and env | an expiry exists and is stable; config reads back;  |
|      |    |                                             | the listing filters by agent and by status          |

Every row launches real sandboxes through the `instance_type` fixture (the
smallest SKU), and every sandbox is tracked, so an assertion that fires
mid-row still leaves nothing running.

Two behaviours of this account shape C-01: the org allows five concurrent
sandboxes per SKU, so the twenty launches run in waves that are deleted before
the next starts; and a launch the backend cannot satisfy is not refused on the
call — it returns an id, and the rejection arrives seconds later as a task in
`error` attached to no agent, which is why the wave gate reads the unfiltered
list.
"""

from __future__ import annotations

import inspect
import math
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import allure
import pytest
from agentbox_sdk import AgentBoxSDKError, APIError, Sandbox, SandboxWaitTimeout

# The SDK's own answer to "has it arrived?" and "will it never arrive?"; a
# local copy of these drifted from the API and mis-polled stopped sandboxes.
from agentbox_sdk.client import RUNNING_STATUS, WAIT_FAILURE_STATUSES, SandboxCollection

from helpers.report import show

FEATURE = "B-1 Sandbox startup and readiness"
P0 = allure.severity_level.BLOCKER

# C-01. Twenty is what the case row asks for; lower it for a cheap smoke run.
LAUNCH_COUNT = max(1, int(os.getenv("GMI_TEST_LAUNCH_COUNT", "20")))
# This account allows five concurrent sandboxes per instance type.
LAUNCH_CONCURRENCY = max(1, int(os.getenv("GMI_TEST_LAUNCH_CONCURRENCY", "5")))
ID_P95_MAX = 1.0
ID_P99_MAX = 5.0

RUNNING_DEADLINE = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))
# One second rather than the SDK's two: this is the error bar on every
# time-to-running number the row reports.
RUNNING_POLL = 1.0
RELEASE_POLL = 2.0
# A deleted sandbox still counts against the quota until it is really gone.
RELEASE_DEADLINE = 120.0

# C-02
WORK_ROUNDS = 10
# A deadline no wait can meet, to check what the SDK's own wait does with it.
IMPOSSIBLE_WAIT = 0.01

# C-03. The SDK cannot ask for a lifetime, so the row reads back what the
# backend granted rather than an amount this test chose.
EXPIRY_RECHECK_AFTER = 5.0
ENV_MARKER_NAME = "C03_MARKER"
ENV_MARKER_VALUE = "b1-c03"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile: with twenty samples P95 is the second slowest."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarise(values: List[float]) -> Any:
    if not values:
        return "no samples"
    return {
        "n": len(values),
        "min_s": round(min(values), 3),
        "median_s": round(percentile(values, 50), 3),
        "p95_s": round(percentile(values, 95), 3),
        "p99_s": round(percentile(values, 99), 3),
        "max_s": round(max(values), 3),
    }


def parse_timestamp(value: str) -> datetime:
    """Parse an API timestamp, trimming the nanoseconds `fromisoformat` rejects."""
    text = re.sub(r"\.(\d{6})\d+", r".\1", value.replace("Z", "+00:00"))
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def poll_until_running(sandbox: Sandbox, started: float, *, deadline: float) -> Dict[str, Any]:
    """How long this sandbox took to run, and — when it did not — why.

    Hand-rolled rather than `wait_until_running`: it runs on its own thread
    from the moment this sandbox has an id, so no startup time includes the
    launches that followed it.
    """
    stop_at = started + deadline
    status: Optional[str] = None
    while time.monotonic() < stop_at:
        try:
            status = sandbox.refresh().status
        except AgentBoxSDKError:
            status = None  # a flake mid-poll is not an answer either way
        if status == RUNNING_STATUS:
            return {"seconds": time.monotonic() - started, "status": status}
        if status in WAIT_FAILURE_STATUSES:
            return {
                "seconds": None,
                "id": sandbox.id,
                "status": status,
                "last_error": sandbox.last_error,
                "why": "reached a terminal status; it will never run",
            }
        time.sleep(RUNNING_POLL)
    return {
        "seconds": None,
        "id": sandbox.id,
        "status": status,
        "why": f"still {status!r} after the {deadline:.0f}s deadline",
    }


def launch_wave(
    agent, tracker, instance_type: str, *, size: int, first: int
) -> Dict[str, Any]:
    """One wave: launch `size` sandboxes one at a time, poll each on its own thread.

    Launches stay sequential — issuing five at once would time contention
    rather than `launch`. Leaving the pool joins every poller, so the wave is
    fully measured before anything in it is deleted.
    """
    sandboxes: List[Sandbox] = []
    samples: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    polls: List[Future] = []

    with ThreadPoolExecutor(max_workers=size) as pollers:
        for offset in range(size):
            started = time.monotonic()
            try:
                sandbox = agent.launch(instance_type=instance_type)
            except APIError as exc:
                refused.append(
                    {
                        "at_launch_number": first + offset,
                        "raised": type(exc).__name__,
                        "status_code": exc.status_code,
                        "message": exc.message,
                    }
                )
                continue
            elapsed = time.monotonic() - started
            tracker.track_sandbox(sandbox)  # tracked before anything else can fail
            sandboxes.append(sandbox)
            samples.append(
                {
                    "launch": first + offset,
                    "sandbox_id": sandbox.id,
                    "time_to_id_s": round(elapsed, 3),
                    "time_to_running_s": None,
                }
            )
            polls.append(
                pollers.submit(poll_until_running, sandbox, started, deadline=RUNNING_DEADLINE)
            )

    for sample, result in zip(samples, [poll.result() for poll in polls]):
        seconds = result["seconds"]
        sample["time_to_running_s"] = round(seconds, 3) if seconds is not None else None
        sample["status"] = result.get("status")
        if seconds is None:
            sample["never_ran"] = result.get("why")
    return {"sandboxes": sandboxes, "samples": samples, "refused": refused}


def discard(sandboxes: List[Sandbox]) -> Dict[str, Any]:
    """Delete now, not at teardown — each one bills for as long as it is up."""
    deleted: List[str] = []
    failed: List[Dict[str, Any]] = []
    for sandbox in sandboxes:
        try:
            sandbox.delete()
        except APIError as exc:
            if exc.status_code == 404:
                deleted.append(sandbox.id)  # already gone
            else:
                failed.append(
                    {"id": sandbox.id, "status_code": exc.status_code, "message": exc.message}
                )
        except AgentBoxSDKError as exc:
            failed.append({"id": sandbox.id, "raised": type(exc).__name__, "message": str(exc)})
        else:
            deleted.append(sandbox.id)
    return {"deleted": len(deleted), "failed": failed}


def wait_until_released(test_client, agent_id: str, *, deadline: float) -> Dict[str, Any]:
    """Block until nothing is holding quota, so the next wave can have it.

    Counted unfiltered: the quota is per instance type across the org, and a
    launch the backend accepts then refuses leaves a task attached to no agent
    at all. Terminal sandboxes hold nothing and are excluded.
    """

    def counted() -> Dict[str, Any]:
        try:
            page = test_client.sandboxes.list(page_size=100)
        except AgentBoxSDKError as exc:
            return {"holding_quota": None, "read_failed": f"{type(exc).__name__}: {exc}"}
        holding = [item for item in page.items if item.status not in WAIT_FAILURE_STATUSES]
        return {
            "holding_quota": len(holding),
            "this_agent": sum(1 for item in holding if item.agent_id == agent_id),
            "other_owners": sum(1 for item in holding if item.agent_id != agent_id),
        }

    stop_at = time.monotonic() + deadline
    seen = counted()
    while seen.get("holding_quota") != 0 and time.monotonic() < stop_at:
        time.sleep(RELEASE_POLL)
        seen = counted()
    return seen


# --------------------------------------------------------------------------
# C-01 launch returns an id at once, twenty times over
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("C-01 launch returns an id at once, twenty times over")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_c01_launch_returns_an_id_immediately(
    test_client, session_agent, tracker, instance_type
):
    with allure.step("1. launch every sandbox, in waves the quota allows"):
        samples: List[Dict[str, Any]] = []
        refused: List[Dict[str, Any]] = []
        done = 0

        while done < LAUNCH_COUNT:
            size = min(LAUNCH_CONCURRENCY, LAUNCH_COUNT - done)
            wave = launch_wave(
                session_agent,
                tracker,
                instance_type,
                size=size,
                first=done + 1,
            )
            samples.extend(wave["samples"])
            refused.extend(wave["refused"])
            # Freed before the next wave, or it is refused on quota. Anything
            # this cannot delete is retried and reported by the tracker.
            discard(wave["sandboxes"])
            wait_until_released(test_client, session_agent.id, deadline=RELEASE_DEADLINE)
            done += size

        id_latency = [s["time_to_id_s"] for s in samples]
        to_running = [
            s["time_to_running_s"] for s in samples if s["time_to_running_s"] is not None
        ]
        lost_after_accept = [s for s in samples if s["time_to_running_s"] is None]
        stats = summarise(id_latency)
        show(
            "agent.launch in waves, each call timed",
            method="agent.launch(instance_type=...); each sandbox polled on its own thread "
            "from the moment its id arrived; each wave deleted before the next",
            args={
                "launches": LAUNCH_COUNT,
                "concurrency": LAUNCH_CONCURRENCY,
                "instance_type": instance_type,
                "poll_interval_s": RUNNING_POLL,
            },
            returns={
                "accepted": len(id_latency),
                "refused": refused,
                "time_to_id": stats,
                "thresholds": {"p95_s": ID_P95_MAX, "p99_s": ID_P99_MAX},
                "time_to_running (recorded, not judged)": summarise(to_running),
                "accepted an id then never ran": lost_after_accept,
                "every launch": samples,
            },
        )

    with allure.step("2. every launch was accepted"):
        assert not refused, f"{len(refused)} of {LAUNCH_COUNT} launches were refused: {refused}"

    with allure.step("3. an accepted id means a sandbox that exists"):
        assert not lost_after_accept, (
            f"{len(lost_after_accept)} of {LAUNCH_COUNT} launches returned an id and then "
            f"never ran, so the id is not a promise the sandbox will exist: {lost_after_accept}"
        )

    with allure.step("4. the id comes back inside the budget"):
        assert stats["p95_s"] <= ID_P95_MAX, (
            f"launch took {stats['p95_s']}s at P95, over the {ID_P95_MAX}s budget — "
            f"the caller is being made to wait for more than an id"
        )
        assert stats["p99_s"] <= ID_P99_MAX, (
            f"launch took {stats['p99_s']}s at P99, over the {ID_P99_MAX}s budget"
        )


# --------------------------------------------------------------------------
# C-02 a sandbox is usable the instant it reports running
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("C-02 a sandbox is usable the instant it reports running")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_c02_upload_and_execute_the_instant_it_runs(session_agent, tracker, instance_type):
    with allure.step("1. launch a sandbox of this row's own, so the flip can be caught"):
        sandbox = tracker.track_sandbox(session_agent.launch(instance_type=instance_type))
        sandbox.refresh()
        status_when_asked = sandbox.status

    with allure.step("2. the SDK's own wait answers for the state it finds"):
        if status_when_asked == RUNNING_STATUS:
            returned = sandbox.wait_until_running(timeout=IMPOSSIBLE_WAIT)
            verdict = "returned without raising — the condition was already met"
            assert returned.status == RUNNING_STATUS, (
                f"wait_until_running returned a sandbox in {returned.status!r}; it must only "
                f"return once the sandbox is running"
            )
        else:
            with pytest.raises(SandboxWaitTimeout):
                sandbox.wait_until_running(timeout=IMPOSSIBLE_WAIT)
            verdict = f"raised SandboxWaitTimeout on a {IMPOSSIBLE_WAIT}s deadline"
        show(
            "the SDK's own wait, given a deadline it cannot meet",
            method=f"sandbox.wait_until_running(timeout={IMPOSSIBLE_WAIT})",
            returns={"status when asked": status_when_asked, "result": verdict},
        )

    with allure.step("3. upload + execute every round, starting the instant it runs"):
        sandbox.wait_until_running(timeout=RUNNING_DEADLINE, poll_interval=RUNNING_POLL)
        became_running = time.monotonic()
        rounds: List[Dict[str, Any]] = []

        for index in range(1, WORK_ROUNDS + 1):
            path = f"/tmp/c02-{index}.txt"
            record: Dict[str, Any] = {
                "round": index,
                "since_running_s": round(time.monotonic() - became_running, 3),
            }
            try:
                sandbox.upload_file(path=path, file=f"round {index}\n".encode("utf-8"))
                execution = sandbox.execute(f"cat {path}")
                stdout = execution.data.get("stdout") or ""
                record.update(
                    {
                        "ok": execution.exit_code == 0 and f"round {index}" in stdout,
                        "exit_code": execution.exit_code,
                        "stdout": stdout.strip(),
                    }
                )
            except APIError as exc:
                record.update(
                    {
                        "ok": False,
                        "raised": type(exc).__name__,
                        "status_code": exc.status_code,
                        "message": exc.message,
                    }
                )
            rounds.append(record)

        show(
            "upload + execute, every round",
            method="sandbox.upload_file(path=..., file=...) then sandbox.execute('cat ...')",
            args={
                "rounds": WORK_ROUNDS,
                "first_attempt_delay_s": rounds[0]["since_running_s"],
                "measured from": (
                    f"the SDK's first observation of 'running', polled every {RUNNING_POLL}s — "
                    f"a zero means 'nothing was waited out', not 'zero seconds after the flip'"
                ),
            },
            returns={"succeeded": sum(1 for record in rounds if record["ok"]), "rounds": rounds},
        )

    with allure.step("4. all ten rounds succeeded"):
        failed = [record for record in rounds if not record["ok"]]
        assert not failed, (
            f"{len(failed)} of {WORK_ROUNDS} upload+execute rounds failed right after the "
            f"sandbox reported running — 'running' does not yet mean 'usable': {failed}"
        )


# --------------------------------------------------------------------------
# C-03 expiry, metadata and env read back as applied
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("C-03 expiry, metadata and env read back as applied")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_c03_expiry_and_launch_config_read_back(
    test_client, session_agent, tracker, instance_type
):
    with allure.step("1. launch with env and a display name, and wait until it runs"):
        env = [{"name": ENV_MARKER_NAME, "value": ENV_MARKER_VALUE, "secret": False}]
        display_name = f"c03-{int(time.time())}"
        launched_at = datetime.now(timezone.utc)
        sandbox = tracker.track_sandbox(
            session_agent.launch(
                instance_type=instance_type, env=env, display_name=display_name
            )
        )
        sandbox.wait_until_running(timeout=RUNNING_DEADLINE, poll_interval=RUNNING_POLL)
        show(
            "agent.launch with env and a display name",
            method="agent.launch(instance_type=..., env=..., display_name=...)",
            args={"env": env, "display_name": display_name},
            returns=sandbox,
        )

    with allure.step("2. an expiry exists, and it does not move between two reads"):
        first = sandbox.expires_at
        granted = (
            round((parse_timestamp(first) - launched_at).total_seconds(), 1) if first else None
        )
        time.sleep(EXPIRY_RECHECK_AFTER)
        sandbox.refresh()
        second = sandbox.expires_at
        show(
            "sandbox.expires_at, read twice",
            method=f"sandbox.expires_at, then sandbox.refresh() after {EXPIRY_RECHECK_AFTER:.0f}s",
            args={"launched_at": launched_at.isoformat()},
            returns={
                "expires_at (first read)": first,
                "expires_at (after refresh)": second,
                "unchanged": first == second,
                "lifetime_granted_s": granted,
                "note": "the SDK cannot ask for a lifetime; this is whatever the backend "
                "chose. Measured 2026-09-23 it is a rolling 24h idle window: every execute() "
                "pushes it to now + 24h, while a refresh() — what this step does — does not, "
                "which is why two reads apart agree",
            },
        )
        assert first, (
            "the sandbox reports no expires_at at all, so nothing in the SDK bounds its "
            "lifetime and a forgotten sandbox bills until a human notices it"
        )
        assert first == second, (
            f"expires_at moved between two reads {EXPIRY_RECHECK_AFTER:.0f}s apart "
            f"({first} -> {second}): the deadline slides every time it is looked at"
        )

    with allure.step("3. what was sent at launch reads back"):
        echoed_env = sandbox.data.get("env")
        echoed_marker = next(
            (
                entry.get("value")
                for entry in (echoed_env or [])
                if entry.get("name") == ENV_MARKER_NAME
            ),
            None,
        )
        show(
            "launch settings, read back from the sandbox",
            method="sandbox.refresh() (-> test_client.sandboxes.get(sandbox_id))",
            returns={
                "display_name sent": display_name,
                "display_name read back": sandbox.display_name,
                "env sent": env,
                f"{ENV_MARKER_NAME} echoed as": echoed_marker,
                "instance_type sent": instance_type,
                "instance_type read back": sandbox.instance_type,
                "body fields available": sorted(sandbox.data),
            },
        )
        assert sandbox.display_name == display_name, (
            f"display_name came back as {sandbox.display_name!r}, not {display_name!r}"
        )
        assert sandbox.instance_type == instance_type, "instance_type did not read back"
        assert echoed_marker == ENV_MARKER_VALUE, (
            f"{ENV_MARKER_NAME} was sent as {ENV_MARKER_VALUE!r} at launch but the sandbox "
            f"body reads back {echoed_marker!r} — launch config does not survive the round trip"
        )

    with allure.step("4. the running process actually has that env"):
        probe = sandbox.execute(f"printenv {ENV_MARKER_NAME}")
        in_process = (probe.data.get("stdout") or "").strip()
        show(
            "the env the running process actually has",
            method=f"sandbox.execute('printenv {ENV_MARKER_NAME}')",
            returns={
                "exit_code": probe.exit_code,
                "stdout": in_process,
                "sent at launch": ENV_MARKER_VALUE,
                "why this is asked as well as the echo": (
                    "the echo shows the backend kept the request body; only this shows the "
                    "variable reached the process the sandbox actually runs"
                ),
            },
        )
        assert in_process == ENV_MARKER_VALUE, (
            f"the API echoes {ENV_MARKER_NAME}={echoed_marker!r} but the running process has "
            f"{in_process!r} — the sandbox was not given what launch accepted"
        )

    with allure.step("5. the listing filters by agent and by status"):
        accepted_filters = [
            name for name in inspect.signature(SandboxCollection.list).parameters if name != "self"
        ]
        listed = test_client.sandboxes.list(agent_id=session_agent.id, status=RUNNING_STATUS)
        show(
            "sandboxes.list",
            method="test_client.sandboxes.list(agent_id=..., status='running')",
            args={
                "agent_id": session_agent.id,
                "status": RUNNING_STATUS,
                "filters the SDK accepts": accepted_filters,
            },
            returns={
                "total": listed.total,
                "ids": [item.id for item in listed.items],
            },
        )
        assert sandbox.id in [item.id for item in listed.items], (
            f"sandbox {sandbox.id} is running under {session_agent.slug} but the filtered "
            f"listing does not return it"
        )
        wrong_agent = [item.id for item in listed.items if item.agent_id != session_agent.id]
        assert not wrong_agent, f"agent_id=... returned sandboxes of other agents: {wrong_agent}"
        wrong_status = [
            (item.id, item.status) for item in listed.items if item.status != RUNNING_STATUS
        ]
        assert not wrong_status, (
            f"status='running' returned sandboxes that are not: {wrong_status}"
        )
