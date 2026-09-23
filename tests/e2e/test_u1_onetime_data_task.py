"""E2E U-1 — one one-off data job, end to end.

A data engineer has a script: it reads `input.csv`, chews for two minutes,
writes `output.parquet` and prints the product's checksum. One AgentBox
lifecycle covers the whole errand, and every case it touches is checked on the
way past:

| Case        | What the step below proves                                      |
|-------------|-----------------------------------------------------------------|
| T-01        | agents.create on a python image, polled to a built template      |
| C-01..C-03  | launch -> wait_until_running, endpoint and expiry read back      |
| E-02        | execute under a short wait ceiling answers "still running";      |
|             | the id polls to succeeded                                        |
| F-01        | upload the csv and the script, download the parquet, checksums   |
|             | match what the job printed                                       |
| D-01        | sandbox.delete()                                                 |
| D-05        | one usage record, flat afterwards — no SDK entry point (gap)     |
"""

from __future__ import annotations

import hashlib
import os
import re
import time

import allure
import pytest

from helpers.report import known_gap, show

FEATURE = "E2E U-1 one-time data task"
pytestmark = [pytest.mark.slow, pytest.mark.billable]

PYTHON_IMAGE = os.getenv("GMI_TEST_PYTHON_IMAGE", "docker.io/library/python:3.12-slim")
JOB_SECONDS = int(os.getenv("GMI_TEST_E2E_JOB_SECONDS", "120"))  # the two-minute job
WAIT_CEILING = int(os.getenv("GMI_TEST_E2E_WAIT_CEILING", "10"))  # well under the job
BUILD_TIMEOUT = float(os.getenv("GMI_TEST_E2E_BUILD_TIMEOUT", "600"))
RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))
EXEC_DEADLINE = JOB_SECONDS + 60.0
EXEC_POLL = 2.0
RUNNING = ("pending", "running")

INPUT = "/home/user/input.csv"
SCRIPT = "/home/user/job.py"
OUTPUT = "/home/user/output.parquet"
INPUT_CSV = b"id,name,value\n1,alpha,10\n2,beta,20\n3,gamma,30\n"

# python:3.12-slim ships no parquet writer, so the job builds its product with
# the stdlib and keeps the name: what U-1 checks is the byte-for-byte round
# trip out of the sandbox, not the file format.
JOB = f"""\
import hashlib, time
data = open({INPUT!r}, 'rb').read()
time.sleep({JOB_SECONDS})
out = b'\\n'.join(sorted(data.splitlines())) + b'\\n'
open({OUTPUT!r}, 'wb').write(out)
print(hashlib.sha256(out).hexdigest())
""".encode("utf-8")


def wait_build(agent):
    """Block until the agent's sandbox image is built (None = not reported)."""
    deadline = time.monotonic() + BUILD_TIMEOUT
    while True:
        agent.refresh()
        status = agent.template_build_status
        if status in (None, "ready"):
            return
        if status == "error":
            pytest.fail(f"image build failed: {agent.template_build_error}")
        if time.monotonic() >= deadline:
            pytest.fail(f"image still {status!r} after {BUILD_TIMEOUT:.0f}s")
        time.sleep(5.0)


def wait_succeeded(sandbox, execution_id):
    """Poll the execution id until the job stops running, and log the timeline."""
    started = time.monotonic()
    timeline = []
    while True:
        execution = sandbox.get_execution(execution_id)
        if not timeline or timeline[-1][1] != execution.status:
            timeline.append((round(time.monotonic() - started, 1), execution.status))
        if execution.status not in RUNNING or time.monotonic() - started >= EXEC_DEADLINE:
            show(
                f"poll the job to a terminal status [{execution_id}]",
                method="sandbox.get_execution(execution_id) in a loop",
                args={"poll_interval_s": EXEC_POLL, "deadline_s": EXEC_DEADLINE},
                returns={"timeline_s": timeline, **execution.data},
            )
            return execution
        time.sleep(EXEC_POLL)


