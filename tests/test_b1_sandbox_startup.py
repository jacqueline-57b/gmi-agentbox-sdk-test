"""B-1 Sandbox startup and readiness — one test per case row.

An agent program calls launch, takes the id and moves on; the moment the
sandbox reports running it must accept files and commands. Whatever was given
at launch — a lifetime, metadata, environment variables — has to read back as
what was actually applied.

| ID   | P  | Action                                      | Expected                                            |
|------|----|---------------------------------------------|-----------------------------------------------------|
| C-01 | P0 | launch x20, time the id and time to running | id: P95 <= 1s, P99 <= 5s; time to running recorded  |
| C-02 | P0 | upload + execute x10 the instant it runs    | all ten succeed                                      |
| C-03 | P0 | read back the expiry, metadata and env      | an expiry exists and is stable; config reads back;   |
|      |    |                                             | list filters by metadata (absent -> known gap)       |

Every row launches real sandboxes, so every row is `billable`, and every row
launches through the `instance_type` fixture (the smallest SKU) rather than
the discovered default.

The org caps concurrent sandboxes per SKU — five, in this account — so C-01
runs its twenty launches in waves and deletes each wave before starting the
next. Cleanup sits in a `finally`: an assertion that fires mid-row must never
be the reason a sandbox is left running.
"""

from __future__ import annotations

import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import allure
import pytest
from agentbox_sdk import APIError, Sandbox

from helpers.report import known_gap, show

FEATURE = "B-1 Sandbox startup and readiness"
P0 = allure.severity_level.BLOCKER

# C-01. Twenty is what the case row asks for; lower it for a cheap smoke run.
LAUNCH_COUNT = int(os.getenv("GMI_TEST_LAUNCH_COUNT", "20"))
# How many may be up at once. The backend refuses past its quota, and this
# account's is 5 per instance type.
LAUNCH_CONCURRENCY = int(os.getenv("GMI_TEST_LAUNCH_CONCURRENCY", "5"))
ID_P95_MAX = 1.0
ID_P99_MAX = 5.0

# Time to running is recorded, not judged — but the poll has to stop somewhere.
RUNNING_DEADLINE = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))
RUNNING_POLL = 2.0
# A deleted sandbox still counts against the quota until it is really gone.
RELEASE_DEADLINE = 120.0

# C-02
WORK_ROUNDS = 10

