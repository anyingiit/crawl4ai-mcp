from pathlib import Path

import pytest

UNIT_PATH = Path(__file__).resolve().parents[2] / "systemd" / "crawl4ai-mcp.service"


@pytest.fixture
def unit_lines():
    return UNIT_PATH.read_text(encoding="utf-8").splitlines()


def _section(lines, name):
    in_section = False
    for line in lines:
        if line.startswith(f"[{name}]"):
            in_section = True
            continue
        if line.startswith("[") and in_section:
            break
        if in_section and line.strip():
            yield line


def _entries(lines, section):
    entries = {}
    for line in _section(lines, section):
        if "=" in line:
            key, _, value = line.partition("=")
            entries[key.strip()] = value.strip()
    return entries


def test_unit_service_entries_match_resource_contract(unit_lines):
    service = _entries(unit_lines, "Service")
    assert service["Type"] == "exec"
    assert service["WorkingDirectory"] == "%h/Workspace/crawl4ai-mcp"
    assert service["EnvironmentFile"] == "%h/Workspace/crawl4ai-mcp/.env"
    assert service["ExecStart"] == "%h/Workspace/crawl4ai-mcp/.venv/bin/crawl4ai-mcp"
    assert service["Restart"] == "always"
    assert service["RestartSec"] == "5"
    assert service["TimeoutStopSec"] == "30"
    assert service["MemoryHigh"] == "1536M"
    assert service["MemoryMax"] == "2560M"
    assert service["MemorySwapMax"] == "0"
    assert service["KillMode"] == "control-group"


def test_unit_has_no_network_online_dependency(unit_lines):
    joined = "\n".join(unit_lines)
    assert "network-online" not in joined


def test_unit_hardening(unit_lines):
    service = _entries(unit_lines, "Service")
    assert service["UMask"] == "0077"
    assert service["NoNewPrivileges"] == "true"
    assert service["PrivateTmp"] == "true"
    assert service["SyslogIdentifier"] == "crawl4ai-mcp"


def test_unit_installs_into_default_target(unit_lines):
    install = _entries(unit_lines, "Install")
    assert install["WantedBy"] == "default.target"