@allure.feature(FEATURE)
@allure.story("U-1 a one-off data job, registered, run, collected and deleted")
@allure.severity(allure.severity_level.BLOCKER)
def test_u1_onetime_data_task(test_client, tracker, run_id, sandbox_target, instance_type):
    with allure.step("1. register the python agent and wait for its image (T-01)"):
        agent = tracker.track_agent(
            test_client.agents.create(
                title=f"sdk-test-u1-{run_id}",
                image_url=PYTHON_IMAGE,
                idc=sandbox_target.idc,
                instance_type=instance_type,
                runtime="sandbox",
            )
        )
        wait_build(agent)
        show(
            "agents.create, polled to a built template",
            method="test_client.agents.create(...) then agent.refresh() in a loop",
            args={"image_url": PYTHON_IMAGE, "idc": sandbox_target.idc, "runtime": "sandbox"},
            returns=agent,
        )
        assert agent.launchable, f"agent {agent.slug} built but does not report itself launchable"

    with allure.step("2. launch it and wait until it runs (C-01..C-03)"):
        sandbox = tracker.track_sandbox(agent.launch(instance_type=instance_type))
        sandbox.wait_until_running(timeout=RUN_TIMEOUT)
        show(
            "agent.launch -> wait_until_running",
            method="agent.launch(instance_type=...) then sandbox.wait_until_running()",
            args={"instance_type": instance_type, "timeout": RUN_TIMEOUT},
            returns={
                "status": sandbox.status,
                "endpoint_url": sandbox.endpoint_url,
                "expires_at": sandbox.expires_at,
            },
        )
        assert sandbox.status == "running", f"sandbox is {sandbox.status!r}"

    with allure.step("3. upload the input and the script (F-01, upload)"):
        sandbox.upload_file(path=INPUT, file=INPUT_CSV)
        sandbox.upload_file(path=SCRIPT, file=JOB)
        show(
            "upload the job's inputs",
            method="sandbox.upload_file(path=..., file=<bytes>)",
            args={INPUT: f"{len(INPUT_CSV)} bytes", SCRIPT: f"{len(JOB)} bytes"},
            returns={"job": f"read {INPUT}, sleep {JOB_SECONDS}s, write {OUTPUT}, print sha256"},
        )

    with allure.step(f"4. execute, waiting at most {WAIT_CEILING}s -> still running (E-02)"):
        started = time.monotonic()
        execution = sandbox.execute(
            f"python {SCRIPT}", wait=True, wait_timeout_seconds=WAIT_CEILING
        )
        waited = time.monotonic() - started
        show(
            "execute a job that outlives the wait ceiling",
            method="sandbox.execute(command, wait=True, wait_timeout_seconds=...)",
            args={"command": f"python {SCRIPT}", "wait_timeout_seconds": WAIT_CEILING},
            returns={"returned_after_s": round(waited, 2), **execution.data},
        )
        assert execution.id, "the ceiling answer carried no execution id to poll"
        assert execution.status in RUNNING, (
            f"a {JOB_SECONDS}s job answered {execution.status!r} at a {WAIT_CEILING}s ceiling"
        )
        assert waited < JOB_SECONDS, (
            f"the call blocked {waited:.1f}s for a {WAIT_CEILING}s ceiling — the ceiling "
            f"bounds nothing and a client cannot hand the job off"
        )

    with allure.step("5. poll the execution id to succeeded (E-02)"):
        final = wait_succeeded(sandbox, execution.id)
        assert final.status == "succeeded", f"the job ended {final.status!r}"
        assert final.exit_code == 0, f"the job exited {final.exit_code}"
        printed = re.search(r"\b[0-9a-f]{64}\b", final.data.get("stdout") or "")
        assert printed, f"stdout carries no checksum: {final.data.get('stdout')!r}"

    with allure.step("6. download the product and match its checksum (F-01, download)"):
        product = sandbox.download_file(path=OUTPUT)
        downloaded = hashlib.sha256(product.content).hexdigest()
        show(
            "download the job's product",
            method="sandbox.download_file(path=...)",
            args={"path": OUTPUT},
            returns={
                "filename": product.filename,
                "bytes": len(product.content),
                "sha256 downloaded": downloaded,
                "sha256 the job printed": printed.group(),
            },
        )
        assert downloaded == printed.group(), (
            f"{OUTPUT} hashes to {downloaded}, not the {printed.group()} the job printed — "
            f"what came out of the sandbox is not what the job wrote"
        )

    with allure.step("7. delete the sandbox (D-01)"):
        sandbox.delete()
        show("sandbox.delete", method="sandbox.delete()", returns={"sandbox_id": sandbox.id})

    with allure.step("8. one usage record, flat after the delete (D-05)"):
        hints = ("usage", "billing", "cost", "invoice", "charge", "spend", "meter")
        found = [name for name in dir(test_client) if any(hint in name for hint in hints)]
        known_gap(
            "KNOWN GAP: AgentBoxClient exposes no usage or billing entry point "
            f"({found or 'no matching attribute'}), so 'the Console shows one record for this "
            f"job and it stops growing after the delete' can only be checked by opening the "
            f"Console — sandbox {sandbox.id}, billed from launch to this delete"
        )
