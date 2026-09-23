"""Create, read, update and delete a real agent (no sandbox is launched)."""

from __future__ import annotations

import time

import allure
import pytest
from agentbox_sdk import AgentBoxClient, NotFoundError

from helpers.report import show

FEATURE = "Agent lifecycle"
NORMAL = allure.severity_level.NORMAL

pytestmark = pytest.mark.slow


def wait_for_build(agent, *, timeout: float = 180.0) -> str:
    deadline = time.monotonic() + timeout
    while True:
        status = agent.refresh().template_build_status
        if status in (None, "ready"):
            return status
        if status == "error":
            pytest.fail(f"the image build failed: {agent.template_build_error}")
        if time.monotonic() >= deadline:
            pytest.fail(f"the image was still {status!r} after {timeout:.0f}s")
        time.sleep(2)


@allure.feature(FEATURE)
@allure.story("an agent survives a create/read/update/delete round trip")
@allure.severity(NORMAL)
def test_agent_crud_round_trip(
    test_client: AgentBoxClient, run_id: str, tracker, sandbox_target, image_url: str
):
    with allure.step("1. create the agent"):
        agent = tracker.track_agent(
            test_client.agents.create(
                title=f"sdk-test-crud-{run_id}",
                image_url=image_url,
                idc=sandbox_target.idc,
                instance_type=sandbox_target.instance_type,
                runtime="sandbox",
            )
        )
        show(
            "agents.create",
            method="test_client.agents.create(title=..., image_url=..., runtime='sandbox')",
            args={"title": agent.title, "image_url": image_url, "idc": sandbox_target.idc},
            returns=agent,
        )
        assert agent.id and agent.slug
        assert agent.idc == sandbox_target.idc
        assert agent.runtime == "sandbox"
        assert agent.image_url == image_url
        assert agent.revision is None or agent.revision >= 1

    with allure.step("2. read it back by slug and in the listing"):
        fetched = test_client.agents.get(agent.slug)
        listed = test_client.agents.list(page=1, page_size=100)
        show(
            "agents.get then agents.list",
            method="test_client.agents.get(slug), test_client.agents.list(page=1, page_size=100)",
            args={"slug": agent.slug},
            returns={
                "fetched id": fetched.id,
                "in the listing": agent.slug in {item.slug for item in listed.items},
            },
        )
        assert fetched.id == agent.id
        assert agent.slug in {item.slug for item in listed.items}

    with allure.step("3. registration did not launch a sandbox"):
        owned = agent.sandboxes()
        show("agent.sandboxes()", method="agent.sandboxes()", returns=owned)
        assert owned.total == 0 and owned.items == [], "registration launched a sandbox"

    with allure.step("4. rename, then read the new title back"):
        agent.update(title=f"sdk-test-renamed-{run_id}")
        renamed = agent.refresh()
        show(
            "agent.update(title=...) then agent.refresh()",
            method="agent.update(title=...), agent.refresh()",
            args={"title": f"sdk-test-renamed-{run_id}"},
            returns={"title": renamed.title},
        )
        assert renamed.title == f"sdk-test-renamed-{run_id}"

    with allure.step("5. delete it and read it back as gone"):
        wait_for_build(agent)
        agent.delete()
        with pytest.raises(NotFoundError) as caught:
            test_client.agents.get(agent.slug)
        show(
            "agent.delete() then agents.get(slug)",
            method="agent.delete(), test_client.agents.get(slug)",
            args={"slug": agent.slug},
            returns={
                "raised": type(caught.value).__name__,
                "status_code": caught.value.status_code,
            },
        )
        assert caught.value.status_code == 404


@allure.feature(FEATURE)
@allure.story("agent env and the create-only key field read back")
@allure.severity(NORMAL)
def test_agent_env_and_the_create_only_key_field_read_back(
    test_client: AgentBoxClient, run_id: str, tracker, sandbox_target, image_url: str
):
    env = [{"name": "LIFECYCLE_MARKER", "value": "present", "secret": False}]

    with allure.step("1. create the agent with one env entry"):
        agent = tracker.track_agent(
            test_client.agents.create(
                title=f"sdk-test-env-{run_id}",
                image_url=image_url,
                idc=sandbox_target.idc,
                instance_type=sandbox_target.instance_type,
                runtime="sandbox",
                env=env,
            )
        )
        show(
            "agents.create with env",
            method="test_client.agents.create(..., env=[...])",
            args={"env": env},
            returns=agent,
        )

    with allure.step("2. the entry and the key field read back from get()"):
        fetched = test_client.agents.get(agent.slug)
        entries = {entry.get("name"): entry.get("value") for entry in fetched.env}
        show(
            "agents.get",
            method="test_client.agents.get(slug)",
            args={"slug": agent.slug},
            returns={
                "env": fetched.env,
                "generated_api_key on create": agent.generated_api_key,
                "generated_api_key on get": fetched.generated_api_key,
            },
        )
        assert entries.get("LIFECYCLE_MARKER") == "present"
        assert agent.generated_api_key is None or isinstance(agent.generated_api_key, str)
        assert fetched.generated_api_key is None or isinstance(fetched.generated_api_key, str)


@allure.feature(FEATURE)
@allure.story("health reports the local client configuration")
@allure.severity(NORMAL)
def test_health_reports_the_local_client_configuration(test_client: AgentBoxClient):
    with allure.step("1. health() answers from local configuration, with no request"):
        health = test_client.health()
        show("client.health()", method="test_client.health()", returns=health)
        assert health["status"] == "ok"
        assert health["baseUrl"] == test_client.base_url
        assert health["authenticated"] is True


@allure.feature(FEATURE)
@allure.story("the session agent's image built")
@allure.severity(NORMAL)
def test_the_session_agent_builds_its_image(session_agent):
    with allure.step("1. the build reached a terminal state"):
        show(
            "the session agent",
            method="(none) - the state the session-scoped agent fixture left",
            returns={
                "slug": session_agent.slug,
                "template_build_status": session_agent.template_build_status,
                "template_build_error": session_agent.template_build_error,
                "launchable": session_agent.launchable,
            },
        )
        assert session_agent.template_build_status in (None, "ready")
        assert session_agent.template_build_error is None
        assert session_agent.launchable is True
