"""The billable path: a running sandbox, commands, files, logs and shell.

These share one session-scoped sandbox. It is deleted by the tracker when the
session ends, not by any individual test.
"""

from __future__ import annotations

import time

import allure
import pytest
from agentbox_sdk import Agent, AgentBoxClient, Sandbox

from helpers.report import show

FEATURE = "Sandbox lifecycle"
NORMAL = allure.severity_level.NORMAL
RUNNING_STATES = {"running", "pending", "accepted"}

pytestmark = [pytest.mark.slow, pytest.mark.billable]


@allure.feature(FEATURE)
@allure.story("the session sandbox reaches running")
@allure.severity(NORMAL)
def test_the_sandbox_reaches_running(session_sandbox: Sandbox):
    with allure.step("1. the fixture's sandbox is running with no error"):
        show(
            "the session sandbox",
            method="(none) - the state the session-scoped fixture left",
            returns={
                "id": session_sandbox.id,
                "status": session_sandbox.status,
                "last_error": session_sandbox.last_error,
            },
        )
        assert session_sandbox.status == "running"
        assert session_sandbox.last_error is None


@allure.feature(FEATURE)
@allure.story("a running sandbox is listed for its agent and readable by get")
@allure.severity(NORMAL)
def test_a_running_sandbox_appears_in_the_list(
    test_client: AgentBoxClient, session_sandbox: Sandbox
):
    with allure.step("1. list by agent and status, then get it by id"):
        page = test_client.sandboxes.list(agent_id=session_sandbox.agent_id, status="running")
        fetched = test_client.sandboxes.get(session_sandbox.id)
        show(
            "sandboxes.list then sandboxes.get",
            method=(
                "test_client.sandboxes.list(agent_id=..., status='running'), "
                "test_client.sandboxes.get(id)"
            ),
            args={"agent_id": session_sandbox.agent_id, "sandbox_id": session_sandbox.id},
            returns={
                "total": page.total,
                "ids": [sandbox.id for sandbox in page.items],
                "fetched": {"id": fetched.id, "status": fetched.status},
            },
        )
        assert session_sandbox.id in {sandbox.id for sandbox in page.items}
        assert fetched.id == session_sandbox.id
        assert fetched.status == "running"


@allure.feature(FEATURE)
@allure.story("the sandbox reads its launch configuration back")
@allure.severity(NORMAL)
def test_the_sandbox_reads_its_launch_configuration_back(
    session_agent: Agent, session_sandbox: Sandbox
):
    with allure.step("1. refresh and read the runtime, agent, idc and endpoint"):
        session_sandbox.refresh()
        show(
            "sandbox.refresh()",
            method="sandbox.refresh()",
            returns={
                "runtime": session_sandbox.runtime,
                "agent_id": session_sandbox.agent_id,
                "agent_slug": session_sandbox.agent_slug,
                "idc_name": session_sandbox.idc_name,
                "endpoint_url": session_sandbox.endpoint_url,
                "expires_at": session_sandbox.expires_at,
                "status_stale": session_sandbox.status_stale,
            },
        )
        assert session_sandbox.runtime == "sandbox"
        assert session_sandbox.agent_id == session_agent.id
        assert session_sandbox.agent_slug == session_agent.slug
        assert session_sandbox.idc_name == session_agent.idc
        assert session_sandbox.endpoint_url
        assert session_sandbox.expires_at
        assert isinstance(session_sandbox.status_stale, bool)


@allure.feature(FEATURE)
@allure.story("execute returns stdout and a zero exit code")
@allure.severity(NORMAL)
def test_execute_returns_stdout_and_a_zero_exit_code(session_sandbox: Sandbox):
    with allure.step("1. run one command and read its result"):
        execution = session_sandbox.execute("echo hello")
        show(
            "sandbox.execute('echo hello')",
            method="sandbox.execute(command)",
            args={"command": "echo hello"},
            returns=execution.data,
        )
        assert execution.exit_code == 0
        assert "hello" in (execution.data.get("stdout") or "")


@allure.feature(FEATURE)
@allure.story("a failing command reports its exit code")
@allure.severity(NORMAL)
def test_a_failing_command_reports_its_exit_code(session_sandbox: Sandbox):
    with allure.step("1. run a command that exits non-zero"):
        execution = session_sandbox.execute("exit 3")
        show(
            "sandbox.execute('exit 3')",
            method="sandbox.execute(command)",
            args={"command": "exit 3"},
            returns=execution.data,
        )
        assert execution.exit_code == 3


