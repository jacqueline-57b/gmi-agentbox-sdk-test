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

from agentbox_sdk import (
    AuthenticationError,
    SandboxFailed,
    SandboxWaitTimeout,
    NotFoundError,
    UnprocessableError,
)

try:
    client.agents.get("missing")
except NotFoundError as exc:
    print(exc.status_code, exc.message)
