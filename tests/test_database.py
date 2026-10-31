"""Real repository and migration tests run only against an explicit disposable DB."""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import asyncpg
import pytest

from autofilter.database.repository import Database


async def test_parameterized_setting_and_default():
    pool = AsyncMock()
    pool.fetchval.return_value = None
    db = Database(pool)
    assert await db.setting("x", 42) == 42
    pool.fetchval.assert_awaited_once_with("SELECT value::text FROM af_settings WHERE key=$1", "x")


async def test_migration_rejects_checksum_change():
    conn = AsyncMock()
    conn.fetchval.return_value = "different"

    @asynccontextmanager
    async def transaction():
        yield conn

    conn.transaction = transaction
    pool = AsyncMock()
    pool.acquire = transaction
    with pytest.raises(RuntimeError, match="migration has changed"):
        await Database(pool).migrate()


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="Set TEST_DATABASE_URL to a disposable PostgreSQL database"
)
async def test_real_migrations_and_repositories():
    pool = await asyncpg.create_pool(os.environ["TEST_DATABASE_URL"], min_size=1, max_size=3)
    db = Database(pool)
    try:
        await db.migrate()
        await db.migrate()
        assert await db.health()
        assert await pool.fetchval("SELECT count(*) FROM af_migrations") == 1
        await db.set_setting("test", {"safe": True}, 1)
        assert await db.setting("test") == {"safe": True}
        await db.change_member("sudo", 999, 1)
        assert await db.is_sudo(999)
        await db.change_member("sudo", 999, 1, False)
        assert not await db.is_sudo(999)
        cid = -1001234567890
        await db.change_member("group", cid, 1)
        assert await db.approved(cid)
        await db.change_member("source", cid, 1)
        await db.change_member("storage", cid, 1)
        f = dict(
            channel_id=cid,
            message_id=1,
            fingerprint="synthetic",
            title="Test Film",
            normalized="test film",
            kind="video",
            size=100,
            caption="Synthetic",
        )
        first = await db.upsert_file(f)
        assert (await db.upsert_file(f))["id"] == first["id"]
        assert any(x["id"] == first["id"] for x in await db.candidates("test film", 10))
        await db.save_file_id(first["id"], "local-file-id")
        assert (await db.file(first["id"]))["file_id"] == "local-file-id"
        f["fingerprint"] = "replacement"
        assert (await db.upsert_file(f))["file_id"] is None
        await db.mark_deleted(first["id"])
        assert await db.file(first["id"]) is None
        await db.seen(999)
        await db.metric("searches")
        assert (await db.stats())["searches"] >= 1
        assert await pool.fetchval("SELECT count(*) FROM af_audit") > 0
    finally:
        await db.close()
