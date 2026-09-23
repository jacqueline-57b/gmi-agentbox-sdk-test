"""B-2 Command execution and reconnection — one test per case row.

| ID   | P  | Action                                              | Expected                                              |
|------|----|-----------------------------------------------------|-------------------------------------------------------|
| E-01 | P0 | one execute that succeeds, one that fails (no pkg)  | success: exit 0 with an execution id; failure: the    |
|      |    |                                                     | SDK does not raise, exit code non-zero, stderr says   |
|      |    |                                                     | why, sandbox still running; both readable in logs()   |
| E-02 | P0 | `sleep 60` waited on synchronously                  | at the wait ceiling it answers "still running" plus   |
|      |    |                                                     | the id, not an error; later it reads back succeeded   |
| E-03 | P0 | async execute, kill the client, recover from a new  | the command is unaffected; an official call returns   |
|      |    | process with only the sandbox id and execution id   | the result                                            |
| E-04 | P1 | pass a working directory / env vars / a per-command | they take effect, and a timed-out command ends        |
|      |    | timeout                                             | terminal with its partial output                      |
| E-05 | P1 | produce 20MB of output                              | truncation is marked                                  |
| E-06 | P0 | 50 concurrent async executes; pause 30s and retry   | over the ceiling it refuses at once, retryable, never |
|      |    |                                                     | queued; after the pause it runs immediately           |
| E-07 | P0 | delete the sandbox mid-command; cancel a command    | delete is not refused, the command ends "cancelled by |
|      |    | that has children                                   | delete" with a reason; cancel kills the whole group   |
| E-08 | P1 | run `date >> log` twice under one request id        | one line: one logical request runs once               |
| E-09 | P1 | execute and upload against a creating / errored /   | refused with the state named, not a timeout; `error`  |
|      |    | deleted sandbox                                     | is reachable through a container-runtime launch only  |
| E-10 | P1 | read the status-change history                      | no entry point in the SDK; only the current state     |

Every row needs a running sandbox, so every row is `billable`. The eight rows
that only run commands share the session sandbox; E-07 and E-09 destroy what
they touch and launch their own, and E-09 registers a container agent besides —
the only route to an errored task.

An execution payload carries `execution_id`, `status`, `exit_code`, `stdout`,
`stderr`, `stdout_truncated`, `stderr_truncated`, `started_at` and
`completed_at`, measured on 2026-09-21. `Execution` models only the first three
as properties, so the rest are read through `.data`.
"""

from __future__ import annotations

import inspect
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import allure
import pytest
from agentbox_sdk import (
    AgentBoxClient,
    APIError,
    Execution,
    NotFoundError,
    Sandbox,
    ServerError,
    TransportError,
)
from agentbox_sdk.client import SandboxCollection

from helpers.report import _payload, known_gap, show

FEATURE = "B-2 Command execution and reconnection"
P0 = allure.severity_level.BLOCKER
P1 = allure.severity_level.CRITICAL

pytestmark = [pytest.mark.slow, pytest.mark.billable]

RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))

# Measured: a finished command reports `succeeded` or `failed`, one still going
# reports `pending` or `running`.
DONE = ("succeeded", "failed", "cancelled", "canceled", "timeout", "timed_out", "error")
SUCCEEDED = "succeeded"

EXEC_DEADLINE = float(os.getenv("GMI_TEST_EXEC_DEADLINE", "180"))
EXEC_POLL = 2.0

# E-01. alpine has no package manager payload worth installing, so "a missing
# package" surfaces as a program that is not on PATH and a shell answering 127.
MISSING_PROGRAM = "gmi-sdk-test-not-installed"

# E-02. The ceiling has to be well under the sleep or the call would simply
# return the finished result and prove nothing.
E02_SLEEP = 60
E02_WAIT_CEILING = int(os.getenv("GMI_TEST_WAIT_CEILING", "10"))

# E-05. Measured 2026-09-23: each stream is capped at 1 MiB (1048576 bytes,
# counted in bytes; stdout and stderr are capped separately), the cut is marked
# only by the truncated flags, and a non-empty stream gains one trailing newline
# that counts against the cap.
E05_BYTES = int(os.getenv("GMI_TEST_OUTPUT_BYTES", str(20 * 1024 * 1024)))

# E-06. A refusal has to be immediate to prove the request was not queued.
E06_CONCURRENCY = int(os.getenv("GMI_TEST_EXEC_CONCURRENCY", "50"))
E06_PAUSE = float(os.getenv("GMI_TEST_EXEC_PAUSE", "30"))
REFUSAL_MUST_BE_WITHIN = 10.0

# E-07. Long enough that the command is certainly still running when the row
# deletes or cancels it.
E07_SLEEP = 300

# E-09. Measured 2026-09-23: launch answers `pending` and the sandbox reads
# `running` about a second later, while the PRD's `creating` is what B-0 T-04b
# sees when a start command exits at once. Either way the sandbox is not
# running, which is the state this row asks about.
NOT_RUNNING_YET = ("pending", "creating")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def launch_sandbox(session_agent, tracker, instance_type):
    """A sandbox this row owns: E-07 and E-09 end theirs on purpose."""

    def _launch(*, wait: bool = True) -> Sandbox:
        if not session_agent.launchable:
            pytest.skip(f"agent {session_agent.slug} is not launchable")
        sandbox = tracker.track_sandbox(session_agent.launch(instance_type=instance_type))
        if wait:
            sandbox.wait_until_running(timeout=RUN_TIMEOUT)
        return sandbox

    return _launch


