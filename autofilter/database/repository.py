import hashlib
import json
from pathlib import Path

import asyncpg


class Database:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def connect(cls, settings):
        pool = await asyncpg.create_pool(
            settings.database_url, min_size=1, max_size=settings.pool_size, command_timeout=15
        )
        return cls(pool)

    async def close(self):
        await self.pool.close()

    async def migrate(self):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(735193021)")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS af_migrations "
                "(name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ DEFAULT now())"
            )
            for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
                sql = path.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                previous = await conn.fetchval("SELECT checksum FROM af_migrations WHERE name=$1", path.name)
                if previous is not None:
                    if previous != checksum:
                        raise RuntimeError("An applied migration has changed; add a new migration instead.")
                    continue
                await conn.execute(sql)
                await conn.execute("INSERT INTO af_migrations(name,checksum) VALUES($1,$2)", path.name, checksum)

    async def health(self):
        return await self.pool.fetchval("SELECT 1") == 1

    async def setting(self, key, default=None):
        value = await self.pool.fetchval("SELECT value::text FROM af_settings WHERE key=$1", key)
        return json.loads(value) if value is not None else default

    async def audit(self, actor, action, target):
        await self.pool.execute(
            "INSERT INTO af_audit(actor_id,action,target) VALUES($1,$2,$3)", actor, action, str(target)
        )

    async def set_setting(self, key, value, actor):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(
                "INSERT INTO af_settings(key,value) VALUES($1,$2::jsonb) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
                key,
                json.dumps(value),
            )
            await c.execute("INSERT INTO af_audit(actor_id,action,target) VALUES($1,$2,$3)", actor, "setting", key)

    async def is_sudo(self, uid):
        return bool(await self.pool.fetchval("SELECT 1 FROM af_sudo WHERE user_id=$1", uid))

    async def approved(self, cid):
        return bool(await self.pool.fetchval("SELECT 1 FROM af_groups WHERE chat_id=$1", cid))

    async def channels(self, role):
        return [
            r["chat_id"]
            for r in await self.pool.fetch("SELECT chat_id FROM af_channels WHERE role=$1 ORDER BY chat_id", role)
        ]

    async def change_member(self, category, target, actor, add=True):
        # Static SQL selection: identifiers never come from command input.
        statements = {
            ("sudo", True): "INSERT INTO af_sudo(user_id,granted_by) VALUES($1,$2) ON CONFLICT DO NOTHING",
            ("sudo", False): "DELETE FROM af_sudo WHERE user_id=$1 AND $2::bigint > 0",
            ("group", True): "INSERT INTO af_groups(chat_id,approved_by) VALUES($1,$2) ON CONFLICT DO NOTHING",
            ("group", False): "DELETE FROM af_groups WHERE chat_id=$1 AND $2::bigint > 0",
            (
                "source",
                True,
            ): "INSERT INTO af_channels(chat_id,role,added_by) VALUES($1,'source',$2) ON CONFLICT DO NOTHING",
            ("source", False): "DELETE FROM af_channels WHERE chat_id=$1 AND role='source' AND $2::bigint > 0",
            (
                "storage",
                True,
            ): "INSERT INTO af_channels(chat_id,role,added_by) VALUES($1,'storage',$2) ON CONFLICT DO NOTHING",
            ("storage", False): "DELETE FROM af_channels WHERE chat_id=$1 AND role='storage' AND $2::bigint > 0",
        }
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(735193022)")
            await c.execute(statements[category, add], target, actor)
            await c.execute(
                "INSERT INTO af_audit(actor_id,action,target) VALUES($1,$2,$3)",
                actor,
                ("add_" if add else "remove_") + category,
                str(target),
            )

    async def upsert_file(self, f):
        return dict(
            await self.pool.fetchrow(
                "INSERT INTO af_files(channel_id,message_id,fingerprint,title,normalized,kind,size,caption) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(channel_id,message_id) DO UPDATE SET "
                "file_id=CASE WHEN af_files.fingerprint=excluded.fingerprint THEN af_files.file_id ELSE NULL END,"
                "fingerprint=excluded.fingerprint,title=excluded.title,normalized=excluded.normalized,"
                "kind=excluded.kind,size=excluded.size,caption=excluded.caption,active=true,updated_at=now() RETURNING *",
                *[
                    f[k]
                    for k in (
                        "channel_id",
                        "message_id",
                        "fingerprint",
                        "title",
                        "normalized",
                        "kind",
                        "size",
                        "caption",
                    )
                ],
            )
        )

    async def candidates(self, title, limit):
        return [
            dict(r)
            for r in await self.pool.fetch(
                "SELECT f.* FROM af_files f WHERE active AND to_tsvector('simple',normalized) @@ plainto_tsquery('simple',$1) "
                "AND EXISTS(SELECT 1 FROM af_channels c WHERE c.chat_id=f.channel_id AND c.role='source') "
                "AND EXISTS(SELECT 1 FROM af_channels c WHERE c.chat_id=f.channel_id AND c.role='storage') "
                "ORDER BY updated_at DESC LIMIT $2",
                title,
                limit,
            )
        ]

    async def file(self, fid):
        row = await self.pool.fetchrow("SELECT * FROM af_files WHERE id=$1 AND active", fid)
        return dict(row) if row else None

    async def mark_deleted(self, fid):
        await self.pool.execute("UPDATE af_files SET active=false WHERE id=$1", fid)

    async def save_file_id(self, fid, telegram_id):
        await self.pool.execute("UPDATE af_files SET file_id=$2 WHERE id=$1", fid, telegram_id)

    async def seen(self, uid):
        await self.pool.execute(
            "INSERT INTO af_users(user_id) VALUES($1) ON CONFLICT(user_id) DO UPDATE SET last_seen=now()", uid
        )

    async def metric(self, key):
        await self.pool.execute(
            "INSERT INTO af_metrics(key,value) VALUES($1,1) ON CONFLICT(key) DO UPDATE SET value=af_metrics.value+1",
            key,
        )

    async def stats(self):
        return dict(
            await self.pool.fetchrow(
                "SELECT (SELECT count(*) FROM af_files WHERE active) AS files, "
                "(SELECT count(*) FROM af_users) AS users, (SELECT count(*) FROM af_groups) AS groups, "
                "coalesce((SELECT value FROM af_metrics WHERE key='searches'),0) AS searches"
            )
        )

    async def listing(self, category, offset, limit=8):
        queries = {
            "sudo": "SELECT user_id AS id FROM af_sudo ORDER BY user_id LIMIT $1 OFFSET $2",
            "group": "SELECT chat_id AS id FROM af_groups ORDER BY chat_id LIMIT $1 OFFSET $2",
            "source": "SELECT chat_id AS id FROM af_channels WHERE role='source' ORDER BY chat_id LIMIT $1 OFFSET $2",
            "storage": "SELECT chat_id AS id FROM af_channels WHERE role='storage' ORDER BY chat_id LIMIT $1 OFFSET $2",
        }
        return await self.pool.fetch(queries[category], limit, offset)
