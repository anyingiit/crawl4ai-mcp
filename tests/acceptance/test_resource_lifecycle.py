"""Live verification of the systemd service's resource contract.

Requires the deployed user service (scripts/install-user-service.sh) and
CRAWL4AI_MCP_LIVE_TESTS=1. Idle-reap assertions take about nine minutes by
design (5 idle + 4 idle) and are ordered.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

LIVE = os.environ.get("CRAWL4AI_MCP_LIVE_TESTS") == "1"
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set CRAWL4AI_MCP_LIVE_TESTS=1 to run"),
]

SERVICE = "crawl4ai-mcp.service"
CGROUP = Path(
    f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/"
    f"user@{os.getuid()}.service/app.slice/{SERVICE}"
)
IDLE_MEMORY_LIMIT = 120 * 1024 * 1024
MEMORY_MAX = 2_684_354_560
CHROMIUM_MARKERS = ("chrome", "chromium", "headless_shell", "firefox")


def systemctl_show(field: str) -> str:
    out = subprocess.check_output(
        ["systemctl", "--user", "show", SERVICE, "-p", field], text=True
    )
    return out.strip().split("=", 1)[1]


def cgroup_memory() -> int:
    current = CGROUP / "memory.current"
    if current.exists():
        return int(current.read_text())
    return int(systemctl_show("MemoryCurrent"))


def cgroup_browser_processes() -> list[int]:
    procs = (CGROUP / "cgroup.procs").read_text().split()
    found = []
    for pid in procs:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        if any(marker in comm for marker in CHROMIUM_MARKERS):
            found.append(int(pid))
    return found


def request_js_page() -> dict:
    import asyncio
    import json

    import tomllib
    from fastmcp import Client

    targets = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "tests" / "acceptance" / "targets.toml").read_text()
    )

    async def _call():
        async with Client("http://127.0.0.1:11236/mcp") as client:
            result = await client.call_tool(
                "scrape",
                {
                    "url": targets["js_quotes"]["url"],
                    "max_tier": "stealth",
                    "force_tier": "stealth",
                },
            )
            return json.loads(result.content[0].text)

    return asyncio.run(_call())


def test_idle_after_five_minutes_memory_and_processes():
    assert systemctl_show("ActiveState") == "active"
    time.sleep(5 * 60)
    memory = cgroup_memory()
    browsers = cgroup_browser_processes()
    assert memory < IDLE_MEMORY_LIMIT, f"idle memory {memory} above 120 MiB"
    assert browsers == [], f"idle browser processes still alive: {browsers}"


def test_browser_appears_after_tier1_request():
    result = request_js_page()
    assert result.get("status") == "success", result.get("error")
    browser_pids = []
    for _ in range(60):
        browser_pids = cgroup_browser_processes()
        if browser_pids:
            break
        time.sleep(1)
    assert browser_pids, "no browser process after Tier 1 request"


def test_browser_disappears_after_four_idle_minutes():
    time.sleep(4 * 60)
    browsers = cgroup_browser_processes()
    memory = cgroup_memory()
    assert browsers == [], f"browser not reaped: {browsers}"
    assert memory < IDLE_MEMORY_LIMIT, f"memory {memory} above 120 MiB after reap"


def test_memory_max_equals_two_point_five_gib():
    assert int(systemctl_show("MemoryMax")) == MEMORY_MAX
    assert int(systemctl_show("MemoryHigh")) == 1_610_612_736
    assert int(systemctl_show("MemorySwapMax")) == 0


def test_restart_self_healing():
    main_pid = int(systemctl_show("MainPID"))
    assert main_pid > 0
    os.kill(main_pid, 9)
    new_pid = main_pid
    for _ in range(30):
        time.sleep(1)
        new_pid = int(systemctl_show("MainPID"))
        if new_pid != main_pid and systemctl_show("ActiveState") == "active":
            break
    assert new_pid != main_pid, "service did not restart after kill -9"
    import httpx

    healthy = False
    for _ in range(30):
        try:
            with httpx.Client() as client:
                health = client.get("http://127.0.0.1:11236/health", timeout=5)
            if health.json() == {"status": "ok"}:
                healthy = True
                break
        except Exception:
            pass
        time.sleep(1)
    assert healthy, "health endpoint not available after restart"