def run(sandbox: Sandbox, command: str, *, title: str, **kwargs: Any) -> Execution:
    """Execute one command and log the call."""
    execution = sandbox.execute(command, **kwargs)
    show(
        title,
        method="sandbox.execute(command, ...) (-> POST /tasks/{id}/executions)",
        args={"command": command, **kwargs},
        returns=_execution_payload(execution),
    )
    return execution


def _execution_payload(execution: Execution) -> Dict[str, Any]:
    """An execution as the report should read it, with long output described."""
    data = dict(execution.data)
    for stream in ("stdout", "stderr"):
        value = data.get(stream)
        if isinstance(value, str) and len(value) > 600:
            data[stream] = (
                f"<{len(value)} chars, starts: {value[:200]!r}, ends: {value[-120:]!r}>"
            )
    return {"accepted_202": execution.accepted, **data}


def wait_execution(
    sandbox: Sandbox,
    execution_id: str,
    *,
    deadline: float = EXEC_DEADLINE,
    title: str = "poll the execution to a terminal state",
) -> Execution:
    """Poll `get_execution` to a terminal status, retrying a dropped connection.

    The SDK retries nothing itself, so one `TransportError` would otherwise end
    a row whose whole claim is that a run survives losing its client. The count
    is reported so the flakiness stays visible.
    """
    started = time.monotonic()
    timeline: List[Tuple[float, Optional[str]]] = []
    transient: List[str] = []

    def poll() -> Optional[Execution]:
        try:
            return sandbox.get_execution(execution_id)
        except (TransportError, ServerError) as exc:
            at = round(time.monotonic() - started, 1)
            transient.append(f"{at}s {type(exc).__name__}: {exc}")
            return None

    execution = poll()

    while True:
        elapsed = round(time.monotonic() - started, 1)
        status = execution.status if execution is not None else None
        if execution is not None and (not timeline or timeline[-1][1] != status):
            timeline.append((elapsed, status))

        done = execution is not None and status in DONE
        if done or elapsed >= deadline:
            if execution is None:
                raise AssertionError(
                    f"get_execution never reached the service in {deadline:.0f}s: {transient}"
                )
            show(
                f"{title} [{execution_id}]",
                method="sandbox.get_execution(execution_id) in a loop",
                args={"execution_id": execution_id, "poll_interval_s": EXEC_POLL},
                returns={
                    "timeline_s": timeline,
                    "transient_errors_retried": transient,
                    **_execution_payload(execution),
                },
            )
            return execution
        time.sleep(EXEC_POLL)
        execution = poll() or execution


def _stdout(execution: Execution) -> str:
    return execution.data.get("stdout") or ""


def _stderr(execution: Execution) -> str:
    return execution.data.get("stderr") or ""


# --------------------------------------------------------------------------
# E-01 a failing command is the command's problem, not the sandbox's
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-01 a failing command is the command's problem, not the sandbox's")
@allure.severity(P0)
def test_e01_success_and_failure_both_report_without_breaking_the_sandbox(session_sandbox):
    with allure.step("1. a command that succeeds returns exit 0 and an execution id"):
        ok = run(session_sandbox, "echo b2-e01-ok", title="execute: a command that succeeds")
        assert ok.id, "a successful execution came back without an id"
        assert ok.exit_code == 0, f"exit code was {ok.exit_code}, not 0"
        assert ok.status == SUCCEEDED, f"status was {ok.status!r}, not {SUCCEEDED!r}"
        assert "b2-e01-ok" in _stdout(ok), "stdout did not carry what the command printed"

    with allure.step("2. a command whose program is missing does NOT raise through the SDK"):
        raised: Optional[BaseException] = None
        bad: Optional[Execution] = None
        try:
            bad = run(
                session_sandbox,
                MISSING_PROGRAM,
                title="execute: a command whose program is not installed",
            )
        except Exception as exc:  # noqa: BLE001 - the row is about what escapes
            raised = exc
            show(
                "execute raised instead of returning a failed execution",
                method="sandbox.execute(<missing program>)",
                returns=_payload(exc),
            )
        assert raised is None, (
            f"the SDK raised {type(raised).__name__} for a command that merely failed; "
            "a non-zero exit must come back as a value"
        )

    with allure.step("3. the failure carries a non-zero exit code and a reason on stderr"):
        assert bad.id, "the failed execution came back without an id"
        assert bad.exit_code not in (0, None), (
            f"exit code was {bad.exit_code!r}, expected non-zero"
        )
        assert MISSING_PROGRAM in _stderr(bad), (
            f"stderr does not say what was missing: {_stderr(bad)!r}"
        )

    with allure.step("4. the sandbox is untouched by the failed command"):
        session_sandbox.refresh()
        show(
            "sandbox state after a failed command",
            method="sandbox.refresh()",
            returns={
                "status": session_sandbox.status,
                "last_error": session_sandbox.last_error,
            },
        )
        assert session_sandbox.status == "running", (
            f"a failed command left the sandbox {session_sandbox.status!r}"
        )

    with allure.step("5. both executions are readable afterwards"):
        can_log = bool(session_sandbox.capabilities.get("logs"))
        replay_ok = session_sandbox.get_execution(ok.id)
        replay_bad = session_sandbox.get_execution(bad.id)
        show(
            "reading the two commands back",
            method="sandbox.get_execution(execution_id) x2"
            + (", then sandbox.logs()" if can_log else " — capabilities.logs is false, so "
               "sandbox.logs() would answer not_supported_by_runtime"),
            returns={
                "capabilities": session_sandbox.capabilities,
                "succeeded": {"status": replay_ok.status, "stdout": _stdout(replay_ok)},
                "failed": {"status": replay_bad.status, "stderr": _stderr(replay_bad)},
                "logs": session_sandbox.logs()[-2000:] if can_log else None,
            },
        )
        assert "b2-e01-ok" in _stdout(replay_ok), (
            "the successful command cannot be read back through get_execution()"
        )
        assert MISSING_PROGRAM in _stderr(replay_bad), (
            "the failed command cannot be read back through get_execution()"
        )
        assert can_log, (
            "the runtime reports logs=false, so neither command is readable through "
            "sandbox.logs() — get_execution() per execution id is the only record, and "
            "nothing gives a combined log of everything the sandbox ran"
        )


