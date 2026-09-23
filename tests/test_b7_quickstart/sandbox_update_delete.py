import os
import sys
from pathlib import Path

from agentbox_sdk import AgentBoxClient, NotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.env import load_dotenv  # noqa: E402

load_dotenv()

api_key=os.environ["GMI_AGENTBOX_API_KEY"]
base_url=os.getenv("GMI_AGENTBOX_BASE_URL")  # unset -> SDK falls back to DEFAULT_BASE_URL

client = AgentBoxClient(api_key=api_key, base_url=base_url)

agent = client.agents.get("agentbox-demo")
# the Sandbox launch_a_sandbox.py left running
running = client.sandboxes.list(agent_id=agent.id, status="running")
if not running.items:
    raise SystemExit("no running Sandbox for agentbox-demo — run launch_a_sandbox.py first")

sandbox = client.sandboxes.get(running.items[0].id)
print(sandbox.id, sandbox.status, sandbox.display_name, sandbox.instance_type, sandbox.expires_at)

# page = client.sandboxes.list(agent_id=agent.id)
# print("sandboxes.list ->", page.total, [(s.id[:8], s.status) for s in page.items])

# # There is no counterpart to agent.update(): neither Sandbox nor client.sandboxes
# # exposes one. display_name, instance_type and idc_name are all fixed at launch,
# # so changing any of them means deleting this Sandbox and launching another.
# print("has update?", hasattr(sandbox, "update"), hasattr(client.sandboxes, "update"))

# # sandbox.delete()  # one-way; the sandbox cannot be restarted
# # print("deleted", sandbox.id)

# # What the record looks like once it is gone.
# try:
#     gone = client.sandboxes.get(sandbox.id)
#     print("after delete ->", gone.status, gone.last_error)
# except NotFoundError as exc:
#     print("after delete ->", exc.status_code, exc.message)
