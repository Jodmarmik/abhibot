"""Read only access to explicitly registered, distributable channels."""

import time

from telethon import errors, utils
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

from .search import ALIASES, normalize


class SourceService:
    def __init__(self, client, db, settings):
        self.client, self.db, self.settings = client, db, settings
        self.cooldown_until = 0

    async def validate(self, cid):
        entity = await self.client.get_entity(cid)
        if utils.get_peer_id(entity) != cid or not getattr(entity, "broadcast", False):
            raise ValueError("Use a channel ID, not a user or basic group.")
        if getattr(entity, "noforwards", False):
            raise ValueError("Protected channels cannot be used for delivery.")
        # Resolve access through a real history request; never join automatically.
        await self.client.get_messages(entity, limit=1)
        return entity

    async def message(self, cid, mid):
        entity = await self.validate(cid)
        message = await self.client.get_messages(entity, ids=mid)
        if not message or not message.document or getattr(message, "noforwards", False):
            return None
        return message

    async def index(self, msg, cid):
        media = msg.document
        kind = "document"
        if any(isinstance(a, DocumentAttributeVideo) for a in media.attributes):
            kind = "video"
        elif any(isinstance(a, DocumentAttributeAudio) for a in media.attributes):
            kind = "audio"
        title = msg.file.name or (msg.message or "").split("\n")[0] or "Untitled file"
        f = dict(
            channel_id=cid,
            message_id=msg.id,
            fingerprint=f"doc:{media.id}",
            title=title[:512],
            normalized=normalize(title),
            kind=kind,
            size=media.size,
            caption=(msg.message or "")[:1024],
        )
        return await self.db.upsert_file(f)

    async def search(self, query, channels):
        if time.monotonic() < self.cooldown_until:
            return [], True
        found, unavailable = [], False
        for cid in channels[: self.settings.max_sources]:
            try:
                entity = await self.validate(cid)
                variants = list(
                    dict.fromkeys(
                        [query.title] + [alias for alias, canonical in ALIASES.items() if canonical == query.title]
                    )
                )
                for term in variants:
                    async for msg in self.client.iter_messages(entity, search=term, limit=self.settings.source_limit):
                        if not msg.document or getattr(msg, "noforwards", False):
                            continue
                        found.append(await self.index(msg, cid))
            except errors.FloodWaitError as exc:
                self.cooldown_until = time.monotonic() + exc.seconds
                unavailable = True
                break
            except (errors.RPCError, OSError, ValueError):
                unavailable = True
        return found, unavailable
