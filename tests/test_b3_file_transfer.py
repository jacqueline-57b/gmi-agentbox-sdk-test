"""B-3 File transfer and integrity — one test per case row.

Upload the input, let a chain of commands write intermediate files, download
the product. A dropped connection, a size past the ceiling, two writers at
once, half a file — none of them may damage what is already on the target
path.

| ID   | P  | Action                                              | Expected                                              |
|------|----|-----------------------------------------------------|-------------------------------------------------------|
| F-01 | P0 | 50MB up, 30MB down, checksummed at both ends;       | every byte matches; cd and export do not carry from   |
|      |    | command 1 writes -> disconnect -> command 2 reads   | one command to the next                               |
| F-02 | P0 | the target already holds A, then: an overwrite past | the target is still A, or some complete new file —    |
|      |    | the ceiling, a half-sent upload, two writers at once| never missing, never half, never a mix of the two     |
| F-03 | P1 | upload into a directory that does not exist; the    | all refused and nothing written. Creating the missing |
|      |    | paths ../ and %2e%2e/; a symlink out of the sandbox | directory for the caller is a failure, not a kindness |
| F-04 | P1 | find the upload and download ceilings; download a   | every one is a named error rather than a hang, and no |
|      |    | missing path, a directory, and a cut-off transfer   | call quietly hands back less than it was asked for    |
| F-05 | P1 | replay the same upload under one idempotency key    | at most once. No such parameter -> Known gap          |

Every row needs a running sandbox, so every row is `billable`. All five share
the session sandbox, work under their own `/tmp/f0N-*` paths, and clean up
after themselves.

Three measurements from 2026-09-21 shape the file. The ceiling a caller meets
is the SDK's own: `AgentBoxClient` defaults to a 30s socket timeout and upload
throughput falls away sharply with size — 1MB in 10.7s, 8MB in 13.8s, 32MB in
145s — so a 32MB upload on a default client dies at 30.5s with a
`TransportError` and never reaches whatever limit the service has. Every row
but F-04 raises the timeout so it can measure the thing it is about; F-04's
ladder raises it further still (`GMI_TEST_LADDER_TIMEOUT`, 300s) so it climbs
until the service answers rather than stopping at the client's wall, while
F-02 keeps the 30s default deliberately — that is the ceiling a caller meets
without having chosen it. Downloads answer differently: a body of any
size arrives chunked with no `Content-Length`, so "did all of it arrive" can
only be answered against the file. And a broken transfer cannot be asked for
through any SDK call, so `helpers.interrupt` stages the reset at the socket.

F-03 tests the two traversal defences apart. `../` and `%2e%2e/` never leave
the machine — `_validate_sandbox_path` rejects them before a request is built —
so those legs pin the SDK's guard; the symlink leg is what reaches the service.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

import allure
import pytest
from agentbox_sdk import AgentBoxClient, NotFoundError, Sandbox
from agentbox_sdk.client import SandboxCollection

from helpers.interrupt import short_read_transport, truncating_transport
from helpers.report import _payload, show

FEATURE = "B-3 File transfer and integrity"
P0 = allure.severity_level.BLOCKER
P1 = allure.severity_level.CRITICAL

pytestmark = [pytest.mark.slow, pytest.mark.billable]

# The SDK's own default, and what these rows raise it to. Kept as a named
# constant because F-04's answer is this number, not a service limit.
DEFAULT_CLIENT_TIMEOUT = 30.0
TRANSFER_TIMEOUT = float(os.getenv("GMI_TEST_TRANSFER_TIMEOUT", "600"))

# Long enough for `dd` and `sha256sum` over the sizes below.
EXEC_WAIT = int(os.getenv("GMI_TEST_EXEC_WAIT", "300"))

# F-01. What the case row asks for; lower both for a cheap smoke run.
UPLOAD_MB = int(os.getenv("GMI_TEST_UPLOAD_MB", "50"))
DOWNLOAD_MB = int(os.getenv("GMI_TEST_DOWNLOAD_MB", "30"))

# F-02. "File A" is small on purpose: this row is about what the failed writes
# do to the target, and a large A would spend its time on a transfer that is
# not the thing being measured.
F02_KEEP_MB = 1
F02_RACE_MB = 2
# The overwrite that cannot finish. 32MB on a default-timeout client is the
# measured ceiling — it fails at ~30s every time, which is both realistic and
# quick, where a genuinely enormous body would take minutes to prove the same.
F02_OVERSIZE_MB = int(os.getenv("GMI_TEST_OVERSIZE_MB", "32"))

# F-04. The ladder gets its own timeout, well past the SDK's 30s default, so it
# climbs until something on the service side answers rather than stopping at the
# client's own wall. Measured 2026-09-21 at 300s: 64MB uploaded in 140s, 100MB
# failed at 300.6s — so the rungs below reach past what this timeout can carry,
# and the row reports what it did not try rather than implying it found a
# ceiling it never reached.
LADDER_TIMEOUT = float(os.getenv("GMI_TEST_LADDER_TIMEOUT", "300"))
LADDER_MB = tuple(
    int(size) for size in os.getenv("GMI_TEST_LADDER_MB", "1,8,32,64,100").split(",") if size
)
# How much of a transfer the interrupting transports deliver before the reset.
CUT_AT = 0.5
# A refusal has to arrive promptly to count as a refusal rather than a hang.
REFUSAL_MUST_BE_WITHIN = float(os.getenv("GMI_TEST_REFUSAL_WITHIN", "30"))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def blob(megabytes: int) -> bytes:
    """Random, not a repeated block: a compressible payload measures the compressor."""
    return os.urandom(megabytes * 1024 * 1024)


def sh(sandbox: Sandbox, command: str) -> str:
    """Run one command and return its stdout, stripped."""
    try:
        execution = sandbox.execute(command, wait_timeout_seconds=EXEC_WAIT)
    except NotFoundError as exc:
        # Otherwise this arrives as a bare traceback out of the SDK and reads
        # like the row's own failure rather than the sandbox being taken away.
        raise AssertionError(
            f"the sandbox is gone mid-row ({exc.status_code} {exc.message}): it was deleted "
            f"out from under the test — by another run, a cleanup script or the Console — so "
            f"nothing from here on measures anything"
        ) from exc
    return (execution.data.get("stdout") or "").strip()


def discard_paths(sandbox: Sandbox, paths: str) -> None:
    """Best-effort cleanup: a failure here must not replace the row's own.

    These calls run in a `finally`, so anything they raise takes the place of
    the assertion that was the row's verdict — including the sandbox having
    been deleted out from under the test.
    """
    try:
        sandbox.execute(f"rm -rf {paths}", wait_timeout_seconds=EXEC_WAIT)
    except Exception as exc:  # noqa: BLE001 - cleanup is never the verdict
        show(
            "cleanup could not run",
            method=f"sandbox.execute('rm -rf {paths}')",
            returns={"raised": type(exc).__name__, "message": str(exc)[:200]},
        )


def file_state(sandbox: Sandbox, path: str) -> Dict[str, Any]:
    """Whether the path exists, its size and its digest — in one command, so two
    concurrent writers cannot slip a third state in between the reads."""
    out = sh(
        sandbox,
        f"if [ -f '{path}' ]; then wc -c < '{path}'; sha256sum '{path}' | cut -d' ' -f1; "
        f"elif [ -e '{path}' ]; then echo NOT-A-FILE; else echo MISSING; fi",
    )
    parts = out.split()
    if not parts or parts[0] in ("MISSING", "NOT-A-FILE"):
        return {"exists": False, "note": parts[0] if parts else "no answer", "raw": out}
    return {
        "exists": True,
        "size": int(parts[0]),
        "sha256": parts[1] if len(parts) > 1 else None,
    }


def bound_to(client: AgentBoxClient, sandbox: Sandbox) -> Sandbox:
    """The same sandbox driven by another client, built from the id rather than
    fetched: the interrupting transports cannot complete an ordinary GET."""
    return Sandbox(client, {"id": sandbox.id})


def attempt(call: Callable[[], Any]) -> Dict[str, Any]:
    """Run a call and describe what came back: here an exception is an outcome."""
    started = time.monotonic()
    try:
        value = call()
    except Exception as exc:  # noqa: BLE001 - the shape of the failure is the answer
        return {
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 2),
            "raised": type(exc).__name__,
            "status_code": getattr(exc, "status_code", None),
            "message": str(exc)[:500],
            "exception": exc,
        }
    return {
        "ok": True,
        "elapsed_s": round(time.monotonic() - started, 2),
        "value": value,
    }


def reportable(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """An outcome with the live objects replaced by something dumpable."""
    trimmed = {key: value for key, value in outcome.items() if key != "exception"}
    value = trimmed.get("value")
    if hasattr(value, "content"):  # a FileDownload
        trimmed["value"] = {
            "bytes": len(value.content),
            "filename": value.filename,
            "content_length_header": _content_length(value),
        }
    elif value is not None:
        trimmed["value"] = _payload(value)
    return trimmed


def _content_length(download: Any) -> Optional[int]:
    for name, value in (download.headers or {}).items():
        if name.lower() == "content-length" and str(value).isdigit():
            return int(value)
    return None


def damage(state: Dict[str, Any], acceptable: Dict[str, str]) -> Optional[str]:
    """What is wrong with the target, or None if it is one of the whole files.

    `acceptable` maps a digest to the file it belongs to, so a failure can name
    which complete file the target should have been.
    """
    if not state["exists"]:
        return f"the target is gone entirely: {state}"
    if state["sha256"] not in acceptable:
        return (
            f"the target holds {state['size']} bytes hashing to {state['sha256']}, which is "
            f"none of the complete files it could legitimately be ({acceptable}) — the write "
            f"left a partial or mixed file behind"
        )
    return None


@pytest.fixture(scope="session")
def transfer_client(test_client: AgentBoxClient) -> AgentBoxClient:
    """A client whose socket timeout can outlast a 50MB body.

    Takes `test_client` without using it so a run with no API key skips there
    rather than failing on this constructor.
    """
    del test_client
    return AgentBoxClient(timeout=TRANSFER_TIMEOUT)


@pytest.fixture
def transfer_sandbox(transfer_client: AgentBoxClient, session_sandbox: Sandbox) -> Sandbox:
    """The shared sandbox, reached through the long-timeout client."""
    return bound_to(transfer_client, session_sandbox)


# --------------------------------------------------------------------------
# F-01 a large file, out and back, with commands in between
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("F-01 a large file survives the round trip and commands keep no state")
@allure.severity(P0)
def test_f01_large_transfer_matches_and_commands_do_not_inherit_state(transfer_sandbox):
    sandbox = transfer_sandbox
    source = "/tmp/f01-input.bin"
    middle = "/tmp/f01-middle.bin"

    try:
        with allure.step("1. cd and export do not carry from one command to the next"):
            # Where a command starts is measured, not assumed: a literal "/"
            # would fail on an image that starts elsewhere, for no good reason.
            default_cwd = sh(sandbox, "pwd")
            first = sh(sandbox, "cd /var && export F01_VAR=set-by-command-1 && pwd")
            second = sh(sandbox, "pwd; echo F01_VAR=[$F01_VAR]")
            lines = second.splitlines()
            landed_in = lines[0].strip() if lines else ""
            show(
                "state left behind by command 1, as command 2 sees it",
                method="sandbox.execute('cd /var && export ...') then "
                "sandbox.execute('pwd; echo ...')",
                returns={
                    "where a command starts by default": default_cwd,
                    "command 1 ended in": first,
                    "command 2 starts in": landed_in,
                    "command 2 sees F01_VAR as": second,
                },
            )
            assert first.endswith("/var"), f"command 1 did not reach /var: {first!r}"
            assert landed_in == default_cwd, (
                f"command 2 started in {landed_in!r} rather than the default {default_cwd!r}, "
                f"so a directory change leaks from one command into the next and a caller's "
                f"commands are not independent"
            )
            assert "F01_VAR=[]" in second, (
                f"an exported variable survived into the next command: {second!r}"
            )

        with allure.step("2. upload the payload and checksum it at both ends"):
            payload = blob(UPLOAD_MB)
            local_sha = hashlib.sha256(payload).hexdigest()
            started = time.monotonic()
            written = sandbox.upload_file(path=source, file=payload)
            elapsed = round(time.monotonic() - started, 2)
            state = file_state(sandbox, source)
            show(
                f"upload {UPLOAD_MB}MB",
                method="sandbox.upload_file(path=..., file=<bytes>)",
                args={"path": source, "bytes": len(payload), "client_timeout_s": TRANSFER_TIMEOUT},
                returns={
                    "returned": written,
                    "seconds": elapsed,
                    "throughput_MB_per_s": round(UPLOAD_MB / elapsed, 2) if elapsed else None,
                    "sha256 sent": local_sha,
                    "on the sandbox": state,
                },
            )
            assert state["exists"], "the upload reported success but wrote no file"
            assert state["size"] == len(payload), (
                f"{len(payload)} bytes were sent and {state['size']} arrived"
            )
            assert state["sha256"] == local_sha, (
                f"the file on the sandbox hashes to {state['sha256']}, not the {local_sha} "
                f"that was uploaded — the bytes changed in transit"
            )

        with allure.step("3. command 1 cuts part of it into an intermediate file"):
            sh(
                sandbox,
                f"dd if={source} of={middle} bs=1M count={DOWNLOAD_MB} 2>/dev/null; "
                f"sha256sum {middle} | cut -d' ' -f1",
            )
            middle_state = file_state(sandbox, middle)
            show(
                "command 1 writes the intermediate file",
                method="sandbox.execute('dd ...')",
                args={"from": source, "to": middle, "megabytes": DOWNLOAD_MB},
                returns=middle_state,
            )
            assert middle_state["exists"], "command 1 wrote no intermediate file"

        with allure.step("4. the client is dropped; a new one reattaches with only the id"):
            # Nothing is carried over: a fresh client, a fresh connection, and
            # the sandbox rebuilt from its id the way a restarted process
            # would have to.
            reattached = bound_to(AgentBoxClient(timeout=TRANSFER_TIMEOUT), sandbox)
            show(
                "reattach after the disconnect",
                method="AgentBoxClient(...) then Sandbox(client, {'id': sandbox_id})",
                args={"sandbox_id": sandbox.id, "carried over": "the id, and nothing else"},
                returns={"status": reattached.refresh().status},
            )
            assert reattached.status == "running", (
                f"the sandbox is {reattached.status!r} after the client went away"
            )

        with allure.step("5. command 2, on the new client, reads what command 1 wrote"):
            seen = sh(reattached, f"sha256sum {middle} | cut -d' ' -f1; wc -c < {middle}")
            show(
                "command 2 reads the intermediate file",
                method="sandbox.execute('sha256sum ...') on the reattached client",
                returns={"stdout": seen, "expected_sha": middle_state["sha256"]},
            )
            assert middle_state["sha256"] in seen, (
                "the file command 1 wrote is not what command 2 reads back after the "
                f"disconnect: {seen!r}"
            )

        with allure.step("6. download the intermediate file and compare byte for byte"):
            started = time.monotonic()
            got = reattached.download_file(path=middle)
            elapsed = round(time.monotonic() - started, 2)
            downloaded_sha = hashlib.sha256(got.content).hexdigest()
            show(
                f"download {DOWNLOAD_MB}MB",
                method="sandbox.download_file(path=...)",
                args={"path": middle},
                returns={
                    "bytes": len(got.content),
                    "filename": got.filename,
                    "content_length_header": _content_length(got),
                    "seconds": elapsed,
                    "throughput_MB_per_s": round(DOWNLOAD_MB / elapsed, 2) if elapsed else None,
                    "sha256 downloaded": downloaded_sha,
                    "sha256 on the sandbox": middle_state["sha256"],
                },
            )
            assert len(got.content) == middle_state["size"], (
                f"the sandbox holds {middle_state['size']} bytes and the download returned "
                f"{len(got.content)}"
            )
            assert downloaded_sha == middle_state["sha256"], (
                "what came down does not hash to what is on the sandbox"
            )

    finally:
        discard_paths(sandbox, f"{source} {middle}")


# --------------------------------------------------------------------------
# F-02 a write that fails must not damage what is already there
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("F-02 a failed write leaves the target whole, as A or as a complete new file")
@allure.severity(P0)
def test_f02_a_failed_write_never_leaves_the_target_damaged(transfer_sandbox):
    sandbox = transfer_sandbox
    target = "/tmp/f02-target.bin"

    original = b"A" * (F02_KEEP_MB * 1024 * 1024)
    original_sha = hashlib.sha256(original).hexdigest()

    try:
        with allure.step("1. the target holds file A"):
            sandbox.upload_file(path=target, file=original)
            state = file_state(sandbox, target)
            show(
                "file A is in place",
                method="sandbox.upload_file(path=..., file=<A>)",
                args={"path": target, "bytes": len(original)},
                returns={"state": state, "sha256 of A": original_sha},
            )
            assert state["sha256"] == original_sha, "A was not written correctly to begin with"

        with allure.step("2. an overwrite that cannot finish inside the client's timeout"):
            oversized = blob(F02_OVERSIZE_MB)
            oversized_sha = hashlib.sha256(oversized).hexdigest()
            # Deliberately the SDK's own default: this is the ceiling a caller
            # meets without having chosen it, and the one a real program hits.
            default_client = bound_to(AgentBoxClient(timeout=DEFAULT_CLIENT_TIMEOUT), sandbox)
            outcome = attempt(lambda: default_client.upload_file(path=target, file=oversized))
            after = file_state(sandbox, target)
            show(
                f"overwrite with {F02_OVERSIZE_MB}MB on a default "
                f"{DEFAULT_CLIENT_TIMEOUT:.0f}s client",
                method="sandbox.upload_file(...) on AgentBoxClient(timeout=30.0)",
                args={"path": target, "bytes": len(oversized)},
                returns={"outcome": reportable(outcome), "target afterwards": after},
            )
            whole = {original_sha: "A, untouched", oversized_sha: "the whole oversized file"}
            assert not damage(after, whole), (
                f"after an overwrite that ran out of time, {damage(after, whole)}"
            )

        with allure.step("3. an upload cut in half on the wire"):
            halved = blob(F02_KEEP_MB)
            halved_sha = hashlib.sha256(halved).hexdigest()
            broken = bound_to(
                AgentBoxClient(
                    timeout=TRANSFER_TIMEOUT, transport=truncating_transport(CUT_AT)
                ),
                sandbox,
            )
            outcome = attempt(lambda: broken.upload_file(path=target, file=halved))
            after = file_state(sandbox, target)
            show(
                f"upload reset after {CUT_AT:.0%} of the body",
                method="sandbox.upload_file(...) on a client whose transport resets mid-body",
                args={"path": target, "bytes": len(halved), "delivered": f"{CUT_AT:.0%}"},
                returns={"outcome": reportable(outcome), "target afterwards": after},
            )
            assert not outcome["ok"], (
                "an upload whose connection was reset halfway reported success"
            )
            whole = {
                original_sha: "A, untouched",
                oversized_sha: "the whole oversized file from step 2",
                halved_sha: "the whole half-sent file",
            }
            assert not damage(after, whole), (
                f"after an upload cut in half on the wire, {damage(after, whole)}"
            )

        with allure.step("4. two clients writing the same path at once"):
            first = blob(F02_RACE_MB)
            second = b"B" * (F02_RACE_MB * 1024 * 1024)
            # What the target holds going in belongs in the acceptable set:
            # if both writers are refused, nothing was written and the file
            # standing untouched is the correct outcome, not a damaged one.
            before = file_state(sandbox, target)
            writers = {
                hashlib.sha256(first).hexdigest(): "the whole of writer 1's file",
                hashlib.sha256(second).hexdigest(): "the whole of writer 2's file",
            }
            if before.get("sha256"):
                writers[before["sha256"]] = "what the target already held, untouched"
            one = bound_to(AgentBoxClient(timeout=TRANSFER_TIMEOUT), sandbox)
            two = bound_to(AgentBoxClient(timeout=TRANSFER_TIMEOUT), sandbox)
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [
                    pool.submit(attempt, lambda: one.upload_file(path=target, file=first)),
                    pool.submit(attempt, lambda: two.upload_file(path=target, file=second)),
                ]
            outcomes = [job.result() for job in jobs]
            after = file_state(sandbox, target)
            show(
                "two clients writing one path concurrently",
                method="two AgentBoxClients, each sandbox.upload_file(path=<same>, "
                "file=<different>)",
                args={"path": target, "bytes each": F02_RACE_MB * 1024 * 1024},
                returns={
                    "writer 1": reportable(outcomes[0]),
                    "writer 2": reportable(outcomes[1]),
                    "target afterwards": after,
                    "acceptable digests": writers,
                },
            )
            assert not damage(after, writers), (
                f"after two concurrent writers, {damage(after, writers)}"
            )
    finally:
        discard_paths(sandbox, target)


# --------------------------------------------------------------------------
# F-03 a path that escapes, or a directory that is not there
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("F-03 a bad destination is refused and nothing is written")
@allure.severity(P1)
def test_f03_bad_destinations_are_refused_and_leave_nothing_behind(transfer_sandbox):
    sandbox = transfer_sandbox
    missing_dir = "/tmp/f03-no-such-dir"
    link = "/tmp/f03-link"
    escaped = "/etc/f03-escaped.txt"

    try:
        with allure.step("1. uploading into a directory that does not exist is refused"):
            sh(sandbox, f"rm -rf {missing_dir}")
            outcome = attempt(
                lambda: sandbox.upload_file(path=f"{missing_dir}/payload.txt", file=b"payload")
            )
            afterwards = sh(sandbox, f"[ -d '{missing_dir}' ] && echo EXISTS || echo MISSING")
            show(
                "upload into a directory that is not there",
                method=f"sandbox.upload_file(path='{missing_dir}/payload.txt', ...)",
                returns={**reportable(outcome), "the directory afterwards": afterwards},
            )
            assert not outcome["ok"], (
                "the upload succeeded, so the service created the missing directory for the "
                "caller — a typo'd destination then lands somewhere nobody meant to write"
            )
            assert afterwards == "MISSING", (
                f"the upload was refused but {missing_dir} exists afterwards, so the refusal "
                f"still left a change behind"
            )

        with allure.step("2. paths that climb out of the tree are refused"):
            # Both are rejected by _validate_sandbox_path before a request is
            # built, so what this step pins is the SDK's guard; the service's
            # own defence is step 3's business.
            for path in ("/tmp/../etc/f03-escaped.txt", "/tmp/%2e%2e/etc/f03-escaped.txt"):
                outcome = attempt(
                    lambda path=path: sandbox.upload_file(path=path, file=b"escaped")
                )
                show(
                    f"upload to a path containing dot segments: {path}",
                    method="sandbox.upload_file(path=<dot segments>, ...)",
                    returns=reportable(outcome),
                )
                assert not outcome["ok"], f"{path} was accepted rather than rejected"

        with allure.step("3. a symlink out of the tree is followed in neither direction"):
            sh(sandbox, f"rm -f {link} {escaped}; ln -s /etc {link}")
            wrote = attempt(
                lambda: sandbox.upload_file(path=f"{link}/f03-escaped.txt", file=b"through a link")
            )
            read = attempt(lambda: sandbox.download_file(path=f"{link}/passwd"))
            landed = file_state(sandbox, escaped)
            body = read["value"].content if read["ok"] else b""
            show(
                "writing and reading through a symlink to /etc",
                method=f"sandbox.upload_file(path='{link}/f03-escaped.txt', ...) then "
                f"sandbox.download_file(path='{link}/passwd')",
                returns={
                    "the write": reportable(wrote),
                    "the read": reportable(read),
                    "what /etc holds afterwards": landed,
                    "bytes read back": len(body),
                    "starts with": body[:60].decode("utf-8", "replace") if body else "",
                },
            )
            assert not landed["exists"], (
                f"the upload followed the symlink and wrote {escaped} — a path reachable by "
                f"pointing a link at it is not protected by whatever guards the path itself"
            )
            assert not body, (
                f"downloading {link}/passwd returned {len(body)} bytes of /etc/passwd — a "
                f"symlink is a way out in both directions, and the read side followed it"
            )
    finally:
        discard_paths(sandbox, f"{missing_dir} {link} {escaped}")


# --------------------------------------------------------------------------
# F-04 where the ceilings are, and what failure looks like
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("F-04 every limit and every failure is a named error, not a hang")
@allure.severity(P1)
def test_f04_limits_and_failures_are_named_errors(transfer_sandbox):
    # Four separate failure modes, so as in F-03 every leg runs before anything
    # is asserted; a reviewer needs all four verdicts, not the first break.
    sandbox = transfer_sandbox
    probe = "/tmp/f04-probe.bin"
    ladder: List[Dict[str, Any]] = []
    violations: List[str] = []

    try:
        with allure.step("1. the upload ladder, climbing until something refuses"):
            climber = bound_to(AgentBoxClient(timeout=LADDER_TIMEOUT), sandbox)
            for megabytes in LADDER_MB:
                # Built outside the timed call: os.urandom of 32MB is not part
                # of what the rung is measuring.
                payload = blob(megabytes)
                outcome = attempt(
                    lambda body=payload: climber.upload_file(path=probe, file=body)
                )
                ladder.append({"megabytes": megabytes, **reportable(outcome)})
                if not outcome["ok"]:
                    break
            reached = [rung["megabytes"] for rung in ladder]
            untried = [size for size in LADDER_MB if size not in reached]
            show(
                "how large an upload a default client can finish",
                method="sandbox.upload_file(...) on "
                f"AgentBoxClient(timeout={LADDER_TIMEOUT})",
                args={
                    "ladder_MB": list(LADDER_MB),
                    "client_timeout_s": LADDER_TIMEOUT,
                    "the SDK's own default, for comparison": DEFAULT_CLIENT_TIMEOUT,
                },
                returns={
                    "rungs": ladder,
                    "largest that finished": max(
                        [rung["megabytes"] for rung in ladder if rung["ok"]], default=None
                    ),
                    "not tried": untried or "none — the ladder ran to the end",
                    "why it stops at the first failure": "every rung past it fails the same "
                    "way for the same reason, at minutes apiece",
                },
            )
            for rung in ladder:
                if rung["ok"] or rung.get("status_code"):
                    continue
                violations.append(
                    f"a {rung['megabytes']}MB upload failed as {rung['raised']}: "
                    f"{rung['message']!r} — even at a {LADDER_TIMEOUT:.0f}s timeout that is "
                    f"the client's own socket giving up rather than the service naming a "
                    f"limit, so a caller learns nothing about the real ceiling and nothing "
                    f"points at AgentBoxClient(timeout=...)"
                )

        with allure.step("2. downloading a path that does not exist"):
            outcome = attempt(lambda: sandbox.download_file(path="/tmp/f04-not-here.bin"))
            show(
                "download a missing file",
                method="sandbox.download_file(path='/tmp/f04-not-here.bin')",
                returns=reportable(outcome),
            )
            if outcome["ok"]:
                violations.append("downloading a file that does not exist returned a body")
            else:
                if outcome["elapsed_s"] >= REFUSAL_MUST_BE_WITHIN:
                    violations.append(
                        f"the missing-file refusal took {outcome['elapsed_s']}s, which is a "
                        f"hang rather than an answer"
                    )
                if not outcome["status_code"]:
                    violations.append(
                        f"the missing-file failure arrived as {outcome['raised']} with no "
                        f"status code ({outcome['message']!r}), so a caller cannot tell "
                        f"'no such file' from a network problem"
                    )

        with allure.step("3. downloading a directory"):
            outcome = attempt(lambda: sandbox.download_file(path="/tmp"))
            show(
                "download a directory",
                method="sandbox.download_file(path='/tmp')",
                returns=reportable(outcome),
            )
            if outcome["ok"]:
                violations.append(
                    "downloading a directory returned a body; a caller writing that to disk "
                    "gets something that is not the directory and no indication of it"
                )
            else:
                if outcome["elapsed_s"] >= REFUSAL_MUST_BE_WITHIN:
                    violations.append(
                        f"the directory refusal took {outcome['elapsed_s']}s, which is a hang "
                        f"rather than an answer"
                    )
                if not outcome["status_code"]:
                    violations.append(
                        f"the directory failure arrived as {outcome['raised']} with no status "
                        f"code ({outcome['message']!r}), so a caller cannot tell 'that is a "
                        f"directory' from a network problem"
                    )

        with allure.step("4. a download whose connection dies halfway"):
            sh(sandbox, f"dd if=/dev/urandom of={probe} bs=1M count=4 2>/dev/null")
            whole = file_state(sandbox, probe)
            cut_client = bound_to(
                AgentBoxClient(timeout=TRANSFER_TIMEOUT, transport=short_read_transport(CUT_AT)),
                sandbox,
            )
            outcome = attempt(lambda: cut_client.download_file(path=probe))
            delivered = len(outcome["value"].content) if outcome["ok"] else None
            # The response carries no Content-Length for a body this size —
            # it is chunked — so the file on the sandbox is what "the whole
            # thing" means here. Reading the header instead would compare a
            # real byte count against None and call every download short.
            declared = _content_length(outcome["value"]) if outcome["ok"] else None
            expected = whole.get("size")
            show(
                "download cut short on the wire",
                method="sandbox.download_file(...) on a client whose transport stops reading",
                args={
                    "path": probe,
                    "bytes on the sandbox": whole.get("size"),
                    "how much the transport lets through": (
                        f"{CUT_AT:.0%} of Content-Length, or 1KB when the response is chunked "
                        f"— and these responses are chunked, so expect the 1KB branch"
                    ),
                },
                returns={
                    "outcome": reportable(outcome),
                    "bytes delivered": delivered,
                    "bytes on the sandbox": expected,
                    "Content-Length the response carried": (
                        declared if declared is not None else "absent (chunked)"
                    ),
                    "the file itself": whole,
                },
            )
            # Raising is a fine answer, and so is the whole file; only a short
            # body handed back as a successful download is unacceptable.
            if outcome["ok"] and delivered != expected:
                violations.append(
                    f"the download was cut off after {delivered} of {expected} bytes and still "
                    f"came back as an ordinary FileDownload — the caller is handed a fragment "
                    f"with nothing on it to say so, and no Content-Length to check it against"
                )

        with allure.step("5. every limit and failure answered for itself"):
            show(
                "what each limit and failure said",
                method="(none) - collecting the four legs above",
                returns={
                    "violations": violations or "none",
                    "legs run": [
                        "the upload ladder",
                        "a download of a missing path",
                        "a download of a directory",
                        "a download cut in half",
                    ],
                },
            )
            assert not violations, (
                f"{len(violations)} limit(s) or failure(s) did not answer clearly: "
                + "; ".join(violations)
            )
    finally:
        discard_paths(sandbox, probe)


# --------------------------------------------------------------------------
# F-05 replaying the same upload
# --------------------------------------------------------------------------


@allure.feature(FEATURE)
@allure.story("F-05 replaying an upload under one idempotency key")
@allure.severity(P1)
def test_f05_replaying_an_upload_has_no_idempotency_key(transfer_sandbox):
    sandbox = transfer_sandbox
    path = "/tmp/f05-replayed.txt"
    body = b"written once\n"

    try:
        with allure.step("1. look for a parameter that could carry an idempotency key"):
            hints = (
                "idempotency", "idempotent", "request_id", "requestid", "key", "token", "nonce",
            )
            surfaces = {
                "Sandbox.upload_file": list(
                    inspect.signature(Sandbox.upload_file).parameters
                ),
                "SandboxCollection.upload_file": list(
                    inspect.signature(SandboxCollection.upload_file).parameters
                ),
            }
            found = {
                where: [name for name in names if any(hint in name.lower() for hint in hints)]
                for where, names in surfaces.items()
            }
            show(
                "every parameter the upload calls accept",
                method="inspect.signature(Sandbox.upload_file), then the same on "
                "SandboxCollection.upload_file",
                args={"searched_for": hints},
                returns={"parameters": surfaces, "matches": found},
            )

        with allure.step("2. send the same upload twice"):
            first = attempt(lambda: sandbox.upload_file(path=path, file=body))
            second = attempt(lambda: sandbox.upload_file(path=path, file=body))
            state = file_state(sandbox, path)
            contents = sh(sandbox, f"cat {path}")
            show(
                "the same upload, replayed",
                method="sandbox.upload_file(path=<same>, file=<same>) twice",
                args={"path": path, "bytes": len(body)},
                returns={
                    "first": reportable(first),
                    "second": reportable(second),
                    "the file afterwards": state,
                    "contents": contents,
                },
            )
            assert state["exists"], "the replayed upload left no file"
            assert state["size"] == len(body), (
                f"the file holds {state['size']} bytes after two uploads of {len(body)} — the "
                f"replay was applied twice, so the effect is not at-most-once"
            )

        with allure.step("3. nothing carries a key, so nothing enforces at-most-once"):
            assert not any(found.values()), (
                f"an idempotency parameter exists after all: {found} — this row is written "
                f"for an SDK that has none, and should be replaced by a real replay test"
            )
    finally:
        discard_paths(sandbox, path)
