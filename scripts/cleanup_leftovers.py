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
# A delete is refused with 409 while the agent's image is still building, and
# that clears on its own once the build reaches a terminal state.
RETRY_FOR = float(os.getenv("GMI_TEST_CLEANUP_RETRY", "180"))
RETRY_EVERY = 5.0


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
    agents = [
        agent
        for agent in client.agents.list(page=1, page_size=100).items
        if agent.slug.startswith(prefixes)
    ]
    if not agents:
        print(f"no agents matching {prefixes} — account is clean")
        return 0

    print(f"{len(agents)} agent(s) matching {prefixes}:")
    failures: List[str] = []

    # Sandboxes first: they are the ones that cost money.
    for agent in agents:
        try:
            tasks = client.sandboxes.list(agent_id=agent.id, page_size=100).items
        except APIError as exc:
            failures.append(f"list sandboxes of {agent.slug}: {exc}")
            continue
        for sandbox in tasks:
            if args.dry_run:
                print(f"  would delete sandbox {sandbox.id} ({sandbox.status}) of {agent.slug}")
                continue
            try:
                sandbox.delete()
                print(f"  deleted sandbox {sandbox.id} of {agent.slug}")
            except APIError as exc:
                if exc.status_code != 404:
                    failures.append(f"sandbox {sandbox.id}: {exc}")

    if args.dry_run:
        for agent in agents:
            print(f"  would delete agent {agent.slug}")
        return 0

    pending = list(agents)
    deadline = time.monotonic() + RETRY_FOR
    while pending:
        still_busy = []
        for agent in pending:
            try:
                agent.delete()
                print(f"  deleted agent {agent.slug}")
            except APIError as exc:
                if exc.status_code == 404:
                    continue
                if exc.status_code == 409 and time.monotonic() < deadline:
                    still_busy.append(agent)  # most likely mid-build
                    continue
                failures.append(f"agent {agent.slug}: {exc.status_code} {exc.message}")
        pending = still_busy
        if pending:
            if time.monotonic() >= deadline:
                failures.extend(
                    f"agent {agent.slug}: still refused after {RETRY_FOR:.0f}s of retries"
                    for agent in pending
                )
                break
            print(f"  {len(pending)} still building, retrying in {RETRY_EVERY:.0f}s")
            time.sleep(RETRY_EVERY)

    if failures:
        print("\nCOULD NOT DELETE — these need a hand:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("\nall clear")
    return 0


if __name__ == "__main__":
    sys.exit(main())
