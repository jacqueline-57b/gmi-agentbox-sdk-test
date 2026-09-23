#!/usr/bin/env python3
"""Turn an Allure result directory into the spreadsheet the PRD review reads.

The layout mirrors docs/TC01-google-sheet-rows.en.csv: one header row per case
(ID, priority, title, verdict) followed by one indented row per step, so a
reviewer can scan verdicts down the left and open a single case for detail.

Two halves of that sheet come from two different places, and the split is the
whole reason this script exists:

  * what the run OBSERVED — status, duration, per-step SDK calls and their
    returns — is read back out of allure-results, never re-derived here;
  * what the PRD EXPECTS is prose that no run can know. Case-level expectations
    are parsed from the test module's own docstring table, which is the one
    machine-readable copy of those PRD rows. Step-level expectations have no
    such source, so the column is left empty for a human to fill.

Leaving that column empty is deliberate. A generated sheet that invents an
expectation reads exactly like one a reviewer wrote, and the reviewer is the
only one who can tell the PRD what it promised.

Usage
-----
    # export whatever is already in allure-results (cheap, no live calls)
    scripts/export_report.py --out docs/B0-report.xlsx

    # run the module first, then export (LIVE: creates agents, may bill)
    scripts/export_report.py --run tests/test_b0_agent_registration.py \
        --out docs/B0-report.xlsx

    # add a second chapter as its own tab, leaving the first one in place
    scripts/export_report.py --module tests/test_b1_sandbox_startup.py \
        --out docs/TC01-B0-report.xlsx --sheet test_b1_sandbox_startup

Export is separate from running on purpose: a live run costs money and minutes,
so reformatting the sheet must never require repeating one.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The sheet is read in UTC+8; the CSV it mirrors stamps that offset explicitly.
REPORT_TZ = timezone(timedelta(hours=8))
REPORT_TZ_LABEL = "UTC+8"

COLUMNS = ("ID", "Priority", "Test Case", "Test Steps", "Expected Result", "Actual Result")

# Excel refuses a cell over 32767 characters and corrupts the file rather than
# saying so, so evidence is capped below that. The cut is announced in the cell
# itself: a sheet that silently drops half a payload reads as if the run only
# ever returned the half that is shown.
MAX_CELL = 30000

# Attachment names allure-pytest adds by itself, which are not SDK calls.
CAPTURE_ATTACHMENTS = {"stdout", "stderr", "log"}

# allure severity -> the priority column the PRD rows use
SEVERITY_TO_PRIORITY = {"blocker": "P0", "critical": "P1", "normal": "P2", "minor": "P3"}

# allure status -> the verdict the sheet leads with
STATUS_LABEL = {
    "passed": "PASSED",
    "failed": "FAILED",
    "broken": "BROKEN",
    "skipped": "SKIPPED",
}

# "T-01 register, then poll the build to ready" -> ("T-01", "register, then ...")
STORY_RE = re.compile(r"^(?P<id>[A-Z]+-\d+[a-z]?)\s+(?P<title>.*)$")
# A row of the PRD table in a test module's docstring.
PRD_ROW_RE = re.compile(r"^\|(?P<cells>.*)\|\s*$")
# Agent slugs the run created, recovered from attachment text for the audit trail.
# Anchored on the run-id tail conftest's `run_id` fixture appends (MMDD-HHMM-xxxx)
# rather than on the `sdk-test-` prefix alone: BAD_IMAGE is spelled
# `gmi-sdk-test-no-such-image`, and a looser pattern lists that as an agent the
# run created, pointing the audit trail at a resource that never existed.
# The segments between prefix and run id are optional because how many there
# are is up to the suite: B-0 names an agent per case (`sdk-test-b0-t01-...`)
# while B-1 shares one session agent and names it `sdk-test-<run id>`. Pinning
# two segments dropped B-1's agent from the trail entirely.
SLUG_RE = re.compile(r"sdk-test-(?:[a-z0-9]+-)*\d{4}-\d{4}-[0-9a-f]{4}")


# --------------------------------------------------------------------------
# allure-results -> cases
# --------------------------------------------------------------------------


def _label(result: Dict[str, Any], name: str) -> Optional[str]:
    for label in result.get("labels") or []:
        if label.get("name") == name:
            return label.get("value")
    return None


def _tags(result: Dict[str, Any]) -> List[str]:
    return [l.get("value") for l in result.get("labels") or [] if l.get("name") == "tag"]


def _attachment_text(results_dir: Path, attachment: Dict[str, Any]) -> str:
    source = attachment.get("source")
    if not source:
        return ""
    path = results_dir / source
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sdk_call(text: str) -> str:
    """The `CALL ...` line show() wrote, which names the SDK method invoked."""
    for line in text.splitlines():
        if line.startswith("CALL "):
            return line[len("CALL ") :].strip()
    return ""


def _returns_block(text: str) -> str:
    """Everything show() printed under RETURNS, minus the rule lines."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("RETURNS <<"))
    except StopIteration:
        return ""
    body = [line for line in lines[start + 1 :] if not line.startswith("---")]
    return "\n".join(body).strip()


