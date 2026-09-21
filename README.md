# gmi-agentbox-sdk test harness

A QA harness for [`gmi-agentbox-sdk`](https://pypi.org/project/gmi-agentbox-sdk/):
every case drives the real SDK against the real AgentBox API and checks the
behaviour a PRD row promises.

One directory per PRD module, so a case is where its feature is:

| File | Covers | Cost |
|---|---|---|
| `tests/test_b0_agent_registration.py` | B-0: registration, image build, update, delete — T-01..T-06, one test per case row | free, except the two rows marked `billable` |

There is no opt-in flag: a plain `pytest` runs against the live service. The
twelve cases marked `billable` launch a real sandbox, so deselect them with
`-m "not billable"` unless you mean to pay. Without `GMI_AGENTBOX_API_KEY` the
whole suite skips itself, which is the only thing that makes a bare checkout
inert.

## Setup

```bash
make install          # python3 -m venv .venv && pip install -r requirements.txt
cp .env.example .env  # GMI_AGENTBOX_API_KEY is required
```

## Running

```bash
make check            # everything that launches nothing and bills nothing
make check-all        # the full suite, including a billable sandbox
```

Underneath, it is plain pytest:

```bash
.venv/bin/python -m pytest -m "not billable"                 # 14 cases, no bill
.venv/bin/python -m pytest tests/test_b0_agent_registration.py   # one file
.venv/bin/python -m pytest -k t01 -s                         # one case, logged
.venv/bin/python -m pytest --idc us-central-iowa2 --instance-type gmi.sandbox.x-small
.venv/bin/python -m pytest                                   # ALL 26, launches a sandbox
```

## Layout

```
tests/
├── conftest.py                       # .env loading, CLI options, shared fixtures
├── helpers/
│   ├── http_log.py                   # --log-http transports: narrate every request
│   └── report.py                     # show() and known_gap(): how a case reports itself
└── test_b0_agent_registration.py     # T-01..T-06, one test per case row
```

The layout is flat on purpose: at this size a directory per feature would hold
one file each and buy nothing. When an area grows past a file or two, give it a
directory named after its PRD module (`b0_agent_registration/`) and name the
files inside after the aspect they cover, never repeating the module name.
`--import-mode=importlib` is already set, so those directories will need no
`__init__.py` and two of them may both hold a `test_errors.py`.

## Writing a case

Cases in `test_b0_agent_registration.py` log every SDK call
through `show()`, which names the method, the HTTP request it becomes, the
arguments and the return value, and attaches all of it to the Allure report:

```python
show(
    f"agents.create [{tag}] -> {elapsed:.2f}s",
    method="test_client.agents.create(**request)",
    http="POST /deployments",
    args=request,
    returns=agent,
)
```

A step that calls nothing says so (`method="(none) - ..."`), so a reader can
tell "this was not tested" from "this has no API". Secrets are reduced to a
shape — type, length, last four characters, a sha256 prefix — which is enough
to tell an absent field from an empty one from a real key, without printing it.

`known_gap()` records a documented shortfall: it tags the case `KNOWN-GAP`,
attaches the reason and xfails, so a gap stays visible instead of turning into
a red failure someone learns to ignore.

## Running against the real API

Markers: `slow` (image build, sandbox boot) and `billable` (launches a
sandbox). `-m "not billable"` is the one selector worth remembering.

Safety rules baked into `tests/conftest.py`:

- every agent and sandbox is registered with a session `ResourceTracker`;
- teardown deletes sandboxes first, then agents, even after a failure, and
  prints `[CLEANUP FAILED — delete by hand]` with the id if a delete does not
  land;
- agent titles carry a per-run suffix (`sdk-test-0918-1432-a3f1`) so parallel
  runs never collide;
- `--keep-resources` skips cleanup for debugging and prints what is still
  running — those resources keep billing until you delete them.

Configuration (CLI flag wins over env var, env var wins over discovery):

| Setting | Flag | Env |
|---|---|---|
| API key | — | `GMI_AGENTBOX_API_KEY` (required) |
| Service origin | — | `GMI_AGENTBOX_BASE_URL` |
| Data center | `--idc` | `GMI_TEST_IDC` |
| SKU | `--instance-type` | `GMI_TEST_INSTANCE_TYPE` |
| Base image | — | `GMI_TEST_IMAGE` |
| Build / boot timeouts | — | `GMI_TEST_BUILD_TIMEOUT`, `GMI_TEST_RUN_TIMEOUT` |

Left unset, the fixtures discover the first data center and SKU the
organization can actually use, and skip the suite if there is none.

## CI

`.github/workflows/tests.yml` runs the suite and publishes the Allure
report to GitHub Pages.

Nothing here belongs on a pull request — every case spends real resources — so
the workflow has no `push` or `pull_request` trigger at all. It runs nightly at
18:00 UTC with `-m "not billable"`, and on demand from the Actions tab, where a
dropdown chooses between *nothing that bills* and *the whole suite*. A
`concurrency` group keeps two live runs from fighting over the account's
sandbox quota, and it deliberately does **not** cancel a run in progress:
killing one mid-flight strands sandboxes that keep billing.

Three things have to be set up once, in the repository settings:

| Where | What |
|---|---|
| Settings → Secrets and variables → Actions → **Secrets** | `GMI_AGENTBOX_API_KEY` |
| Settings → Secrets and variables → Actions → **Variables** | `GMI_AGENTBOX_BASE_URL` — set it on `dev`, leave it unset on `prod` |
| Settings → **Pages** → Source | **GitHub Actions** |

Use **Settings → Environments** rather than repository-wide secrets when dev
and prod have different accounts: the job declares
`environment: ${{ inputs.environment }}`, so `GMI_AGENTBOX_API_KEY` and
`GMI_AGENTBOX_BASE_URL` resolve to whichever environment was chosen. A
repository-level secret still works as a fallback while only one account is in
play.

The workflow materialises those two settings into a `.env` at the repo root,
the same file a developer keeps locally, so the suite,
`scripts/cleanup_leftovers.py` and `allure/stamp_env.py` all read their
configuration the one way. It is written with `umask 077`, the values never
reach the log, and it is removed at the end of the job. Nothing else goes in:
the data center, the SKU and the base image are left to the fixtures, which
discover the first usable data center, the smallest SKU on offer and
`docker.io/library/alpine:3.20`. Pin one by adding it to that step only if a
run needs it.

Leave a variable unset rather than empty. An empty `GMI_AGENTBOX_BASE_URL` is
worse than a missing one: the SDK reads it with `os.getenv(name, default)`, so
`""` beats the default and the client ends up with no base URL at all.

A missing `GMI_AGENTBOX_API_KEY` fails the run immediately rather than letting
it pass. Without a key every case skips itself, pytest exits 0, and a run that
tested nothing would otherwise report green.

The report publishes whether the suite passed or failed; the job still ends
red if anything failed, so a broken suite cannot look green. Trend graphs come
from the previous run's `history/`, carried forward in the Actions cache.

Two safety nets beyond the session tracker: `timeout-minutes: 60` bounds a hung
run, and `scripts/cleanup_leftovers.py` runs afterwards no matter how the job
ended. Run it by hand too when something dies locally:

```bash
python scripts/cleanup_leftovers.py --dry-run   # list what it would delete
python scripts/cleanup_leftovers.py             # delete it
```

It only touches agents whose slug starts with `sdk-test-` or `sdk-probe-`, and
the sandboxes under them — never anything else in the account.

## Conventions

PEP 8, so: `snake_case` for modules, functions, fixtures and variables,
`PascalCase` for classes, `UPPER_SNAKE_CASE` for constants, a leading `_` for
anything private. Test names are full sentences describing the behaviour
(`test_launch_without_an_idc_fails_before_any_request`) — pytest prints them on
failure, so they double as the failure report.
