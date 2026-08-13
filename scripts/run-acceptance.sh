#!/usr/bin/env bash
# Complete deployment acceptance for crawl4ai-mcp.
#
# Opt-in is mandatory: CRAWL4AI_MCP_LIVE_TESTS=1
#
# Exit codes:
#   0  acceptance complete   (zero failures, errors, and skips)
#   1  acceptance failed     (at least one failure or error)
#   2  not opted in or unusable environment
#   3  acceptance incomplete (no failures/errors, but at least one skip)
#
# Skips are allowed only for disabled or unconfigured optional providers
# (camoufox, proxy, rayobyte, firecrawl). Any skip means the deployment is
# not certified: the script exits 3 and prints "acceptance incomplete"
# instead of claiming success.
#
# Evidence is persisted as JUnit XML plus a run log under
# $ACCEPTANCE_ARTIFACT_DIR (default: .superpowers/sdd/acceptance, which is
# git-ignored by the SDD directory's own .gitignore).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${CRAWL4AI_MCP_PYTHON:-$ROOT/.venv/bin/python}"

if [[ "${CRAWL4AI_MCP_LIVE_TESTS:-}" != "1" ]]; then
  echo "run-acceptance.sh: CRAWL4AI_MCP_LIVE_TESTS=1 is required (opt-in)" >&2
  exit 2
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "run-acceptance.sh: python not found at $PYTHON" >&2
  exit 2
fi

ARTIFACT_DIR="${ACCEPTANCE_ARTIFACT_DIR:-$ROOT/.superpowers/sdd/acceptance}"
mkdir -p "$ARTIFACT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
JUNIT="$ARTIFACT_DIR/junit-$STAMP.xml"
LOG="$ARTIFACT_DIR/run-$STAMP.log"

echo "== crawl4ai-mcp acceptance ($STAMP) ==" | tee "$LOG"

set +e
(
  cd "$ROOT"
  CRAWL4AI_MCP_LIVE_TESTS=1 "$PYTHON" -m pytest tests/acceptance -v \
    --junitxml="$JUNIT" -p no:cacheprovider
) 2>&1 | tee -a "$LOG"
PYTEST_STATUS=${PIPESTATUS[0]}
set -e

if [[ "$PYTEST_STATUS" -ne 0 ]]; then
  echo "pytest exited with status $PYTEST_STATUS" | tee -a "$LOG"
fi

if [[ ! -f "$JUNIT" ]]; then
  echo "run-acceptance.sh: no JUnit XML produced; acceptance cannot be judged" | tee -a "$LOG"
  exit 2
fi

read -r TESTS FAILURES ERRORS SKIPPED <<< "$(
  "$PYTHON" - "$JUNIT" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
tests = failures = errors = skipped = 0
for suite in root.iter("testsuite"):
    tests += int(suite.get("tests", 0))
    failures += int(suite.get("failures", 0))
    errors += int(suite.get("errors", 0))
    skipped += int(suite.get("skipped", 0))
print(tests, failures, errors, skipped)
PY
)"

echo "tests=$TESTS failures=$FAILURES errors=$ERRORS skipped=$SKIPPED" | tee -a "$LOG"

if [[ "$FAILURES" -gt 0 || "$ERRORS" -gt 0 ]]; then
  echo "acceptance failed: $FAILURES failure(s), $ERRORS error(s)" | tee -a "$LOG"
  echo "junit=$JUNIT" | tee -a "$LOG"
  exit 1
fi

if [[ "$SKIPPED" -gt 0 ]]; then
  echo "acceptance incomplete: $SKIPPED criterion/criteria skipped" | tee -a "$LOG"
  echo "junit=$JUNIT" | tee -a "$LOG"
  exit 3
fi

echo "acceptance complete" | tee -a "$LOG"
echo "junit=$JUNIT" | tee -a "$LOG"
exit 0
