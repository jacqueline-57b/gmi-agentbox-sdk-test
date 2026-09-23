import os
import sys
from pathlib import Path

from agentbox_sdk import AgentBoxClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.env import load_dotenv  # noqa: E402

load_dotenv()

api_key=os.environ["GMI_AGENTBOX_API_KEY"]
base_url=os.getenv("GMI_AGENTBOX_BASE_URL")  # unset -> SDK falls back to DEFAULT_BASE_URL

client = AgentBoxClient(api_key=api_key, base_url=base_url)
instance_type = "gmi.sandbox.x-small"

agent = client.agents.get("agentbox-demo")

# sandbox = agent.launch(instance_type=instance_type)
# # or: client.sandboxes.launch(agent.slug, instance_type=..., idc_name=agent.idc)

# sandbox.wait_until_running(timeout=300, poll_interval=2.0)
# sandbox.refresh()
# print(sandbox.status, sandbox.last_error, sandbox.endpoint_url)
# print(sandbox.expires_at)

sandbox_list = client.sandboxes.list(status="terminated");

print("list()                     ", sandbox_list.total, [(s.id[:8], s.status) for s in sandbox_list.items])
