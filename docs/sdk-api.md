# gmi-agentbox-sdk API reference

Every public class, method and attribute in `gmi-agentbox-sdk`, read out of the
installed package by introspection rather than from the upstream docs — so it
matches the version this harness actually tests against.

| | |
|---|---|
| Version | `0.1.0b2` |
| Generated from | `.venv/lib/python3.14/site-packages/agentbox_sdk` |
| Default base URL | `https://console.gmicloud.ai` |
| Default wait timeout | `300.0`s |

Regenerate after a version bump:

```bash
.venv/bin/python -c "import agentbox_sdk, inspect; print(inspect.getsource(agentbox_sdk.client))" | less
```

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

`api_key` falls back to `$GMI_AGENTBOX_API_KEY`, `base_url` to
`$GMI_AGENTBOX_BASE_URL` and then to `DEFAULT_BASE_URL`.

The four transport/sleep parameters are the seam this harness is built on: the
offline layer passes fakes, and `--log-http` passes wrappers that print the
traffic. See `tests/helpers/fakes.py` and `tests/helpers/http_log.py`.

### Methods on the client itself

There are only two — everything else lives under a namespace.

| Method | Returns |
|---|---|
| `eligibility()` | `Eligibility` |
| `health()` | `Mapping[str, Any]` |

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

`title` is the only required argument to `create`; everything else is omitted
from the request body when left as `None`.

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

### Lifecycle

```python
launch(
    slug: str, *,
    instance_type: str,
    idc_name: Optional[str],                 # required positionally-by-keyword, may be None
    env: Optional[Sequence[Mapping[str, Any]]] = None,
    display_name: Optional[str] = None,
    assign_public_ip: Optional[bool] = None,
    ports: Optional[Sequence[Mapping[str, Any]]] = None,
    storages: Optional[Sequence[Mapping[str, Any]]] = None,
    template_id: Optional[str] = None,
) -> Sandbox

delete(sandbox_id: str) -> None
```

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

### Files

```python
upload_file(
    sandbox_id: str, *, path: str,
    file: Union[str, os.PathLike, bytes, BinaryIO],
) -> list[Dict[str, Any]]

download_file(sandbox_id: str, *, path: str) -> FileDownload
```

### Streaming

```python
stream(sandbox_id: str, *, timeout: Optional[float] = None) -> Iterator[StreamEvent]
shell(sandbox_id: str, *, timeout: Optional[float] = None) -> Any    # WebSocket
```

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

Note the asymmetry: `Agent.launch` defaults `idc_name` to `None`, while
`SandboxCollection.launch` has no default for it. `Agent.launch` also has no
`template_id` parameter.

| Attribute | Notes |
|---|---|
| `.id` `.slug` `.title` | |
| `.idc` `.runtime` `.image_url` `.revision` `.env` | |
| `.launchable` | False when the upstream template is missing |
| `.template_build_status` | `None` means the backend reports no build state, which counts as ready |
| `.template_build_error` | |
| `.generated_api_key` | Plaintext MaaS key, **present only on create** (and rare patch retries) |

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

`wait_until_running` exists only on the object, not on `SandboxCollection`. It
raises `SandboxFailed` on a terminal non-running status and
`SandboxWaitTimeout` when the deadline passes.

| Attribute | Notes |
|---|---|
| `.id` `.status` | |
| `.status_stale` | |
| `.agent_id` `.agent_slug` | |
| `.instance_type` `.idc_name` `.display_name` | |
| `.endpoint_url` `.expires_at` `.runtime` `.capabilities` | |
| `.last_error` | |

---

## Data classes

| Class | Fields | Extra |
|---|---|---|
| `Page` | `items` `total` `page` `page_size` | |
| `Eligibility` | `eligible` `data_centers` `data` | |
| `Execution` | `data` `accepted` | properties `.id` `.status` `.exit_code` |
| `FileDownload` | `content` `filename` `headers` | |
| `StreamEvent` | `event` `data` `raw` | one Server-Sent Event |
| `MetricSeries` | `kind` `unit` `empty_reason` `points` `data` | |
| `MetricsBatch` | `results` `data` | |

`data` on these holds the raw payload, so a field the SDK does not model yet is
still reachable.

---

## Exceptions

```
AgentBoxSDKError
├── APIError                    (.status_code, .code, .details)
│   ├── BadRequestError         400
│   ├── AuthenticationError     401
│   ├── PermissionDeniedError   403
│   ├── NotFoundError           404
│   ├── ConflictError           409
│   ├── UnprocessableError      422
│   ├── RateLimitError          429
│   └── ServerError             >= 500
├── TransportError              no HTTP response — DNS, TLS, connection refused
├── SandboxFailed               terminal non-running status while waiting
└── SandboxWaitTimeout          wait_until_running deadline passed
```

Mapping happens in `errors.error_from_response`. Anything outside the table
and below 500 — a 402 or a 451, say — raises **plain `APIError`**, not a
subclass, so matching on exact types will miss it.

The message is read from the payload's `error`, then `message`, falling back to
`"Request failed"`; `code` and `details` are carried through when present.

Catch `APIError` for anything the server answered, `AgentBoxSDKError` for
everything the SDK can raise.

---

## Module constants

| Name | Value |
|---|---|
| `DEFAULT_BASE_URL` | `https://console.gmicloud.ai` |
| `DEFAULT_WAIT_TIMEOUT` | `300.0` |
| `__version__` | `0.1.0b2` |

All requests go out under the `/api/v1/ie/container` prefix.

---

## Things the SDK does not have

- **No logging.** No `logging` import, no request hooks, no debug flag. To see
  traffic, inject a wrapping transport — `--log-http` in this harness does
  exactly that (`tests/helpers/http_log.py`).
- **No async client.** Everything is synchronous.
- **No retry/backoff** beyond what `wait_until_running` does for polling.
- **No pagination iterator.** `list()` returns one `Page`; advance `page`
  yourself.
