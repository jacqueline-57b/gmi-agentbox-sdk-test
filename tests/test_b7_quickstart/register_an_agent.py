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
idc_id = "sandbox-runloop-us"
instance_type = "gmi.sandbox.x-small"

# agent = client.agents.create(
#     title="agentbox-demo",
#     image_url="docker.io/library/alpine:3.20",
#     idc=idc_id,
#     env=[{"name": "GMI_MODELS", "value": "llama", "secret": False}],
# )

# agent = client.agents.create(
#     title="agentbox-demo",  # choose a unique name
#     image_url="docker.io/library/alpine:3.20",
#     idc=idc_id,
#     instance_type=instance_type,
#     runtime="sandbox",
#     # start_cmd="sleep infinity",
# )

agent = client.agents.create(
    title="agentbox-demo-with-start-cmd-2",  # choose a unique name
    image_url="node:20-alpine",
    idc=idc_id,
    instance_type=instance_type,
    runtime="sandbox",
    start_cmd="sleep infinity",
)
print(agent.slug, agent.launchable, agent.title)
