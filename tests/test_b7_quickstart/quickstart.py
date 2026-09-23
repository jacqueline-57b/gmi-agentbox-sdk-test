import os
import sys
import time
from pathlib import Path

from agentbox_sdk import AgentBoxClient, SandboxFailed, SandboxWaitTimeout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.env import load_dotenv  # noqa: E402

load_dotenv()

api_key=os.environ["GMI_AGENTBOX_API_KEY"]
base_url=os.getenv("GMI_AGENTBOX_BASE_URL")  # unset -> SDK falls back to DEFAULT_BASE_URL


client = AgentBoxClient(api_key=api_key, base_url=base_url)

# Available values vary by organization. Use the catalogue to select an IDC
# and a SKU for the Sandbox runtime.
idcs = client.idcs.list(runtime="sandbox")
print("idcs ->", [i.idc_id for i in idcs])
# Choose an `idc_id` returned above. This is the production example value.
# idc_id = "us-central-iowa2"
idc_id = "sandbox-runloop-us"
products = client.products.list(idc_name=idc_id, runtime="sandbox")
print("products ->", [p.instance_type for p in products])
# Choose an `instance_type` returned above.
instance_type = "gmi.sandbox.x-small"

def wait_for_template(agent, timeout=600, poll_interval=3):
    deadline = time.monotonic() + timeout
    while True:
        agent.refresh()
        status = agent.template_build_status
        if status == "ready" or status is None:
            return
        if status == "error":
            raise RuntimeError(agent.template_build_error or "Sandbox image build failed")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Sandbox image is still {status!r} after {timeout}s")
        print("...")
        time.sleep(poll_interval)

agent = None
sandbox = None
try:
    agent = client.agents.create(
        title="agentbox-demo-1",  # choose a unique name
        image_url="docker.io/library/alpine:3.20",
        idc=idc_id,
        instance_type=instance_type,
        runtime="sandbox",
    )
    print("Building Sandbox image; a new image can take several minutes.")
    wait_for_template(agent)
    sandbox = agent.launch(instance_type=instance_type)
    sandbox.wait_until_running()  # default timeout 300s
    print(sandbox.status, sandbox.endpoint_url)
    execution = sandbox.execute("echo hello")
    print(execution.status, execution.exit_code, execution.data.get("stdout"))
except SandboxFailed as exc:
    print("failed", exc.status, exc.message)
except SandboxWaitTimeout:
    print("still not running", sandbox.status, sandbox.last_error)
finally:
    if sandbox is not None:
        sandbox.delete()
    if agent is not None:
        agent.delete()