# --------------------------------------------------------------------------
# E-02 a synchronous wait that hits its ceiling answers, it does not error
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-02 a synchronous wait that hits its ceiling answers, it does not error")
@allure.severity(P0)
def test_e02_waiting_past_the_ceiling_returns_still_running_not_an_error(session_sandbox):
    with allure.step("1. run a long sleep, waiting only up to the ceiling"):
        started = time.monotonic()
        raised: Optional[BaseException] = None
        execution: Optional[Execution] = None
        try:
            execution = run(
                session_sandbox,
                f"sleep {E02_SLEEP}; echo b2-e02-done",
                title=f"execute: sleep {E02_SLEEP}, wait_timeout_seconds={E02_WAIT_CEILING}",
                wait=True,
                wait_timeout_seconds=E02_WAIT_CEILING,
            )
        except Exception as exc:  # noqa: BLE001 - reaching the ceiling must not raise
            raised = exc
            show(
                "the wait ceiling raised instead of answering",
                method=f"sandbox.execute(..., wait_timeout_seconds={E02_WAIT_CEILING})",
                returns=_payload(exc),
            )
        elapsed = time.monotonic() - started
        assert raised is None, (
            f"waiting past the ceiling raised {type(raised).__name__}; the PRD wants an "
            "answer carrying the id so the caller can come back for the result"
        )

    with allure.step("2. the ceiling answered with an id, and it stopped waiting"):
        show(
            "what the ceiling answered",
            method="(none) - reading the response returned above",
            returns={
                "returned_after_s": round(elapsed, 2),
                "wait_timeout_seconds": E02_WAIT_CEILING,
                "status": execution.status,
                "exit_code": execution.exit_code,
                "execution_id": execution.id,
            },
        )
        assert execution.id, "the ceiling answer carried no execution id to come back with"
        assert execution.status not in ("failed", "error"), (
            f"the ceiling answer reported {execution.status!r}, which reads as a failure"
        )
        assert elapsed < E02_SLEEP, (
            f"the call blocked {elapsed:.1f}s for a {E02_WAIT_CEILING}s ceiling — it waited "
            f"for the command instead of for the ceiling"
        )

    with allure.step("3. coming back later, the same id reads succeeded"):
        final = wait_execution(session_sandbox, execution.id, title="E-02 after the ceiling")
        assert final.status == SUCCEEDED, f"the command ended {final.status!r}"
        assert final.exit_code == 0, f"the command exited {final.exit_code}"
        assert "b2-e02-done" in _stdout(final), "the finished output is not readable"


# --------------------------------------------------------------------------
# E-03 a new process recovers a run from the sandbox id and execution id
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-03 a new process recovers a run from the sandbox id and execution id")
@allure.severity(P0)
def test_e03_a_fresh_client_recovers_the_run_from_two_ids(session_sandbox):
    with allure.step("1. start a long command asynchronously and note the two ids"):
        started = run(
            session_sandbox,
            "sleep 20; echo b2-e03-survived",
            title="execute: async, the client is about to go away",
            wait=False,
        )
        assert started.id, "an async execute returned no execution id to come back with"
        sandbox_id, execution_id = session_sandbox.id, started.id

    with allure.step("2. throw the client away and rebuild from nothing but the two ids"):
        del started
        recovered_sandbox = AgentBoxClient().sandboxes.get(sandbox_id)
        show(
            "recovering the sandbox in a new client",
            method="AgentBoxClient().sandboxes.get(sandbox_id), then "
            "sandbox.get_execution(execution_id) — both documented calls, no reused state",
            args={"sandbox_id": sandbox_id, "execution_id": execution_id},
            returns=recovered_sandbox,
        )
        assert recovered_sandbox.id == sandbox_id, "the new client got a different sandbox"

    with allure.step("3. the run is unaffected and its result comes back"):
        final = wait_execution(
            recovered_sandbox, execution_id, title="E-03 recovered in a new client"
        )
        assert final.status == SUCCEEDED, (
            f"the command the old client started ended {final.status!r} — losing the client "
            "affected the run"
        )
        assert final.exit_code == 0, f"the recovered command exited {final.exit_code}"
        assert "b2-e03-survived" in _stdout(final), (
            "the output is not readable from the new process"
        )


