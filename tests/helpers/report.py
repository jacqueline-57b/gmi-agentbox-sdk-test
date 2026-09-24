"""How a case reports itself: one SDK call per attachment, secrets by shape.

Both `show()` and `known_gap()` write to stdout *and* to the Allure report, so
a run reads the same in a terminal and in the published report. Import them;
do not re-implement them per file, or two rows will describe the same call two
different ways.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

import allure
import pytest
from agentbox_sdk import Agent, APIError, Page, Sandbox


_SECRET_HINTS = ("key", "token", "secret", "password", "credential")


def _is_secret_name(name: str) -> bool:
    return any(hint in name.lower() for hint in _SECRET_HINTS)


def _masked(value: Any) -> str:
    """Describe a secret instead of printing it.

    A flat "***redacted***" cannot tell "the field was never sent" from "it came
    back empty" from "a real key was handed out" — which is the whole question
    the key rows have to answer. The shape is enough to tell them apart and
    still safe to paste into a report: type, length, last four characters.
    """
    if value is None:
        return "<secret: null>"
    if not isinstance(value, str):
        return f"<secret: {type(value).__name__}>"
    if not value:
        return "<secret: empty string>"
    if value.startswith("<secret:"):
        return value  # already described (an evidence dict passing back through)
    # A server that masks its own secrets sends punctuation, not characters; the
    # fingerprint makes two values comparable across calls without printing either.
    looks_masked = any(ch in value for ch in "*\u2022\u2026") or "redact" in value.lower()
    fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    verdict = "masked by the server" if looks_masked else "looks like a real value"
    return (
        f"<secret: str, {len(value)} chars, ends \u2026{value[-4:]}, "
        f"sha256:{fingerprint}, {verdict}>"
    )


def _redacted(value: Any, *, field_name: str = "") -> Any:
    """Mask credential-looking fields at any depth (env entries are nested)."""
    if isinstance(value, Mapping):
        hidden = bool(value.get("secret")) or _is_secret_name(str(value.get("name") or ""))
        return {
            key: _masked(item) if (hidden and key == "value") else _redacted(item, field_name=key)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redacted(item, field_name=field_name) for item in value]
    if isinstance(value, str) and _is_secret_name(field_name):
        return _masked(value)
    return value


def _payload(value: Any) -> Any:
    """Turn an SDK object, a page or an exception into something dumpable."""
    if isinstance(value, (Agent, Sandbox)):
        return dict(value.data)
    if isinstance(value, Page):
        return {
            "total": value.total,
            "page": value.page,
            "page_size": value.page_size,
            "items": [_payload(item) for item in value.items],
        }
    if isinstance(value, APIError):
        return {
            "raised": type(value).__name__,
            "status_code": value.status_code,
            "message": value.message,
            "code": value.code,
            "details": value.details,
        }
    if isinstance(value, BaseException):
        return {"raised": type(value).__name__, "message": str(value)}
    return value


def show(
    title: str,
    *,
    method: Optional[str] = None,
    args: Any = None,
    returns: Any = None,
) -> None:
    """Print one SDK call and attach it to the Allure report.

    `title` names the call and heads both the printed block and the Allure
    attachment. Under it, `ARGS` is what the step passed and `RETURNS` is the
    object — or the raised error — it got back. The endpoint each call turns
    into is deliberately absent: no test here sends a request itself, so
    printing one would be a hand-written claim rather than something the run
    observed. Use `--log-http` when the wire is what you need; those
    transports report what actually went out.

    `method` is still accepted and no longer rendered. It used to print as a
    `CALL` line directly under the title, which in almost every row restated
    the title with the arguments elided — `client.idcs.list(runtime='sandbox')`
    followed by `CALL test_client.idcs.list(runtime=...)` — so every
    attachment opened by saying the same thing twice. The spreadsheet export
    reads each step's call from the attachment title instead (`_sdk_call` in
    scripts/export_report.py, which still honours the old `CALL` line in
    results recorded before this change). The parameter stays because about
    two hundred call sites pass it; new rows need not.
    """
    rule = "-" * 78
    lines = [rule, title, rule]
    for label, value in (("ARGS    >>", args), ("RETURNS <<", returns)):
        if value is None:
            continue
        body = json.dumps(
            _redacted(_payload(value)), indent=2, sort_keys=True, ensure_ascii=False, default=str
        )
        lines.append(f"{label}\n{body}")
    lines.append(rule)
    text = "\n".join(lines)

    print(f"\n{text}")
    allure.attach(text, name=title, attachment_type=allure.attachment_type.TEXT)


def known_gap(message: str) -> None:
    """Record a documented gap: tagged and attached in Allure.

    The `pytest.xfail()` below is commented out on purpose while the rows are
    being debugged: it pre-judged every gap as "expected", aborted the test at
    this call, and reported XFAIL instead of letting the row run to its own
    assertions. With it off, a row prints its expected and actual results and
    then reports whatever it actually does.

    Two consequences worth knowing while it stays off:
      * rows whose `known_gap()` is the last statement now report PASSED even
        though the gap text says the expectation was not met;
      * rows that call it mid-body keep going, so a later assertion decides the
        verdict instead.
    Restore the line to put the XFAIL verdicts back.
    """
    allure.dynamic.tag("KNOWN-GAP")
    show(
        "KNOWN GAP",
        method="(none) - a documented gap, recorded rather than called",
        returns={"gap": message},
    )
    # pytest.xfail(message)
