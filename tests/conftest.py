"""Shared configuration and fixtures for the whole suite.

The suite is flat: one file per feature area, all of them next to this
conftest. The fixtures below are shared by every file, so they live here.
Once a feature area grows past a file or two, give it a directory named after
its PRD module and move its files in — the fixtures stay put.

Every test here talks to the real AgentBox API, so a plain `pytest` runs
against the live service and the twelve cases marked `billable` launch a real
sandbox. Deselect them with `-m "not billable"` when you do not want the bill.
Without GMI_AGENTBOX_API_KEY the whole suite skips itself.

Every resource is registered with the session `ResourceTracker`, which deletes
sandboxes first and agents second at the end of the run — including after a
failure or a KeyboardInterrupt. Sandboxes bill the Console account until they
are deleted, so nothing here creates a resource it does not track.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import pytest
from agentbox_sdk import Agent, AgentBoxClient, APIError, Sandbox


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load PROJECT_ROOT/.env without pulling in python-dotenv.

    Existing environment variables win, so `GMI_AGENTBOX_API_KEY=... pytest`
    still overrides the file.
    """
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("agentbox")
    group.addoption(
        "--keep-resources",
        action="store_true",
        default=False,
        help="skip cleanup of live agents/sandboxes so failures can be inspected "
        "(they keep billing until you delete them)",
    )
    group.addoption(
        "--log-http",
        action="store_true",
        default=False,
        help="print every live request and response (needs -s; the API key is redacted)",
    )
    group.addoption(
        "--idc",
        default=None,
        help="data center id for live tests (default: $GMI_TEST_IDC, else discovered)",
    )
    group.addoption(
        "--instance-type",
        default=None,
        help="sandbox SKU for live tests (default: $GMI_TEST_INSTANCE_TYPE, else discovered)",
    )


def pytest_report_header(config: pytest.Config) -> str:
    """Say up front which account is about to be used, and whether it bills."""
    import agentbox_sdk

    selected = config.getoption("-m") or ""
    billing = "billable cases DESELECTED" if "not billable" in selected else "BILLABLE — launches a sandbox"
    base_url = os.getenv("GMI_AGENTBOX_BASE_URL", agentbox_sdk.DEFAULT_BASE_URL)
    return f"agentbox-sdk {agentbox_sdk.__version__} | live: {base_url} | {billing}"


DEFAULT_IMAGE = os.getenv("GMI_TEST_IMAGE", "docker.io/library/alpine:3.20")
TEMPLATE_BUILD_TIMEOUT = float(os.getenv("GMI_TEST_BUILD_TIMEOUT", "30"))
SANDBOX_RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))

# Teardown keeps retrying a refused agent delete for this long: the backend
# answers 409 `sandbox_template_busy` while an image is still building, and
# that clears on its own once the build reaches a terminal state.
CLEANUP_RETRY_FOR = float(os.getenv("GMI_TEST_CLEANUP_RETRY", "120"))
CLEANUP_RETRY_EVERY = 5.0


@dataclass(frozen=True)
class SandboxTarget:
    """Where live sandboxes get launched."""

    idc: str
    instance_type: str


class ResourceTracker:
    """Remembers what a run created so the session can tear it down."""

    def __init__(self, client: AgentBoxClient) -> None:
        self._client = client
        self._sandboxes: List[Sandbox] = []
        self._agents: List[Agent] = []

    def track_sandbox(self, sandbox: Sandbox) -> Sandbox:
        self._sandboxes.append(sandbox)
        return sandbox

    def track_agent(self, agent: Agent) -> Agent:
        self._agents.append(agent)
        return agent

    def leftovers(self) -> List[Tuple[str, str]]:
        return [("sandbox", s.id) for s in self._sandboxes] + [
            ("agent", a.slug) for a in self._agents
        ]

    def cleanup(self) -> List[str]:
        """Delete sandboxes then agents. Returns human-readable failures.

        Agent deletes are retried: the backend refuses one with 409
        `sandbox_template_busy` while the image is still building, and a run
        that registers several agents would otherwise strand every one it did
        not wait for. Sandboxes are deleted first and are not retried — they
        bill, so a stuck one is worth reporting immediately rather than
        holding teardown open for it.
        """
        failures: List[str] = []
        for sandbox in reversed(self._sandboxes):
            try:
                sandbox.delete()
            except APIError as exc:
                if exc.status_code != 404:
                    failures.append(f"sandbox {sandbox.id}: {exc}")
            except Exception as exc:  # network flake during teardown
                failures.append(f"sandbox {sandbox.id}: {exc}")

        pending = list(reversed(self._agents))
        deadline = time.monotonic() + CLEANUP_RETRY_FOR
        while pending:
            still_busy: List[Agent] = []
            for agent in pending:
                try:
                    agent.delete()
                except APIError as exc:
                    if exc.status_code == 404:
                        continue
                    if exc.status_code == 409 and time.monotonic() < deadline:
                        still_busy.append(agent)  # most likely mid-build
                        continue
                    failures.append(f"agent {agent.slug}: {exc}")
                except Exception as exc:
                    failures.append(f"agent {agent.slug}: {exc}")
            pending = still_busy
            if pending:
                if time.monotonic() >= deadline:
                    failures.extend(
                        f"agent {agent.slug}: still 409 after {CLEANUP_RETRY_FOR:.0f}s of retries"
                        for agent in pending
                    )
                    break
                time.sleep(CLEANUP_RETRY_EVERY)

        self._sandboxes.clear()
        self._agents.clear()
        return failures


