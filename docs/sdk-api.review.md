# gmi-agentbox-sdk API reference (reviewed)

Reviewed against the actual `gmi-agentbox-sdk` `0.1.0b2` source (the local
`.venv` install is byte-for-byte identical to the PyPI `0.1.0b2` sdist) **and**
against the upstream `docs/reference.md` shipped in that sdist.

This file corrects and extends `sdk-api.md`. Every change vs. that file is
listed in [Review notes](#review-notes) at the bottom. Everything above that
section is derived from the source; behaviour that only shows up against a
running backend is kept separate, in
[Observed against the live API](#observed-against-the-live-api).

| | |
|---|---|
| Version | `0.1.0b2` |
| Import | `agentbox_sdk` |
| Default base URL | `https://console.gmicloud.ai` |
| Default wait timeout | `300.0`s |
| Service prefix | `/api/v1/ie/container` |
| Requires Python | `>=3.9` |
| Dependencies | `certifi>=2024.0.0`, `websocket-client>=1.8` |

---

## Client

```python
from agentbox_sdk import AgentBoxClient

client = AgentBoxClient()          # reads GMI_AGENTBOX_API_KEY / GMI_AGENTBOX_BASE_URL
```

```python
AgentBoxClient(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    *,
    timeout: float = 30.0,
    transport: Optional[Transport] = None,
    stream_transport: Optional[StreamTransport] = None,
    shell_transport: Optional[ShellTransport] = None,
    sleep: Optional[Sleep] = None,
)
```

`api_key` falls back to `$GMI_AGENTBOX_API_KEY` (raises `ValueError` if still
unset), `base_url` to `$GMI_AGENTBOX_BASE_URL` and then to `DEFAULT_BASE_URL`.
The four `transport`/`sleep` parameters are injection seams for offline
testing / traffic logging.

### Methods on the client itself

| Method | Returns | Notes |
|---|---|---|
| `eligibility()` | `Eligibility` | `GET /eligibility` |
| `health()` | `Mapping[str, Any]` | local-only; does **not** call the network |

`health()` returns `{"status": "ok", "baseUrl": ..., "authenticated": bool}`.
Note: in `0.1.0b2`, `eligibility()` ends with an unreachable `return {...}`
block (dead code copied from `health()`); it is harmless but present.

### Namespaces

| Attribute | Class |
|---|---|
| `client.agents` | `AgentCollection` |
| `client.sandboxes` | `SandboxCollection` |
| `client.idcs` | `IdcCollection` |
| `client.products` | `ProductCollection` |

---

## client.agents

```python
create(
    *, title: str,
    image_url: Optional[str] = None,
    idc: Optional[str] = None,
    env: Optional[Sequence[Mapping[str, Any]]] = None,
    ports: Optional[Sequence[Mapping[str, Any]]] = None,
    deployment_type: Optional[str] = None,
    instance_type: Optional[str] = None,
    assign_public_ip: Optional[bool] = None,
    image_credential: Optional[Mapping[str, Any]] = None,
    short_desc: Optional[str] = None,
    share_storage_mount_path: Optional[str] = None,
    clone_source_id: Optional[str] = None,
    runtime: Optional[str] = None,
    start_cmd: Optional[str] = None,
) -> Agent

get(slug: str) -> Agent
list(*, page: int = 1, page_size: int = 20) -> Page
update(slug: str, **fields: Any) -> Agent
delete(slug: str) -> None
```

`title` is the only required Python-level argument; every other `None` is
omitted from the request body. Two behaviours worth knowing:

- **Auto `deployment_type`:** when `runtime == "sandbox"` and
  `deployment_type is None`, the SDK sends `deployment_type="gmi-ce"`.
- **Backend requirements:** with `runtime="sandbox"` the API itself requires
  `idc` and `instance_type`; the SDK cannot enforce this and only fails at
  the server if you omit them.

---

## client.sandboxes

### Read

```python
get(sandbox_id: str) -> Sandbox

list(
    *, agent_id: Optional[str] = None,
    status: Optional[Union[str, Sequence[str]]] = None,
    page: int = 1,
    page_size: int = 20,
) -> Page

logs(sandbox_id: str) -> str

metrics(
    sandbox_id: str, *, start: int, end: int,
    kinds: Optional[Union[str, Sequence[str]]] = None,
    step: Optional[int] = None,
) -> MetricsBatch

metrics_timeseries(
    sandbox_id: str, *, kind: str, start: int, end: int,
    step: Optional[int] = None,
) -> MetricSeries
```

A sequence passed to `status` is joined into one comma-separated query value
(`status=running,error`), not repeated parameters. Stopped and deleted
sandboxes are excluded by default.

`metrics`: `start`/`end` are unix seconds; `kinds` is a string or sequence
(`"cpu,memory"`). Omitting `kinds` requests the API default of all 8 charts:
`gpu_util`, `gpu_mem`, `cpu`, `memory`, `disk_read`, `disk_write`, `net_rx`,
`net_tx`. `step` defaults to 60s on the API (min 5, max 3600).

### Lifecycle

```python
launch(
    slug: str, *,
    instance_type: str,
    idc_name: Optional[str],                 # required keyword, may be None
    env: Optional[Sequence[Mapping[str, Any]]] = None,
    display_name: Optional[str] = None,
    assign_public_ip: Optional[bool] = None,
    ports: Optional[Sequence[Mapping[str, Any]]] = None,
    storages: Optional[Sequence[Mapping[str, Any]]] = None,
    template_id: Optional[str] = None,
) -> Sandbox

delete(sandbox_id: str) -> None
```

`idc_name` has no default: it must be passed, and raises `ValueError` if it is
falsy. `Agent.launch` (below) supplies the agent's own `idc` for you.

### Execution

```python
execute(
    sandbox_id: str, command: str, *,
    wait: bool = True,
    wait_timeout_seconds: Optional[int] = None,
) -> Execution

get_execution(sandbox_id: str, execution_id: str) -> Execution
cancel_execution(sandbox_id: str, execution_id: str) -> Execution
```

`wait_timeout_seconds` is sent only when `wait=True`. A completed command
returns `accepted=False`; an accepted asynchronous command (`HTTP 202`)
returns `accepted=True`.

### Files

```python
upload_file(
    sandbox_id: str, *, path: str,
    file: Union[str, os.PathLike, bytes, BinaryIO],
) -> list[Dict[str, Any]]

download_file(sandbox_id: str, *, path: str) -> FileDownload
```

Paths must be absolute and cannot contain `.` or `..` segments
(`ValueError` otherwise). `download_file` falls back to the basename of
`path` for `FileDownload.filename` when no `Content-Disposition` filename is
returned.

### Streaming

```python
stream(sandbox_id: str, *, timeout: Optional[float] = None) -> Iterator[StreamEvent]
shell(sandbox_id: str, *, timeout: Optional[float] = None) -> Any    # WebSocket
```

`stream` yields one `StreamEvent` per Server-Sent Event. Comment lines
(`: ping`) surface as `event="heartbeat"`; otherwise `event` defaults to
`"message"`. `timeout=None` means no socket idle timeout. 4xx/5xx raise the
same `APIError` types as other calls.

`shell` needs the `websocket-client` package (declared dependency).

---

## client.idcs / client.products

```python
client.idcs.list(*, runtime: Optional[str] = None) -> list[Idc]
client.products.list(*, idc_name: Optional[str] = None,
                        runtime: Optional[str] = None) -> list[Product]
```

| Class | Attributes |
|---|---|
| `Idc` | `.idc_id` `.name` |
| `Product` | `.instance_type` `.price` |

> **Careful:** `eligibility().data_centers` and `idcs.list(runtime="sandbox")`
> return different sets — on both production and staging the two have been
> observed to be entirely disjoint. Select a data center from `idcs.list`, not
> from `eligibility()`, or launches will fail.

---

## Agent

Returned by `agents.create/get/list`. The methods mirror the collection's,
minus the `slug` argument.

```python
launch(
    *, instance_type: str,
    idc_name: Optional[str] = None,
    env: Optional[Sequence[Mapping[str, Any]]] = None,
    display_name: Optional[str] = None,
    assign_public_ip: Optional[bool] = None,
    ports: Optional[Sequence[Mapping[str, Any]]] = None,
    storages: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Sandbox

sandboxes(*, page: int = 1, page_size: int = 20) -> Page
refresh() -> Agent
update(**fields: Any) -> Agent
delete() -> None
```

`Agent.launch` defaults `idc_name` to the agent's own `.idc`
(`idc_name or self.idc`) and has no `template_id` parameter; it passes the
agent's `id` as `template_id` internally. If neither `idc_name` nor
`agent.idc` is set, `SandboxCollection.launch` raises `ValueError`.

`SandboxCollection.launch`, by contrast, has **no default** for `idc_name` —
you must supply it.

| Attribute | Notes |
|---|---|
| `.id` `.slug` `.title` | |
| `.idc` `.runtime` `.image_url` `.revision` `.env` | |
| `.launchable` | False when the upstream template is missing |
| `.template_build_status` | `None` means the backend reports no build state, which counts as ready |
| `.template_build_error` | |
| `.generated_api_key` | Plaintext MaaS key, **present only on create** (and rare patch retries) |
| `.data` | Raw payload dict (present on `Agent`, `Sandbox`, `Idc`, `Product` too) |

---

## Sandbox

Returned by `sandboxes.get/list/launch` and `Agent.launch`.

```python
wait_until_running(*, timeout: float = 300.0, poll_interval: float = 2.0) -> Sandbox

execute(command: str, *, wait: bool = True,
        wait_timeout_seconds: Optional[int] = None) -> Execution
get_execution(execution_id: str) -> Execution
cancel_execution(execution_id: str) -> Execution

upload_file(*, path: str, file: Union[str, os.PathLike, bytes, BinaryIO]) -> list[Dict[str, Any]]
download_file(*, path: str) -> FileDownload

logs() -> str
metrics(*, start: int, end: int,
        kinds: Optional[Union[str, Sequence[str]]] = None,
        step: Optional[int] = None) -> MetricsBatch
metrics_timeseries(*, kind: str, start: int, end: int,
                   step: Optional[int] = None) -> MetricSeries

stream(*, timeout: Optional[float] = None) -> Iterator[StreamEvent]
shell(*, timeout: Optional[float] = None) -> Any

refresh() -> Sandbox
delete() -> None
```

`wait_until_running` exists only on the object, not on `SandboxCollection`.
On a terminal non-running status it raises `SandboxFailed(status, message)`
and on deadline it raises `SandboxWaitTimeout`.

| Attribute | Notes |
|---|---|
| `.id` `.status` | `.status` reads `task_status` then `status` |
| `.status_stale` | `bool` |
| `.agent_id` `.agent_slug` | |
| `.instance_type` `.idc_name` `.display_name` | |
| `.endpoint_url` | empty/`None` until running (and may still be `None` after) |
| `.expires_at` `.runtime` `.capabilities` | |
| `.last_error` | |
| `.data` | Raw payload dict |

---

## Data classes

| Class | Fields | Extra |
|---|---|---|
| `Page` | `items` `total` `page` `page_size` | |
| `Eligibility` | `eligible` `data_centers` `data` | `data_centers` reads `dataCenters` then `data_centers` |
| `Execution` | `data` `accepted` | properties `.id` `.status` `.exit_code` |
| `FileDownload` | `content` `filename` `headers` | `filename` may be `None` |
| `StreamEvent` | `event` `data` `raw` | one Server-Sent Event |
| `MetricSeries` | `kind` `unit` `empty_reason` `points` `data` | |
| `MetricsBatch` | `results` `data` | `results` is a list of `MetricSeries` |

`data` on these holds the raw payload, so a field the SDK does not model yet
is still reachable. The model objects (`Agent`, `Sandbox`, `Idc`, `Product`)
also expose `.data` the same way.

`Execution.id` reads `execution_id` then `id` from `data`; `exit_code` is
`int | None`. Command output such as `stdout` lives in `execution.data`.

---

## Exceptions

```
AgentBoxSDKError
├── APIError                    (.status_code, .message, .code, .details)
│   ├── BadRequestError         400
│   ├── AuthenticationError     401
│   ├── PermissionDeniedError   403
│   ├── NotFoundError           404
│   ├── ConflictError           409
│   ├── UnprocessableError      422
│   ├── RateLimitError          429
│   └── ServerError             >= 500
├── TransportError              no HTTP response — DNS, TLS, connection refused
├── SandboxFailed               (.status, .message)  terminal non-running status
└── SandboxWaitTimeout          wait_until_running deadline passed
```

Mapping happens in `errors.error_from_response`. Anything outside the table
and below 500 — a 402 or a 451, say — raises **plain `APIError`**, not a
subclass, so matching on exact types will miss it.

The message is read from the payload's `error`, then `message`, falling back to
`"Request failed"`; `code` and `details` are carried through when present
(`details` only if it is a dict). `APIError` carries `.status_code` and
`.message`; `SandboxFailed` carries `.status` (the sandbox status) and
`.message` (`last_error` when set).

Catch `APIError` for anything the server answered, `AgentBoxSDKError` for
everything the SDK can raise.

---

## Module constants

| Name | Value |
|---|---|
| `DEFAULT_BASE_URL` | `https://console.gmicloud.ai` |
| `DEFAULT_WAIT_TIMEOUT` | `300.0` |
| `__version__` | `0.1.0b2` (falls back to `"0.0.0"` if metadata missing) |

All requests go out under the `/api/v1/ie/container` prefix.

---

## Things the SDK does not have

- **No logging.** No `logging` import, no request hooks, no debug flag.
- **No async client.** Everything is synchronous.
- **No retry/backoff** beyond what `wait_until_running` does for polling.
- **No pagination iterator.** `list()` returns one `Page`; advance `page`
  yourself.

---

## Observed against the live API

Source reading cannot answer what the backend actually does. The items below
come from running the suite against `https://ce-tot.gmicloud-dev.com`
(`idc` `sandbox-runloop-us`, `gmi.sandbox.x-small`, `docker.io/library/alpine:3.20`)
on **2026-09-20** and **2026-09-23** with SDK `0.1.0b2`. Another deployment may
answer differently — re-check before relying on any of it.

### `launch()` has no idempotency and no "already running" signal

With one agent built and ready, three consecutive `agent.launch(instance_type=...)`
calls were **all accepted** and returned three distinct ids; `agent.sandboxes()`
then reported `total = 3`, every one of them `running` and billing:

```
1st -> e0db0a50-a21d-4e37-8d63-10bf8378dbfa   running
2nd -> 5da6876f-b49b-45a0-8469-ce7f97fef259   pending   (a new sandbox)
3rd -> 2865f3e0-7beb-4c19-8ff1-77fecced071f   pending   (a new sandbox)
```

The multiplicity looks deliberate: the `api_examples` block the backend returns
on create describes provisioning one container per user session and terminating
it when that session ends, so many tasks under one deployment is the intended
model.

The gap is not that a second sandbox comes up — it is that **nothing in the SDK
lets a caller notice one is already up.** `launch()` takes no idempotency key,
`Agent` caches no sandbox state, and the only way to check is a separate
`client.sandboxes.list(agent_id=..., status="running")` call, which races with
the launch anyway. A client that retries a launch after a network timeout pays
twice and gets no error either time.

Treat `launch()` as create-always: check first if you need at-most-one, and
make the caller — not the SDK — own that invariant.

### `generated_api_key` never appears in this environment

Both the published README and the property docstring say the key is *"plaintext
only on create (and rare patch retries)"*. In practice, across four agents
registered at different times that day, **`generated_api_key` was absent from
every response body** — the field is not in the JSON at all, which is not the
same as `null`:

| Read | `'generated_api_key' in body` | `Agent.generated_api_key` |
|---|---|---|
| `agents.create` (POST) | `false` | `None` |
| `agents.get` (GET) | `false` | `None` |

One of the four registrations reproduced the published sample exactly, including
`env=[{"name": "GMI_MODELS", "value": "llama", "secret": False}]`, in case
declaring a model is what mints a key. It made no difference.

`env[].GMI_MAAS_API_KEY` is a **different credential** and is easy to mistake for
this one. It is org-level, not per-agent: 16 characters, byte-identical across
all four agents (`sha256` prefix `bcb775b8`), returned by both create and get,
and **masked by the server** — six leading `*` followed by ten characters.

So `Agent.generated_api_key` is `None` in practice here, and any flow written to
capture a key at registration time has nothing to capture. The documented note is
not violated — zero occurrences does satisfy "only on create" — but it says
nothing about what makes the backend mint one. Worth asking upstream: (a) what
triggers generation, (b) whether it is the same credential as the `env` entry,
(c) what the "rare patch retries" are.

### `delete()` is refused with `sandbox_template_busy` — sometimes permanently

Two different situations return the same 409, and only one of them clears.

**Transient — the image is still building.** Deleting an agent immediately after
`create` is refused; polling until `template_build_status == "ready"` and
retrying the same call succeeds. Wait for a terminal build state, then delete.

**Permanent — after the agent's own sandbox was deleted.** The T-04b row
registers an agent with `start_cmd="exit 1"`, waits for the build to reach
`ready`, launches, deletes the sandbox, then deletes the agent. That last call
is refused and **stayed refused for 303s of polling at 5s intervals**. By then
the API's own view of the agent contradicts the error:

| | |
|---|---|
| `template_build_status` | `ready` |
| `launchable` | `true` |
| `client.sandboxes.list(agent_id=...).total` | `0` |
| `agent.sandboxes().total` | `0` |

Nothing is attached, yet the template reports busy, and the agent cannot be
removed from the account. A suite that creates agents therefore accumulates
undeletable ones: `ResourceTracker` prints `[CLEANUP FAILED — delete by hand]`
and moves on, and "by hand" does not work either.

Hypothesis worth confirming with the backend: the sandbox in this row never
reached `running` — it sat in `creating` until `wait_until_running` timed out
after 90s — so deleting a task that never fully materialised may leave the
template pinned.

A probe on **2026-09-23** narrows it. An agent registered the same way but with
no `start_cmd`, whose sandbox reached `running` and was then deleted, deleted
**on the first attempt** with no 409 at all. The permanent form of the refusal
has so far only been reproduced behind a sandbox that never ran; a sandbox that
ran and was deleted cleanly leaves its template free.

**The error carries no machine-readable code.** The full payload is:

```json
{
  "type": "ConflictError",
  "status_code": 409,
  "message": "sandbox_template_busy",
  "code": null,
  "details": {}
}
```

The token lives in `message`; `code` is `null` and `details` is empty. Retry
logic cannot branch on `exc.code` for this error — it has to string-match
`exc.message`, which is the one thing an API is free to reword.

### Listings ignore every query parameter they do not recognise

`sandboxes.list()` sends four parameters — `deployment_id` (spelled `agent_id`
in the SDK), `status`, `page` and `page_size` — so two of them are filters and
two are pagination. What the backend does with anything else is the part worth
knowing: **it ignores it and answers 200**.

Probed on 2026-09-23 against `GET /tasks` with exactly one task in the account,
passing a value that matches nothing:

| Query parameter | `total` |
|---|---|
| *(none)* | 1 |
| `deployment_id=<the agent>` | 1 |
| `deployment_id=<unknown uuid>` | **0** |
| `status=creating` (the task's own status) | 1 |
| `status=deleted` | **0** |
| `status=running,error` | **0** |
| `display_name` `name` `title` `instance_type` `idc_name` `runtime` `provider` `search` `q` `keyword` `metadata` `label` `tag` `id` `task_id` `container_id` `health` `org_id` `user_id` `created_after` `sort` `order_by` `bogus_param_xyz` | 1 each |

Passing the *real* value for `display_name`, `instance_type`, `idc_name`, `id`,
`task_id` or `runtime` returns 1 as well, so the parameter is not being read at
all rather than matching. None of the twenty-three was rejected — an invented
`bogus_param_xyz` is answered exactly like the rest.

`GET /deployments` behaves the same way: with two agents registered, `title`,
`name`, `search`, `q`, `keyword`, `slug` and `bogus_param_xyz` all returned
`total = 2`, whether the value matched an agent exactly or matched nothing.

Two consequences:

* **a sandbox cannot be looked up by name.** `display_name` is accepted at
  launch and reads back verbatim, but nothing filters on it — pull the pages
  and match client-side, advancing `page` yourself;
* **hand-rolling a request to reach a filter the SDK does not expose is worse
  than useless.** `GET /tasks?display_name=x` returns the full list with a 200,
  so the caller cannot tell a filter that ran from one that was dropped.

### `start_cmd` belongs to the template, and one that exits is invisible

`launch()` takes no start command, and the server's own `api_examples` (returned
inside the `agents.create` body) show the provision call carrying only
`idc_name`, `instance_type` and `template_id`. The start command is fixed by
`agents.create(start_cmd=...)`, and the T-05 row finds it cannot be changed
afterwards.

Two sandboxes launched on 2026-09-23 from the same image and the same call,
differing only in what their template was registered with:

| `start_cmd` at registration | Result |
|---|---|
| not set | `task_status: running`, `endpoint_url` assigned, ~2s |
| `"exit 1"` | still `creating` after 90s; `last_error: null`; `capabilities.logs: false` |

So the sandbox runtime needs no start command of its own — which is why a bare
`alpine:3.20`, whose entrypoint would exit at once under Docker, runs here. A
start command that exits takes the container with it, and that failure is
**unobservable through the SDK**: the status never leaves `creating`, no
`last_error` is ever set, and the logs capability is off, so a timeout is the
only signal the caller gets.

### `env` at launch is applied, and merged over the org's own entries

`launch(env=[...])` takes the same `{name, value, secret}` shape as `create`.
Sending `{"name": "C03_MARKER", "value": "b1-c03", "secret": False}` on
2026-09-23 produced a task body carrying:

```json
"env": [
  {"locked": true, "name": "GMI_MAAS_API_KEY",  "secret": true, "value": "<masked by the server>"},
  {"locked": true, "name": "GMI_MAAS_BASE_URL", "value": "https://api.gmi-serving.com"},
  {"name": "C03_MARKER", "overridden": true, "value": "b1-c03"}
]
```

and `execute("printenv C03_MARKER")` answered `b1-c03` with exit code 0, so the
variable reaches the process and not only the API's record of it.

* the launch env is **merged**, not substituted: the org's `GMI_MAAS_*` entries
  survive it and carry `locked: true`, while the launch entry is flagged
  `overridden: true`;
* the `secret` flag sent at launch is **not echoed back** (agent-level env does
  carry one), so whether `secret: true` is honoured at launch cannot be checked
  through the SDK;
* there is no metadata equivalent. `launch()` has no metadata argument, and
  none of the task body's 25 fields is one.

### A duplicate `create` is refused with the real code buried in `message`

Registering the same title twice is rejected rather than being answered with the
first agent. The SDK raises `UnprocessableError` (422), and the upstream 409 —
status, code and human message alike — arrives as a JSON document embedded in
the `message` string:

```json
{
  "raised": "UnprocessableError",
  "status_code": 422,
  "code": null,
  "details": {},
  "message": "create container template: business-cloud-api https://ce-tot.gmicloud-dev.com/api/v2/templates returned 409: {\"request_id\":\"req-…\",\"code\":\"Resource.AlreadyExists\",\"message\":\"Template name already exists: sdk-test-b0-t03-0923-1100-4f9f\",\"trace_id\":\"…\"}"
}
```

`exc.code` is `null` again, as with `sandbox_template_busy`. Branching on
"already exists" means finding the first `{` in `exc.message` and parsing what
follows — two error shapes deep, and both layers are free to reword themselves.

### A failed build reports no reason and still calls itself launchable

Registering `docker.io/library/gmi-sdk-test-no-such-image:0.0.0` is accepted,
and the build reaches a terminal failure in ~10s. What the agent says afterwards:

| Field | Value |
|---|---|
| `template_build_status` | `error` (`building` at 0.7s, `error` at 9.6s) |
| `template_build_error` | `null` |
| `launchable` | `true` |

Nothing anywhere in the SDK says *why* the image failed, and the agent keeps
advertising itself as launchable while holding a template that does not exist.
Deleting it works, and a subsequent `agents.get(slug)` 404s as it should.

### The sandbox runtime has no logs, and `capabilities` cannot be configured

`Sandbox.capabilities` is read-only — the SDK mentions it in exactly two places,
both the same property reading `data["capabilities"]`. Nothing in
`agents.create`, `agents.update` or `launch` writes it, and `eligibility()`
describes runtimes only as `{"container": {"available": true}, "sandbox":
{"available": true}}`, with no feature flags.

It describes the **runtime**, not the agent. Measured on 2026-09-23, the same
dict comes back for every sandbox on the `sandbox` runtime — from different
agents, with and without `env`, with and without `start_cmd`, and read three
different ways (the launch response, `sandboxes.get()`, and an item out of
`sandboxes.list()`):

```json
{"exec": true, "files": true, "shell": true, "expiry": true,
 "logs": false, "logs_stream": false, "metrics": false, "ports": false,
 "public_ip": false, "private_image": false, "shared_storage": false,
 "runtime": "sandbox"}
```

Called anyway, on a healthy running sandbox:

| Call | Answer |
|---|---|
| `sandbox.logs()` | `ServerError` **501 `not_supported_by_runtime`**, `code` `null`, `details` `{}` |
| `sandbox.stream()` | no error at all — the read **times out** (`TimeoutError`) |

The 501 is at least an answer; `stream()` is the worse of the two, because a
caller who does not guard on `capabilities.logs_stream` hangs rather than being
refused. And `code` is `null` again, so "is this runtime missing the feature?"
is only answerable by string-matching `message`.

The guard the quickstart writes —

```python
if sandbox.capabilities.get("logs"):
    print(sandbox.logs())
```

— is therefore dead code on this runtime: it prints nothing and raises nothing,
which reads like a call that succeeded and returned no logs.

**What carries output instead**: `execute()` returns `stdout`/`stderr`/`exit_code`
per command and `get_execution(execution_id)` replays them afterwards; an
application's own log has to be written to a file and read back with `tail` or
`download_file`; `shell` is `true`, so a WebSocket session works. None of these
is a combined log of everything the sandbox ran — that record does not exist
outside the Console.

**The only lever is the runtime.** This account has exactly two (anything else
answers `UnprocessableError: runtime_invalid`): `sandbox`, with one data center
(`sandbox-runloop-us`) and SKUs priced at 0 in this environment, and
`container`, with seven data centers and priced SKUs (`gmi.container.intel.*`,
47500–100000). Whether a `container` task reports `logs: true` is **untested** —
it is a different product path at a different price.

### Command output is capped at 1 MiB per stream, counted in bytes

`execute()` carries `stdout` and `stderr` in the response body, and the service
caps each one. Measured 2026-09-23 against a live sandbox:

| Asked for on stdout | `stdout` returned | `stdout_truncated` |
|---|---|---|
| 1 048 575 bytes | 1 048 576 chars | `false` |
| 1 048 576 bytes | 1 048 576 chars | `true` |
| 1 048 577 bytes | 1 048 576 chars | `true` |
| 20 MiB | 1 048 576 chars | `true` |

Four things that table hides:

**The cap is per stream, not shared.** 700 KiB written to each of stdout and
stderr in one command came back complete on both — 1.4 MiB in one response —
while 1 MiB+ to each came back cut to 1 048 576 on both, each with its own flag
set.

**It counts bytes, not characters.** 3 MiB of `世` (three bytes each) came back
as 349 525 characters, which is 1 048 575 bytes. Counted as characters that is
nowhere near any cap.

**A character straddling the boundary is dropped, not mangled.** 1 048 576 / 3
is 349 525.33, so the cut lands inside a character; the service discards the
partial one and returns one byte under the cap. Everything that comes back is
valid UTF-8 — no `U+FFFD`, no broken JSON.

**Non-empty output is normalised to end in exactly one `\n`, and that byte
counts against the cap.** `printf abc`, which prints no newline of its own,
answers `'abc\n'`; `printf 'abc\n'` answers `'abc\n'` rather than two
newlines; `printf ''` answers `''`. That is why asking for 1 048 575 bytes
returns 1 048 576 characters and is *not* flagged — the appended newline fills
the cap exactly.

Nothing marks the cut inside the text; the string simply ends. A caller that
does not read `stdout_truncated` / `stderr_truncated` — neither is an
`Execution` property, both live in `.data` — sees a complete-looking megabyte.
For output that can exceed the cap, redirect it to a file in the sandbox and
`download_file()` it instead.

### An execution dies with its sandbox, and nothing else can reach it

Deleting a sandbox while one of its commands is still running is **accepted** —
no 409, no wait. What does not survive is the execution record. Reading it back
afterwards with the id the caller kept answers `404 container_hub: not found`.

Every other route was probed on 2026-09-23, using the ids of a sandbox that had
already been deleted:

| Request | Answer |
|---|---|
| `GET /tasks/{sandbox_uuid}/executions/{execution_id}` (what the SDK calls) | 404 `container_hub: not found` |
| `GET /executions/{execution_id}` | 404 with no message — no such route |
| `GET /tasks/{sandbox_uuid}/executions` | 404 with no message — no such route |
| `GET /tasks/{sandbox_uuid}` | 404 `container_hub: not found` |

There is no top-level executions endpoint and no per-sandbox execution listing,
so a record's lifetime is bounded by its sandbox — which itself expires 24h after
the last thing that touched it.
A caller that starts a long job and keeps only the execution id has no way to
learn how it ended once the sandbox is gone, whether it was deleted on purpose,
by a cleanup, or by expiry. Take the result while the sandbox is alive: have the
command write to a file and `download_file()` it, or poll to a terminal state
before deleting.

The 404 does not distinguish "no such execution" from "the sandbox is gone", and
names an internal component (`container_hub`) rather than either.

**`execution.data["sandbox_id"]` is not an id the API accepts.** An execution
payload carries the provider-side id (`i9bb109047d45639e1c77`, the same shape as
the host prefix in `endpoint_url`), while `sandboxes.get()` and every `/tasks/…`
route want the UUID (`911e3b08-…`). The two are never the same string.

**Feeding it back leaks a database error.** `GET /tasks/i9bb…/executions/{id}`
answers **500 `pq: invalid input syntax for type uuid: "i9bb109047d45639e1c77"`**
— the raw Postgres driver message, reaching the client. A non-UUID id should be
a 400 from validation, not a 500 from the driver.

### Cancelling a command ends its children too

The positive half of the same row. A command that forks —
`sh -c 'sleep 300 & sleep 300 & sleep 300'`, where two of the three are children
the API never registered — is ended completely by `cancel_execution()`:
counted before the cancel, three `sleep` processes; counted after, zero.

**It takes nothing else with it.** Measured on 2026-09-23 with three things
alive in one sandbox at once — the target execution's three `sleep 888`, a
background `sleep 777` left behind by an *earlier* `execute()` call, and a
separate still-running execution's `sleep 999`:

| | before the cancel | after |
|---|---|---|
| the cancelled execution's own processes | 3 | **0** |
| a background process from another call | 1 | 1 |
| another live execution | 1 (status `running`) | 1 (status `running`) |

The sandbox itself keeps serving commands. So the blast radius is exactly the
execution's own process group — which makes cancelling safe on a shared sandbox.

**The signal is SIGTERM.** A command that traps it —
`trap "echo caught-TERM >> /tmp/sig.log" TERM` alongside handlers for INT and
HUP — logged `caught-TERM` and nothing else, so a cleanup handler does get to
run. Whether a process that ignores SIGTERM is then SIGKILLed, and after what
grace period, is untested.

Two more details:

* `cancel_execution()` answers **202 with `status: "cancelling"`** — an
  intermediate state, so the call is a request, not a completion. The execution
  reached `cancelled` about a second later.
* a cancelled execution has **`exit_code: null`**, not 130 or a negative signal
  number. `status` is the only thing that says how it ended.

### `expires_at` is a rolling 24h idle window, not a lifetime

A sandbox's `expires_at` is not "created_at plus a day". Measured 2026-09-23 on
a sandbox created at 06:22:11, reading and working on it by turns:

| Action | `expires_at` | from then |
|---|---|---|
| `refresh()` | `2026-09-24T07:39:29` | +23.596h |
| `refresh()` again, 20s later, no commands | `2026-09-24T07:39:29` | +23.590h — unmoved |
| one `execute("true")` | `2026-09-24T08:04:05` | **+24.000h** |
| `refresh()` 20s after that execute | `2026-09-24T08:04:05` | +23.994h — unmoved |
| another `execute` | `2026-09-24T08:04:28` | **+24.000h** |

So **every `execute()` pushes the deadline to exactly 24 hours from that moment,
and reads do not move it at all**. The sandbox above was already 1h17m past the
24h its creation timestamp would imply.

File transfers count as activity too: a later probe watched `expires_at` move
across an `upload_file()` and a `download_file()` as well, while `refresh()` and
`sandboxes.get()` left it alone. The rule is the container being touched, not
the record being read.

Two consequences:

* a sandbox in active use **never expires** — an idle timeout is not a cost
  ceiling, and a loop that touches its sandbox hourly keeps it alive forever.
  The only bound on spend is deleting it;
* a deadline read once is only valid until the next command. Anything planning
  around the expiry has to re-read it after activity, not cache it.

B-1 C-03 asserts the deadline does not move between two reads and still holds:
it only reads. That row's evidence now says why.

### No entry point anywhere for a status-change history

The SDK exposes only the current state — `status`, `status_stale`, `last_error`,
`expires_at`, and `provider_status_detail` (`{"phase": …, "readiness": …}`), all
of them "right now". Seventeen candidate routes were probed on 2026-09-23 and
every one answered 404 with no message, meaning the route does not exist rather
than the resource being absent:

```
/tasks/<id>/events  history  status  statuses  transitions  timeline
            audit   activity activities state  changes      revisions
/events  /audit  /audit-logs  /activities  /history
```

The nearest things that do exist, and what each is worth:

| Source | What it gives | What it cannot |
|---|---|---|
| `created_at` / `updated_at` on the task | when it appeared, when it last changed | which transition that change was |
| `provider_status_detail` | the provider's own `phase`/`readiness` | history — it is the current pair |
| an execution's `started_at` / `completed_at` | a per-command timeline | anything about the sandbox's own state |
| `stream()` | SSE carrying `status` events — the one live transition feed in the SDK | unusable here: `capabilities.logs_stream` is false, and the call read-times-out rather than refusing |

So a caller that wants a history has to build one by polling `refresh()`, which
loses every transition between two polls — including the one that explains why a
sandbox stopped. The Console is the only place a transition can be looked up
after the fact.

### A deleted sandbox cannot be found again, and `status` ignores values it does not know

Measured 2026-09-23: one sandbox launched, found every way the SDK offers, then
deleted and looked for again immediately and 20s later.

| Lookup | While running | After the delete |
|---|---|---|
| `sandboxes.get(id)` | `running` | **404 `container_hub: not found`** |
| `sandboxes.list()` | found | not found |
| `list(status="deleted")` | `total = 0` | **`total = 0`** |
| `list(status="stopped" / "stopping" / "error" / "creating")` | `total = 0` | not found |
| `list(status=["deleted", "stopped"])` | `total = 0` | not found |
| `agent.sandboxes()` | found | `total = 0` |

Nothing brings it back. `status="deleted"` does not return the row with a
`deleted` status — it returns **nothing at all**, unchanged 20s later, so this
is the record being gone rather than a replica lagging. A caller reconciling its
own records against the service cannot tell a sandbox it deleted from one that
never existed.

**And the status filter ignores values it does not recognise.** With that same
sandbox still `running`:

| `status=` | `total` | Returned the running sandbox? |
|---|---|---|
| `running` | 2 | yes — correct |
| `creating` `stopped` `stopping` `deleted` `error` | 0 | no — correctly excluded |
| **`terminated`** | 2 | **yes** |
| **`failed`** | 2 | **yes** |
| **`all`** / **`*`** | 2 | **yes** |

Real statuses filter; anything else is dropped and the call answers with the
whole live list, 200 and no warning. `list(status="terminated")` reads like a
query for terminated sandboxes and is in fact a query for everything. This is
the same silent-ignore behaviour as the unknown *query parameters* above, one
level down: now the parameter is honoured and its **value** is not.

### The concurrent-sandbox quota is 5, and refusing one still leaves a record

Measured 2026-09-23 by launching `gmi.sandbox.x-small` one at a time until the
account said no. Four reached `running`; one sandbox was already up from another
agent; the fifth call was refused.

```json
{
  "raised": "UnprocessableError",
  "status_code": 422,
  "code": null,
  "elapsed_s": 0.71,
  "message": "upstream create container: business-cloud-api .../api/v2/sandboxes returned 429:
              {\"code\":\"Resource.QuotaExceeded\",
               \"message\":\"sandbox quota exceeded: instance_type gmi.sandbox.x-small in
                            sandbox-runloop-us (5/5, requested 1)\",
               \"details\":{\"idc_name\":\"sandbox-runloop-us\", …}}"
}
```

**The quota is 5 per instance type per data center, counted across the whole
organization** — the four this probe launched plus one an unrelated agent was
already holding made 5/5. It is not per agent.

**The prose is excellent and the status code is wrong.** The message names the
resource, the data center, the ratio and the amount requested — far more than
most refusals in this API carry. But it arrives as HTTP **422** wrapping a
verbatim **429**, with `code` null as usual. A client written the obvious way —
`except RateLimitError`, or a retry policy keyed on 429 — never sees a 429 and
has to string-match the message to learn that waiting will not help.

**A refused launch still creates a task.** The call raised rather than returning
an id, yet a fifth task record appeared and settled into `error`. The caller has
no id for it, so nothing it kept can delete it; only listing the whole account
and subtracting what you know about will find it. It is inert and does not bill,
but a CI job retrying into a full quota accumulates one per attempt.

**A freed slot is reusable immediately.** Deleting one of the four and launching
again succeeded on the first try with no settle wait, and the new sandbox
reached `running` normally.

Note the shape depends on concurrency: fired one at a time the call is refused
up front, while B-1 C-01 saw concurrent launches accepted with the rejection
arriving later on the task. A caller has to handle both.

---

## Review notes (differences vs `sdk-api.md`)

The original `sdk-api.md` is accurate at the introspection level but missed
several things the source and the upstream `reference.md` contain:

1. **`APIError` attributes** — original listed `.status_code, .code, .details`,
   omitting **`.message`** (present in `errors.py` and in the upstream
   reference).
2. **`SandboxFailed` attributes** — original did not list `.status` and
   `.message`, which the quickstart relies on (`exc.status`, `exc.message`).
3. **`Agent.launch` fallback/`ValueError`** — original noted the asymmetry
   (`idc_name` defaults to `None`), but not that it falls back to `agent.idc`
   and that `SandboxCollection.launch` raises `ValueError` when `idc_name` is
   unset.
4. **`create` backend requirements** — `runtime="sandbox"` requires `idc` and
   `instance_type` at the API level, and the SDK auto-sets
   `deployment_type="gmi-ce"` in that case. Neither was in the original.
5. **`.data` on model objects** — `Agent`, `Sandbox`, `Idc`, `Product` all
   expose `.data`; the original only documented it for the dataclasses.
6. **Dependencies** — original omitted `certifi>=2024.0.0` and
   `websocket-client>=1.8` (the latter is needed by `shell`).
7. **Metrics defaults** — original omitted the 8 default chart kinds and the
   `step` bounds (60s default, min 5, max 3600).
8. **`health()`** — original listed it but not that it is a local-only helper
   (no network call). Also flagged the dead code in `eligibility()`.

No signature, attribute name, exception mapping, or constant in the original
was found to be wrong against the `0.1.0b2` source.
