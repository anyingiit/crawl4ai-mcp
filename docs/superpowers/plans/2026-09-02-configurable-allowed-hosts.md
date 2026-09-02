# Configurable Allowed Hosts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the hard-coded Tailscale FQDN from `mcp.run(allowed_hosts=[...])` and let users inject extra allowed hosts (with glob wildcards) via the untracked `config.toml`.

**Architecture:** Config pass-through. A new non-secret `extra_allowed_hosts: list[str]` field on `AppConfig` is read from `config.toml`, appended to the built-in loopback entries in `__main__.py`, and handed to fastmcp's `HostOriginGuardMiddleware`, which already does glob matching (`fnmatchcase`) and port normalization. No new module, no self-implemented matching.

**Tech Stack:** Python 3.12, pydantic v2, fastmcp 3.4.x, pytest, tomllib.

## Global Constraints

- `extra_allowed_hosts` is **non-secret** — it MUST NOT be added to `SECRET_ENV_VARS`.
- Matching semantics are delegated to fastmcp `_host_matches` → `fnmatchcase`; do NOT implement matching logic in this repo.
- `_normalize_host` strips ports before matching; config entries are hostname patterns only (no port entries needed).
- `bind_host` remains constrained by the existing `loopback_only` validator (`127.0.0.1` only). Do not change it.
- `config.toml` MUST NOT be committed; `config.example.toml` stays tracked as the template.
- Default (no `config.toml` / empty field) behavior MUST remain exactly localhost-only.
- Validator rejects entries that are empty after stripping; `"*"` logs a warning but is allowed.

## Pre-existing failure this plan fixes

`tests/test_main.py::test_run_server_uses_validated_bind_config` currently FAILS on master because `__main__.py` hard-codes the Tailscale FQDN, while the test asserts `allowed_hosts == ["127.0.0.1:12345", "localhost:12345"]`. Task 2 removes the hard-coding and makes this test pass. Do not "fix" the test to keep the FQDN — the test encodes the intended behavior.

---

### Task 1: Add `extra_allowed_hosts` field + validator to AppConfig

**Files:**
- Modify: `src/crawl4ai_mcp/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `AppConfig`, `load_config`, pydantic `field_validator`.
- Produces: `AppConfig.extra_allowed_hosts: list[str]` (default `[]`); entries stripped; empty-after-strip entries raise `ValidationError`; `"*"` triggers a `warnings.warn` (allowed).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_extra_allowed_hosts_default_empty(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config = load_config(path, env={})
    assert config.extra_allowed_hosts == []


def test_extra_allowed_hosts_loads_from_toml(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        'extra_allowed_hosts = ["*.ts.net", "my-host.example.com"]\n',
        encoding="utf-8",
    )
    config = load_config(path, env={})
    assert config.extra_allowed_hosts == ["*.ts.net", "my-host.example.com"]


def test_extra_allowed_hosts_strips_whitespace(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('extra_allowed_hosts = ["  *.ts.net  "]\n', encoding="utf-8")
    config = load_config(path, env={})
    assert config.extra_allowed_hosts == ["*.ts.net"]


def test_extra_allowed_hosts_rejects_empty_entry(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('extra_allowed_hosts = ["*.ts.net", "   "]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_config(path, env={})


def test_extra_allowed_hosts_wildcard_star_warns(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('extra_allowed_hosts = ["*"]\n', encoding="utf-8")
    with pytest.warns(UserWarning, match="host origin protection"):
        load_config(path, env={})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_config.py -k extra_allowed_hosts -v`
Expected: FAIL with `ValidationError` / `AttributeError` mentioning `extra_allowed_hosts` (field does not exist yet).

- [ ] **Step 3: Implement the field + validator**

In `src/crawl4ai_mcp/config.py`, add `import warnings` at the top (with the other stdlib imports). Add the field to `AppConfig` (place near `bind_port`):

```python
    extra_allowed_hosts: list[str] = Field(default_factory=list)
```

Add the validator after the `loopback_only` validator:

```python
    @field_validator("extra_allowed_hosts")
    @classmethod
    def validate_extra_allowed_hosts(cls, value: list[str]) -> list[str]:
        stripped = []
        for entry in value:
            cleaned = entry.strip()
            if not cleaned:
                raise ValueError("extra_allowed_hosts entries must not be empty")
            if cleaned == "*":
                warnings.warn(
                    "extra_allowed_hosts contains '*', which disables host "
                    "origin protection",
                    UserWarning,
                    stacklevel=2,
                )
            stripped.append(cleaned)
        return stripped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_config.py -k extra_allowed_hosts -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/crawl4ai_mcp/config.py tests/test_config.py
git commit -m "feat: add extra_allowed_hosts config field with validation"
```

---

### Task 2: Wire config into `__main__.py` and remove hard-coded FQDN

**Files:**
- Modify: `src/crawl4ai_mcp/__main__.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `AppConfig.extra_allowed_hosts` (Task 1).
- Produces: `run_server` passes `allowed_hosts = [f"{bind_host}:{bind_port}", f"localhost:{bind_port}", *config.extra_allowed_hosts]`; no hard-coded hostnames.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main.py`:

