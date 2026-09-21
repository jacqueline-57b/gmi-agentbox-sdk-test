"""Delete anything a killed run left behind.

`ResourceTracker` cleans up after itself, but it only runs if the process
lives long enough to reach teardown. A cancelled CI job, a timeout or a hard
kill skips it, and a sandbox bills until someone deletes it.

Scope is deliberately narrow: only agents whose slug starts with one of the
test prefixes, and only the sandboxes belonging to those agents. Nothing else
in the account is touched, because this may run against an account people
share.

    python scripts/cleanup_leftovers.py --dry-run
    python scripts/cleanup_leftovers.py
    python scripts/cleanup_leftovers.py --prefix sdk-test- --prefix my-branch-
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PREFIXES = ("sdk-test-", "sdk-probe-")
# A delete is refused with 409 while the agent's image is still building or a
# sandbox has not finished starting; both clear on their own once they reach a
# terminal state.
RETRY_FOR = float(os.getenv("GMI_TEST_CLEANUP_RETRY", "180"))
RETRY_EVERY = 5.0
# Listing is paginated and the default page is small; a run that died early can
# strand more than one page of agents.
PAGE_SIZE = 100


def load_dotenv() -> None:
    """Mirror tests/conftest.py: real environment variables win over the file."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def list_all(list_page, **filters) -> List:
    """Every item the filter matches, not just the first page."""
    items: List = []
    page = 1
    while True:
        result = list_page(page=page, page_size=PAGE_SIZE, **filters)
        items.extend(result.items)
        # `not result.items` guards against a server whose `total` disagrees
        # with what it actually returns, which would otherwise loop forever.
        if not result.items or len(items) >= result.total:
            return items
        page += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        action="append",
        default=None,
        help=f"slug prefix to clean (repeatable; default: {', '.join(DEFAULT_PREFIXES)})",
    )
    parser.add_argument("--dry-run", action="store_true", help="list what would go, delete nothing")
    args = parser.parse_args()
    prefixes = tuple(args.prefix) if args.prefix else DEFAULT_PREFIXES

    load_dotenv()
    if not os.getenv("GMI_AGENTBOX_API_KEY"):
        print("GMI_AGENTBOX_API_KEY is not set — nothing to do")
        return 0

    from agentbox_sdk import AgentBoxClient, APIError

    client = AgentBoxClient()
    failures: List[str] = []

    agents = [
        agent for agent in list_all(client.agents.list) if agent.slug.startswith(prefixes)
    ]
    if not agents:
        print(f"no agents matching {prefixes} — account is clean")
        return 0

    print(f"{len(agents)} agent(s) matching {prefixes}:")

    def drain(targets: List, describe) -> None:
        """Delete each target, retrying the ones that come back 409.

        A 409 means "not yet", not "no": an agent whose image is still
        building, or a sandbox that has not finished starting. Both clear on
        their own, so retry until the deadline rather than reporting a leak
        that isn't one. Each phase gets its own budget — sandboxes running
        long must not eat the agents' retries.
        """
        deadline = time.monotonic() + RETRY_FOR
        pending = list(targets)
        while pending:
            still_busy = []
            for target in pending:
                try:
                    target.delete()
                    print(f"  deleted {describe(target)}")
                except APIError as exc:
                    if exc.status_code == 404:
                        continue
                    if exc.status_code == 409 and time.monotonic() < deadline:
                        still_busy.append(target)
                        continue
                    failures.append(f"{describe(target)}: {exc.status_code} {exc.message}")
            pending = still_busy
            if not pending:
                return
            if time.monotonic() >= deadline:
                failures.extend(
                    f"{describe(target)}: still refused after {RETRY_FOR:.0f}s of retries"
                    for target in pending
                )
                return
            print(f"  {len(pending)} not ready yet, retrying in {RETRY_EVERY:.0f}s")
            time.sleep(RETRY_EVERY)

    owned: List = []
    for agent in agents:
        try:
            found = list_all(client.sandboxes.list, agent_id=agent.id)
        except APIError as exc:
            failures.append(f"list sandboxes of {agent.slug}: {exc}")
            continue
        owned.extend((agent, sandbox) for sandbox in found)

    if args.dry_run:
        for agent, sandbox in owned:
            print(f"  would delete sandbox {sandbox.id} ({sandbox.status}) of {agent.slug}")
        for agent in agents:
            print(f"  would delete agent {agent.slug}")
        return 0

    # Sandboxes first: they are the ones that cost money.
    owner = {sandbox.id: agent.slug for agent, sandbox in owned}
    drain(
        [sandbox for _, sandbox in owned],
        lambda sandbox: f"sandbox {sandbox.id} of {owner[sandbox.id]}",
    )
    drain(agents, lambda agent: f"agent {agent.slug}")

    if failures:
        print("\nCOULD NOT DELETE — these need a hand:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("\nall clear")
    return 0


if __name__ == "__main__":
    sys.exit(main())