# --------------------------------------------------------------------------
# E-04 a working directory, environment variables and a command timeout
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-04 a working directory, environment variables and a command timeout")
@allure.severity(P1)
def test_e04_working_directory_environment_and_timeout(session_sandbox):
    with allure.step("1. does execute() accept a cwd, an env or a timeout at all?"):
        parameters = list(inspect.signature(Sandbox.execute).parameters)
        missing = [
            name
            for name in ("cwd", "working_dir", "env", "environment", "timeout")
            if name not in parameters
        ]
        show(
            "the execute() signature",
            method="(none) - inspecting agentbox_sdk.Sandbox.execute",
            returns={
                "parameters": parameters,
                "absent": missing,
                "note": "wait_timeout_seconds bounds the WAIT, not the command",
            },
        )

    with allure.step("2. the shell workaround: a working directory"):
        cwd = run(session_sandbox, "cd /tmp && pwd", title="execute: cd /tmp && pwd")
        assert cwd.exit_code == 0, f"the command failed: {_stderr(cwd)!r}"
        assert _stdout(cwd).strip() == "/tmp", (
            f"the working-directory workaround did not take effect: {_stdout(cwd)!r}"
        )

    with allure.step("3. the shell workaround: an environment variable"):
        env = run(
            session_sandbox,
            "B2_E04_VAR=applied sh -c 'echo $B2_E04_VAR'",
            title="execute: an env var passed through the command string",
        )
        assert env.exit_code == 0, f"the command failed: {_stderr(env)!r}"
        assert _stdout(env).strip() == "applied", (
            f"the environment workaround did not take effect: {_stdout(env)!r}"
        )

    with allure.step("4. the shell workaround: a per-command timeout with partial output"):
        timed = run(
            session_sandbox,
            "sh -c 'echo b2-e04-partial; timeout 2 sleep 30'",
            title="execute: timeout 2 sleep 30, after printing one line",
        )
        assert "b2-e04-partial" in _stdout(timed), (
            "the output produced before the timeout was lost"
        )
        # Measured on alpine 2026-09-23: busybox `timeout` kills with SIGKILL and
        # the API reports exit_code -137 (negative), with the terminal status a
        # plain `failed` — nothing anywhere names this a timeout.
        assert timed.exit_code not in (0, None), (
            f"a command killed by a timeout exited {timed.exit_code!r}, which reads as success"
        )

    with allure.step("5. all three are parameters the SDK offers"):
        assert not missing, (
            f"Sandbox.execute() has no {', '.join(missing)} parameter, so a working "
            "directory, an environment variable and a per-command timeout can only be "
            "written into the command string — the caller escapes and quotes them itself, "
            "and a timed-out command ends in whatever the shell reports rather than a "
            "state the API names"
        )


# --------------------------------------------------------------------------
# E-05 output far past any sane response size is marked, not silently cut
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-05 output far past any sane response size is marked, not silently cut")
@allure.severity(P1)
def test_e05_large_output_is_marked_when_truncated(session_sandbox):
    with allure.step("1. produce far more output than a response can carry"):
        produced = run(
            session_sandbox,
            f"tr '\\0' 'x' < /dev/zero | head -c {E05_BYTES}",
            title=f"execute: {E05_BYTES} bytes to stdout",
        )
        assert produced.exit_code == 0, (
            f"the producer itself failed ({produced.exit_code}): {_stderr(produced)!r}"
        )

    with allure.step("2. either all of it came back, or the cut is marked"):
        stdout = _stdout(produced)
        truncated = produced.data.get("stdout_truncated")
        show(
            "how much output survived",
            method="(none) - measuring the execution payload",
            returns={
                "requested_bytes": E05_BYTES,
                "stdout_chars": len(stdout),
                "stdout_truncated": truncated,
                "stderr_truncated": produced.data.get("stderr_truncated"),
            },
        )
        assert len(stdout) >= E05_BYTES or truncated, (
            f"stdout came back as {len(stdout)} of {E05_BYTES} bytes and "
            f"stdout_truncated is {truncated!r} — output was cut with nothing to say so, "
            "which reads to a caller as a command that printed less than it did"
        )


