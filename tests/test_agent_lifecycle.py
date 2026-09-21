"""Create, read, update and delete a real agent (no sandbox is launched)."""

from __future__ import annotations

import pytest
from agentbox_sdk import AgentBoxClient, NotFoundError

pytestmark = pytest.mark.slow


def test_agent_crud_round_trip(
    test_client: AgentBoxClient, run_id: str, tracker, sandbox_target, image_url: str
):
    agent = tracker.track_agent(
        test_client.agents.create(
            title=f"sdk-test-crud-{run_id}",
            image_url=image_url,
            idc=sandbox_target.idc,
            instance_type=sandbox_target.instance_type,
            runtime="sandbox",
        )
    )

    assert agent.id and agent.slug
    assert agent.idc == sandbox_target.idc
    assert agent.runtime == "sandbox"

    fetched = test_client.agents.get(agent.slug)
    assert fetched.id == agent.id

    listed = test_client.agents.list(page=1, page_size=100)
    assert agent.slug in {item.slug for item in listed.items}

    agent.update(title=f"sdk-test-renamed-{run_id}")
    assert agent.refresh().title == f"sdk-test-renamed-{run_id}"

    agent.delete()
    with pytest.raises(NotFoundError):
        test_client.agents.get(agent.slug)


def test_the_session_agent_builds_its_image(session_agent):
    assert session_agent.template_build_status in (None, "ready")
    assert session_agent.template_build_error is None
    assert session_agent.launchable is True