def wait_for_template(agent: Agent, *, timeout: float, poll_interval: float = 5.0) -> Agent:
    """Block until the agent's sandbox image is built.

    A status of None means the backend does not report build state for this
    agent, which the SDK README treats as ready.
    """
    deadline = time.monotonic() + timeout
    while True:
        agent.refresh()
        status = agent.template_build_status
        if status in (None, "ready"):
            return agent
        if status == "error":
            pytest.fail(f"image build failed: {agent.template_build_error}")
        if time.monotonic() >= deadline:
            pytest.fail(f"image still {status!r} after {timeout}s")
        time.sleep(poll_interval)


@pytest.fixture(scope="session")
def test_client(request: pytest.FixtureRequest) -> AgentBoxClient:
    """The SDK client under test, pointed at the real AgentBox API."""
    if not os.getenv("GMI_AGENTBOX_API_KEY"):
        pytest.skip("GMI_AGENTBOX_API_KEY is not set (copy .env.example to .env)")
    if not request.config.getoption("--log-http"):
        return AgentBoxClient()

    # The SDK logs nothing itself, so --log-http swaps in transports that
    # narrate the traffic. Printed, not logged, so -s is what surfaces it.
    from agentbox_sdk._transport import urlopen_stream_transport, urlopen_transport

    from helpers.http_log import logging_stream_transport, logging_transport

    return AgentBoxClient(
        transport=logging_transport(urlopen_transport, print),
        stream_transport=logging_stream_transport(urlopen_stream_transport, print),
    )


@pytest.fixture(scope="session")
def image_url() -> str:
    """Base image every test agent is built from."""
    return DEFAULT_IMAGE


@pytest.fixture(scope="session")
def run_id() -> str:
    """Suffix that keeps concurrent runs from colliding on agent titles."""
    return f"{time.strftime('%m%d-%H%M')}-{uuid.uuid4().hex[:4]}"


@pytest.fixture(scope="session")
def tracker(request: pytest.FixtureRequest, test_client: AgentBoxClient):
    tracker = ResourceTracker(test_client)
    yield tracker

    reporter = request.config.pluginmanager.get_plugin("terminalreporter")

    def report(message: str) -> None:
        if reporter is not None:
            reporter.write_line(message)

    if request.config.getoption("--keep-resources"):
        for kind, identifier in tracker.leftovers():
            report(f"[kept, still billing] {kind} {identifier}")
        return

    for failure in tracker.cleanup():
        report(f"[CLEANUP FAILED — delete by hand] {failure}")


@pytest.fixture(scope="session")
def sandbox_target(request: pytest.FixtureRequest, test_client: AgentBoxClient) -> SandboxTarget:
    """Pin the data center and SKU, or discover the first usable pair."""
    idc = request.config.getoption("--idc") or os.getenv("GMI_TEST_IDC")
    instance_type = request.config.getoption("--instance-type") or os.getenv(
        "GMI_TEST_INSTANCE_TYPE"
    )
    if idc and instance_type:
        return SandboxTarget(idc, instance_type)

    idcs = test_client.idcs.list(runtime="sandbox")
    if not idcs:
        pytest.skip("this organization has no sandbox data centers")

    for candidate in idcs:
        candidate_id = idc or candidate.idc_id
        if not candidate_id:
            continue
        products = test_client.products.list(idc_name=candidate_id, runtime="sandbox")
        for product in products:
            if product.instance_type:
                return SandboxTarget(candidate_id, instance_type or product.instance_type)

    pytest.skip("no sandbox SKU is available to this organization")


@pytest.fixture(scope="session")
def instance_type(test_client: AgentBoxClient, sandbox_target: SandboxTarget) -> str:
    """The smallest SKU this data center sells.

    `sandbox_target` discovers the *first* product the org can use, which in
    this account is `gmi.sandbox.x-large`. Nothing in the suite needs that much
    machine, so every billable row launches through here instead.
    """
    available = {
        product.instance_type
        for product in test_client.products.list(idc_name=sandbox_target.idc, runtime="sandbox")
    }
    for candidate in ("gmi.sandbox.x-small", "gmi.sandbox.small", "gmi.sandbox.medium"):
        if candidate in available:
            return candidate
    return sandbox_target.instance_type


@pytest.fixture(scope="session")
def session_agent(
    test_client: AgentBoxClient,
    run_id: str,
    tracker: ResourceTracker,
    sandbox_target: SandboxTarget,
    image_url: str,
) -> Agent:
    """One agent shared by the session; its image builds once."""
    agent = tracker.track_agent(
        test_client.agents.create(
            title=f"sdk-test-{run_id}",
            image_url=image_url,
            idc=sandbox_target.idc,
            instance_type=sandbox_target.instance_type,
            runtime="sandbox",
        )
    )
    return wait_for_template(agent, timeout=TEMPLATE_BUILD_TIMEOUT)


@pytest.fixture(scope="session")
def session_sandbox(
    session_agent: Agent, tracker: ResourceTracker, instance_type: str
) -> Sandbox:
    """One running sandbox shared by the session — the expensive resource."""
    if not session_agent.launchable:
        pytest.skip(f"agent {session_agent.slug} is not launchable (upstream template missing)")

    sandbox = tracker.track_sandbox(session_agent.launch(instance_type=instance_type))
    sandbox.wait_until_running(timeout=SANDBOX_RUN_TIMEOUT)
    return sandbox
