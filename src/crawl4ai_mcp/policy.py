from __future__ import annotations

import sqlite3

from pydantic import BaseModel
from aiosqlite import Connection, connect

from crawl4ai_mcp.egress import parse_public_url
from crawl4ai_mcp.models import Tier

DAY_SECONDS = 86_400
BACKOFF_SEQUENCE = (600, 3_600, 21_600, 86_400)

SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_policy (
    domain TEXT PRIMARY KEY,
    best_tier INTEGER,
    last_success_at INTEGER,
    fail_count INTEGER NOT NULL DEFAULT 0,
    cooldown_until INTEGER,
    last_error_kind TEXT,
    updated_at INTEGER NOT NULL
);
"""


class DomainPolicy(BaseModel):
    domain: str
    best_tier: Tier | None = None
    last_success_at: int | None = None
    fail_count: int = 0
    cooldown_until: int | None = None
    last_error_kind: str | None = None
    updated_at: int


def normalize_domain(url: str) -> str:
    return parse_public_url(url).host


def _row_to_policy(row: sqlite3.Row) -> DomainPolicy:
    return DomainPolicy(
        domain=row["domain"],
        best_tier=Tier(row["best_tier"]) if row["best_tier"] is not None else None,
        last_success_at=row["last_success_at"],
        fail_count=row["fail_count"],
        cooldown_until=row["cooldown_until"],
        last_error_kind=row["last_error_kind"],
        updated_at=row["updated_at"],
    )


class PolicyStore:
    def __init__(self, conn: Connection, decay_days: int):
        self._conn = conn
        self.decay_days = decay_days

    @classmethod
    async def open(cls, path, decay_days: int = 7) -> "PolicyStore":
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await connect(path)
        conn.row_factory = sqlite3.Row
        await conn.execute(SCHEMA)
        await conn.commit()
        return cls(conn, decay_days=decay_days)

    async def close(self) -> None:
        await self._conn.close()

    async def get_start_tier(self, url: str, now: int) -> Tier:
        domain = normalize_domain(url)
        async with self._conn.execute(
            "SELECT * FROM domain_policy WHERE domain = ?", (domain,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["best_tier"] is None:
            return Tier.HTTP
        best = Tier(row["best_tier"])
        if row["last_success_at"] is not None and now - row["last_success_at"] >= self.decay_days * DAY_SECONDS:
            return max(Tier.HTTP, Tier(best - 1))
        return best

    async def record_success(self, url: str, tier: Tier, now: int | None = None) -> DomainPolicy:
        domain = normalize_domain(url)
        updated_at = now if now is not None else int(__import__("time").time())
        await self._conn.execute(
            """
            INSERT INTO domain_policy (domain, best_tier, last_success_at, fail_count,
                                       cooldown_until, last_error_kind, updated_at)
            VALUES (?, ?, ?, 0, NULL, NULL, ?)
            ON CONFLICT(domain) DO UPDATE SET
                best_tier = excluded.best_tier,
                last_success_at = excluded.last_success_at,
                fail_count = 0,
                cooldown_until = NULL,
                last_error_kind = NULL,
                updated_at = excluded.updated_at
            """,
            (domain, int(tier), updated_at, updated_at),
        )
        await self._conn.commit()
        return DomainPolicy(
            domain=domain, best_tier=tier, last_success_at=updated_at,
            updated_at=updated_at,
        )

    async def record_failure(
        self, url: str, error_kind: str, now: int | None = None
    ) -> DomainPolicy:
        domain = normalize_domain(url)
        updated_at = now if now is not None else int(__import__("time").time())
        await self._conn.execute(
            """
            INSERT INTO domain_policy (domain, fail_count, last_error_kind, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                fail_count = domain_policy.fail_count + 1,
                last_error_kind = excluded.last_error_kind,
                updated_at = excluded.updated_at
            """,
            (domain, error_kind, updated_at),
        )
        await self._conn.commit()
        async with self._conn.execute(
            "SELECT * FROM domain_policy WHERE domain = ?", (domain,)
        ) as cursor:
            row = await cursor.fetchone()
        policy = _row_to_policy(row)
        index = min(policy.fail_count, len(BACKOFF_SEQUENCE)) - 1
        cooldown_until = updated_at + BACKOFF_SEQUENCE[index]
        await self._conn.execute(
            "UPDATE domain_policy SET cooldown_until = ? WHERE domain = ?",
            (cooldown_until, domain),
        )
        await self._conn.commit()
        policy.cooldown_until = cooldown_until
        return policy

    async def get_active_cooldown(self, url: str, now: int) -> DomainPolicy | None:
        domain = normalize_domain(url)
        async with self._conn.execute(
            "SELECT * FROM domain_policy WHERE domain = ?", (domain,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["cooldown_until"] is None or row["cooldown_until"] <= now:
            return None
        return _row_to_policy(row)

    async def clear(self, domain: str | None = None) -> None:
        if domain is None:
            await self._conn.execute("DELETE FROM domain_policy")
        else:
            await self._conn.execute(
                "DELETE FROM domain_policy WHERE domain = ?", (normalize_domain(domain),)
            )
        await self._conn.commit()

    async def list_policies(self, domain: str | None = None) -> list[DomainPolicy]:
        if domain is None:
            async with self._conn.execute(
                "SELECT * FROM domain_policy ORDER BY domain"
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._conn.execute(
                "SELECT * FROM domain_policy WHERE domain = ? ORDER BY domain",
                (normalize_domain(domain),),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_policy(row) for row in rows]
