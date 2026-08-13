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
# The verdict is delegated to scripts/acceptance_account.py, which combines
# the pytest exit status with the JUnit counts. A nonzero pytest status
# (interrupted, internal error, no tests collected) or a junit with zero
# tests can never produce exit 0; missing evidence exits 2 when pytest
# reported success.
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

set +e
"$PYTHON" "$ROOT/scripts/acceptance_account.py" "$JUNIT" "$PYTEST_STATUS" \
  2>&1 | tee -a "$LOG"
ACCOUNT_STATUS=${PIPESTATUS[0]}
set -e

echo "junit=$JUNIT" | tee -a "$LOG"
exit "$ACCOUNT_STATUS"