@allure.feature(FEATURE)
@allure.story("a long command can be started and cancelled")
@allure.severity(NORMAL)
def test_a_long_command_can_be_started_and_cancelled(session_sandbox: Sandbox):
    with allure.step("1. start a long command asynchronously"):
        execution = session_sandbox.execute("sleep 120", wait=False)
        show(
            "sandbox.execute('sleep 120', wait=False)",
            method="sandbox.execute(command, wait=False)",
            args={"command": "sleep 120"},
            returns=execution.data,
        )
        assert execution.accepted is True
        assert execution.id

    with allure.step("2. cancel it and poll it out of the running state"):
        session_sandbox.cancel_execution(execution.id)

        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            status = session_sandbox.get_execution(execution.id).status
            if status not in RUNNING_STATES:
                break
            time.sleep(2)
        show(
            "sandbox.cancel_execution then get_execution in a loop",
            method="sandbox.cancel_execution(execution_id), sandbox.get_execution(execution_id)",
            args={"execution_id": execution.id},
            returns={"final status": status},
        )
        assert status not in RUNNING_STATES, (
            f"execution never left the running state after cancel: {status!r}"
        )


@allure.feature(FEATURE)
@allure.story("a file survives an upload/download round trip")
@allure.severity(NORMAL)
def test_a_file_survives_a_round_trip(session_sandbox: Sandbox):
    payload = f"round-trip {time.time()}\n".encode("utf-8")
    remote_path = "/tmp/sdk-test-round-trip.txt"

    with allure.step("1. upload the bytes, download them, compare"):
        uploaded = session_sandbox.upload_file(path=remote_path, file=payload)
        download = session_sandbox.download_file(path=remote_path)
        show(
            "sandbox.upload_file then sandbox.download_file",
            method="sandbox.upload_file(path=..., file=<bytes>), sandbox.download_file(path=...)",
            args={"path": remote_path, "bytes": len(payload)},
            returns={
                "uploaded": uploaded,
                "bytes downloaded": len(download.content),
                "filename": download.filename,
            },
        )
        assert download.content == payload
        assert download.filename == "sdk-test-round-trip.txt"


@allure.feature(FEATURE)
@allure.story("an uploaded file is visible to commands")
@allure.severity(NORMAL)
def test_uploaded_files_are_visible_to_commands(session_sandbox: Sandbox):
    with allure.step("1. upload a file, then cat it in a command"):
        session_sandbox.upload_file(path="/tmp/sdk-test-cat.txt", file=b"from the test\n")
        execution = session_sandbox.execute("cat /tmp/sdk-test-cat.txt")
        show(
            "sandbox.upload_file then sandbox.execute('cat ...')",
            method="sandbox.upload_file(path=...), sandbox.execute(command)",
            args={"path": "/tmp/sdk-test-cat.txt"},
            returns=execution.data,
        )
        assert "from the test" in (execution.data.get("stdout") or "")


@allure.feature(FEATURE)
@allure.story("logs are readable when the runtime supports them")
@allure.severity(NORMAL)
def test_logs_are_readable_when_the_runtime_supports_them(session_sandbox: Sandbox):
    with allure.step("1. read the logs if the runtime advertises the capability"):
        if not session_sandbox.capabilities.get("logs"):
            show(
                "sandbox.logs()",
                method="(none) - the runtime does not advertise the logs capability",
                returns={"capabilities.logs": False},
            )
            pytest.skip("runtime does not advertise the logs capability")
        logs = session_sandbox.logs()
        show(
            "sandbox.logs()",
            method="sandbox.logs()",
            returns={"chars": len(logs), "head": logs[:1000]},
        )
        assert isinstance(logs, str)


