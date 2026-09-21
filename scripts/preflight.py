"""Fail fast when the API this run targets cannot be reached.

An unreachable base URL does not look like a network problem from the test
output: every case fails in fixture setup with the same urllib traceback, and
the cleanup step dies the same way half a minute later. Nothing in either says
which host, or that the host is the problem. One cheap call up front does.

    python scripts/preflight.py
"""

from __future__ import annotations

import os
import sys

from cleanup_leftovers import load_dotenv

# Deliberately shorter than the SDK's 30s default: this is a reachability
# probe, not a request worth waiting out.
PROBE_TIMEOUT = float(os.getenv("GMI_TEST_PREFLIGHT_TIMEOUT", "15"))


def main() -> int:
    load_dotenv()
    if not os.getenv("GMI_AGENTBOX_API_KEY"):
        # The workflow refuses to start without a key, and a developer running
        # this by hand gets the same message from the suite itself.
        print("GMI_AGENTBOX_API_KEY is not set — nothing to probe")
        return 0

    from agentbox_sdk import AgentBoxClient, APIError, TransportError

    client = AgentBoxClient(timeout=PROBE_TIMEOUT)
    print(f"probing {client.base_url} ...")
    try:
        # The cheapest authenticated call there is. It proves two things at
        # once: the host answers, and the key is accepted.
        client.agents.list(page=1, page_size=1)
    except TransportError as exc:
        print(f"::error::cannot reach {client.base_url} — {exc}")
        print(f"No answer within {PROBE_TIMEOUT:.0f}s. A host that resolves but never")
        print("replies is what a firewall dropping packets looks like; a host that")
        print("refuses outright fails instantly instead.")
        print("Either run this on a runner inside the network that can reach it,")
        print("point GMI_AGENTBOX_BASE_URL at a publicly reachable environment, or")
        print("have that environment admit the runner's egress addresses.")
        return 1
    except APIError as exc:
        if exc.status_code in (401, 403):
            print(f"::error::{client.base_url} rejected the API key: {exc.status_code} {exc.message}")
            print("The key is set but not accepted — check the secret for this environment.")
            return 1
        # It answered, which is all this step set out to establish. Whatever
        # the status means is the suite's problem to report, not ours.
        print(f"reachable — the API answered {exc.status_code}")
        return 0

    print("reachable, and the key is accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
