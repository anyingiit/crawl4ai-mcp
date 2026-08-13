#!/usr/bin/env python3
"""Verdict accounting for crawl4ai-mcp deployment acceptance.

Reads the JUnit XML produced by the acceptance pytest run plus pytest's exit
status, prints counts and a verdict, and returns the acceptance exit code.

Exit codes (mirror run-acceptance.sh):
  0  acceptance complete   (pytest status 0, valid junit, tests > 0,
                            zero failures/errors/skips)
  1  acceptance failed     (abnormal pytest status, no tests executed, or
                            failures/errors recorded)
  2  evidence unusable     (junit missing or unparseable while pytest
                            reported success)
  3  acceptance incomplete (valid run, no failures, at least one skip)

A nonzero pytest status can never produce exit 0, and a junit with zero
tests can never produce exit 0.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def verdict(
    pytest_status: int,
    tests: int,
    failures: int,
    errors: int,
    skipped: int,
) -> tuple[int, str]:
    """Return (exit_code, message) for the given pytest status and counts."""
    if pytest_status not in (0, 1):
        return 1, f"acceptance failed: abnormal pytest status {pytest_status}"
    if tests == 0:
        return 1, "acceptance failed: no tests executed"
    if failures > 0 or errors > 0:
        return 1, (
            f"acceptance failed: {failures} failure(s), {errors} error(s)"
        )
    if pytest_status == 1:
        return (
            1,
            "acceptance failed: pytest exited 1 without recorded failures",
        )
    if skipped > 0:
        return (
            3,
            f"acceptance incomplete: {skipped} criterion/criteria skipped",
        )
    return 0, "acceptance complete"


def _parse_counts(junit_path: Path) -> tuple[int, int, int, int] | None:
    try:
        root = ET.parse(junit_path).getroot()
    except (ET.ParseError, OSError):
        return None
    tests = failures = errors = skipped = 0
    for suite in root.iter("testsuite"):
        tests += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
    return tests, failures, errors, skipped


def judge(junit_path: str | Path, pytest_status: int) -> int:
    """Print counts and verdict for a run; return the acceptance exit code."""
    junit = Path(junit_path)
    if not junit.exists():
        code = 2 if pytest_status == 0 else 1
        print(
            f"acceptance failed: no junit evidence at {junit}"
            f" (pytest status {pytest_status})"
        )
        return code
    counts = _parse_counts(junit)
    if counts is None:
        code = 2 if pytest_status == 0 else 1
        print(
            f"acceptance failed: junit {junit} is unparseable"
            f" (pytest status {pytest_status})"
        )
        return code
    tests, failures, errors, skipped = counts
    print(f"tests={tests} failures={failures} errors={errors} skipped={skipped}")
    code, message = verdict(pytest_status, tests, failures, errors, skipped)
    print(message)
    return code


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            f"usage: {sys.argv[0]} <junit.xml> <pytest_status>", file=sys.stderr
        )
        sys.exit(2)
    sys.exit(judge(sys.argv[1], int(sys.argv[2])))
