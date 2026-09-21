"""The billable path: a running sandbox, commands, files, logs and shell.

These share one session-scoped sandbox. It is deleted by the tracker when the
session ends, not by any individual test.
"""

from __future__ import annotations

import time

import pytest
from agentbox_sdk import AgentBoxClient, Sandbox

pytestmark = [pytest.mark.slow, pytest.mark.billable]


def test_the_sandbox_reaches_running(session_sandbox: Sandbox):
    assert session_sandbox.status == "running"
    assert session_sandbox.last_error is None


def test_a_running_sandbox_appears_in_the_list(test_client: AgentBoxClient, session_sandbox: Sandbox):
    page = test_client.sandboxes.list(agent_id=session_sandbox.agent_id, status="running")

    assert session_sandbox.id in {sandbox.id for sandbox in page.items}


def test_execute_returns_stdout_and_a_zero_exit_code(session_sandbox: Sandbox):
    execution = session_sandbox.execute("echo hello")

    assert execution.exit_code == 0
    assert "hello" in (execution.data.get("stdout") or "")


def test_a_failing_command_reports_its_exit_code(session_sandbox: Sandbox):
    execution = session_sandbox.execute("exit 3")

    assert execution.exit_code == 3


def test_a_long_command_can_be_started_and_cancelled(session_sandbox: Sandbox):
    execution = session_sandbox.execute("sleep 120", wait=False)
    assert execution.accepted is True
    assert execution.id

    session_sandbox.cancel_execution(execution.id)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = session_sandbox.get_execution(execution.id).status
        if status not in {"running", "pending", "accepted"}:
            return
        time.sleep(2)
    pytest.fail("execution never left the running state after cancel")


def test_a_file_survives_a_round_trip(session_sandbox: Sandbox):
    payload = f"round-trip {time.time()}\n".encode("utf-8")
    remote_path = "/tmp/sdk-test-round-trip.txt"

    session_sandbox.upload_file(path=remote_path, file=payload)
    download = session_sandbox.download_file(path=remote_path)

    assert download.content == payload
    assert download.filename == "sdk-test-round-trip.txt"


def test_uploaded_files_are_visible_to_commands(session_sandbox: Sandbox):
    session_sandbox.upload_file(path="/tmp/sdk-test-cat.txt", file=b"from the test\n")

    execution = session_sandbox.execute("cat /tmp/sdk-test-cat.txt")

    assert "from the test" in (execution.data.get("stdout") or "")


def test_logs_are_readable_when_the_runtime_supports_them(session_sandbox: Sandbox):
    if not session_sandbox.capabilities.get("logs"):
        pytest.skip("runtime does not advertise the logs capability")

    assert isinstance(session_sandbox.logs(), str)


def test_metrics_are_readable_when_the_runtime_supports_them(session_sandbox: Sandbox):
    if not session_sandbox.capabilities.get("metrics"):
        pytest.skip("runtime does not advertise the metrics capability")

    end = int(time.time())
    batch = session_sandbox.metrics(start=end - 600, end=end, kinds=["cpu", "memory"], step=60)

    assert {series.kind for series in batch.results} <= {"cpu", "memory"}


def test_the_interactive_shell_echoes(session_sandbox: Sandbox):
    connection = session_sandbox.shell(timeout=30)
    try:
        connection.send("echo shell-works\n")
        assert connection.recv()
    finally:
        connection.close()
