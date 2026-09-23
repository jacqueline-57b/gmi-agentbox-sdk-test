import json
import os
import sys
import time
from pathlib import Path

from agentbox_sdk import AgentBoxClient

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

# Finished command
result = sandbox.execute("echo hello")
print(result.status, result.exit_code, result.data.get("stdout"))

result = sandbox.execute("pwd")
print(result.status, result.exit_code, result.data.get("stdout"))


result = sandbox.execute("pip install gmi-agentbox-sdk")
print(result.status, result.exit_code, result.data.get("stdout"))


# # Start a long-running command, then cancel it.
# execution = sandbox.execute("sleep 120", wait=False)
# if execution.accepted:
#     sandbox.cancel_execution(execution.id)

# File round trip
sandbox.upload_file(path="/home/user/test/input.txt", file=b"hello\n")
download = sandbox.download_file(path="/home/user/input.txt")
print(download.filename, download.content)

# An interactive shell. The socket speaks JSON frames in BOTH directions:
# input must be {"type": "stdin", "data": "...\n"} — raw text is dropped with
# no error and no output — and every frame that comes back is
# {"type": "stdout", "data": "..."}, a character at a time, with the terminal's
# own echo and ANSI escapes mixed in. There is no end-of-command marker, so a
# reader stops when the socket goes quiet.
socket = sandbox.shell(timeout=10)


def send(command: str) -> None:
    socket.send(json.dumps({"type": "stdin", "data": command + "\n"}))


def read(seconds: float = 3.0) -> str:
    chunks, stop = [], time.monotonic() + seconds
    while time.monotonic() < stop:
        try:
            frame = socket.recv()
        except Exception as exc:  # the websocket read timeout is how it goes quiet
            if "timed out" in str(exc).lower():
                break
            raise
        if not frame:
            break
        event = json.loads(frame)
        if event.get("type") == "stdout":
            chunks.append(event.get("data") or "")
    return "".join(chunks)


try:
    print(read(2))  # the connect frame and the first prompt
    send("echo hello world")
    print(read())

    # Reading an application's own log file — the shell is a normal interactive
    # terminal, so it sees what is on disk, not the output of earlier execute()
    # calls and not any container-wide stream (this runtime has none).
    send("tail -n 50 /tmp/app.log")
    print(read())

    # Following it live: keep reading, each call returns whatever arrived.
    # send("tail -f /tmp/app.log")
    # while True:
    #     print(read(5), end="")
finally:
    socket.close()

# page = client.sandboxes.list()  # omits stopped and deleted
# print("list()                     ", page.total, [(s.id[:8], s.status) for s in page.items])
# page = client.sandboxes.list(status=["running", "creating", "error"])
# print("list(status=[running,...]) ", page.total, [(s.id[:8], s.status) for s in page.items])
# page = client.sandboxes.list(agent_id=agent.id, status="running")
# print("list(agent_id=, running)   ", page.total, [(s.id[:8], s.status) for s in page.items])


# if sandbox.capabilities.get("logs"):
#     print(sandbox.logs())

# if sandbox.capabilities.get("logs_stream"):
#     for event in sandbox.stream():
#         print(event.event, event.data)
#         if event.event == "status" and isinstance(event.data, dict):
#             break

# # `logs` is False on this runtime, so `logs()` raises not_supported_by_runtime.
# # What does carry output here is execute(): write a line, then read it back.
# sandbox.execute("sh -c 'echo hello-from-the-app >> /tmp/app.log'")
# tail = sandbox.execute("tail -n 5 /tmp/app.log")
# print(tail.status, tail.exit_code, repr(tail.data.get("stdout")), repr(tail.data.get("stderr")))
# print("truncated?", tail.data.get("stdout_truncated"), tail.data.get("stderr_truncated"))
