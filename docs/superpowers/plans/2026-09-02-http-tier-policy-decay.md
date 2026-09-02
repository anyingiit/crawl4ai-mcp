# HTTP Tier Policy Decay Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent `crawl4ai_scrape` from raising `-1 is not a valid Tier` when a domain's remembered successful tier is `HTTP` and that memory has reached the policy-decay age.

**Architecture:** Keep the existing policy-memory design and clamp the integer tier value before constructing the `Tier` enum. Add focused policy-store and cascade regression tests for the `HTTP` lower boundary, then run MCP contract and full-suite verification to confirm explicit tier inputs continue to work.

**Tech Stack:** Python 3.12, `IntEnum`, aiosqlite, pytest, pytest-asyncio, fastmcp 3.4.x.

## Global Constraints

- Preserve the intended decay rule: a remembered tier at least `policy_decay_days` old retries exactly one tier lower.
- `Tier.HTTP` is the lower boundary and must remain `Tier.HTTP`; no `Tier(-1)` construction is permitted.
- Do not change the public `scrape` MCP schema, `max_tier` default, `force_tier` behavior, database schema, or stored tier values.
- Keep the fix in `PolicyStore.get_start_tier`; the failure originates in policy decay, not request parsing.
- Use TDD: demonstrate the exact `HTTP` boundary failure before changing implementation.

## Root Cause Evidence

- `src/crawl4ai_mcp/server.py:27-37` accepts `max_tier` as a valid `TierName` and forwards it unchanged.
- `src/crawl4ai_mcp/service.py:277-285` parses `max_tier` successfully, then calls the cascade engine.
- `src/crawl4ai_mcp/cascade.py:86-90` asks `PolicyStore.get_start_tier` for the starting tier whenever `force_tier` is absent.
- `src/crawl4ai_mcp/policy.py:103-105` evaluates `Tier(best - 1)` before `max(...)`; when `best == Tier.HTTP`, Python evaluates `Tier(-1)` and raises `ValueError: -1 is not a valid Tier` before clamping can occur.
- A direct policy-store reproduction with an eight-day-old `Tier.HTTP` success raises the issue's exact error at `policy.py:105`.
- A live opencode MCP call with explicit `max_tier="http", force_tier="http"` passes because the forced path bypasses policy start-tier lookup, matching the report that `force_tier` works. This acceptance test is contract coverage only; it is not regression coverage for the stale-policy failure.

---

### Task 1: Clamp policy decay at the HTTP boundary

**Files:**
- Modify: `src/crawl4ai_mcp/policy.py:95-106`
- Test: `tests/test_policy.py:62-68`
- Test: `tests/test_cascade.py:132-137,191-215`

**Interfaces:**
- Consumes: `PolicyStore.get_start_tier(url: str, now: int) -> Tier`, `CascadeEngine.scrape(url: str, maximum: Tier, force: Tier | None)`, `Tier.HTTP`, persisted `domain_policy.best_tier` and `last_success_at`, `ScriptedProvider`, and `FakeClock`.
- Produces: the unchanged `get_start_tier` interface, with an explicit guarantee that an expired `Tier.HTTP` memory returns `Tier.HTTP`; regression coverage also proves a no-force cascade reaches the HTTP provider.

- [ ] **Step 1: Write the failing boundary regression test**

Add this test immediately after `test_policy_decays_one_tier_after_seven_days` in `tests/test_policy.py`:

```python
@pytest.mark.asyncio
async def test_policy_decay_keeps_http_at_lower_bound(tmp_path):
    store = await PolicyStore.open(tmp_path / "policy.db", decay_days=7)
    await store.record_success("https://example.com/a", Tier.HTTP, now=1_000)
    seven_days = 1_000 + 7 * 86_400

    assert await store.get_start_tier(
        "https://example.com/b", now=seven_days
    ) == Tier.HTTP

    await store.close()
```

- [ ] **Step 2: Write the failing cascade regression test**

Add this test near the existing policy-memory tests in `tests/test_cascade.py`:

```python
@pytest.mark.asyncio
async def test_decayed_http_policy_stays_on_http(tmp_path):
    seven_days = 1_000 + 7 * 86_400
    engine, policy = await make_engine(
        tmp_path, {Tier.HTTP: success}, now=seven_days
    )
    await policy.record_success(
        "https://example.com/a", Tier.HTTP, now=1_000
    )

    try:
        outcome = await engine.scrape(
            "https://example.com/b", maximum=Tier.HTTP
        )
    finally:
        await policy.close()

    assert outcome.response.status == "success"
    assert outcome.response.tier_used == "http"
    assert engine.calls == [Tier.HTTP]
```

- [ ] **Step 3: Run both regression tests and verify the exact failure**

Run:

```bash
.venv/bin/python3 -m pytest \
  tests/test_policy.py::test_policy_decay_keeps_http_at_lower_bound \
  tests/test_cascade.py::test_decayed_http_policy_stays_on_http -v
```

