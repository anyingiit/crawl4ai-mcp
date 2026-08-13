"""Unpaid tests for the acceptance verdict accounting.

Covers the pure verdict matrix (pytest status 0/1/2/3/5 x junit contents) and
the run-acceptance.sh wrapper via a fake python that stands in for pytest.
No live tests run and no network or paid providers are touched.
"""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import acceptance_account as account  # noqa: E402

FAKE_PYTHON_SRC = r'''#!/usr/bin/env python3
"""Fake python for exercising run-acceptance.sh without running live tests.

For the pytest invocation it writes a junit file described by FAKE_* env vars
and exits with FAKE_PYTEST_STATUS. For the acceptance_account.py invocation it
delegates to the real accounting module.
"""
import os
import sys
import xml.etree.ElementTree as ET

args = sys.argv[1:]

account_args = [a for a in args if a.endswith("acceptance_account.py")]
if account_args:
    account_path = account_args[0]
    index = args.index(account_path)
    junit = args[index + 1]
    status = int(args[index + 2])
    sys.path.insert(0, os.path.dirname(account_path))
    import acceptance_account

    sys.exit(acceptance_account.judge(junit, status))

junit = None
for i, arg in enumerate(args):
    if arg.startswith("--junitxml="):
        junit = arg.split("=", 1)[1]
    elif arg == "--junitxml" and i + 1 < len(args):
        junit = args[i + 1]

status = int(os.environ.get("FAKE_PYTEST_STATUS", "0"))
if junit and os.environ.get("FAKE_NO_JUNIT") != "1":
    tests = int(os.environ.get("FAKE_TESTS", "1"))
    failures = int(os.environ.get("FAKE_FAILURES", "0"))
    errors = int(os.environ.get("FAKE_ERRORS", "0"))
    skipped = int(os.environ.get("FAKE_SKIPPED", "0"))
    suite = ET.Element(
        "testsuite",
        {
            "name": "fake",
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    ET.ElementTree(suite).write(junit)
sys.exit(status)
'''


def write_junit(path: Path, tests=1, failures=0, errors=0, skipped=0) -> None:
    suite = ET.Element(
        "testsuite",
        {
            "name": "acc",
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    ET.ElementTree(suite).write(path)


@pytest.mark.parametrize(
    ("status", "tests", "failures", "errors", "skipped", "expected", "fragment"),
    [
        (0, 3, 0, 0, 0, 0, "acceptance complete"),
        (0, 3, 0, 0, 1, 3, "acceptance incomplete"),
        (0, 3, 1, 0, 0, 1, "failure"),
        (0, 3, 0, 1, 0, 1, "error"),
        (0, 0, 0, 0, 0, 1, "no tests"),
        (1, 3, 2, 0, 0, 1, "failure"),
        (1, 3, 0, 0, 1, 1, "pytest exited 1"),
        (2, 3, 0, 0, 0, 1, "abnormal pytest status 2"),
        (3, 3, 0, 0, 0, 1, "abnormal pytest status 3"),
        (5, 3, 0, 0, 0, 1, "abnormal pytest status 5"),
    ],
)
def test_verdict_matrix(
    status, tests, failures, errors, skipped, expected, fragment
):
    code, message = account.verdict(status, tests, failures, errors, skipped)
    assert code == expected
    assert fragment in message


def test_judge_missing_junit_with_pytest_zero(tmp_path):
    assert account.judge(tmp_path / "missing.xml", 0) == 2


def test_judge_missing_junit_with_pytest_nonzero(tmp_path):
    assert account.judge(tmp_path / "missing.xml", 1) == 1


def test_judge_unparseable_junit_with_pytest_zero(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("this is not xml")
    assert account.judge(bad, 0) == 2


def test_judge_unparseable_junit_with_pytest_nonzero(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("this is not xml")
    assert account.judge(bad, 1) == 1


def test_judge_zero_tests_fails(tmp_path):
    junit = tmp_path / "j.xml"
    write_junit(junit, tests=0)
    assert account.judge(junit, 0) == 1


def test_judge_skips_exit_three(tmp_path):
    junit = tmp_path / "j.xml"
    write_junit(junit, tests=3, skipped=1)
    assert account.judge(junit, 0) == 3


def test_judge_failures_exit_one(tmp_path):
    junit = tmp_path / "j.xml"
    write_junit(junit, tests=3, failures=1)
    assert account.judge(junit, 0) == 1


@pytest.mark.parametrize(
    ("env", "expected_exit", "fragment"),
    [
        ({"FAKE_PYTEST_STATUS": "0", "FAKE_TESTS": "1"}, 0, "acceptance complete"),
        (
            {"FAKE_PYTEST_STATUS": "0", "FAKE_TESTS": "1", "FAKE_SKIPPED": "1"},
            3,
            "acceptance incomplete",
        ),
        (
            {"FAKE_PYTEST_STATUS": "0", "FAKE_TESTS": "1", "FAKE_FAILURES": "1"},
            1,
            "acceptance failed",
        ),
        ({"FAKE_PYTEST_STATUS": "0", "FAKE_TESTS": "0"}, 1, "no tests"),
        (
            {"FAKE_PYTEST_STATUS": "1", "FAKE_TESTS": "1", "FAKE_FAILURES": "1"},
            1,
            "acceptance failed",
        ),
        ({"FAKE_PYTEST_STATUS": "2", "FAKE_TESTS": "1"}, 1, "abnormal pytest status"),
        ({"FAKE_PYTEST_STATUS": "3", "FAKE_TESTS": "1"}, 1, "abnormal pytest status"),
        ({"FAKE_PYTEST_STATUS": "5", "FAKE_TESTS": "1"}, 1, "abnormal pytest status"),
        ({"FAKE_PYTEST_STATUS": "0", "FAKE_NO_JUNIT": "1"}, 2, "no junit"),
        ({"FAKE_PYTEST_STATUS": "1", "FAKE_NO_JUNIT": "1"}, 1, "no junit"),
    ],
)
def test_run_acceptance_script_verdicts(tmp_path, env, expected_exit, fragment):
    fake = tmp_path / "fake-python.py"
    fake.write_text(FAKE_PYTHON_SRC, encoding="utf-8")
    fake.chmod(0o755)
    run_env = {
        **os.environ,
        "CRAWL4AI_MCP_LIVE_TESTS": "1",
        "CRAWL4AI_MCP_PYTHON": str(fake),
        "ACCEPTANCE_ARTIFACT_DIR": str(tmp_path / "artifacts"),
        **env,
    }
    completed = subprocess.run(
        [str(ROOT / "scripts" / "run-acceptance.sh")],
        env=run_env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == expected_exit, completed.stdout + completed.stderr
    assert fragment in completed.stdout


def test_run_acceptance_script_requires_optin(tmp_path):
    fake = tmp_path / "fake-python.py"
    fake.write_text(FAKE_PYTHON_SRC, encoding="utf-8")
    fake.chmod(0o755)
    completed = subprocess.run(
        [str(ROOT / "scripts" / "run-acceptance.sh")],
        env={**os.environ, "CRAWL4AI_MCP_PYTHON": str(fake)},
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "CRAWL4AI_MCP_LIVE_TESTS=1" in completed.stderr
