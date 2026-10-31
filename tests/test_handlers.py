from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from autofilter.config import Settings
from autofilter.handlers import Handlers


@pytest.fixture
def handler():
    settings = Settings(
        token="local", api_id=1, api_hash="local", session_path="local", database_url="local", owners=frozenset({1})
    )
    db = SimpleNamespace(
        is_sudo=AsyncMock(return_value=False),
        approved=AsyncMock(return_value=False),
        setting=AsyncMock(side_effect=lambda key, default=None: default),
        change_member=AsyncMock(),
        set_setting=AsyncMock(),
    )
    return Handlers(settings, db, SimpleNamespace())


def update_for(uid=2, cid=2, mid=10, data="p:token:0"):
    message = SimpleNamespace(chat=SimpleNamespace(id=cid, type="private"), message_id=mid)
    cb = SimpleNamespace(data=data, message=message, answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_chat=message.chat, callback_query=cb)


async def test_result_callback_rejects_other_user(handler):
    handler.sessions.put("token", dict(uid=3, cid=2, mid=10, results=[]))
    update = update_for()
    await handler.callback(update, SimpleNamespace())
    assert "another user" in update.callback_query.answer.call_args.args[0]
    update.callback_query.edit_message_text.assert_not_called()


async def test_result_callback_rejects_other_message(handler):
    handler.sessions.put("token", dict(uid=2, cid=2, mid=11, results=[]))
    update = update_for()
    await handler.callback(update, SimpleNamespace())
    update.callback_query.edit_message_text.assert_not_called()


async def test_sudo_cannot_use_owner_panel(handler):
    handler.db.is_sudo.return_value = True
    update = update_for(data="t:private_search")
    await handler.callback(update, SimpleNamespace())
    handler.db.set_setting.assert_not_called()


async def test_confirmation_rechecks_revoked_staff(handler):
    handler.confirmations.put("token", (2, 2, 10, ("remove", "group", -1001234567890)))
    update = update_for(data="c:token:yes")
    await handler.callback(update, SimpleNamespace())
    handler.db.change_member.assert_not_called()
    assert "revoked" in update.callback_query.answer.call_args.args[0]


async def test_callback_cannot_select_arbitrary_file(handler):
    handler.sessions.put("token", dict(uid=1, cid=1, mid=10, results=[{"id": 5}]))
    handler.delivery.send = AsyncMock()
    update = update_for(uid=1, cid=1, data="f:token:6")
    await handler.callback(update, SimpleNamespace())
    handler.delivery.send.assert_not_called()


async def test_registration_uses_separate_channel_handler(handler):
    app = Mock()
    handler.install(app)
    assert app.add_handler.call_count >= 20