Expected: 2 FAIL with `ValueError: -1 is not a valid Tier`, originating from `Tier(best - 1)` in `PolicyStore.get_start_tier`; the cascade test fails before any provider call.

- [ ] **Step 4: Implement the minimal lower-bound fix**

In `src/crawl4ai_mcp/policy.py`, replace:

```python
            return max(Tier.HTTP, Tier(best - 1))
```

with:

```python
            return Tier(max(Tier.HTTP, best - 1))
```

This compares/clamps integer-compatible values first and constructs `Tier` only after the result is guaranteed to be within the enum's lower bound.

- [ ] **Step 5: Run the focused policy and cascade tests**

Run:

```bash
.venv/bin/python3 -m pytest \
  tests/test_policy.py::test_policy_decays_one_tier_after_seven_days \
  tests/test_policy.py::test_policy_decay_keeps_http_at_lower_bound \
  tests/test_cascade.py::test_decayed_http_policy_stays_on_http -v
```

Expected: 3 PASS. The existing `RAYOBYTE -> PROXY` behavior proves normal one-step decay is preserved; the new tests prove `HTTP -> HTTP` is clamped and the no-force cascade calls exactly the HTTP provider.

- [ ] **Step 6: Run the complete policy and cascade modules**

Run:

```bash
.venv/bin/python3 -m pytest tests/test_policy.py tests/test_cascade.py -v
```

Expected: all policy and cascade tests PASS.

- [ ] **Step 7: Commit the fix and regressions**

```bash
git add src/crawl4ai_mcp/policy.py tests/test_policy.py tests/test_cascade.py
git commit -m "fix: clamp policy decay at http tier"
```

---

### Task 2: Verify MCP contracts and the full suite

**Files:**
- No source changes expected.
- Verify: `tests/test_server.py`
- Verify: `tests/test_service.py`
- Verify: `tests/acceptance/test_opencode_mcp.py`

**Interfaces:**
- Consumes: fixed `PolicyStore.get_start_tier`, existing `CrawlService.scrape`, and existing FastMCP `scrape` tool contract.
- Produces: evidence that the policy fix does not alter MCP tier schemas or forced-tier behavior. The stale-policy regression itself is covered by Task 1 because the current live acceptance fixture does not control policy age.

- [ ] **Step 1: Run unit-level MCP and service tests**

Run:

```bash
.venv/bin/python3 -m pytest tests/test_server.py tests/test_service.py -v
```

Expected: all tests PASS, including schema assertions that `max_tier` defaults to `"firecrawl"` and accepts the existing tier-name enum.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
.venv/bin/python3 -m pytest -q
```

Expected: all non-live tests PASS; live/acceptance tests may skip according to their existing opt-in markers.

- [ ] **Step 3: Run the deployed opencode MCP acceptance test**

After deploying/restarting the fixed service, run:

```bash
CRAWL4AI_MCP_LIVE_TESTS=1 \
  .venv/bin/python3 -m pytest tests/acceptance/test_opencode_mcp.py -v
```

Expected: 2 PASS. The invocation must return successful `crawl4ai_scrape` output with `tier_used == "http"` and `cost_kind == "free"`. This confirms deployed MCP compatibility but intentionally does not claim to reproduce the stale-policy branch because the existing acceptance call supplies `force_tier="http"`.

- [ ] **Step 4: Verify the original stale-policy condition against the deployed service**

For optional deployment-level confirmation, use a disposable test database or controlled policy fixture to create a domain row with:

```text
best_tier = 0
last_success_at <= current_time - policy_decay_days * 86400
```

Invoke `crawl4ai_scrape` for that domain with `max_tier="http"` and no `force_tier`.

Expected: the request reaches the HTTP provider or returns its normal domain-level outcome; it must not raise `-1 is not a valid Tier`.

Do not mutate production policy data solely for this check. If the deployed instance cannot safely use a disposable database, Task 1 is the authoritative regression evidence and Steps 1-3 are compatibility evidence.

---

## Self-Review

**Spec coverage:**
- Exact `-1 is not a valid Tier` reproduction is documented and covered by Task 1 Steps 1-3.
- Root cause is fixed at `PolicyStore.get_start_tier`, not masked in MCP parameter parsing.
- Existing one-tier decay behavior is retained and verified alongside the lower-bound case.
- The complete no-force cascade path is covered by Task 1, including `maximum=Tier.HTTP` and the provider call.
- MCP schema and forced-tier compatibility are covered by Task 2 without mislabeling the bypassing acceptance test as regression coverage.
- No database migration, public API change, or unrelated refactor is included.

**Placeholder scan:** none. Every implementation and verification step includes concrete code, commands, and expected outcomes.

**Type consistency:** `PolicyStore.get_start_tier(url: str, now: int) -> Tier` remains unchanged; `Tier(max(Tier.HTTP, best - 1))` always constructs a valid lower-bounded `Tier`.