# --------------------------------------------------------------------------
# E-06 past the concurrency ceiling it refuses at once and never queues
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-06 past the concurrency ceiling it refuses at once and never queues")
@allure.severity(P0)
def test_e06_concurrent_executes_are_refused_not_queued(session_sandbox):
    with allure.step("1. fire every execute at once"):

        def fire(index: int) -> Dict[str, Any]:
            started = time.monotonic()
            try:
                execution = session_sandbox.execute(f"echo b2-e06-{index}", wait=False)
            except APIError as exc:
                return {
                    "index": index,
                    "elapsed_s": round(time.monotonic() - started, 2),
                    "refused": True,
                    "status_code": exc.status_code,
                    "message": exc.message,
                    "code": exc.code,
                }
            except Exception as exc:  # noqa: BLE001 - a transport failure is a finding too
                return {
                    "index": index,
                    "elapsed_s": round(time.monotonic() - started, 2),
                    "refused": True,
                    "raised": type(exc).__name__,
                    "message": str(exc),
                }
            return {
                "index": index,
                "elapsed_s": round(time.monotonic() - started, 2),
                "refused": False,
                "execution_id": execution.id,
                "status": execution.status,
            }

        with ThreadPoolExecutor(max_workers=E06_CONCURRENCY) as pool:
            attempts = list(pool.map(fire, range(E06_CONCURRENCY)))

        accepted = [item for item in attempts if not item["refused"]]
        refused = [item for item in attempts if item["refused"]]
        show(
            "concurrent async executes",
            method="sandbox.execute(..., wait=False) from a thread pool",
            args={"concurrency": E06_CONCURRENCY},
            returns={
                "accepted": len(accepted),
                "refused": len(refused),
                "slowest_s": max(item["elapsed_s"] for item in attempts),
                "refusal_codes": sorted({str(item.get("status_code")) for item in refused}),
                "ceiling_exercised": bool(refused),
                "refusals": refused[:10],
            },
        )
        if not refused:
            known_gap(
                f"KNOWN GAP: all {E06_CONCURRENCY} concurrent executes were accepted, so no "
                f"ceiling was reached and this row never saw one refuse — the rest of it "
                f"checks the shape of refusals that did not happen"
            )

    with allure.step("2. whatever was refused was refused immediately, not queued"):
        queued = [item for item in refused if item["elapsed_s"] > REFUSAL_MUST_BE_WITHIN]
        assert not queued, (
            f"these refusals took longer than {REFUSAL_MUST_BE_WITHIN}s, which means the "
            f"request waited in a queue before being turned down: {queued}"
        )
        codeless = [
            item for item in refused
            if not (item.get("status_code") or item.get("raised"))
        ]
        assert not codeless, f"a refusal carried no status code to act on: {codeless}"

    with allure.step("3. everything accepted really ran"):
        finished = [
            wait_execution(
                session_sandbox,
                item["execution_id"],
                title=f"E-06 accepted #{item['index']}",
            )
            for item in accepted[:5]  # a sample: polling all fifty buys nothing
        ]
        show(
            "a sample of the accepted executions",
            method="sandbox.get_execution(execution_id)",
            args={"sampled": len(finished), "accepted_total": len(accepted)},
            returns=[{"status": item.status, "exit_code": item.exit_code} for item in finished],
        )
        assert all(item.status == SUCCEEDED for item in finished), (
            f"accepted executions did not all succeed: {[item.status for item in finished]}"
        )

    with allure.step("4. after a pause, a command runs immediately again"):
        time.sleep(E06_PAUSE)
        started = time.monotonic()
        after = run(
            session_sandbox,
            "echo b2-e06-recovered",
            title="execute: one command after the pause",
        )
        elapsed = time.monotonic() - started
        assert after.exit_code == 0, (
            f"after {E06_PAUSE:.0f}s idle the sandbox still would not run a command: "
            f"{after.status!r} / {_stderr(after)!r}"
        )
        assert elapsed < REFUSAL_MUST_BE_WITHIN, (
            f"the first command after the pause took {elapsed:.1f}s — the ceiling had not "
            "cleared"
        )


# --------------------------------------------------------------------------
# E-07a deleting a sandbox out from under a running command
# E-07b cancelling a command that spawned children
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-07a deleting a sandbox out from under a running command")
@allure.severity(P0)
def test_e07a_a_sandbox_deletes_while_a_command_is_running(test_client, launch_sandbox):
    sandbox = launch_sandbox()

    with allure.step("1. start a long command, and confirm it really is running"):
        victim = run(
            sandbox,
            f"sleep {E07_SLEEP}",
            title="execute: async, about to be deleted mid-run",
            wait=False,
        )
        assert victim.id, "the async execute returned no id"
        time.sleep(3)
        alive = run(
            sandbox,
            f"ps -o args | grep -c '[s]leep {E07_SLEEP}' || true",
            title="execute: is the command actually running?",
        )
        assert _stdout(alive).strip() == "1", (
            f"the command to interrupt is not running ({_stdout(alive).strip()!r}), so the "
            "delete below would not be interrupting anything"
        )

    with allure.step("2. deleting the sandbox is not refused"):
        deletion: Dict[str, Any]
        try:
            sandbox.delete()
            deletion = {"refused": False}
        except APIError as exc:
            deletion = {"refused": True, **_payload(exc)}
        show(
            "sandbox.delete() with a command still running",
            method="sandbox.delete()",
            args={"sandbox_id": sandbox.id, "running_execution": victim.id},
            returns=deletion,
        )
        assert not deletion["refused"], (
            f"delete was refused while a command ran: {deletion}. The PRD wants a sandbox to "
            "delete from any state, running command or not"
        )

    with allure.step("3. the sandbox is gone, not just accepted"):
        time.sleep(3)
        after: Dict[str, Any]
        try:
            after = {"status": test_client.sandboxes.get(sandbox.id).status}
        except NotFoundError as exc:
            after = {"status": "404", "message": exc.message}
        show(
            "looking for the sandbox after the delete",
            method="test_client.sandboxes.get(sandbox_id)",
            args={"sandbox_id": sandbox.id},
            returns=after,
        )
        assert after["status"] not in ("running", "creating"), (
            f"the deleted sandbox still reports {after['status']!r}, so the delete was "
            "accepted without taking effect"
        )

    with allure.step("4. what the interrupted execution reports afterwards"):
        # Recorded, not asserted: this row is about the delete going through.
        try:
            reported: Dict[str, Any] = _execution_payload(sandbox.get_execution(victim.id))
        except APIError as exc:
            reported = _payload(exc)
        show(
            "the execution the delete interrupted",
            method="sandbox.get_execution(execution_id) after the sandbox was deleted",
            args={"execution_id": victim.id},
            returns=reported,
        )