def load_cases(results_dir: Path, suite: Optional[str]) -> List[Dict[str, Any]]:
    """Read every *-result.json, newest run wins when a test was run twice."""
    by_test: Dict[str, Dict[str, Any]] = {}

    for path in sorted(results_dir.glob("*-result.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if suite and _label(result, "suite") != suite:
            continue

        name = result.get("fullName") or result.get("name") or path.stem
        previous = by_test.get(name)
        # A debugging session re-runs rows; the sheet should show the last word.
        if previous is None or (result.get("stop") or 0) > (previous.get("stop") or 0):
            by_test[name] = result

    cases = [_case(results_dir, result) for result in by_test.values()]
    # T-01, T-02, T-04a, T-04b ... sort on the id, falling back to the test name.
    cases.sort(key=lambda case: (case["id"] or "~", case["test_name"]))
    return cases


def _case(results_dir: Path, result: Dict[str, Any]) -> Dict[str, Any]:
    story = _label(result, "story") or ""
    match = STORY_RE.match(story)
    case_id = match.group("id") if match else ""
    title = match.group("title") if match else story or result.get("name", "")

    steps = [
        _step(results_dir, index, step)
        for index, step in enumerate(result.get("steps") or [], start=1)
    ]
    # Rows that never opened an allure.step still attach their SDK calls at the
    # top level; each attachment is one call, so it becomes one row.
    # A row that opened no allure.step has no per-step verdicts to report: its
    # attachments are recorded SDK calls, not assertions. They still become rows
    # (that is the detail a reviewer wants), but with an empty status rather
    # than a fabricated one, and _verdict() counts them as calls, not as steps.
    if not steps:
        recorded = [
            att
            for att in result.get("attachments") or []
            # allure-pytest attaches the captured streams to every row; they are
            # the terminal transcript, not a call the case made.
            if att.get("name") not in CAPTURE_ATTACHMENTS
        ]
        steps = [
            _step(results_dir, index, {"name": att.get("name", ""), "attachments": [att]})
            for index, att in enumerate(recorded, start=1)
        ]

    start_ms, stop_ms = result.get("start"), result.get("stop")
    duration = (stop_ms - start_ms) / 1000.0 if (start_ms and stop_ms) else None
    started = (
        datetime.fromtimestamp(start_ms / 1000, tz=REPORT_TZ) if start_ms else None
    )

    slugs: List[str] = []
    for step in steps:
        for slug in SLUG_RE.findall(step["evidence"]):
            if slug not in slugs:
                slugs.append(slug)

    details = result.get("statusDetails") or {}
    return {
        "id": case_id,
        "title": title,
        "test_name": result.get("name", ""),
        "priority": SEVERITY_TO_PRIORITY.get(_label(result, "severity") or "", ""),
        "status": STATUS_LABEL.get(result.get("status", ""), (result.get("status") or "").upper()),
        "tags": _tags(result),
        "duration": duration,
        "started": started,
        "slugs": slugs,
        "failure": (details.get("message") or "").strip(),
        "steps": steps,
    }


def _step(results_dir: Path, index: int, step: Dict[str, Any]) -> Dict[str, Any]:
    texts = [_attachment_text(results_dir, att) for att in step.get("attachments") or []]
    calls = [call for call in (_sdk_call(text) for text in texts) if call]
    returns = [block for block in (_returns_block(text) for text in texts) if block]
    return {
        "index": index,
        "name": re.sub(r"^\d+\.\s*", "", step.get("name", "")),
        "status": STATUS_LABEL.get(step.get("status", ""), (step.get("status") or "").upper()),
        "sdk": "; ".join(dict.fromkeys(calls)),
        "evidence": "\n\n".join(returns),
    }


# --------------------------------------------------------------------------
# the PRD table in a test module's docstring -> expectations
# --------------------------------------------------------------------------


def load_prd_rows(module: Path) -> Dict[str, Dict[str, str]]:
    """Parse the `| ID | P | Action | Expected |` table out of the docstring.

    Continuation rows carry an empty ID and belong to the row above, which is
    how the table wraps long expectations; they are folded back together here.
    """
    try:
        source = module.read_text(encoding="utf-8")
    except OSError:
        return {}

    rows: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None

    for line in source.splitlines():
        # The docstring is the first thing in the file; the table ends with it.
        if line.startswith('"""') and rows:
            break
        match = PRD_ROW_RE.match(line.strip())
        if not match:
            continue
        cells = [cell.strip() for cell in match.group("cells").split("|")]
        if len(cells) < 4 or set("".join(cells)) <= set("-: "):
            continue  # the separator row, or something that is not this table
        row_id, priority, action, expected = cells[0], cells[1], cells[2], cells[3]
        if row_id.upper() in {"ID"}:
            continue  # the header row

        if row_id:
            current = row_id
            rows[current] = {"priority": priority, "action": action, "expected": expected}
        elif current:  # a wrapped continuation of the row above
            for key, value in (("action", action), ("expected", expected)):
                if value:
                    rows[current][key] = f"{rows[current][key]} {value}".strip()

    return rows


def _prd_for(case_id: str, prd: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """T-04a and T-04b are two halves of the PRD's single T-04 row."""
    if case_id in prd:
        return prd[case_id]
    return prd.get(case_id.rstrip("abcdefgh"), {})


# --------------------------------------------------------------------------
# the test source -> the assertions each step makes
# --------------------------------------------------------------------------
#
# The Expected Result column asks what a step was supposed to prove, and the
# assertions are the only copy of that written in a form the run must obey.
# They are read out of the source rather than the run, because an assertion
# that held leaves no trace in allure-results — only a failing one does, and a
# column that filled in only for failures would read as if passing steps
# expected nothing.
#
# `pytest.fail()` guards are NOT captured: they read as control flow around the
# real assertion (T-04a fails early on a timeout before asserting the reason),
# and hoisting them into the expectation would overstate what the step pins.


def _assert_text(node: "ast.Assert", source: str) -> str:
    test = ast.get_source_segment(source, node.test) or ""
    text = f"assert {' '.join(test.split())}"
    if node.msg is not None:
        msg = ast.get_source_segment(source, node.msg) or ""
        text += f"\n  -> {' '.join(msg.split())}"
    return text


def _raises_text(node: "ast.With", source: str) -> Optional[str]:
    for item in node.items:
        call = item.context_expr
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "raises":
            args = ", ".join(ast.get_source_segment(source, a) or "" for a in call.args)
            return f"expects {args} to be raised"
    return None


def _step_title(node: "ast.With", source: str) -> Optional[str]:
    """The literal title of a `with allure.step("...")` block, if it is one."""
    del source
    for item in node.items:
        call = item.context_expr
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "step" and call.args:
            arg = call.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return re.sub(r"^\d+\.\s*", "", arg.value)
    return None


def _show_title(node: ast.AST) -> Optional[str]:
    """The literal title of a `show("...")` call.

    f-string titles (`show(f"build finished [{agent.slug}]")`) deliberately
    return None: they are written by the shared helpers, so the assertion that
    checks them lives in the test body, not beside the call.
    """
    call = node.value if isinstance(node, ast.Expr) else node
    if not isinstance(call, ast.Call):
        return None
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name != "show" or not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _collect(
    body: List[ast.AST],
    source: str,
    out: Dict[str, List[str]],
    current: List[str],
    in_step: bool = False,
) -> None:
    """Walk a function body, filing each assertion under the step it follows.

    Two shapes appear in this suite and both are handled here: a row that opens
    `with allure.step(...)` blocks (the assertion belongs to the block it sits
    in), and a row that just calls `show()` and then asserts (the assertion
    belongs to the most recent `show()`). Anything asserted before either one
    is filed under "" and lands on the case header row instead.

    Inside a step block the step wins: those blocks call `show()` too, and
    letting the call retitle the segment would file the step's own assertion
    under the call instead, leaving the step reading as if it checked nothing.
    """
    for node in body:
        if isinstance(node, ast.With):
            title = _step_title(node, source)
            raises = _raises_text(node, source)
            if raises:
                out.setdefault(current[0], []).append(raises)
            if title is not None:
                # A step block owns its body and nothing after it, so the title
                # is restored on the way out.
                out.setdefault(title, [])
                previous = current[0]
                current[0] = title
                _collect(node.body, source, out, current, True)
                current[0] = previous
            else:
                _collect(node.body, source, out, current, in_step)
            continue

        title = _show_title(node)
        if title is not None and not in_step:
            current[0] = title
            out.setdefault(title, [])
            continue

        if isinstance(node, ast.Assert):
            out.setdefault(current[0], []).append(_assert_text(node, source))
            continue

        # Asserts hide inside loops, try/except and if-branches; keep walking.
        # Each branch gets its own copy of the current title: a `show()` in a
        # branch that did not run would otherwise retitle everything after the
        # block, filing later assertions under a step the row never reached.
        for field in ("body", "orelse", "finalbody"):
            nested = getattr(node, field, None)
            if isinstance(nested, list):
                _collect(nested, source, out, list(current), in_step)
        for handler in getattr(node, "handlers", []) or []:
            _collect(handler.body, source, out, list(current), in_step)


def load_assertions(module: Path) -> Dict[str, Dict[str, List[str]]]:
    """test function name -> {step or show title -> the assertions it makes}."""
    try:
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return {}

    found: Dict[str, Dict[str, List[str]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            per_step: Dict[str, List[str]] = {}
            _collect(node.body, source, per_step, [""])
            found[node.name] = per_step
    return found


# --------------------------------------------------------------------------
# cases -> rows
# --------------------------------------------------------------------------


def _verdict(case: Dict[str, Any]) -> str:
    """The Actual Result cell of a case header row: verdict plus audit trail."""
    steps = case["steps"]
    graded = [step for step in steps if step["status"]]
    parts = [case["status"]]
    if graded:
        passed = sum(1 for step in graded if step["status"] == "PASSED")
        parts[0] += f" - {passed} of {len(graded)} steps"
    elif steps:
        parts[0] += f" - {len(steps)} SDK calls recorded"
    if case["started"]:
        parts.append(f"Run {case['started']:%Y-%m-%d %H:%M} ({REPORT_TZ_LABEL})")
    if case["duration"] is not None:
        parts.append(f"{case['duration']:.2f}s")
    if case["slugs"]:
        parts.append("agent " + ", ".join(case["slugs"]))
    if "billable" in case["tags"]:
        parts.append("billable - launched a real sandbox")
    if case["failure"]:
        parts.append(f"Failure: {case['failure']}")
    return ". ".join(parts)


def _capped(text: str) -> str:
    if len(text) <= MAX_CELL:
        return text
    dropped = len(text) - MAX_CELL
    return text[:MAX_CELL] + f"\n... [{dropped} more characters, see the Allure report]"


def build_rows(
    cases: List[Dict[str, Any]],
    prd: Dict[str, Dict[str, str]],
    assertions: Dict[str, Dict[str, List[str]]],
) -> List[Tuple]:
    rows: List[Tuple] = [COLUMNS]

    for case in cases:
        prd_row = _prd_for(case["id"], prd)
        per_step = assertions.get(case["test_name"], {})

        # Assertions the source made outside any step land on the header row,
        # under the PRD expectation they serve.
        case_expected = [prd_row.get("expected", "")]
        if per_step.get(""):
            case_expected.append("Asserted by the row itself:\n" + "\n".join(per_step[""]))

        rows.append(
            (
                case["id"],
                case["priority"] or prd_row.get("priority", ""),
                case["title"],
                prd_row.get("action", ""),
                "\n\n".join(part for part in case_expected if part),
                _verdict(case),
            )
        )
        for step in case["steps"]:
            step_label = f"{step['index']}. {step['name']}"
            if step["sdk"]:
                step_label += f"\nSDK: {step['sdk']}"
            actual = "\n".join(
                part for part in (step["status"], _capped(step["evidence"])) if part
            )
            # What the source pins for this step. A step with nothing here
            # asserted nothing: it only recorded a call, which is worth seeing.
            checks = per_step.get(step["name"]) or []
            expected = "\n".join(checks) if checks else "(records the call; asserts nothing)"
            rows.append(("", "", "", step_label, _capped(expected), actual))

    return rows


# --------------------------------------------------------------------------
# rows -> xlsx
# --------------------------------------------------------------------------


def _open_book(out: Path, sheet_title: str):
    """Return (workbook, sheet) for `sheet_title`, adding to `out` if it exists.

    One workbook holds one chapter's tabs, so an export must add a tab rather
    than replace the file: writing B-1 must not take B-0's tab with it. A tab
    of the same name is dropped first and recreated in the same position, so
    re-exporting a suite refreshes its own tab instead of leaving a second
    `suite1` beside it.
    """
    from openpyxl import Workbook, load_workbook

    title = sheet_title[:31]  # Excel's hard limit on sheet names
    if not out.exists():
        book = Workbook()
        sheet = book.active
        sheet.title = title
        return book, sheet

    book = load_workbook(out)
    if title in book.sheetnames:
        index = book.sheetnames.index(title)
        del book[title]
    else:
        index = len(book.sheetnames)
    return book, book.create_sheet(title, index)


def write_xlsx(rows: List[Tuple], out: Path, sheet_title: str) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    out.parent.mkdir(parents=True, exist_ok=True)
    # Announced because the two outcomes look identical afterwards and are not
    # at all the same thing: a typo'd or since-renamed --out silently starts a
    # fresh workbook holding one tab, which reads like the other chapters were
    # dropped rather than like the file was never there.
    print(
        f"{'adding a tab to' if out.exists() else 'creating'} {out}", flush=True
    )
    book, sheet = _open_book(out, sheet_title)

    header_fill = PatternFill("solid", fgColor="1F3864")
    header_font = Font(bold=True, color="FFFFFF")
    case_fill = PatternFill("solid", fgColor="D9E2F3")
    verdict_colour = {
        "PASSED": "1E7B34",
        "FAILED": "B00020",
        "BROKEN": "B00020",
        "SKIPPED": "7F6000",
    }
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    top_left = Alignment(vertical="top", wrap_text=True)

    for row in rows:
        sheet.append(row)

    for index, cell in enumerate(sheet[1], start=1):
        cell.fill, cell.font = header_fill, header_font
        cell.alignment = Alignment(vertical="center", horizontal="center")
        del index

    for row in sheet.iter_rows(min_row=2):
        is_case_header = bool(row[0].value)  # only case rows carry an ID
        for cell in row:
            cell.alignment = top_left
            cell.border = border
            if is_case_header:
                cell.fill = case_fill
                cell.font = Font(bold=True)
        if is_case_header:
            verdict = str(row[5].value or "").split(" ")[0]
            if verdict in verdict_colour:
                row[5].font = Font(bold=True, color=verdict_colour[verdict])

    for column, width in zip("ABCDEF", (9, 9, 34, 52, 52, 78)):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:F{sheet.max_row}"

    book.save(out)


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def run_pytest(target: str, results_dir: Path, *, clean: bool = True) -> int:
    """Run one module into the result dir. Failures are expected output.

    `clean` is on by default so a re-run of the same module cannot leave a
    deleted row behind in the sheet. Turn it off to add a second suite's
    results beside a first one's — each export filters by suite anyway, so one
    directory can hold every chapter that has been run.
    """
    command = [
        sys.executable if "venv" in sys.executable else str(PROJECT_ROOT / ".venv/bin/python"),
        "-m",
        "pytest",
        target,
        f"--alluredir={results_dir}",
    ]
    if clean:
        command.append("--clean-alluredir")
    print(f"$ {' '.join(command)}", flush=True)
    # A red row is a finding, not a reason to skip the export, so the exit code
    # is reported and then deliberately ignored.
    return subprocess.call(command, cwd=PROJECT_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--run",
        metavar="PYTEST_TARGET",
        help="run this pytest target first (LIVE: creates agents and may bill)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=PROJECT_ROOT / "allure-results",
        help="allure result directory to read (default: allure-results)",
    )
    parser.add_argument(
        "--module",
        type=Path,
        help="test module whose docstring holds the PRD table "
        "(default: the --run target, when it is a file)",
    )
    parser.add_argument(
        "--suite",
        help="keep only this allure suite, e.g. test_b0_agent_registration "
        "(default: derived from --module or --run)",
    )
    parser.add_argument(
        "--sheet",
        help="tab to write inside --out, replacing it if it is already there "
        "(default: the suite name). Other tabs in the file are left alone.",
    )
    parser.add_argument(
        "--keep-results",
        action="store_true",
        help="with --run, add to --results instead of emptying it first, so a "
        "second suite's results sit beside a first one's",
    )
    parser.add_argument("--out", type=Path, required=True, help="xlsx file to write")
    args = parser.parse_args()

    module = args.module
    if module is None and args.run and args.run.endswith(".py"):
        module = Path(args.run)

    suite = args.suite
    if suite is None and module is not None:
        suite = module.stem

    if args.run:
        exit_code = run_pytest(args.run, args.results, clean=not args.keep_results)
        print(f"pytest exited {exit_code}; exporting whatever it recorded", flush=True)

    if not args.results.is_dir():
        parser.error(
            f"{args.results} does not exist — run with --run, or with "
            f"`pytest --alluredir={args.results}` first"
        )

    cases = load_cases(args.results, suite)
    if not cases and suite:
        # Not an error: one workbook holds a tab per chapter, and a chapter
        # that was not part of the last run simply has nothing new to say. Its
        # tab stays as the run that did produce it left it. Failing here made
        # a whole-workbook refresh die on the first chapter that had not been
        # re-run, which is the normal case once `--clean-alluredir` has been
        # used for a single suite.
        print(
            f"no results for suite {suite!r} in {args.results} — leaving its tab in "
            f"{args.out} untouched. Re-run that module (with --keep-results, so the "
            f"other suites' results survive) to refresh it."
        )
        return 0
    if not cases:
        parser.error(
            f"no results in {args.results}. A bare `pytest` writes nothing; "
            f"it needs --alluredir."
        )

    prd = load_prd_rows(PROJECT_ROOT / module) if module else {}
    if module and not prd:
        print(f"warning: no PRD table found in {module}; Expected Result will be empty")

    assertions = load_assertions(PROJECT_ROOT / module) if module else {}
    rows = build_rows(cases, prd, assertions)
    sheet = args.sheet or suite or "results"
    write_xlsx(rows, args.out, sheet)

    print(
        f"\nwrote {args.out} [{sheet}] — {len(cases)} cases, "
        f"{len(rows) - 1 - len(cases)} steps"
    )
    for case in cases:
        note = " [expected result not in the PRD table]" if not _prd_for(case["id"], prd) else ""
        print(f"  {case['id'] or '?':6} {case['status']:8} {case['title']}{note}")
    pinned = sum(
        1
        for case in cases
        for step in case["steps"]
        if (assertions.get(case["test_name"], {}).get(step["name"]))
    )
    total_steps = sum(len(case["steps"]) for case in cases)
    print(
        f"\nExpected Result: {pinned} of {total_steps} step rows carry the assertions "
        "from the source; the rest only record a call and assert nothing."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
