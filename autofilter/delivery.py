import asyncio

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .search import Busy, Limiter


class DeliveryService:
    def __init__(self, db, source, settings):
        self.db, self.source, self.settings = db, source, settings
        self.active = set()
        self.gate = asyncio.Semaphore(settings.concurrency)
        self.limiter = Limiter(10)

    async def send(self, bot, uid, cid, fid):
        key = (uid, cid, fid)
        if key in self.active or self.gate.locked():
            raise Busy("Delivery is already in progress. Please wait.")
        self.limiter.check([(key, 1)])
        self.active.add(key)
        try:
            async with self.gate, asyncio.timeout(self.settings.timeout):
                f = await self.db.file(fid)
                if not f:
                    raise Busy("This file is no longer available. Try a new search.")
                sources = await self.db.channels("source")
                stores = await self.db.channels("storage")
                if f["channel_id"] not in sources or f["channel_id"] not in stores:
                    raise Busy("This file is no longer available for delivery.")
                # Check existence/protection even when a cached Bot API file_id exists.
                original = await self.source.message(f["channel_id"], f["message_id"])
                if not original:
                    await self.db.mark_deleted(fid)
                    raise Busy("This file has been removed. Try a new search.")
                if f"doc:{original.document.id}" != f["fingerprint"]:
                    await self.db.save_file_id(fid, None)
                    await self.source.index(original, f["channel_id"])
                    raise Busy("This file has changed. Please start a new search.")
                if f["file_id"]:
                    try:
                        method = getattr(bot, "send_" + f["kind"])
                        await method(
                            chat_id=cid,
                            **{f["kind"]: f["file_id"]},
                            caption=f["caption"],
                            parse_mode=None,
                            protect_content=True,
                        )
                        await self.db.metric("deliveries")
                        return
                    except BadRequest:
                        await self.db.save_file_id(fid, None)
                # Never download with the user account to bypass Bot API restrictions.
                await bot.copy_message(
                    chat_id=cid,
                    from_chat_id=f["channel_id"],
                    message_id=f["message_id"],
                    caption=f["caption"],
                    parse_mode=None,
                    protect_content=True,
                )
                await self.db.metric("deliveries")
        except RetryAfter:
            raise Busy("Telegram is limiting deliveries. Please wait before trying again.") from None
        except (BadRequest, Forbidden):
            raise Busy("Telegram could not deliver this file. An owner should check storage permissions.") from None
        except (NetworkError, TimeoutError):
            # An ambiguous send is never retried automatically.
            raise Busy("Delivery status is uncertain. Check the chat before requesting it again.") from None
        finally:
            self.active.discard(key)