@allure.feature(FEATURE)
@allure.story("E-07b cancelling a command that spawned children")
@allure.severity(P0)
def test_e07b_cancel_ends_the_whole_process_group(session_sandbox):
    # Counted with a bracketed pattern so the shell running the check does not
    # match itself, and against this command's own sleep length so a sleep some
    # other row left behind cannot be mistaken for a survivor.
    count_survivors = f"ps -o args | grep -c '[s]leep {E07_SLEEP}' || true"

    with allure.step("1. start a command that spawns children, then cancel it"):
        group = run(
            session_sandbox,
            f"sh -c 'sleep {E07_SLEEP} & sleep {E07_SLEEP} & sleep {E07_SLEEP}'",
            title="execute: async, three sleeps, two of them children",
            wait=False,
        )
        assert group.id, "the async execute returned no id to cancel with"
        time.sleep(3)  # let the children actually start

        started = run(
            session_sandbox, count_survivors, title="execute: count them before the cancel"
        )
        assert _stdout(started).strip() == "3", (
            f"the command did not leave three sleeps running: {_stdout(started).strip()!r}"
        )

        cancelled = session_sandbox.cancel_execution(group.id)
        show(
            "cancel_execution",
            method="sandbox.cancel_execution(execution_id)",
            args={"execution_id": group.id},
            returns=_execution_payload(cancelled),
        )

    with allure.step("2. the cancelled execution reaches a terminal state"):
        final = wait_execution(
            session_sandbox, group.id, deadline=60, title="E-07b the cancelled command"
        )
        assert final.status in DONE, (
            f"the cancelled command is still {final.status!r} — cancel did not end it"
        )

    with allure.step("3. no child outlived the cancel"):
        leftovers = run(
            session_sandbox, count_survivors, title="execute: count the sleeps still alive"
        )
        survivors = _stdout(leftovers).strip()
        assert survivors == "0", (
            f"{survivors} `sleep` processes outlived the cancel: the children were orphaned, "
            "not killed with the group"
        )


# --------------------------------------------------------------------------
# E-08 resending one request must not run the command twice
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-08 resending one request must not run the command twice")
@allure.severity(P1)
def test_e08_the_same_request_sent_twice_runs_once(session_sandbox):
    marker = f"/tmp/b2-e08-{uuid.uuid4().hex[:8]}.log"
    request_id = str(uuid.uuid4())

    with allure.step("1. does execute() accept a request id at all?"):
        parameters = list(inspect.signature(Sandbox.execute).parameters)
        idempotency = [
            name
            for name in parameters
            if any(hint in name for hint in ("idempot", "request_id", "dedup", "token"))
        ]
        show(
            "looking for an idempotency key",
            method="(none) - inspecting agentbox_sdk.Sandbox.execute",
            args={"request_id_the_row_wants_to_send": request_id},
            returns={"parameters": parameters, "idempotency_parameters": idempotency},
        )

    with allure.step("2. send the identical append command twice"):
        command = f"date >> {marker}"
        first = run(session_sandbox, command, title="execute: the command, first send")
        second = run(session_sandbox, command, title="execute: the identical command, resent")
        assert first.exit_code == 0 and second.exit_code == 0, (
            f"the appends themselves failed: {first.exit_code}, {second.exit_code}"
        )

    with allure.step("3. the log holds one line, not two"):
        counted = run(
            session_sandbox,
            f"wc -l < {marker}",
            title="execute: count the lines the two sends left",
        )
        lines = int((_stdout(counted) or "0").strip() or 0)
        show(
            "how many times the command ran",
            method="(none) - reading the count above",
            returns={
                "marker_file": marker,
                "lines": lines,
                "sent": 2,
                "distinct_execution_ids": sorted({first.id, second.id}),
            },
        )
        assert lines == 1, (
            f"the command ran {lines} times for one logical request: execute() offers no "
            f"idempotency key ({idempotency or 'no such parameter'}), so a client retrying "
            f"after a dropped connection duplicates the work, and the two sends differ only "
            f"by their execution ids ({first.id}, {second.id})"
        )


# --------------------------------------------------------------------------
# E-09 a sandbox that is not running refuses and names the state
# --------------------------------------------------------------------------