```python
def test_run_server_appends_extra_allowed_hosts(monkeypatch, tmp_path):
    config = AppConfig(
        bind_host="127.0.0.1",
        bind_port=12345,
        database_path=tmp_path / "p.db",
        extra_allowed_hosts=["*.ts.net"],
    )
    fake = FakeMCP()
    monkeypatch.setattr(main_module, "create_server", lambda *_a, **_k: fake)
    main_module.run_server(config, service=FakeService())
    assert fake.run_kwargs["allowed_hosts"] == [
        "127.0.0.1:12345",
        "localhost:12345",
        "*.ts.net",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_main.py::test_run_server_appends_extra_allowed_hosts -v`
Expected: FAIL — `allowed_hosts` currently contains the hard-coded FQDN and not `"*.ts.net"`.

- [ ] **Step 3: Implement the change**

In `src/crawl4ai_mcp/__main__.py`, replace the `allowed_hosts=[...]` block:

```python
        allowed_hosts=[
            f"{config.bind_host}:{config.bind_port}",
            f"localhost:{config.bind_port}",
            *config.extra_allowed_hosts,
        ],
```

(Delete the two hard-coded `instance-20250526-0820.taila20d2.ts.net` lines.)

- [ ] **Step 4: Run the full main test module to verify pass (incl. the pre-existing failure)**

Run: `.venv/bin/python3 -m pytest tests/test_main.py -v`
Expected: ALL PASS, including `test_run_server_uses_validated_bind_config` (which failed before this task).

- [ ] **Step 5: Commit**

```bash
git add src/crawl4ai_mcp/__main__.py tests/test_main.py
git commit -m "feat: wire extra_allowed_hosts into mcp allowed hosts, drop hard-coded FQDN"
```

---

### Task 3: Gitignore protection + example config

**Files:**
- Modify: `.gitignore`
- Modify: `config.example.toml`

**Interfaces:**
- Consumes: none.
- Produces: `config.toml` ignored by git; `config.example.toml` documents the new field.

- [ ] **Step 1: Add `config.toml` to `.gitignore`**

Edit `.gitignore`, add `config.toml` on its own line (near the `.env` entry):

```
config.toml
```

- [ ] **Step 2: Verify git ignores it**

Run:

```bash
touch config.toml && git status --porcelain | grep -c config.toml; rm config.toml
```

Expected: prints `0` (file not shown as untracked). The `grep -c` returning `0` confirms the ignore works.

- [ ] **Step 3: Document the field in `config.example.toml`**

In `config.example.toml`, after the `bind_port` line, add:

```toml
# Extra Host-header hostnames allowed through host origin protection.
# Supports glob wildcards (e.g. "*.ts.net" for tailscale serve). Ports are
# ignored during matching — list bare hostnames/patterns only.
# extra_allowed_hosts = ["*.ts.net"]
```

- [ ] **Step 4: Commit**

```bash
git add .gitignore config.example.toml
git commit -m "chore: gitignore config.toml and document extra_allowed_hosts"
```

---

### Task 4: Full verification + local deployment migration

**Files:**
- Create (local, untracked): `config.toml`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: green test suite; running service keeps working via Tailscale.

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: PASS (acceptance/live tests may skip per their opt-in markers; no failures).

- [ ] **Step 2: Create local `config.toml` with this machine's Tailscale FQDN**

Write `config.toml` (untracked, same dir as `config.example.toml`):

```toml
extra_allowed_hosts = ["instance-20250526-0820.taila20d2.ts.net"]
```

- [ ] **Step 3: Restart the service and verify both local and Tailscale access atomically**

Run:

```bash
systemctl --user restart crawl4ai-mcp.service
sleep 3
curl -fsS http://127.0.0.1:11236/health
curl -s -o /dev/null -w "local mcp: %{http_code}\n" -X POST http://127.0.0.1:11236/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify","version":"1.0"}}}'
curl -s -o /dev/null -w "tailscale mcp: %{http_code}\n" -X POST \
  "https://instance-20250526-0820.taila20d2.ts.net:11237/mcp" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify","version":"1.0"}}}' --max-time 15
```

Expected: health `ok`, `local mcp: 200`, `tailscale mcp: 200` (the FQDN now comes from `config.toml`, not source). The `config.toml` write (Step 2) and this restart MUST happen back-to-back so the running service never runs without the FQDN.

- [ ] **Step 4: Confirm `config.toml` is not tracked**

Run: `git status --porcelain`
Expected: no `config.toml` line (clean tree).

---

## Self-Review

**Spec coverage:**
- Remove hard-coded FQDN → Task 2 Step 3. ✓
- `extra_allowed_hosts` field + validator (strip / empty-reject / `"*"` warn) → Task 1. ✓
- Non-secret (not in `SECRET_ENV_VARS`) → Task 1 touches only the field; Global Constraints restate it. ✓
- `.gitignore` `config.toml` → Task 3. ✓
- `config.example.toml` example → Task 3. ✓
- tests: test_config.py cases → Task 1; test_main.py default + extra cases → Task 2 (default case is the pre-existing test). ✓
- Local deployment migration (`config.toml` with FQDN) → Task 4. ✓

**Placeholder scan:** none — every code step has concrete code/commands.

**Type consistency:** `extra_allowed_hosts: list[str]` used identically in Tasks 1, 2, 4. Validator name `validate_extra_allowed_hosts` referenced only in Task 1. `run_server(config, service=...)` signature matches existing `__main__.py` and `test_main.py` fakes.
