"""B-6 Tenant isolation and credentials — one test per case row.

A colleague can see my sandbox and my results and cannot change them; another
organization cannot see that it exists at all; two sandboxes cannot reach each
other; and the moment a sandbox is deleted its credentials are dead. Nothing in
the SDK should open a terminal.

| ID   | P  | Action                                              | Expected                                              |
|------|----|-----------------------------------------------------|-------------------------------------------------------|
| I-01 | P0 | organization B does everything it can to A's        | every one answers "does not exist"; B's own list      |
|      |    | sandbox, and lists its own                          | holds only B's                                        |
| I-02 | P1 | a colleague in the same organization: read and      | reading and downloading succeed; the upload is        |
|      |    | download; then upload                               | refused, and nothing of theirs lands                  |
| I-03 | P0 | from S2: read S1's files, kill its processes, reach | every one refused, including the endpoint reached     |
|      |    | it with S2's credentials, and reach it with none    | with no credentials at all                            |
| I-04 | P0 | the old credentials, used after the delete and      | refused                                               |
|      |    | after the expiry                                    |                                                       |
| I-05 | P1 | what get, list and the debug log actually print     | no credential in the clear, and no name of whatever   |
|      |    |                                                     | runs the sandbox underneath                           |
| I-06 | P0 | is there a shell() in the SDK's public API or docs  | R0 ships no terminal (Q-18). One exists -> Fail       |

I-06 needs nothing live and runs first; I-05 reads a sandbox it shares with
I-03; the rest launch what they need. Every row that launches is `billable`.

Two rows need credentials the suite cannot mint, and they are handled
differently on purpose. I-01 needs a second organization, from
`GMI_AGENTBOX_API_KEY_2_1`, and the fixture proves it really is a second one
before trusting it — a key that could already see A's agents would make every
"does not exist" below meaningless. I-02 needs two credentials inside one
organization (`GMI_AGENTBOX_API_KEY_2_1` and `..._2_2`), so it runs in
organization B, where both live; its first step is the proof the rest stands
on, because two keys from *different* organizations would also fail every
write — for a reason that is I-01's subject, not this row's.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import allure
import pytest
from agentbox_sdk import AgentBoxClient, APIError, Sandbox

from helpers.report import _payload, show

FEATURE = "B-6 Tenant isolation and credentials"
P0 = allure.severity_level.BLOCKER
P1 = allure.severity_level.CRITICAL

RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))
EXEC_WAIT = int(os.getenv("GMI_TEST_EXEC_WAIT", "120"))

# The second organization's key, and — if one is ever issued — a non-admin
# member of the first. Named here rather than inline so a reader can see at a
# glance what this chapter needs that the others do not.
# Two keys belonging to organization B: the first owns what I-02 creates, the
# second is the colleague who may read it and must not change it. I-01 needs
# only the first. The older single-key spelling is still honoured so a
# checkout configured before this chapter grew its second key keeps working.
OTHER_ORG_OWNER_ENV = "GMI_AGENTBOX_API_KEY_2_1"
OTHER_ORG_COLLEAGUE_ENV = "GMI_AGENTBOX_API_KEY_2_2"
OTHER_ORG_FALLBACK_ENV = "GMI_AGENTBOX_API_KEY_2"

# I-01. A refusal that says "forbidden" confirms the resource exists, which is
# the one thing a tenant boundary must not confirm. The case row is explicit:
# every answer is "does not exist".
NOT_FOUND = 404
EXISTENCE_LEAKING = (401, 403, 409, 422)

# I-03/I-04. A direct connection to a sandbox endpoint is made without the SDK
# on purpose: the SDK always attaches the org key, and the question is what
# happens when nobody does.
DIRECT_TIMEOUT = float(os.getenv("GMI_TEST_DIRECT_TIMEOUT", "15"))

# I-05. Names of things that run sandboxes. A tenant that can read which one is
# underneath learns the shape of the platform's supply chain, and the case row
# treats that as a leak. `docker` is deliberately absent: this suite supplies
# `docker.io/library/alpine` as the image itself, so it would match the
# caller's own input in every run.
VENDOR_NAMES = (
    "runloop", "e2b", "modal", "firecracker", "gvisor", "kata", "daytona",
    "codesandbox", "fly.io", "nomad", "kubernetes", "k8s", "aws", "ec2",
    "gcp", "gce", "azure", "alibaba", "tencent",
)

# I-06. Anything that would hand a caller an interactive session.
TERMINAL_NAMES = ("shell", "terminal", "pty", "tty", "attach", "interactive", "console")
DOC_FILES = ("README.md", "docs/sdk-api.md")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def attempt(call: Callable[[], Any]) -> Dict[str, Any]:
    """Run a call and describe what came back — raised or returned.

    Every row here is about what gets refused, so an exception is the answer
    rather than something to escape through the test.
    """
    started = time.monotonic()
    try:
        value = call()
    except Exception as exc:  # noqa: BLE001 - the shape of the refusal is the point
        return {
            "refused": True,
            "elapsed_s": round(time.monotonic() - started, 2),
            "raised": type(exc).__name__,
            "status_code": getattr(exc, "status_code", None),
            "message": str(exc)[:300],
        }
    return {
        "refused": False,
        "elapsed_s": round(time.monotonic() - started, 2),
        "returned": _payload(value),
    }


def direct_get(url: str, *, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Fetch a URL without the SDK, so nothing attaches a key behind our back.

    `AgentBoxClient` puts the organization's bearer token on every request it
    makes, which is exactly the thing these rows need absent.
    """
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(
            request, timeout=DIRECT_TIMEOUT, context=ssl.create_default_context()
        ) as response:
            body = response.read(2048)
            return {
                "refused": False,
                "status_code": response.status,
                "elapsed_s": round(time.monotonic() - started, 2),
                "body_head": body[:400].decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as exc:
        return {
            "refused": True,
            "status_code": exc.code,
            "elapsed_s": round(time.monotonic() - started, 2),
            "body_head": exc.read()[:400].decode("utf-8", "replace"),
        }
    except Exception as exc:  # noqa: BLE001 - a refused connection is a refusal
        return {
            "refused": True,
            "status_code": None,
            "elapsed_s": round(time.monotonic() - started, 2),
            "raised": type(exc).__name__,
            "message": str(exc)[:300],
        }


def sh(sandbox: Sandbox, command: str) -> str:
    """Run one command in a sandbox and return its stdout, stripped."""
    execution = sandbox.execute(command, wait_timeout_seconds=EXEC_WAIT)
    return (execution.data.get("stdout") or "").strip()


def bound_to(client: AgentBoxClient, sandbox_id: str) -> Sandbox:
    """The same sandbox id, driven by another organization's client.

    Built from the id rather than fetched: fetching is one of the operations
    the row is about, and a fixture that had to succeed at it first would have
    nothing left to test.
    """
    return Sandbox(client, {"id": sandbox_id})


def walk_strings(value: Any, path: str = "") -> List[Tuple[str, str]]:
    """Every string in a nested structure, with the path that reached it."""
    found: List[Tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(walk_strings(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(walk_strings(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        found.append((path, value))
    elif value is not None:
        found.append((path, str(value)))
    return found


def scan(payload: Any, needles: Tuple[str, ...], *, supplied: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """Find `needles` in `payload`, marking anything the caller itself sent.

    The mark matters: this suite launches from `docker.io/library/alpine`, and
    a scan that reported the caller's own image as a leaked vendor name would
    be noise a reviewer has to re-check by hand every run.
    """
    hits: List[Dict[str, Any]] = []
    for path, text in walk_strings(payload):
        lowered = text.lower()
        for needle in needles:
            if needle in lowered:
                hits.append(
                    {
                        "field": path,
                        "value": text[:200],
                        "matched": needle,
                        "caller supplied": any(
                            item and item.lower() in lowered for item in supplied if item
                        ),
                    }
                )
    return hits


@pytest.fixture(scope="session")
def other_org_client(test_client: AgentBoxClient) -> AgentBoxClient:
    """A client for the second organization, proven to be a different one.

    The proof is the point. A second key that could already see this
    organization's agents would make every "does not exist" in I-01 vacuous,
    so the fixture refuses to hand one over rather than let the row pass for
    the wrong reason.
    """
    key = os.getenv(OTHER_ORG_OWNER_ENV) or os.getenv(OTHER_ORG_FALLBACK_ENV)
    if not key:
        pytest.skip(
            f"neither {OTHER_ORG_OWNER_ENV} nor {OTHER_ORG_FALLBACK_ENV} is set, so there is "
            f"no second organization to test with"
        )
    if key == os.getenv("GMI_AGENTBOX_API_KEY"):
        pytest.skip(f"{OTHER_ORG_OWNER_ENV} holds the same key as GMI_AGENTBOX_API_KEY")

    other = AgentBoxClient(api_key=key)
    mine = {agent.slug for agent in test_client.agents.list(page_size=50).items}
    theirs = {agent.slug for agent in other.agents.list(page_size=50).items}
    shared = mine & theirs
    if shared:
        pytest.skip(
            f"the second key can already see {len(shared)} of this organization's agents "
            f"({sorted(shared)[:3]}), so it is not a separate tenant and cannot prove isolation"
        )
    return other


@pytest.fixture
def colleague_client() -> AgentBoxClient:
    """The second credential inside organization B — I-02's colleague.

    Only checked for being present and distinct here. Whether it really is a
    member of the same organization is not something the environment can
    promise, so I-02 proves it against a resource it creates rather than
    trusting the variable's name.
    """
    key = os.getenv(OTHER_ORG_COLLEAGUE_ENV)
    if not key:
        pytest.skip(f"{OTHER_ORG_COLLEAGUE_ENV} is not set, so there is no colleague to test with")
    if key == os.getenv(OTHER_ORG_OWNER_ENV):
        pytest.skip(
            f"{OTHER_ORG_COLLEAGUE_ENV} holds the same key as {OTHER_ORG_OWNER_ENV}; one "
            f"credential cannot play both the owner and the colleague"
        )
    return AgentBoxClient(api_key=key)


@pytest.fixture
def second_org_sandbox(other_org_client, tracker, run_id, image_url):
    """An agent and a running sandbox created inside organization B.

    I-02 is about two people in one organization, so its subject has to live
    in the organization both of its credentials belong to. The data centre and
    instance type are discovered through B's own catalogue: organization A's
    are not B's to launch into, and reusing them is how a row ends up testing
    the wrong tenant's quota.
    """

    def _create() -> Sandbox:
        centres = other_org_client.idcs.list(runtime="sandbox")
        if not centres:
            pytest.skip("the second organization has no sandbox data centres")
        for centre in centres:
            if not centre.idc_id:
                continue
            products = other_org_client.products.list(idc_name=centre.idc_id, runtime="sandbox")
            offered = {p.instance_type for p in products if p.instance_type}
            if not offered:
                continue
            sku = next(
                (s for s in ("gmi.sandbox.x-small", "gmi.sandbox.small") if s in offered),
                sorted(offered)[0],
            )
            agent = tracker.track_agent(
                other_org_client.agents.create(
                    title=f"sdk-test-b6-orgb-{run_id}",
                    image_url=image_url,
                    idc=centre.idc_id,
                    instance_type=sku,
                    runtime="sandbox",
                )
            )
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                agent.refresh()
                if agent.template_build_status in (None, "ready"):
                    break
                if agent.template_build_status == "error":
                    pytest.fail(f"organization B's image failed to build: {agent.template_build_error}")
                time.sleep(5)
            sandbox = tracker.track_sandbox(agent.launch(instance_type=sku))
            sandbox.wait_until_running(timeout=RUN_TIMEOUT)
            return agent, sandbox
        pytest.skip("the second organization has no sandbox SKU available")

    return _create


@pytest.fixture
def isolated_sandbox(session_agent, tracker, instance_type):
    """Launch a sandbox this row owns and ends itself."""

    def _launch() -> Sandbox:
        if not session_agent.launchable:
            pytest.skip(f"agent {session_agent.slug} is not launchable")
        sandbox = tracker.track_sandbox(session_agent.launch(instance_type=instance_type))
        sandbox.wait_until_running(timeout=RUN_TIMEOUT)
        return sandbox

    return _launch


# --------------------------------------------------------------------------
# I-06 is there a terminal in here
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("I-06 the SDK ships no interactive terminal")
@allure.severity(P0)
def test_i06_the_sdk_exposes_no_terminal():
    from agentbox_sdk import client as sdk_client

    with allure.step("1. the public surface of every object a caller touches"):
        surfaces = {
            "agentbox_sdk (module)": [n for n in dir(__import__("agentbox_sdk")) if not n.startswith("_")],
            "AgentBoxClient": [n for n in dir(AgentBoxClient) if not n.startswith("_")],
            "Sandbox": [n for n in dir(Sandbox) if not n.startswith("_")],
            "SandboxCollection": [n for n in dir(sdk_client.SandboxCollection) if not n.startswith("_")],
        }
        public = {
            where: sorted(n for n in names if any(word in n.lower() for word in TERMINAL_NAMES))
            for where, names in surfaces.items()
        }
        show(
            "public methods that would open an interactive session",
            method="(none) - inspecting the installed agentbox_sdk",
            args={"searched_for": TERMINAL_NAMES},
            returns={"surfaces": surfaces, "matches": public},
        )

    with allure.step("2. what the package carries underneath the public names"):
        private = sorted(
            name
            for name in dir(sdk_client)
            if any(word in name.lower() for word in TERMINAL_NAMES)
        )
        show(
            "module-level names that support a terminal",
            method="(none) - inspecting agentbox_sdk.client",
            returns={
                "names": private,
                "note": (
                    "a private transport is not an API a caller can reach, but it is what a "
                    "public method would be built on, so it says whether the capability was "
                    "removed or only left undocumented"
                ),
            },
        )

    with allure.step("3. what the documentation tells a reader they can call"):
        documented: Dict[str, List[str]] = {}
        for name in DOC_FILES:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
            try:
                with open(path, encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                documented[name] = ["<not present in this checkout>"]
                continue
            documented[name] = [
                f"{index}: {line.strip()[:140]}"
                for index, line in enumerate(lines, start=1)
                if any(word in line.lower() for word in TERMINAL_NAMES)
            ]
        show(
            "every documented mention of a terminal",
            method="(none) - reading " + ", ".join(DOC_FILES),
            returns=documented,
        )

    with allure.step("4. R0 ships no terminal"):
        offenders = {where: names for where, names in public.items() if names}
        show(
            "the verdict",
            method="(none) - collecting the three steps above",
            returns={
                "public entry points found": offenders or "none",
                "supporting machinery": private or "none",
                "documented": {k: v for k, v in documented.items() if v} or "none",
            },
        )
        assert not offenders, (
            f"the SDK exposes an interactive terminal on its public API: {offenders}. Q-18 "
            f"says R0 ships without one, and this is reachable by any caller holding an "
            f"organization key — every isolation guarantee in this chapter is only as strong "
            f"as what that session allows"
        )


# --------------------------------------------------------------------------
# I-01 another organization
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("I-01 another organization is told the sandbox does not exist")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_i01_another_organization_cannot_see_or_touch_the_sandbox(
    test_client, other_org_client, isolated_sandbox, session_agent
):
    sandbox = isolated_sandbox()
    stranger = bound_to(other_org_client, sandbox.id)
    marker = f"i01-{uuid.uuid4().hex[:8]}"
    sh(sandbox, f"echo {marker} > /tmp/i01.txt")

    operations: Dict[str, Dict[str, Any]] = {}

    with allure.step("1. every operation B can aim at A's sandbox"):
        operations["get"] = attempt(lambda: other_org_client.sandboxes.get(sandbox.id))
        operations["execute"] = attempt(lambda: stranger.execute("cat /tmp/i01.txt"))
        operations["download"] = attempt(lambda: stranger.download_file(path="/tmp/i01.txt"))
        operations["upload"] = attempt(lambda: stranger.upload_file(path="/tmp/i01-b.txt", file=b"B was here"))
        operations["logs"] = attempt(lambda: stranger.logs())
        operations["delete"] = attempt(lambda: stranger.delete())
        show(
            "organization B against organization A's sandbox",
            method="other_org_client.sandboxes.* on A's sandbox id",
            args={"sandbox_id": sandbox.id, "agent": session_agent.slug},
            returns=operations,
        )

    with allure.step("2. A's sandbox is untouched by any of it"):
        sandbox.refresh()
        still_there = sh(sandbox, "cat /tmp/i01.txt")
        planted = sh(sandbox, "cat /tmp/i01-b.txt 2>&1 | head -1")
        show(
            "what A sees afterwards",
            method="sandbox.refresh(), then sandbox.execute('cat ...')",
            returns={
                "status": sandbox.status,
                "the file A wrote": still_there,
                "anything B wrote": planted,
            },
        )
        assert sandbox.status == "running", (
            f"another organization's calls left A's sandbox {sandbox.status!r}"
        )
        assert still_there == marker, "another organization changed A's file"
        assert "No such file" in planted or not planted, (
            f"another organization wrote into A's sandbox: {planted!r}"
        )

    with allure.step("3. B's own list holds only B's"):
        theirs = other_org_client.sandboxes.list(page_size=100)
        their_agents = other_org_client.agents.list(page_size=50)
        show(
            "what B can enumerate",
            method="other_org_client.sandboxes.list(), other_org_client.agents.list()",
            returns={
                "sandboxes B sees": [item.id for item in theirs.items],
                "agents B sees": [item.slug for item in their_agents.items],
                "A's sandbox is among them": sandbox.id in {item.id for item in theirs.items},
                "A's agent is among them": session_agent.slug
                in {item.slug for item in their_agents.items},
            },
        )
        assert sandbox.id not in {item.id for item in theirs.items}, (
            "A's sandbox appears in another organization's list"
        )
        assert session_agent.slug not in {item.slug for item in their_agents.items}, (
            "A's agent appears in another organization's list"
        )

    with allure.step("4. every answer is 'does not exist', not 'not allowed'"):
        allowed = {name: result for name, result in operations.items() if not result["refused"]}
        leaking = {
            name: result["status_code"]
            for name, result in operations.items()
            if result["refused"] and result["status_code"] in EXISTENCE_LEAKING
        }
        show(
            "what each refusal said",
            method="(none) - reading the status codes above",
            args={
                "the answer the case row requires": NOT_FOUND,
                "codes that confirm the resource exists": list(EXISTENCE_LEAKING),
            },
            returns={
                "operations that were allowed": allowed or "none",
                "refusals that confirm existence": leaking or "none",
                "status codes": {
                    name: result["status_code"] for name, result in operations.items()
                },
            },
        )
        assert not allowed, (
            f"another organization was allowed to {sorted(allowed)} on A's sandbox"
        )
        assert not leaking, (
            f"these refusals tell another organization that the sandbox exists: {leaking}. A "
            f"tenant that can tell 403 from 404 can enumerate ids across the boundary; the "
            f"case row asks for {NOT_FOUND} on every one"
        )


# --------------------------------------------------------------------------
# I-02 an ordinary member of the same organization
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("I-02 a colleague in the same organization reads but cannot write")
@allure.severity(P1)
@pytest.mark.slow
@pytest.mark.billable
def test_i02_a_colleague_reads_but_cannot_write(
    other_org_client, colleague_client, second_org_sandbox
):
    agent, sandbox = second_org_sandbox()
    marker = f"i02-{uuid.uuid4().hex[:8]}"
    sh(sandbox, f"echo {marker} > /tmp/i02.txt")
    colleague = bound_to(colleague_client, sandbox.id)

    with allure.step("1. the two credentials really are in one organization"):
        theirs = {item.slug for item in colleague_client.agents.list(page_size=50).items}
        shares = agent.slug in theirs
        show(
            "what the colleague can enumerate",
            method="colleague_client.agents.list()",
            args={"the agent the owner created": agent.slug},
            returns={
                "agents the colleague sees": sorted(theirs),
                "the owner's agent is among them": shares,
            },
        )
        if not shares:
            pytest.skip(
                f"{OTHER_ORG_COLLEAGUE_ENV} cannot see the agent {OTHER_ORG_OWNER_ENV} just "
                f"created, so the two keys are not members of one organization and this row "
                f"has no colleague to test with. Whether a second person in my organization "
                f"can read my sandbox and is stopped from changing it stays untested until "
                f"two keys are issued to the same organization, ideally with the second "
                f"holding a non-admin role"
            )

    with allure.step("2. the colleague can read the sandbox and its results"):
        reads = {
            "get": attempt(lambda: colleague_client.sandboxes.get(sandbox.id)),
            "list": attempt(lambda: colleague_client.sandboxes.list(page_size=50)),
            "download": attempt(lambda: colleague.download_file(path="/tmp/i02.txt")),
        }
        listed = reads["list"]
        show(
            "what a colleague may read",
            method="colleague_client.sandboxes.get() / .list(), then sandbox.download_file()",
            args={"sandbox_id": sandbox.id},
            returns={
                "attempts": reads,
                "the owner's sandbox is in the colleague's list": (
                    sandbox.id in {item["id"] for item in (listed.get("returned") or {}).get("items", [])}
                    if not listed["refused"]
                    else None
                ),
            },
        )
        for name, result in reads.items():
            assert not result["refused"], (
                f"a colleague in the same organization was refused {name}, which the case row "
                f"says must succeed: {result}"
            )

    with allure.step("3. the colleague cannot change anything"):
        writes = {
            "upload": attempt(
                lambda: colleague.upload_file(
                    path="/tmp/i02-colleague.txt", file=b"colleague was here"
                )
            ),
            "execute": attempt(lambda: colleague.execute("echo colleague")),
        }
        # Asked of the shell rather than matched in an error message, and asked
        # before the delete attempt: a delete that succeeds takes the sandbox
        # this question is put to.
        planted = sh(
            sandbox, "[ -f /tmp/i02-colleague.txt ] && echo EXISTS || echo MISSING"
        )

        # If execute was allowed, the refusal of upload decides nothing: a
        # shell command writes files too. This turns "execute succeeded" into
        # a demonstration of what that actually permits.
        bypass: Dict[str, Any] = {"attempted": False}
        if not writes["execute"]["refused"]:
            written = f"i02-bypass-{uuid.uuid4().hex[:6]}"
            bypass = {
                "attempted": True,
                "how": "sandbox.execute(\'echo ... > /tmp/i02-bypass.txt\') on the colleague's client",
                "result": attempt(
                    lambda: colleague.execute(f"echo {written} > /tmp/i02-bypass.txt")
                ),
                "what the owner finds afterwards": sh(
                    sandbox, "cat /tmp/i02-bypass.txt 2>&1 | head -1"
                ),
                "expected marker": written,
            }
        writes["delete"] = attempt(lambda: colleague.delete())
        try:
            sandbox.refresh()
            status_after = sandbox.status
        except APIError as exc:
            status_after = f"unreadable: {exc.status_code} {exc.message}"
        show(
            "what a colleague may not do",
            method="sandbox.upload_file() / .execute() / .delete() on the colleague's client",
            returns={
                "attempts": writes,
                "anything the colleague uploaded": planted,
                "writing through execute instead": bypass,
                "the sandbox afterwards": status_after,
                "note": (
                    "execute and delete are checked alongside the upload because this "
                    "chapter's line is read versus change, and running a command is a change"
                ),
            },
        )
        assert writes["upload"]["refused"], (
            f"a colleague uploaded into a sandbox they do not own: {writes['upload']}"
        )
        assert planted == "MISSING", (
            f"the colleague's file is on the sandbox after the upload was refused: {planted!r}"
        )
        assert writes["delete"]["refused"], (
            f"a colleague deleted a sandbox they do not own: {writes['delete']}"
        )
        assert status_after == "running", (
            f"a colleague's calls left the owner's sandbox {status_after!r}"
        )
        assert writes["execute"]["refused"], (
            f"a colleague ran a command in a sandbox they do not own. upload and delete were "
            f"both refused with 403, so the boundary exists — execute is simply on the wrong "
            f"side of it, and it is the stronger permission of the three: "
            f"{bypass.get('what the owner finds afterwards')!r} is what the colleague wrote "
            f"through it after the upload had been refused. Refusing upload while allowing "
            f"execute stops nothing"
        )


# --------------------------------------------------------------------------
# I-03 one sandbox against another
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("I-03 two sandboxes cannot see or reach each other")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_i03_one_sandbox_cannot_reach_another(isolated_sandbox, test_client):
    first = isolated_sandbox()
    second = isolated_sandbox()
    secret_path = "/tmp/i03-secret.txt"
    secret = f"i03-{uuid.uuid4().hex[:8]}"
    process_tag = f"i03proc{uuid.uuid4().hex[:6]}"

    sh(first, f"echo {secret} > {secret_path}")
    sh(first, f"(setsid sleep 600 > /dev/null 2>&1 & echo started) ; echo {process_tag} > /tmp/{process_tag}")
    endpoint = first.data.get("endpoint_url") or None

    with allure.step("1. S2 asks for S1's file, through the API and from inside"):
        through_api = attempt(lambda: second.download_file(path=secret_path))
        from_inside = sh(second, f"cat {secret_path} 2>&1 | head -1")
        show(
            "S2 reaching for a file that only S1 has",
            method="S2.download_file(path=<S1's path>), then S2.execute('cat <S1's path>')",
            args={"path": secret_path, "S1": first.id, "S2": second.id},
            returns={
                "through the API": through_api,
                "from inside S2": from_inside,
                "the secret S1 holds": secret,
            },
        )
        assert secret not in json.dumps(through_api, default=str), (
            "S2 downloaded S1's file through the API"
        )
        assert secret not in from_inside, f"S2 read S1's file from inside: {from_inside!r}"

    with allure.step("2. S2 tries to kill what S1 is running"):
        killed = sh(second, f"pkill -f 'sleep 600' 2>&1; echo exit=$?")
        alive = sh(first, "pgrep -f 'sleep 600' >/dev/null && echo alive || echo gone")
        show(
            "S2 aiming at S1's processes",
            method="S2.execute('pkill -f ...'), then S1.execute('pgrep -f ...')",
            returns={"what S2's pkill said": killed, "S1's process afterwards": alive},
        )
        assert alive == "alive", "S2 killed a process running in S1"

    with allure.step("3. what credentials S2 is holding, and what they reach"):
        # Every sandbox is handed environment variables by the platform. If one
        # of them is a credential, the question is what it is a credential for.
        env_names = sh(second, "printenv | cut -d= -f1 | sort | tr '\\n' ' '")
        reached = {}
        if endpoint:
            reached["S2 curling S1's endpoint"] = sh(
                second,
                f"(command -v wget >/dev/null && wget -qO- --timeout=10 '{endpoint}' 2>&1 || "
                f"echo 'no wget') | head -c 300",
            )
        show(
            "the credentials inside S2, and where they get it",
            method="S2.execute('printenv'), then S2.execute('wget <S1 endpoint>')",
            returns={
                "environment variable names in S2": env_names,
                "S1's endpoint_url": endpoint or "the sandbox exposes none",
                "what S2 got": reached or "not attempted: S1 exposes no endpoint_url",
            },
        )
        if endpoint:
            assert secret not in str(reached), "S2 reached S1 over the network and got its data"

    with allure.step("4. S1's endpoint, fetched with no credentials at all"):
        if not endpoint:
            show(
                "a direct connection to S1",
                method="(none) - the sandbox body carries no endpoint_url to connect to",
                returns={
                    "endpoint_url": None,
                    "body fields available": sorted(first.data),
                    "what this leaves untested": "with no address there is nothing to aim the "
                    "no-credentials request at. That is not the endpoint being closed — it is "
                    "the SDK never telling a caller where the sandbox is, and nothing here "
                    "says whether one exists and answers to somebody who finds it another way",
                },
            )
        else:
            naked = direct_get(endpoint)
            show(
                "S1's endpoint with no Authorization header",
                method="urllib.request.urlopen(endpoint_url) - the SDK is not involved",
                args={"endpoint_url": endpoint},
                returns=naked,
            )
            assert naked["refused"], (
                f"S1's endpoint answered {naked.get('status_code')} to a request carrying no "
                f"credentials at all: {naked.get('body_head')!r}"
            )


# --------------------------------------------------------------------------
# I-04 credentials after the sandbox is gone
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("I-04 old credentials are dead once the sandbox is")
@allure.severity(P0)
@pytest.mark.slow
@pytest.mark.billable
def test_i04_old_credentials_die_with_the_sandbox(isolated_sandbox, test_client):
    sandbox = isolated_sandbox()
    sandbox_id = sandbox.id
    endpoint = sandbox.data.get("endpoint_url") or None
    sh(sandbox, "echo i04 > /tmp/i04.txt")

    with allure.step("1. what a caller is holding while the sandbox is alive"):
        before = {
            "endpoint_url": endpoint or "none is exposed",
            "sandbox_id": sandbox_id,
            "the organization key": "unchanged by a delete — it is not the sandbox's",
        }
        if endpoint:
            before["a direct fetch now"] = direct_get(endpoint)
        show(
            "the handles that exist before the delete",
            method="sandbox.data, then a direct fetch of endpoint_url",
            returns=before,
        )

    with allure.step("2. delete it"):
        sandbox.delete()
        show("delete", method="sandbox.delete()", returns={"sandbox_id": sandbox_id})

    with allure.step("3. every held handle, used again"):
        afterwards = {
            "sandboxes.get": attempt(lambda: test_client.sandboxes.get(sandbox_id)),
            "execute": attempt(lambda: bound_to(test_client, sandbox_id).execute("echo after")),
            "download": attempt(
                lambda: bound_to(test_client, sandbox_id).download_file(path="/tmp/i04.txt")
            ),
            "upload": attempt(
                lambda: bound_to(test_client, sandbox_id).upload_file(path="/tmp/i04b.txt", file=b"x")
            ),
        }
        if endpoint:
            afterwards["the endpoint, with no credentials"] = direct_get(endpoint)
            afterwards["the endpoint, with the organization key"] = direct_get(
                endpoint, headers={"Authorization": f"Bearer {os.environ['GMI_AGENTBOX_API_KEY']}"}
            )
        show(
            "the same handles after the delete",
            method="every call that worked a moment ago, repeated",
            args={"sandbox_id": sandbox_id, "endpoint_url": endpoint or "none"},
            returns=afterwards,
        )
        alive = {name: result for name, result in afterwards.items() if not result["refused"]}
        assert not alive, (
            f"these still worked after the sandbox was deleted: {sorted(alive)} — a credential "
            f"or address that outlives its sandbox is one nobody knows to revoke: {alive}"
        )

    with allure.step("4. the same question after an expiry"):
        # Recorded, not asserted: the state cannot be produced inside a run, and
        # "cannot be tested" reads like "does not work" unless the row says so.
        show(
            "why the expiry half is not covered",
            method="(none) - no SDK call expires a sandbox on demand",
            returns={
                "launch()": "has no lifetime, ttl or expiry parameter, and B-4 D-03 found no "
                "extend or renew call",
                "the expiry itself": "a rolling 24h idle window (measured 2026-09-23: every "
                "execute, upload and download pushes it to now + 24h), so a sandbox in use "
                "never reaches it and a test would have to idle for a day",
                "covered here": "the delete path above; whether expiry revokes the same "
                "handles as a delete is unverified",
            },
        )


# --------------------------------------------------------------------------
# I-05 what the payloads and the logs give away
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("I-05 no credential in the clear, no vendor underneath")
@allure.severity(P1)
@pytest.mark.slow
@pytest.mark.billable
def test_i05_payloads_and_logs_leak_no_secret_and_no_vendor(
    isolated_sandbox, session_agent, image_url, sandbox_target
):
    sandbox = isolated_sandbox()
    sandbox.refresh()

    # The one value this suite chooses for itself. A scan that flagged it would
    # report the caller's own input as the platform's leak.
    #
    # The data centre and the instance type are deliberately NOT on this list,
    # even though the suite passes them in: it does not choose them, it reads
    # them back from `idcs.list()` and `products.list()` and hands them
    # straight to `launch`. They are the platform's own identifiers, and
    # excluding them hid the finding this row exists for — `sandbox-runloop-us`
    # was filtered out as "caller supplied" and the row passed clean.
    supplied = (image_url,)
    discovered = {
        "idc (read from idcs.list)": sandbox_target.idc,
        "instance_type (read from products.list)": sandbox_target.instance_type,
    }
    # Every key this checkout holds, so a payload echoing any of them is caught
    # — not only the one that made the request.
    secrets = tuple(
        value
        for value in (
            os.getenv("GMI_AGENTBOX_API_KEY"),
            os.getenv(OTHER_ORG_OWNER_ENV),
            os.getenv(OTHER_ORG_COLLEAGUE_ENV),
            os.getenv(OTHER_ORG_FALLBACK_ENV),
        )
        if value
    )

    with allure.step("1. capture what get, list and the debug log print"):
        from agentbox_sdk._transport import urlopen_transport

        from helpers.http_log import logging_transport

        captured: List[str] = []
        logged_client = AgentBoxClient(
            transport=logging_transport(urlopen_transport, captured.append)
        )
        logged_client.sandboxes.get(sandbox.id)
        logged_client.sandboxes.list(page_size=10)
        logged_client.agents.get(session_agent.slug)
        debug_log = "\n".join(captured)

        payloads = {
            "sandbox (get)": dict(sandbox.data),
            "agent (get)": dict(session_agent.data),
        }
        show(
            "what a caller can print",
            method="sandboxes.get(), sandboxes.list(), agents.get() through a logging transport",
            returns={
                "sandbox fields": sorted(payloads["sandbox (get)"]),
                "agent fields": sorted(payloads["agent (get)"]),
                "debug log size_chars": len(debug_log),
            },
        )

    with allure.step("2. is any credential in the clear?"):
        in_payloads = [
            hit for hit in scan(payloads, tuple(s.lower() for s in secrets), supplied=())
        ]
        in_log = [secret[:6] + "…" for secret in secrets if secret in debug_log]
        env_echo = payloads["sandbox (get)"].get("env")
        show(
            "looking for the keys themselves",
            method="(none) - scanning the payloads and the debug log",
            returns={
                "API keys found in a payload": in_payloads or "none",
                "API keys found in the debug log": in_log or "none",
                "the env the sandbox reports": _payload(env_echo) if env_echo else "absent",
                "note": (
                    "the debug log redacts Authorization itself; this asks whether the key "
                    "turns up anywhere it does not know to redact"
                ),
            },
        )
        assert not in_payloads, f"an API key is echoed back in a payload: {in_payloads}"
        assert not in_log, f"an API key appears unredacted in the debug log: {in_log}"

    with allure.step("3. is the thing underneath named?"):
        payload_hits = [
            hit for hit in scan(payloads, VENDOR_NAMES, supplied=supplied)
            if not hit["caller supplied"]
        ]
        log_hits = [
            hit
            for hit in scan({"debug_log": debug_log}, VENDOR_NAMES, supplied=supplied)
            if not hit["caller supplied"]
        ]
        show(
            "looking for the name of whatever runs the sandbox",
            method="(none) - scanning the payloads and the debug log",
            args={
                "searched_for": VENDOR_NAMES,
                "values this suite chose, and so does not count": supplied,
                "values the suite read back from the platform, which do count": discovered,
            },
            returns={
                "in the payloads": payload_hits or "none",
                "in the debug log": log_hits[:10] or "none",
                "in the platform's own identifiers": scan(discovered, VENDOR_NAMES, supplied=supplied)
                or "none",
                "why this is a leak": (
                    "a tenant that can read which product runs its code learns the platform's "
                    "supply chain, and can aim at it directly"
                ),
            },
        )
        identifier_hits = [
            hit for hit in scan(discovered, VENDOR_NAMES, supplied=supplied)
            if not hit["caller supplied"]
        ]
        assert not identifier_hits, (
            f"the identifiers the platform hands out name what runs the sandbox underneath: "
            f"{identifier_hits} — a caller does not pick these, it reads them from the "
            f"catalogue and passes them back, so every tenant sees the name"
        )
        assert not payload_hits, (
            f"the sandbox payload names what runs it underneath: {payload_hits}"
        )
        assert not log_hits, (
            f"the debug log names what runs the sandbox underneath: {log_hits[:5]}"
        )
