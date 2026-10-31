import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors

from autofilter.runtime import JsonFormatter, Runtime
from autofilter.search import Query
from autofilter.source import SourceService


async def test_flood_wait_cools_down_without_repeated_requests():
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=90)))
    service = SourceService(client, SimpleNamespace(), SimpleNamespace(max_sources=20))
    assert await service.search(Query.parse("Film"), [-1001234567890]) == ([], True)
    assert await service.search(Query.parse("Other Film"), [-1001234567890]) == ([], True)
    assert client.get_entity.await_count == 1


async def test_network_failure_returns_partial_state():
    client = SimpleNamespace(get_entity=AsyncMock(side_effect=OSError("private information")))
    service = SourceService(client, SimpleNamespace(), SimpleNamespace(max_sources=20))
    assert await service.search(Query.parse("Film"), [-1001234567890]) == ([], True)


async def test_shutdown_closes_database_even_on_disconnect_failure():
    runtime = Runtime(SimpleNamespace())
    runtime.client = SimpleNamespace(disconnect=AsyncMock(side_effect=OSError()))
    db = SimpleNamespace(close=AsyncMock())
    runtime.db = db
    with pytest.raises(OSError):
        await runtime.shutdown(None)
    db.close.assert_awaited_once()
    assert runtime.db is None


def test_logs_exclude_credentials_and_exception_messages():
    record = logging.LogRecord("httpx", 40, "", 1, "https://api.telegram.org/botSECRET", (), None)
    output = JsonFormatter().format(record)
    assert "SECRET" not in output and "library_event" in output
