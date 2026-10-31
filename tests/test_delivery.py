from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError

from autofilter.delivery import DeliveryService
from autofilter.search import Busy


@pytest.fixture
def deps():
    f = dict(
        id=1,
        channel_id=-1001234567890,
        message_id=5,
        file_id="cached",
        kind="document",
        caption="Film",
        fingerprint="doc:1",
    )
    db = SimpleNamespace(
        file=AsyncMock(return_value=f),
        channels=AsyncMock(return_value=[f["channel_id"]]),
        save_file_id=AsyncMock(),
        mark_deleted=AsyncMock(),
        metric=AsyncMock(),
    )
    source = SimpleNamespace(
        message=AsyncMock(return_value=SimpleNamespace(document=SimpleNamespace(id=1))), index=AsyncMock()
    )
    bot = SimpleNamespace(send_document=AsyncMock(), copy_message=AsyncMock())
    service = DeliveryService(db, source, SimpleNamespace(concurrency=2, timeout=5))
    return service, db, source, bot


async def test_reuses_file_id_and_duplicate_throttle(deps):
    service, db, source, bot = deps
    await service.send(bot, 2, 2, 1)
    bot.send_document.assert_awaited_once()
    bot.copy_message.assert_not_called()
    with pytest.raises(Busy):
        await service.send(bot, 2, 2, 1)


async def test_expired_file_id_falls_back_to_copy(deps):
    service, db, source, bot = deps
    bot.send_document.side_effect = BadRequest("Wrong file identifier")
    await service.send(bot, 2, 2, 1)
    bot.copy_message.assert_awaited_once()
    db.save_file_id.assert_awaited_once_with(1, None)


async def test_deleted_source_not_delivered(deps):
    service, db, source, bot = deps
    source.message.return_value = None
    with pytest.raises(Busy):
        await service.send(bot, 2, 2, 1)
    db.mark_deleted.assert_awaited_once_with(1)
    bot.send_document.assert_not_called()


async def test_revoked_channel_not_delivered(deps):
    service, db, source, bot = deps
    db.channels.return_value = []
    with pytest.raises(Busy):
        await service.send(bot, 2, 2, 1)
    source.message.assert_not_called()


async def test_ambiguous_send_not_retried(deps):
    service, db, source, bot = deps
    bot.send_document.side_effect = NetworkError("Disconnected")
    with pytest.raises(Busy, match="uncertain"):
        await service.send(bot, 2, 2, 1)
    bot.copy_message.assert_not_called()
    assert not service.active


async def test_replaced_media_invalidates_cached_id(deps):
    service, db, source, bot = deps
    source.message.return_value.document.id = 99
    with pytest.raises(Busy, match="changed"):
        await service.send(bot, 2, 2, 1)
    db.save_file_id.assert_awaited_once_with(1, None)
    source.index.assert_awaited_once()
    bot.send_document.assert_not_called()
