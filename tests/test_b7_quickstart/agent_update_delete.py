import os
import sys
from pathlib import Path

from agentbox_sdk import AgentBoxClient
from agentbox_sdk import APIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.env import load_dotenv  # noqa: E402

load_dotenv()

api_key=os.environ["GMI_AGENTBOX_API_KEY"]
base_url=os.getenv("GMI_AGENTBOX_BASE_URL")  # unset -> SDK falls back to DEFAULT_BASE_URL

client = AgentBoxClient(api_key=api_key, base_url=base_url)

agent = client.agents.get("agentbox-demo")
print(agent.slug, agent.launchable, agent.title)

page = client.agents.list(page=1, page_size=20)
print("agents.list ->", page.total, [a.slug for a in page.items])
# agent.update(title="renamed")
# print(agent.slug, agent.launchable, agent.title)

try:
    agent.update(start_cmd="pwd")
except APIError as exc:
    print("refused:", exc.status_code, exc.message)

print(agent.slug, agent.launchable, agent.title)
print("start_cmd:", agent.data.get("start_cmd"))
# agent.delete()