@allure.feature(FEATURE)
@allure.story("metrics are readable when the runtime supports them")
@allure.severity(NORMAL)
def test_metrics_are_readable_when_the_runtime_supports_them(session_sandbox: Sandbox):
    with allure.step("1. read the metrics batch if the runtime advertises the capability"):
        if not session_sandbox.capabilities.get("metrics"):
            show(
                "sandbox.metrics()",
                method="(none) - the runtime does not advertise the metrics capability",
                returns={"capabilities.metrics": False},
            )
            pytest.skip("runtime does not advertise the metrics capability")
        end = int(time.time())
        batch = session_sandbox.metrics(
            start=end - 600, end=end, kinds=["cpu", "memory"], step=60
        )
        show(
            "sandbox.metrics(start=..., end=..., kinds=['cpu', 'memory'])",
            method="sandbox.metrics(start=..., end=..., kinds=..., step=60)",
            args={"start": end - 600, "end": end},
            returns=[
                {"kind": series.kind, "points": len(series.points)} for series in batch.results
            ],
        )
        assert {series.kind for series in batch.results} <= {"cpu", "memory"}


@allure.feature(FEATURE)
@allure.story("metrics_timeseries is readable when the runtime supports it")
@allure.severity(NORMAL)
def test_metrics_timeseries_is_readable_when_the_runtime_supports_it(session_sandbox: Sandbox):
    with allure.step("1. read one timeseries if the runtime advertises the capability"):
        if not session_sandbox.capabilities.get("metrics"):
            show(
                "sandbox.metrics_timeseries()",
                method="(none) - the runtime does not advertise the metrics capability",
                returns={"capabilities.metrics": False},
            )
            pytest.skip("runtime does not advertise the metrics capability")
        end = int(time.time())
        series = session_sandbox.metrics_timeseries(
            kind="cpu", start=end - 600, end=end, step=60
        )
        show(
            "sandbox.metrics_timeseries(kind='cpu', ...)",
            method="sandbox.metrics_timeseries(kind=..., start=..., end=..., step=60)",
            args={"kind": "cpu", "start": end - 600, "end": end},
            returns={
                "kind": series.kind,
                "unit": series.unit,
                "empty_reason": series.empty_reason,
                "points": len(series.points),
            },
        )
        assert series.kind == "cpu"
        assert isinstance(series.points, list)


@allure.feature(FEATURE)
@allure.story("the event stream answers or is refused within its timeout")
@allure.severity(NORMAL)
def test_the_event_stream_answers_or_is_refused_within_its_timeout(session_sandbox: Sandbox):
    with allure.step("1. read one event, or record the refusal, inside 30s"):
        started = time.monotonic()
        outcome = {"events": 0, "raised": None}
        try:
            for _ in session_sandbox.stream(timeout=10):
                outcome["events"] += 1
                break
        except Exception as exc:  # noqa: BLE001 - the shape of the refusal is the answer
            outcome["raised"] = f"{type(exc).__name__}: {exc}"
        outcome["elapsed_s"] = round(time.monotonic() - started, 1)
        show(
            "sandbox.stream(timeout=10)",
            method="sandbox.stream(timeout=10)",
            args={"timeout": 10},
            returns=outcome,
        )
        assert outcome["elapsed_s"] < 30, (
            f"the stream took {outcome['elapsed_s']}s against a 10s socket timeout: {outcome}"
        )
        assert outcome["events"] or outcome["raised"], (
            f"the stream neither delivered an event nor named an error: {outcome}"
        )


@allure.feature(FEATURE)
@allure.story("the interactive shell echoes")
@allure.severity(NORMAL)
def test_the_interactive_shell_echoes(session_sandbox: Sandbox):
    with allure.step("1. send one command and read the echo back"):
        connection = session_sandbox.shell(timeout=30)
        try:
            connection.send("echo shell-works\n")
            received = connection.recv()
        finally:
            connection.close()
        show(
            "sandbox.shell(timeout=30)",
            method="sandbox.shell(timeout=30), connection.send/recv",
            args={"command": "echo shell-works"},
            returns={"received": str(received)[:200]},
        )
        assert received