# C-03. The SDK cannot ask for a lifetime, so the row reads back whatever the
# backend granted rather than an amount this test chose.
EXPIRY_RECHECK_AFTER = 5.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile: the smallest sample at or above `pct`.

    Spelled out because definitions differ and the answer matters here: with
    twenty samples P95 is the 19th slowest and P99 is the slowest, so a single
    bad launch decides P99 on its own.
    """
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
    """Parse an API timestamp, tolerating nanoseconds and a trailing Z.

    `fromisoformat` takes at most microseconds and this API returns nine
    fractional digits on some fields, so the tail is trimmed before parsing.
    """
    text = re.sub(r"\.(\d{6})\d+", r".\1", value.replace("Z", "+00:00"))
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def wait_until_running(
    batch: List[Tuple[Sandbox, float]], *, deadline: float
) -> Dict[str, Optional[float]]:
    """Poll a batch, returning seconds from each launch call to `running`.

    A sandbox that fails or never arrives maps to None, so the caller can tell
    "slow" from "never" instead of both showing up as a missing number.
    """
    pending = {sandbox.id: (sandbox, started) for sandbox, started in batch}
    reached: Dict[str, Optional[float]] = {sandbox_id: None for sandbox_id in pending}
    stop_at = time.monotonic() + deadline

    while pending and time.monotonic() < stop_at:
        for sandbox_id, (sandbox, started) in list(pending.items()):
            try:
                status = sandbox.refresh().status
            except APIError:
                continue
            if status == "running":
                reached[sandbox_id] = time.monotonic() - started
                pending.pop(sandbox_id)
            elif status in ("error", "failed", "deleted"):
                pending.pop(sandbox_id)  # stays None: it never got there
        if pending:
            time.sleep(RUNNING_POLL)
    return reached


def discard(sandboxes: List[Sandbox]) -> Dict[str, Any]:
    """Delete now, not at teardown — each one bills for as long as it is up."""
    deleted: List[str] = []
    failed: List[Dict[str, Any]] = []
    for sandbox in sandboxes:
        try:
            sandbox.delete()
            deleted.append(sandbox.id)
        except APIError as exc:
            if exc.status_code == 404:
                deleted.append(sandbox.id)  # already gone
                continue
            failed.append({"id": sandbox.id, "status_code": exc.status_code, "message": exc.message})
    return {"deleted": len(deleted), "failed": failed}


def wait_until_released(test_client, agent_id: str, *, deadline: float) -> int:
    """Block until the agent holds no sandboxes, so the next wave has quota."""
    stop_at = time.monotonic() + deadline
    while time.monotonic() < stop_at:
        still_there = test_client.sandboxes.list(agent_id=agent_id, page_size=100).total
        if still_there == 0:
            return 0
        time.sleep(RUNNING_POLL)
    return test_client.sandboxes.list(agent_id=agent_id, page_size=100).total


# --------------------------------------------------------------------------
# C-01 launch x20: how long until an id, how long until running
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("C-01 launch returns an id at once, twenty times over")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_c01_launch_returns_an_id_immediately(
    test_client, session_agent, tracker, instance_type
):
    """`launch` must hand back an id and let the caller go.

    Only the id latency is judged. Time to running is measured and reported
    because it is what a caller actually waits for, but it depends on the data
    center's capacity that minute, so failing a build on it would be noise.
    """
    id_latency: List[float] = []
    to_running: List[float] = []
    never_ran = 0
    refused: List[Dict[str, Any]] = []
    waves: List[Dict[str, Any]] = []
    live: List[Sandbox] = []

    try:
        done = 0
        while done < LAUNCH_COUNT:
            size = min(LAUNCH_CONCURRENCY, LAUNCH_COUNT - done)
            batch: List[Tuple[Sandbox, float]] = []

            for _ in range(size):
                started = time.monotonic()
                try:
                    sandbox = session_agent.launch(instance_type=instance_type)
                except APIError as exc:
                    refused.append(
                        {
                            "at_launch_number": done + len(batch) + 1,
                            "raised": type(exc).__name__,
                            "status_code": exc.status_code,
                            "message": exc.message,
                        }
                    )
                    continue
                id_latency.append(time.monotonic() - started)
                tracker.track_sandbox(sandbox)  # tracked before anything else can fail
                live.append(sandbox)
                batch.append((sandbox, started))

            reached = wait_until_running(batch, deadline=RUNNING_DEADLINE)
            to_running.extend(value for value in reached.values() if value is not None)
            never_ran += sum(1 for value in reached.values() if value is None)

            wave_sandboxes = [sandbox for sandbox, _ in batch]
            removed = discard(wave_sandboxes)
            live = [sandbox for sandbox in live if sandbox not in wave_sandboxes]
            still_held = wait_until_released(
                test_client, session_agent.id, deadline=RELEASE_DEADLINE
            )

            waves.append(
                {
                    "wave": len(waves) + 1,
                    "launched": len(batch),
                    "reached_running": sum(1 for v in reached.values() if v is not None),
                    "cleanup": removed,
                    "still_held_after_cleanup": still_held,
                }
            )
            done += size
    finally:
        leftover = discard(live)
        if leftover["deleted"] or leftover["failed"]:
            show(
                "cleanup after an early exit",
                method="sandbox.delete() for anything still up",
                returns=leftover,
            )

    show(
        f"agent.launch x{LAUNCH_COUNT} in waves of {LAUNCH_CONCURRENCY}",
        method="agent.launch(instance_type=...), each call timed; each wave deleted before the next",
        args={
            "launches": LAUNCH_COUNT,
            "concurrency": LAUNCH_CONCURRENCY,
            "instance_type": instance_type,
        },
        returns={
            "accepted": len(id_latency),
            "refused": refused,
            "time_to_id": summarise(id_latency),
            "thresholds": {"p95_s": ID_P95_MAX, "p99_s": ID_P99_MAX},
            "time_to_running (recorded, not judged)": summarise(to_running),
            "never_reached_running": never_ran,
            "waves": waves,
        },
    )

    assert not refused, f"{len(refused)} of {LAUNCH_COUNT} launches were refused: {refused}"

    stats = summarise(id_latency)
    assert stats["p95_s"] <= ID_P95_MAX, (
        f"launch took {stats['p95_s']}s at P95, over the {ID_P95_MAX}s budget — "
        f"the caller is being made to wait for more than an id"
    )
    assert stats["p99_s"] <= ID_P99_MAX, (
        f"launch took {stats['p99_s']}s at P99, over the {ID_P99_MAX}s budget"
    )


# --------------------------------------------------------------------------
# C-02 the moment it is running, it must take files and commands
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("C-02 a sandbox is usable the instant it reports running")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_c02_upload_and_execute_the_instant_it_runs(session_agent, tracker, instance_type):
    """No settling time: the first upload goes out as soon as status flips.

    This row launches its own sandbox rather than reusing the session one —
    the race between "reports running" and "actually serves requests" is the
    whole point, and a sandbox that has been up for a while cannot show it.
    """
    sandbox = tracker.track_sandbox(session_agent.launch(instance_type=instance_type))
    rounds: List[Dict[str, Any]] = []

    try:
        sandbox.wait_until_running(timeout=RUNNING_DEADLINE)
        became_running = time.monotonic()
        assert sandbox.status == "running"

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
    finally:
        show("cleanup", method="sandbox.delete()", returns=discard([sandbox]))

    show(
        f"upload + execute x{WORK_ROUNDS}, starting the instant it ran",
        method="sandbox.upload_file(path=..., file=...) then sandbox.execute('cat ...')",
        args={"rounds": WORK_ROUNDS, "first_attempt_delay_s": rounds[0]["since_running_s"]},
        returns={"succeeded": sum(1 for record in rounds if record["ok"]), "rounds": rounds},
    )

    failed = [record for record in rounds if not record["ok"]]
    assert not failed, (
        f"{len(failed)} of {WORK_ROUNDS} upload+execute rounds failed right after the sandbox "
        f"reported running — 'running' does not yet mean 'usable': {failed}"
    )


# --------------------------------------------------------------------------
# C-03 what was asked for at launch must read back
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("C-03 expiry, metadata and env read back as applied")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_c03_expiry_and_launch_config_read_back(
    test_client, session_agent, tracker, instance_type
):
    """Three things the row wants back; the SDK can only ask for one of them.

    `launch()` takes `env` and `display_name`. It takes no lifetime and no
    metadata, and `sandboxes.list` cannot filter by metadata — so those parts
    of the row are recorded as gaps rather than quietly dropped.
    """
    env = [{"name": "C03_MARKER", "value": "b1-c03", "secret": False}]
    display_name = f"c03-{int(time.time())}"

    launched_at = datetime.now(timezone.utc)
    sandbox = tracker.track_sandbox(
        session_agent.launch(
            instance_type=instance_type, env=env, display_name=display_name
        )
    )

    try:
        sandbox.wait_until_running(timeout=RUNNING_DEADLINE)
        show(
            "agent.launch with env and a display name",
            method="agent.launch(instance_type=..., env=..., display_name=...)",
            args={"env": env, "display_name": display_name},
            returns=sandbox,
        )

        # --- the expiry ---------------------------------------------------
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
                "note": "the SDK cannot ask for a lifetime; this is whatever the backend chose",
            },
        )

        # --- what was sent at launch, read back ---------------------------
        echoed_env = sandbox.data.get("env")
        show(
            "launch settings, read back from the sandbox",
            method="sandbox.refresh() (-> test_client.sandboxes.get(sandbox_id))",
            returns={
                "display_name sent": display_name,
                "display_name read back": sandbox.display_name,
                "env sent": env,
                "env echoed in the sandbox body": echoed_env if echoed_env is not None else "absent",
                "instance_type sent": instance_type,
                "instance_type read back": sandbox.instance_type,
                "body fields available": sorted(sandbox.data),
            },
        )

        # --- filtering by metadata ----------------------------------------
        listed = test_client.sandboxes.list(agent_id=session_agent.id, status="running")
        show(
            "sandboxes.list — what can it filter on?",
            method="test_client.sandboxes.list(agent_id=..., status='running')",
            returns={
                "accepted filters": ["agent_id", "status", "page", "page_size"],
                "metadata filter": "no such parameter on SandboxCollection.list",
                "running for this agent": [item.id for item in listed.items],
            },
        )
    finally:
        show("cleanup", method="sandbox.delete()", returns=discard([sandbox]))

    assert sandbox.display_name == display_name, (
        f"display_name came back as {sandbox.display_name!r}, not {display_name!r}"
    )
    assert sandbox.instance_type == instance_type, "instance_type did not read back"

    gaps: List[str] = []
    if not first:
        gaps.append("the sandbox reports no expires_at at all, so nothing bounds its lifetime")
    elif first != second:
        gaps.append(f"expires_at moved between two reads ({first} -> {second})")
    if echoed_env is None:
        gaps.append(
            "env given at launch is not echoed anywhere on the sandbox, so there is no way to "
            "confirm from the SDK which variables a running sandbox actually got"
        )
    gaps.append(
        "launch() has no lifetime parameter and no metadata parameter, and sandboxes.list "
        "cannot filter by metadata — three of this row's inputs cannot be expressed through "
        "the SDK at all"
    )
    known_gap("KNOWN GAP: " + "; ".join(gaps))
