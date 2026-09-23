"""B-5 Retry, idempotency and quota — one test per case row.

CI builds and tears down hundreds of sandboxes a day, and a request resent
after a network hiccup must not leave a second sandbox running or a second
charge behind. At full quota the refusal has to say quota — not "no capacity",
and not a timeout.

| ID   | P  | Action                                              | Expected                                              |
|------|----|-----------------------------------------------------|-------------------------------------------------------|
| R-01 | P0 | launch under one idempotency key: resent, three     | one sandbox in every case. No such key -> Known gap   |
|      |    | threads at once, and resent after a timeout         |                                                       |
| R-02 | P1 | 20 concurrent launches, each running `hostname`     | all reach running, and no id or hostname is shared or |
|      |    |                                                     | crossed between them                                  |
| R-03 | P1 | fill the quota, then free a slot by deleting,       | the refusal names quota; every freed slot can be      |
|      |    | failing or expiring one, and launch again           | launched into again                                   |

Every row launches real sandboxes, so every row is `billable`, and every row
deletes each sandbox as soon as it is done with it rather than at teardown —
the quota these rows are about is the same quota they would otherwise exhaust.

Four measurements from the earlier chapters decide what these rows can do.

`launch()` takes no idempotency key, request id or client token (B-2 E-08 found
the same of `execute()`), so R-01 cannot send the key its case row is written
around; it sends the request the way a retrying client would and counts what
that leaves behind. `display_name` is the only caller-supplied identifier on a
launch, so the row holds it constant to give any accidental deduplication its
best chance.

This organization admits somewhere between five and eight concurrent sandboxes
of the smallest instance type, so R-02's twenty cannot be up at once. The row
fires all twenty anyway — the refusal shape at that concurrency is worth having
— then works through the remainder in further rounds, and reads back the number
it got rather than assuming one.

A launch past the quota is answered in two places at once, so R-03 looks in
both. Sequentially — measured 2026-09-23 — the call itself raises after ~0.7s,
and it *also* leaves a task record behind that settles into `error`. Fired
concurrently, some of those calls instead return an id and the rejection only
shows up seconds later on the task. Either way a record exists that the caller
may have no id for, which is why every count here reads the unfiltered list and
attributes what it finds rather than trusting what it tracked.

A refusal on the call leaves a record too, so these rows clean up by difference
rather than by what they tracked: twenty launches against a quota of eight
returned eight ids, raised twelve times, and left twenty task records behind.
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
from agentbox_sdk import AgentBoxClient, AgentBoxSDKError, APIError, Sandbox
from agentbox_sdk.client import RUNNING_STATUS, WAIT_FAILURE_STATUSES, SandboxCollection

from helpers.interrupt import abandon_response_transport
from helpers.report import show

FEATURE = "B-5 Retry, idempotency and quota"
P0 = allure.severity_level.BLOCKER
P1 = allure.severity_level.CRITICAL

pytestmark = [pytest.mark.slow, pytest.mark.billable]

RUN_TIMEOUT = float(os.getenv("GMI_TEST_RUN_TIMEOUT", "300"))
# One second rather than the SDK's two: these rows time launches, and the poll
# interval is the error bar on every number they report.
POLL = 1.0
# A deleted sandbox holds its quota slot for a moment after the call returns.
RELEASE_POLL = 2.0
RELEASE_DEADLINE = float(os.getenv("GMI_TEST_RELEASE_DEADLINE", "180"))

# R-01. Three threads is what the case row asks for.
R01_THREADS = 3
# The timeout leg needs the request to arrive and the answer to be thrown
# away. `AgentBoxClient(timeout=...)` cannot do that: urllib's timeout bounds
# a single socket operation rather than the call, so a launch shorter than any
# one blocking read simply succeeds — measured, with a 1.89s budget against a
# 1.98s launch. `abandon_response_transport` does it at the socket instead,
# and this is how long it waits before closing, so the service has certainly
# read the request it is about to answer into a closed connection.
R01_ABANDON_AFTER = float(os.getenv("GMI_TEST_ABANDON_AFTER", "1.5"))

# R-02. Twenty distinct sandboxes, fired at the highest concurrency the
# account will take, in as many rounds as that needs.
R02_TOTAL = int(os.getenv("GMI_TEST_CONCURRENT_LAUNCHES", "20"))
R02_MAX_ROUNDS = int(os.getenv("GMI_TEST_CONCURRENT_ROUNDS", "6"))

# R-03. An upper bound on the search for the quota, so a raised quota turns
# into a reported number rather than an unbounded spend.
R03_MAX_FILL = int(os.getenv("GMI_TEST_MAX_FILL", "8"))
# How long to wait for an accepted-then-rejected launch to reach `error`.
R03_SETTLE = float(os.getenv("GMI_TEST_QUOTA_SETTLE", "60"))
# What a refusal has to mention for it to be about quota rather than capacity.
QUOTA_WORDS = ("quota", "429", "limit", "exceeded", "toomany", "too many")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def snapshot(client: AgentBoxClient) -> Dict[str, Optional[str]]:
    """Every sandbox the account can see right now, id -> the agent that owns it.

    Unfiltered on purpose. A launch the backend accepts and then rejects on
    quota leaves a task whose `deployment_slug` is null, and no per-agent list
    returns it — so a row counting only its own agent's sandboxes would not
    see the one it had just created by accident, which is the entire thing
    R-01 is trying to count.
    """
    return {item.id: item.agent_id for item in client.sandboxes.list(page_size=100).items}


def created_since(
    before: Dict[str, Optional[str]], after: Dict[str, Optional[str]], agent_id: str
) -> Dict[str, List[str]]:
    """New sandbox ids since `before`, split by who they turned out to belong to.

    The third bucket exists so that a suite running in another terminal shows
    up as itself instead of being counted as something this row created.
    """
    fresh = {sid: owner for sid, owner in after.items() if sid not in before}
    return {
        "this agent": sorted(sid for sid, owner in fresh.items() if owner == agent_id),
        "no agent at all": sorted(sid for sid, owner in fresh.items() if not owner),
        "another agent, i.e. a concurrent run": sorted(
            sid for sid, owner in fresh.items() if owner and owner != agent_id
        ),
    }


def ours(buckets: Dict[str, List[str]]) -> List[str]:
    """What this row is answerable for: its own, plus anything left unowned."""
    return buckets["this agent"] + buckets["no agent at all"]


def holding_quota(client: AgentBoxClient) -> Optional[int]:
    """How many sandboxes are in a state that can still be holding a slot."""
    try:
        page = client.sandboxes.list(page_size=100)
    except AgentBoxSDKError:
        return None  # "could not tell" must stay distinct from "none"
    return sum(1 for item in page.items if item.status not in WAIT_FAILURE_STATUSES)


def wait_for_room(client: AgentBoxClient, *, baseline: int, deadline: float) -> Dict[str, Any]:
    """Wait until the account is back down to `baseline` occupied slots.

    Measured against a baseline rather than zero: another run may legitimately
    be holding sandboxes, and waiting for the account to empty would then hang
    until the deadline every time.
    """
    started = time.monotonic()
    held = holding_quota(client)
    while held is not None and held > baseline and time.monotonic() - started < deadline:
        time.sleep(RELEASE_POLL)
        held = holding_quota(client)
    return {
        "holding_quota": held,
        "baseline": baseline,
        "waited_s": round(time.monotonic() - started, 1),
    }


def discard(sandboxes: List[Sandbox]) -> Dict[str, Any]:
    """Delete now, not at teardown — every one of these is a quota slot.

    Catches every SDK error, not only `APIError`: `TransportError` is its
    sibling rather than its subclass, and letting one escape here would
    abandon the sandboxes after it while a `finally` swallowed the row's real
    verdict.
    """
    deleted: List[str] = []
    failed: List[Dict[str, Any]] = []
    for sandbox in sandboxes:
        try:
            sandbox.delete()
        except APIError as exc:
            if exc.status_code == 404:
                deleted.append(sandbox.id)
            else:
                failed.append(
                    {"id": sandbox.id, "status_code": exc.status_code, "message": exc.message}
                )
        except AgentBoxSDKError as exc:
            failed.append({"id": sandbox.id, "raised": type(exc).__name__, "message": str(exc)})
        else:
            deleted.append(sandbox.id)
    return {"deleted": len(deleted), "failed": failed}


def discard_ids(client: AgentBoxClient, ids: List[str]) -> Dict[str, Any]:
    """Delete by id, for sandboxes this row only ever learned the id of."""
    return discard([Sandbox(client, {"id": sandbox_id}) for sandbox_id in ids])


def sweep(
    client: AgentBoxClient, before: Dict[str, Optional[str]], agent_id: str
) -> Dict[str, Any]:
    """Delete anything that appeared since `before` and belongs to this run.

    A launch the service refuses on the call still leaves a task record, and
    the SDK raised instead of returning an id — so the caller has no handle on
    it and nothing a row tracked can clean it up. One round of twenty launches
    against a quota of eight left twelve such records here. They are inert and
    they do not bill, but they accumulate in the account for as long as nobody
    lists it and deletes by difference, which is what this does.
    """
    buckets = created_since(before, snapshot(client), agent_id)
    strays = ours(buckets)
    return {
        "left behind": len(strays),
        "ids": strays,
        "removed": discard_ids(client, strays) if strays else {"deleted": 0, "failed": []},
        "belonging to another run, left alone": buckets["another agent, i.e. a concurrent run"],
    }


def settle(sandbox: Sandbox, *, deadline: float) -> Dict[str, Any]:
    """Poll one launched sandbox until it runs or reaches a terminal state.

    Returns the reason as well as the verdict: a launch rejected on quota
    arrives here, and the whole question R-03 asks is what it says.
    """
    started = time.monotonic()
    status: Optional[str] = None
    while time.monotonic() - started < deadline:
        try:
            status = sandbox.refresh().status
        except AgentBoxSDKError:
            status = None
        if status == RUNNING_STATUS:
            return {"reached": "running", "seconds": round(time.monotonic() - started, 2)}
        if status in WAIT_FAILURE_STATUSES:
            return {
                "reached": status,
                "seconds": round(time.monotonic() - started, 2),
                "last_error": sandbox.last_error,
            }
        time.sleep(POLL)
    return {"reached": status, "seconds": round(deadline, 2), "note": "still not terminal"}


def names_quota(text: Optional[str]) -> bool:
    """Whether a refusal says it is about quota rather than about capacity."""
    lowered = (text or "").lower().replace(" ", "")
    return any(word.replace(" ", "") in lowered for word in QUOTA_WORDS)


def launch_concurrently(
    agent, instance_type: str, count: int, tracker
) -> List[Dict[str, Any]]:
    """Fire `count` launches from `count` threads and describe every outcome.

    Each thread times its own call, so a refusal that took as long as a success
    is visible as one — a queued request and a refused one are the same number
    of sandboxes and very different behaviour.
    """

    def _one(index: int) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            sandbox = agent.launch(instance_type=instance_type)
        except Exception as exc:  # noqa: BLE001 - the shape of a refusal is the answer
            return {
                "index": index,
                "ok": False,
                "elapsed_s": round(time.monotonic() - started, 2),
                "raised": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None),
                "message": str(exc)[:300],
            }
        tracker.track_sandbox(sandbox)  # tracked before anything else can fail
        return {
            "index": index,
            "ok": True,
            "elapsed_s": round(time.monotonic() - started, 2),
            "id": sandbox.id,
            "sandbox": sandbox,
        }

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(_one, range(1, count + 1)))


def read_hostnames(sandboxes: List[Sandbox]) -> Dict[str, Any]:
    """Ask every sandbox for its hostname at the same moment.

    Concurrently, because that is where identities get crossed: one command
    per sandbox issued one after another would never catch a response routed
    to the wrong caller.
    """

    def _one(sandbox: Sandbox) -> Tuple[str, Optional[str], Optional[str]]:
        try:
            execution = sandbox.execute("hostname", wait_timeout_seconds=60)
            return sandbox.id, (execution.data.get("stdout") or "").strip(), None
        except Exception as exc:  # noqa: BLE001
            return sandbox.id, None, f"{type(exc).__name__}: {exc}"

    if not sandboxes:
        return {}
    with ThreadPoolExecutor(max_workers=len(sandboxes)) as pool:
        answers = list(pool.map(_one, sandboxes))
    return {
        sandbox_id: {"hostname": hostname, "error": error}
        for sandbox_id, hostname, error in answers
    }


def reportable(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """An outcome with the live SDK objects dropped, ready for an attachment."""
    return {key: value for key, value in outcome.items() if key != "sandbox"}


def inspect_parameters(function: Any) -> List[str]:
    """The parameter names of a callable, read off its signature."""
    return list(inspect.signature(function).parameters)


# --------------------------------------------------------------------------
# R-01 the same launch, sent more than once
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("R-01 one logical launch, however many times the request goes out")
@allure.severity(P0)
def test_r01_resending_one_launch_must_not_build_a_second_sandbox(
    test_client, session_agent, tracker, instance_type
):
    # The nearest thing to a key this API has, held constant across every
    # request in this row so that any accidental deduplication would show.
    key = uuid.uuid4().hex[:12]
    display_name = f"r01-{key}"
    legs: Dict[str, Dict[str, Any]] = {}
    # Counted the way `wait_for_room` counts, not as the length of a snapshot:
    # a snapshot lists terminal tasks too, and a baseline that included them
    # would be satisfied while real slots were still occupied.
    baseline = holding_quota(test_client) or 0

    def launch_once(client: AgentBoxClient) -> Dict[str, Any]:
        # `sandboxes.launch` rather than `agent.launch`, which is only a
        # wrapper around it: this way an attempt is exactly one request. The
        # timeout leg gives the client a budget smaller than a launch takes,
        # and a preliminary `agents.get` would spend that budget before the
        # request the leg is about ever went out.
        started = time.monotonic()
        try:
            sandbox = client.sandboxes.launch(
                session_agent.slug,
                instance_type=instance_type,
                idc_name=session_agent.idc,
                display_name=display_name,
                template_id=session_agent.id,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "elapsed_s": round(time.monotonic() - started, 2),
                "raised": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None),
                "message": str(exc)[:300],
            }
        tracker.track_sandbox(sandbox)
        return {"ok": True, "elapsed_s": round(time.monotonic() - started, 2), "id": sandbox.id}

    with allure.step("1. does launch accept an idempotency key at all?"):
        hints = ("idempot", "request_id", "requestid", "client_token", "dedup", "nonce", "key")
        surfaces = {
            "Agent.launch": [
                name for name in inspect_parameters(type(session_agent).launch) if name != "self"
            ],
            "SandboxCollection.launch": [
                name for name in inspect_parameters(SandboxCollection.launch) if name != "self"
            ],
        }
        found = {
            where: [name for name in names if any(hint in name.lower() for hint in hints)]
            for where, names in surfaces.items()
        }
        show(
            "looking for an idempotency key on launch",
            method="inspect.signature(Agent.launch), inspect.signature(SandboxCollection.launch)",
            args={"the key this row would send": key, "searched_for": hints},
            returns={"parameters": surfaces, "idempotency parameters": found},
        )

    with allure.step("2. the same launch, sent twice in a row"):
        before = snapshot(test_client)
        first = launch_once(test_client)
        second = launch_once(test_client)
        after = snapshot(test_client)
        buckets = created_since(before, after, session_agent.id)
        legs["resent"] = {
            "sent": 2,
            "first": first,
            "second": second,
            "sandboxes created": buckets,
            "count": len(ours(buckets)),
        }
        show(
            "launch, then the identical launch again",
            method="test_client.sandboxes.launch(slug, instance_type=..., "
            "display_name=<same>) twice",
            args={"display_name": display_name},
            returns=legs["resent"],
        )
        discard_ids(test_client, ours(buckets))
        wait_for_room(test_client, baseline=baseline, deadline=RELEASE_DEADLINE)

    with allure.step("3. the same launch from several threads at once"):
        before = snapshot(test_client)

        def _threaded(_: int) -> Dict[str, Any]:
            return launch_once(test_client)

        with ThreadPoolExecutor(max_workers=R01_THREADS) as pool:
            concurrent = list(pool.map(_threaded, range(R01_THREADS)))
        after = snapshot(test_client)
        buckets = created_since(before, after, session_agent.id)
        legs["three at once"] = {
            "sent": R01_THREADS,
            "outcomes": concurrent,
            "sandboxes created": buckets,
            "count": len(ours(buckets)),
        }
        show(
            f"the identical launch from {R01_THREADS} threads",
            method=f"test_client.sandboxes.launch(slug, ...) x{R01_THREADS} concurrently, "
            "same display_name",
            args={"display_name": display_name},
            returns=legs["three at once"],
        )
        discard_ids(test_client, ours(buckets))
        wait_for_room(test_client, baseline=baseline, deadline=RELEASE_DEADLINE)

    with allure.step("4. a launch that times out on the client, then is retried"):
        healthy = first.get("elapsed_s") or 2.0
        before = snapshot(test_client)
        abandoned = launch_once(
            AgentBoxClient(transport=abandon_response_transport(R01_ABANDON_AFTER))
        )
        # Give the service time to finish what it was already doing.
        time.sleep(RELEASE_POLL)
        midway = snapshot(test_client)
        orphaned = created_since(before, midway, session_agent.id)
        retried = launch_once(test_client)
        after = snapshot(test_client)
        buckets = created_since(before, after, session_agent.id)
        legs["abandoned, then retried"] = {
            "the connection was closed after_s": R01_ABANDON_AFTER,
            "a healthy launch took_s": healthy,
            "the abandoned call": abandoned,
            "created by the abandoned call": orphaned,
            "the retry": retried,
            "sandboxes created in total": buckets,
            "count": len(ours(buckets)),
            "scenario reproduced": bool(ours(orphaned)) and not abandoned["ok"],
        }
        show(
            "a launch abandoned mid-flight, then retried",
            method=f"sandboxes.launch(slug, ...) on a client that closes the connection after "
            f"{R01_ABANDON_AFTER}s without reading the answer, then again on a healthy client",
            args={"display_name": display_name},
            returns=legs["abandoned, then retried"],
        )
        discard_ids(test_client, ours(buckets))
        wait_for_room(test_client, baseline=baseline, deadline=RELEASE_DEADLINE)

    with allure.step("5. one logical launch, one sandbox"):
        duplicated = {
            name: leg["count"] for name, leg in legs.items() if leg["count"] != 1
        }
        show(
            "how many sandboxes each way of sending one launch produced",
            method="(none) - collecting the three legs above",
            returns={
                "per leg": {name: leg["count"] for name, leg in legs.items()},
                "expected": 1,
                "not one": duplicated or "none",
            },
        )
        assert not any(found.values()), (
            f"an idempotency parameter exists after all: {found} — this row's Known gap is "
            f"stale and should be replaced by a real test that sends the key"
        )
        assert not duplicated, (
            f"one logical launch produced more than one sandbox: {duplicated} (expected 1 "
            f"each). launch() takes no idempotency key, request id or client token, and "
            f"display_name does not deduplicate, so a CI job that retries after a dropped "
            f"connection pays for both — and the abandoned one is not discoverable from "
            f"anything the caller kept"
        )


# --------------------------------------------------------------------------
# R-02 twenty at once, with nothing crossed between them
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("R-02 concurrent launches keep their identities apart")
@allure.severity(P1)
def test_r02_concurrent_launches_do_not_cross_identities(
    test_client, session_agent, tracker, instance_type
):
    baseline = holding_quota(test_client) or 0
    identities: Dict[str, str] = {}
    rounds: List[Dict[str, Any]] = []
    unstable: List[str] = []
    unreadable: List[str] = []

    while len(identities) < R02_TOTAL and len(rounds) < R02_MAX_ROUNDS:
        want = R02_TOTAL - len(identities)
        before_round = snapshot(test_client)
        outcomes = launch_concurrently(session_agent, instance_type, want, tracker)
        accepted = [outcome for outcome in outcomes if outcome["ok"]]
        refused = [reportable(outcome) for outcome in outcomes if not outcome["ok"]]
        live: List[Sandbox] = []
        settled: Dict[str, Any] = {}

        try:
            for outcome in accepted:
                sandbox = outcome["sandbox"]
                result = settle(sandbox, deadline=RUN_TIMEOUT)
                settled[sandbox.id] = result
                if result["reached"] == RUNNING_STATUS:
                    live.append(sandbox)

            first_read = read_hostnames(live)
            second_read = read_hostnames(live)
            for sandbox in live:
                one = first_read.get(sandbox.id, {})
                two = second_read.get(sandbox.id, {})
                first_name, second_name = one.get("hostname"), two.get("hostname")
                if first_name and first_name == second_name:
                    identities[sandbox.id] = first_name
                elif first_name is None and second_name is None:
                    # Neither read landed. That is a sandbox this row could not
                    # ask, not a sandbox that answered inconsistently, and
                    # filing it under `unstable` would report a crossed
                    # identity that was never observed.
                    unreadable.append(
                        f"{sandbox.id}: {one.get('error')} / {two.get('error')}"
                    )
                else:
                    unstable.append(
                        f"{sandbox.id} answered {first_name!r} then {second_name!r} to the "
                        f"same question"
                    )
        finally:
            removed = discard(live + [o["sandbox"] for o in accepted if o["sandbox"] not in live])
            # The refused launches never produced an id, so `removed` cannot
            # have covered them; they are found by difference or not at all.
            swept = sweep(test_client, before_round, session_agent.id)
            wait_for_room(test_client, baseline=baseline, deadline=RELEASE_DEADLINE)

        rounds.append(
            {
                "round": len(rounds) + 1,
                "fired at once": want,
                "accepted": len(accepted),
                "reached running": len(live),
                "refused on the call": refused,
                "what the accepted ones settled to": settled,
                "cleanup": removed,
                "task records left by the refused launches": swept,
            }
        )

    with allure.step("1. every launch, at the highest concurrency the quota allows"):
        show(
            f"launch x{R02_TOTAL} concurrently, in rounds",
            method="agent.launch(instance_type=...) from one thread each, then "
            "sandbox.execute('hostname')",
            args={
                "wanted at once": R02_TOTAL,
                "rounds allowed": R02_MAX_ROUNDS,
                "note": "round 1 fires all of them; later rounds fire whatever is still missing",
            },
            returns={
                "rounds": rounds,
                "distinct sandboxes checked": len(identities),
                "first round accepted": rounds[0]["accepted"] if rounds else None,
            },
        )

    with allure.step("2. no id and no hostname is shared between two sandboxes"):
        by_hostname: Dict[str, List[str]] = {}
        for sandbox_id, hostname in identities.items():
            by_hostname.setdefault(hostname, []).append(sandbox_id)
        shared = {name: ids for name, ids in by_hostname.items() if len(ids) > 1}
        show(
            "the identity each sandbox reported",
            method="(none) - collecting the hostname reads above",
            returns={
                "id -> hostname": identities,
                "hostnames claimed by more than one sandbox": shared or "none",
                "sandboxes that changed their answer": unstable or "none",
                "sandboxes that could not be asked at all": unreadable or "none",
            },
        )
        assert not shared, (
            f"two sandboxes reported the same hostname: {shared} — a concurrent launch handed "
            f"back an id that does not belong to the machine behind it"
        )
        assert not unstable, (
            f"a sandbox gave two different answers to the same question while others were "
            f"being asked: {unstable}"
        )

    with allure.step("3. every one of them ran and answered for itself"):
        show(
            "what the account would admit",
            method="(none) - summarising the rounds",
            returns={
                "checked": len(identities),
                "wanted": R02_TOTAL,
                "rounds used": len(rounds),
                "largest number running at one time": max(
                    (item["reached running"] for item in rounds), default=0
                ),
                "admitted at once on the first round": rounds[0]["accepted"] if rounds else 0,
                "simultaneity this row could not reach": (
                    f"the row asks for {R02_TOTAL} at once and the quota admitted "
                    f"{rounds[0]['accepted']}, so the identities below are distinct but were "
                    f"reached over {len(rounds)} rounds"
                    if rounds and rounds[0]["accepted"] < R02_TOTAL
                    else "none — the first round took them all"
                ),
            },
        )
        assert len(identities) == R02_TOTAL, (
            f"only {len(identities)} of {R02_TOTAL} sandboxes reached running and answered for "
            f"themselves within {len(rounds)} rounds"
            + (
                f"; {len(unreadable)} could not be asked at all: {unreadable}"
                if unreadable
                else ""
            )
        )


# --------------------------------------------------------------------------
# R-03 a full quota, and what frees it
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("R-03 at full quota the refusal says quota, and a freed slot is reusable")
@allure.severity(P1)
def test_r03_full_quota_refuses_clearly_and_a_freed_slot_is_reusable(
    test_client, session_agent, tracker, instance_type
):
    baseline = holding_quota(test_client) or 0
    before_row = snapshot(test_client)
    filled: List[Sandbox] = []
    refusal: Dict[str, Any] = {}

    try:
        with allure.step("1. launch until the quota refuses"):
            ladder: List[Dict[str, Any]] = []
            over_quota: Optional[Sandbox] = None
            while len(ladder) < R03_MAX_FILL:
                started = time.monotonic()
                try:
                    sandbox = session_agent.launch(instance_type=instance_type)
                except Exception as exc:  # noqa: BLE001
                    refusal = {
                        "where": "on the launch call",
                        "raised": type(exc).__name__,
                        "status_code": getattr(exc, "status_code", None),
                        "message": str(exc)[:400],
                        "elapsed_s": round(time.monotonic() - started, 2),
                    }
                    ladder.append({"attempt": len(ladder) + 1, "accepted": False, **refusal})
                    break
                tracker.track_sandbox(sandbox)
                result = settle(sandbox, deadline=RUN_TIMEOUT)
                ladder.append(
                    {
                        "attempt": len(ladder) + 1,
                        "accepted": True,
                        "id": sandbox.id,
                        "settled": result,
                    }
                )
                if result["reached"] == RUNNING_STATUS:
                    filled.append(sandbox)
                    continue
                # Accepted, then rejected: this is the refusal, arriving late.
                over_quota = sandbox
                refusal = {
                    "where": "on the task, after the call had already succeeded",
                    "status": result["reached"],
                    "last_error": result.get("last_error"),
                    "seconds after launch": result["seconds"],
                }
                break

            show(
                "launching until the account says no",
                method="agent.launch(instance_type=...) one at a time, each waited to a verdict",
                args={"ceiling on attempts": R03_MAX_FILL},
                returns={
                    "ladder": ladder,
                    "reached running": len(filled),
                    "the refusal": refusal or "none — the ceiling was never hit",
                },
            )
            if over_quota is not None:
                discard([over_quota])

        with allure.step("2. the refusal is about quota, not about capacity"):
            reason = refusal.get("message") or refusal.get("last_error")
            show(
                "what the refusal said",
                method="(none) - reading the refusal above",
                args={"words that would make it a quota answer": list(QUOTA_WORDS)},
                returns={
                    "refusal": refusal or "none",
                    "reason": reason,
                    "names quota": names_quota(reason),
                },
            )
            assert refusal, (
                f"{R03_MAX_FILL} launches were all accepted and reached running, so this "
                f"account's quota was never reached and the row could not see a refusal at all"
            )
            assert names_quota(reason), (
                f"the refusal reads {reason!r}, which names no quota — a caller told this "
                f"waits and retries, where one told 'quota exceeded' knows to delete something "
                f"instead"
            )

            # The prose says quota; the status code is what a client actually
            # branches on, and the two disagree here.
            code = refusal.get("status_code")
            if refusal.get("where", "").startswith("on the launch call"):
                assert code == 429, (
                    f"the quota refusal arrives as HTTP {code} ({refusal.get('raised')}) while "
                    f"the body it carries is a verbatim 429 with code Resource.QuotaExceeded. "
                    f"A client branching on the status code — `except RateLimitError`, or a "
                    f"retry policy keyed on 429 — never sees the 429 and has to parse the "
                    f"message text to learn that waiting will not help"
                )

            # Measured while this row was being built: twenty launches against
            # a quota of eight left twelve task records that no caller could
            # delete, because the call that made them raised instead of
            # returning an id.
            leftover = created_since(before_row, snapshot(test_client), session_agent.id)
            show(
                "what a refused launch leaves behind",
                method="(none) - listing the account and diffing against the start of the row",
                returns={
                    "sandboxes this row is holding an id for": [item.id for item in filled],
                    "records found by difference": ours(leftover),
                    "why this matters": (
                        "a launch refused on the call still creates a task record, and the "
                        "SDK raised rather than returning its id — so the only way to find "
                        "one is to list the whole account and subtract what you know about"
                    ),
                },
            )
            unheld = [
                sandbox_id for sandbox_id in ours(leftover)
                if sandbox_id not in {item.id for item in filled}
            ]
            assert not unheld, (
                f"a launch the service refused still left {len(unheld)} task record(s) behind, "
                f"and the refusing call raised instead of returning an id, so nothing the "
                f"caller kept can delete them: {unheld}. They are inert and do not bill, but a "
                f"CI job retrying into a full quota accumulates one per attempt and the only "
                f"way to clean up is to list the whole account and delete by difference"
            )

        with allure.step("3. deleting one frees a slot that can be launched into"):
            assert filled, "nothing reached running, so there is no slot to free"
            victim = filled.pop()
            show(
                "delete one of the sandboxes holding the quota",
                method="sandbox.delete()",
                args={"sandbox_id": victim.id},
                returns=discard([victim]),
            )
            room = wait_for_room(
                test_client, baseline=baseline + len(filled), deadline=RELEASE_DEADLINE
            )
            started = time.monotonic()
            replacement = session_agent.launch(instance_type=instance_type)
            tracker.track_sandbox(replacement)
            filled.append(replacement)
            result = settle(replacement, deadline=RUN_TIMEOUT)
            show(
                "launch into the slot the delete freed",
                method="agent.launch(instance_type=...) after the delete",
                args={"waited for room": room},
                returns={
                    "id": replacement.id,
                    "settled": result,
                    "seconds from delete to running": round(time.monotonic() - started, 2),
                },
            )
            assert result["reached"] == RUNNING_STATUS, (
                f"the slot freed by a delete could not be launched into: the replacement "
                f"{result['reached']!r} ({result.get('last_error')})"
            )

        with allure.step("4. the other two ways a slot comes free"):
            # Recorded, not asserted: neither can be produced through the SDK,
            # and "cannot be tested" reads like "does not work" unless the row
            # says which it is.
            show(
                "why failure and expiry are not covered here",
                method="(none) - no SDK call produces either state on demand",
                returns={
                    "failure": "a sandbox cannot be made to fail on demand — B-0 T-04b "
                    "measured that a start command exiting at once leaves it reporting "
                    "'creating' for at least 300s rather than reaching a terminal failure, so "
                    "whether a failed sandbox releases its slot is untested",
                    "expiry": "launch() has no lifetime or ttl parameter, and the expiry is a "
                    "rolling 24h idle window (measured 2026-09-23: every execute, upload and "
                    "download pushes it to now + 24h), so a sandbox in use never reaches it "
                    "and a test would have to idle for a day",
                    "covered here": "only the delete path above",
                },
            )
    finally:
        removed = discard(filled)
        swept = sweep(test_client, before_row, session_agent.id)
        wait_for_room(test_client, baseline=baseline, deadline=RELEASE_DEADLINE)
        show(
            "cleanup: everything this row was holding",
            method="sandbox.delete() for each, then a sweep by difference",
            returns={
                "sandboxes this row held an id for": removed,
                "task records it had no id for": swept,
            },
        )