@allure.feature(FEATURE)
@allure.story("a command can be run through the collection")
@allure.severity(NORMAL)
def test_a_command_can_be_run_through_the_collection(
    test_client: AgentBoxClient, session_sandbox: Sandbox
):
    with allure.step("1. execute through the collection and read the result"):
        execution = test_client.sandboxes.execute(session_sandbox.id, "echo collection")
        show(
            "sandboxes.execute(id, 'echo collection')",
            method="test_client.sandboxes.execute(sandbox_id, command)",
            args={"sandbox_id": session_sandbox.id, "command": "echo collection"},
            returns=execution.data,
        )
        assert execution.exit_code == 0
        assert "collection" in (execution.data.get("stdout") or "")

    with allure.step("2. start async, fetch by id, cancel, poll it out"):
        running = test_client.sandboxes.execute(session_sandbox.id, "sleep 120", wait=False)
        fetched = test_client.sandboxes.get_execution(session_sandbox.id, running.id)
        test_client.sandboxes.cancel_execution(session_sandbox.id, running.id)

        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            status = test_client.sandboxes.get_execution(session_sandbox.id, running.id).status
            if status not in RUNNING_STATES:
                break
            time.sleep(2)
        show(
            "sandboxes.execute(wait=False), get_execution, cancel_execution",
            method=(
                "test_client.sandboxes.execute(..., wait=False), "
                "test_client.sandboxes.get_execution(id), "
                "test_client.sandboxes.cancel_execution(id)"
            ),
            args={"execution_id": running.id},
            returns={"fetched id": fetched.id, "final status": status},
        )
        assert running.accepted is True
        assert running.id
        assert fetched.id == running.id
        assert status not in RUNNING_STATES, (
            f"execution never left the running state after cancel: {status!r}"
        )


@allure.feature(FEATURE)
@allure.story("a file survives a round trip through the collection")
@allure.severity(NORMAL)
def test_a_file_survives_a_round_trip_through_the_collection(
    test_client: AgentBoxClient, session_sandbox: Sandbox
):
    payload = f"collection round-trip {time.time()}\n".encode("utf-8")
    remote_path = "/tmp/sdk-test-collection-round-trip.txt"

    with allure.step("1. upload and download through the collection"):
        uploaded = test_client.sandboxes.upload_file(
            session_sandbox.id, path=remote_path, file=payload
        )
        download = test_client.sandboxes.download_file(session_sandbox.id, path=remote_path)
        show(
            "sandboxes.upload_file then sandboxes.download_file",
            method=(
                "test_client.sandboxes.upload_file(sandbox_id, path=..., file=...), "
                "test_client.sandboxes.download_file(sandbox_id, path=...)"
            ),
            args={"sandbox_id": session_sandbox.id, "path": remote_path},
            returns={
                "uploaded": uploaded,
                "bytes downloaded": len(download.content),
                "filename": download.filename,
            },
        )
        assert download.content == payload
        assert download.filename == "sdk-test-collection-round-trip.txt"


@allure.feature(FEATURE)
@allure.story("logs, metrics and shell work through the collection")
@allure.severity(NORMAL)
def test_logs_metrics_and_shell_through_the_collection(
    test_client: AgentBoxClient, session_sandbox: Sandbox
):
    with allure.step("1. read logs and metrics through the collection"):
        if session_sandbox.capabilities.get("logs"):
            logs = test_client.sandboxes.logs(session_sandbox.id)
            show(
                "sandboxes.logs(id)",
                method="test_client.sandboxes.logs(sandbox_id)",
                args={"sandbox_id": session_sandbox.id},
                returns={"chars": len(logs)},
            )
            assert isinstance(logs, str)
        else:
            show(
                "sandboxes.logs(id)",
                method="(none) - the runtime does not advertise the logs capability",
                returns={"capabilities.logs": False},
            )

        if session_sandbox.capabilities.get("metrics"):
            end = int(time.time())
            batch = test_client.sandboxes.metrics(
                session_sandbox.id, start=end - 600, end=end, kinds=["cpu", "memory"], step=60
            )
            show(
                "sandboxes.metrics(id, ...)",
                method="test_client.sandboxes.metrics(sandbox_id, start=..., end=..., kinds=...)",
                args={"sandbox_id": session_sandbox.id},
                returns=[
                    {"kind": series.kind, "points": len(series.points)}
                    for series in batch.results
                ],
            )
            assert {series.kind for series in batch.results} <= {"cpu", "memory"}
        else:
            show(
                "sandboxes.metrics(id, ...)",
                method="(none) - the runtime does not advertise the metrics capability",
                returns={"capabilities.metrics": False},
            )

    with allure.step("2. open a shell through the collection"):
        connection = test_client.sandboxes.shell(session_sandbox.id, timeout=30)
        try:
            connection.send("echo collection-shell\n")
            received = connection.recv()
        finally:
            connection.close()
        show(
            "sandboxes.shell(id, timeout=30)",
            method="test_client.sandboxes.shell(sandbox_id, timeout=30)",
            args={"sandbox_id": session_sandbox.id},
            returns={"received": str(received)[:200]},
        )
        assert received