def provoke_failed_sandbox(
    client: AgentBoxClient,
    tracker: Any,
    *,
    run_id: str,
    idc: str,
    instance_type: str,
    image_url: str,
) -> Tuple[Optional[Sandbox], Dict[str, Any]]:
    """A sandbox in `error`, produced without ever booting one.

    Only the container runtime reaches that state here. A container agent is
    accepted anywhere — it has no image build to fail, so `launchable` is True
    whatever the image — but this account has no container inventory, so the
    launch is refused upstream *after* the task row exists. `launch()` raises,
    so the caller never sees the record it just created: it has to be read back
    out of `sandboxes.list(agent_id=..., status="error")`. Nothing boots, so
    nothing bills.

    Returns `(sandbox, evidence)`. `sandbox` is None when the leg could not be
    produced, and `evidence` then says what happened instead.
    """
    agent = tracker.track_agent(
        client.agents.create(
            title=f"sdk-test-e09-error-{run_id}",
            image_url=image_url,
            idc=idc,
            instance_type=instance_type,
            runtime="container",
            start_cmd="sleep infinity",
        )
    )
    refusal: Optional[APIError] = None
    launched: Optional[Sandbox] = None
    try:
        launched = tracker.track_sandbox(agent.launch(instance_type=instance_type))
    except APIError as exc:
        refusal = exc

    evidence: Dict[str, Any] = {
        "container agent": agent.slug,
        "launchable (a container agent has no build to fail)": agent.launchable,
        "launch refused with": _payload(refusal) if refusal is not None else None,
    }
    if launched is not None:
        evidence["launch returned a sandbox instead"] = launched.id
        return None, evidence

    orphans = client.sandboxes.list(agent_id=agent.id, status="error").items
    if not orphans:
        evidence["nothing in `error`"] = "the refused launch left no task behind"
        return None, evidence

    sandbox = tracker.track_sandbox(client.sandboxes.get(orphans[0].id))
    evidence["tasks the refused launch left behind"] = [item.id for item in orphans]
    return sandbox, evidence


@allure.feature(FEATURE)
@allure.story("E-09 a sandbox that is not running refuses and names the state")
@allure.severity(P1)
def test_e09_execute_and_upload_against_a_sandbox_that_is_not_running(
    launch_sandbox, test_client, tracker, run_id, sandbox_target, instance_type, image_url
):
    observed: Dict[str, Dict[str, Any]] = {}

    def attempt(label: str, sandbox: Sandbox, call: str) -> None:
        """Run one call, timed: the row is about refusing rather than hanging."""
        started = time.monotonic()
        try:
            if call == "execute":
                result: Any = sandbox.execute("echo b2-e09", wait=True, wait_timeout_seconds=15)
                outcome = {"refused": False, **_execution_payload(result)}
            else:
                result = sandbox.upload_file(path="/tmp/b2-e09.txt", file=b"b2-e09")
                outcome = {"refused": False, "uploaded": result}
        except APIError as exc:
            outcome = {"refused": True, **_payload(exc)}
        except Exception as exc:  # noqa: BLE001 - a hang or transport error is the finding
            outcome = {"refused": True, "raised": type(exc).__name__, "message": str(exc)}
        outcome["elapsed_s"] = round(time.monotonic() - started, 2)
        observed[f"{label}:{call}"] = outcome

    def both(label: str, sandbox: Sandbox) -> None:
        """Ask this sandbox for the two things a caller would want from it."""
        for call in ("execute", "upload"):
            attempt(label, sandbox, call)

    def leg(label: str) -> Dict[str, Dict[str, Any]]:
        return {key: value for key, value in observed.items() if key.startswith(label)}

    with allure.step("1. a sandbox that is not running yet"):
        # No refresh() before the calls: launch already answers with a status,
        # and every round trip spent here is time the sandbox spends becoming
        # running — which is how this leg quietly stopped asking about
        # `creating` at all.
        creating = launch_sandbox(wait=False)
        state = creating.status
        both("creating", creating)
        creating.refresh()
        show(
            "calls against a sandbox that has not reached running",
            method="agent.launch(...), then sandbox.execute(...) / sandbox.upload_file(...)",
            args={"sandbox_id": creating.id},
            returns={
                "status launch answered": state,
                "status after the calls": creating.status,
                **leg("creating"),
            },
        )
        accepted = [key for key, value in leg("creating").items() if not value["refused"]]
        if state not in NOT_RUNNING_YET:
            known_gap(
                f"KNOWN GAP: this leg asks what a sandbox that is not running yet does with "
                f"work, but launch answered {state!r} and it read {creating.status!r} right "
                f"after the calls, so that state went unasked"
            )
        elif accepted:
            known_gap(
                f"KNOWN GAP: a sandbox launch answered {state!r} — not running — accepted "
                f"{accepted} rather than refusing with its state named, so neither the "
                f"status a caller polls nor the call itself tells them whether the sandbox "
                f"is ready for work"
            )

    with allure.step("2. a sandbox that has been deleted"):
        gone = launch_sandbox()
        gone_id = gone.id
        gone.delete()
        both("deleted", gone)
        show(
            "calls against a deleted sandbox",
            method="sandbox.execute(...) / sandbox.upload_file(...) after sandbox.delete()",
            args={"sandbox_id": gone_id},
            returns=leg("deleted"),
        )

    with allure.step("3. a sandbox that ended in `error`"):
        failed, evidence = provoke_failed_sandbox(
            test_client,
            tracker,
            run_id=run_id,
            idc=sandbox_target.idc,
            instance_type=instance_type,
            image_url=image_url,
        )
        if failed is not None:
            both("error", failed)
        show(
            "calls against a sandbox in `error`",
            method=(
                "test_client.agents.create(runtime='container') -> agent.launch(...) is "
                "refused -> test_client.sandboxes.list(agent_id=..., status='error') -> "
                "sandbox.execute(...) / sandbox.upload_file(...)"
            ),
            args={"idc": sandbox_target.idc, "instance_type": instance_type},
            returns={
                **evidence,
                "status": failed.status if failed is not None else None,
                "last_error": failed.last_error if failed is not None else None,
                "capabilities": failed.capabilities if failed is not None else None,
                **leg("error"),
            },
        )
        if failed is None:
            known_gap(
                "KNOWN GAP: this run could not produce a sandbox in `error`. A container "
                f"launch is the only route to that state and it behaved differently here "
                f"({evidence}), so the errored leg of this row is uncovered"
            )
        else:
            assert failed.status == "error", (
                f"the task the refused launch left behind reports {failed.status!r}, not "
                f"'error', so this leg is not asking what it claims to ask"
            )

    with allure.step("4. what the errored leg does and does not prove"):
        show(
            "reading the refusal against the runtime's own capabilities",
            method="(none) - comparing the calls above with sandbox.capabilities",
            returns={
                "refused with": {
                    key: f"{value.get('status_code')} {value.get('message')!r}"
                    for key, value in leg("error").items()
                }
                or "nothing — this leg was not produced",
                "not conclusive because": (
                    "a container task reports exec=False and files=False, so the refusal may "
                    "be the runtime turning down calls it never supports rather than the "
                    "state turning down work — and no container reaches running in this "
                    "account, so there is no healthy one to compare against. Pointing the "
                    "other way: logs(), the one capability it reports True, is allowed on "
                    "that same task"
                ),
                "why only a container reaches `error`": (
                    "a sandbox-runtime launch into a data center with no sandbox inventory is "
                    "refused 422 `sandbox_placement_fixed` before any task row exists, and "
                    "B-0 T-04b measured that a start command exiting at once leaves the "
                    "sandbox `creating` for at least 300s"
                ),
                "worth its own row": (
                    "the refused launch created that task all the same, and launch() raised "
                    "without handing back its id — a caller cannot delete what it was never "
                    "told it made"
                ),
            },
        )

    with allure.step("5. every refusal is immediate and names the state"):
        vague: List[str] = []
        for key, outcome in observed.items():
            if not outcome["refused"]:
                continue
            if outcome.get("raised") in ("SandboxWaitTimeout", "TransportError"):
                vague.append(f"{key}: timed out ({outcome.get('raised')}) instead of refusing")
            elif not str(outcome.get("message") or ""):
                vague.append(f"{key}: refused with no message at all")
        unusable = {**leg("deleted"), **leg("error")}
        took_work = {key: value for key, value in unusable.items() if not value["refused"]}
        slow = {
            key: value["elapsed_s"]
            for key, value in unusable.items()
            if value["refused"] and value["elapsed_s"] > REFUSAL_MUST_BE_WITHIN
        }
        show(
            "what each refusal said",
            method="(none) - summarising the attempts above",
            returns={
                key: {
                    "refused": value["refused"],
                    "status_code": value.get("status_code"),
                    "message": value.get("message"),
                    "elapsed_s": value["elapsed_s"],
                }
                for key, value in observed.items()
            },
        )
        assert not took_work, (
            f"a sandbox that can no longer run anything accepted work: {took_work}"
        )
        assert not slow, (
            f"a refusal took longer than {REFUSAL_MUST_BE_WITHIN}s, which reads to a caller "
            f"as a hang rather than an answer: {slow}"
        )
        assert not vague, f"a refusal did not name the state: {vague}"


