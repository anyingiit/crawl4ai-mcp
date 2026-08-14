"""Live verification of real opencode MCP discovery and tool invocation.

Requires the deployed crawl4ai-mcp service on http://127.0.0.1:11236/mcp,
the crawl4ai remote entry in the opencode config
(~/.config/opencode/opencode.jsonc), and CRAWL4AI_MCP_LIVE_TESTS=1.

The invocation pins the scrape to the free http tier
(max_tier="http", force_tier="http") so no paid provider is contacted.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

LIVE = os.environ.get("CRAWL4AI_MCP_LIVE_TESTS") == "1"
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set CRAWL4AI_MCP_LIVE_TESTS=1 to run"),
]

ROOT = Path(__file__).resolve().parents[2]


def opencode_binary() -> str:
    return shutil.which("opencode") or str(Path.home() / ".opencode/bin/opencode")


def crawl4ai_tool_outputs(stdout: str) -> list[str]:
    """Extract crawl4ai_scrape tool outputs from `opencode run --format json`.

    The JSON-lines event stream embeds the tool result as an escaped JSON
    string, so it is parsed rather than matched as text.
    """
    outputs = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part") or {}
        if part.get("type") == "tool" and part.get("tool") == "crawl4ai_scrape":
            output = (part.get("state") or {}).get("output") or part.get("output")
            if output:
                outputs.append(output)
    return outputs


@pytest.mark.acceptance_required
def test_opencode_lists_connected_crawl4ai_server():
    completed = subprocess.run(
        [opencode_binary(), "mcp", "list"],
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    output = completed.stdout + completed.stderr
    assert "crawl4ai" in output and "connected" in output.lower()


@pytest.mark.acceptance_required
def test_opencode_invokes_crawl4ai_scrape():
    prompt = (
        'Use crawl4ai_scrape exactly once for https://example.com/ with '
        'format="markdown", max_tier="http", force_tier="http".'
    )
    completed = subprocess.run(
        [
            opencode_binary(),
            "run",
            "--format",
            "json",
            "--auto",
            "--dir",
            str(ROOT),
            prompt,
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=180,
    )
    assert "crawl4ai_scrape" in completed.stdout
    assert "Example Domain" in completed.stdout
    # The event stream escapes the embedded result JSON, so backslashes and
    # spaces are stripped before the substring check.
    assert '"tier_used":"http"' in completed.stdout.replace("\\", "").replace(" ", "")
    outputs = crawl4ai_tool_outputs(completed.stdout)
    assert outputs, "no crawl4ai_scrape tool result in opencode output"
    result = json.loads(outputs[-1])
    assert result["status"] == "success"
    assert result["tier_used"] == "http"
    assert result["cost_kind"] == "free"
