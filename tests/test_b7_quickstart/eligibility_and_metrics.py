import os
import sys
import time
from pathlib import Path

from agentbox_sdk import AgentBoxClient, AgentBoxSDKError

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
sandbox = running.items[0]

# the page uses `start`/`end` without ever defining them; metrics wants epoch seconds
end = int(time.time())
start = end - 3600

entitlement = client.eligibility()
print(entitlement.eligible, entitlement.data_centers)

if sandbox.capabilities.get("metrics"):
    batch = sandbox.metrics(start=start, end=end, kinds=["cpu", "memory"], step=60)
    series = sandbox.metrics_timeseries(kind="cpu", start=start, end=end)
    print(series.empty_reason, series.points)

# `metrics` is False on this runtime, so the block above never runs. Called
# anyway, the two endpoints disagree about how to say "no data": metrics()
# answers 200 and names the reason per kind, metrics_timeseries() raises.
# Omitting `kinds` returns every kind the service charts.
batch = sandbox.metrics(start=start, end=end)
print("kinds:", [(s.kind, s.unit, s.empty_reason, len(s.points)) for s in batch.results])
try:
    series = sandbox.metrics_timeseries(kind="cpu", start=start, end=end)
    print("timeseries:", series.empty_reason, series.points)
except AgentBoxSDKError as exc:
    print("timeseries:", type(exc).__name__, exc)