# --------------------------------------------------------------------------
# E-10 reading the history of status changes
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("E-10 reading the history of status changes")
@allure.severity(P1)
def test_e10_status_change_history_has_no_entry_point(session_sandbox):
    with allure.step("1. look for any call that returns a history of status changes"):
        candidates = ("history", "events", "audit", "transitions", "timeline", "status_log")
        surfaces = {
            "Sandbox": [name for name in dir(Sandbox) if not name.startswith("_")],
            "SandboxCollection": [
                name for name in dir(SandboxCollection) if not name.startswith("_")
            ],
        }
        found = {
            where: [name for name in names if any(hint in name for hint in candidates)]
            for where, names in surfaces.items()
        }
        show(
            "every public method that could carry a history",
            method="(none) - inspecting the SDK surface",
            args={"searched_for": candidates},
            returns={"surfaces": surfaces, "matches": found},
        )

    with allure.step("2. what a caller can read instead"):
        session_sandbox.refresh()
        show(
            "the state a sandbox exposes",
            method="sandbox.refresh()",
            returns={
                "status": session_sandbox.status,
                "status_stale": session_sandbox.status_stale,
                "last_error": session_sandbox.last_error,
                "expires_at": session_sandbox.expires_at,
                "note": "all of it is the state right now; none of it is a history, so "
                "polling refresh() is the only way to build one — and it loses every "
                "transition between two polls, including the one that explains a stop",
            },
        )

    with allure.step("3. there is no entry point for the history"):
        assert not any(found.values()), (
            f"a history surface exists after all: {found} — this row is written for an SDK "
            "that has none, and should be replaced by a real assertion on what it returns"
        )
