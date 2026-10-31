from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from autofilter.config import ConfigError, Settings, channel_id, positive_id
from autofilter.permissions import Permissions
from autofilter.search import Busy, Cache, Limiter, Query, SearchService, normalize, page, rank
from autofilter.welcome import render, validate_template


@pytest.fixture
def settings():
    return Settings.load(
        dict(
            BOT_TOKEN="123456:" + "a" * 35,
            API_ID="12345",
            API_HASH="b" * 32,
            OWNER_IDS="1,2",
            DATABASE_URL="postgresql://user:secret@localhost/db",
            TELETHON_SESSION_PATH="private.session",
        )
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Money.Heist_S02E03 Hindi", "money heist s02 e03 hindi"),
        ("La Casa De Papel Season 2", "money heist s02"),
        ("Interstellar (2014) 1080p", "interstellar 2014 1080p"),
    ],
)
def test_normalize(value, expected):
    assert normalize(value) == expected


def test_ranking_and_dedup():
    files = [
        dict(id=1, title="Breaking Bad S02 Hindi", fingerprint="1"),
        dict(id=2, title="Breaking Bad S01 Hindi 1080p", fingerprint="2"),
        dict(id=3, title="Breaking Bad S01 Hindi 1080p", fingerprint="2"),
    ]
    results = rank(Query.parse("Breaking Bad S01 Hindi"), files, 100)
    assert [f["id"] for f in results] == [2, 1]
    assert len(rank(Query.parse("Breaking Bad"), files, 1)) == 1


@pytest.mark.parametrize("query", ["", "!!!", "1080p S01", "x", "x" * 121])
def test_invalid_query(query):
    with pytest.raises(ValueError):
        Query.parse(query)


def test_pagination():
    assert page(list(range(17)), 2, 8) == ([16], 3)
    assert page([], 0, 8) == ([], 1)
    with pytest.raises(ValueError):
        page([1], -1, 8)
    with pytest.raises(ValueError):
        page([1], 1, 8)


def test_cache_expiry_and_bound():
    now = [0]
    cache = Cache(2, 5, clock=lambda: now[0])
    cache.put("a", [])
    assert cache.get("a") == []
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None
    now[0] = 5
    assert cache.get("b") is None


def test_limiter_atomic_buckets_and_expiry():
    now = [0]
    limiter = Limiter(60, clock=lambda: now[0])
    limiter.check([("u1", 2), ("chat", 1)])
    with pytest.raises(Busy):
        limiter.check([("u2", 2), ("chat", 1)])
    assert "u2" not in limiter.events
    now[0] = 60
    limiter.check([("u2", 2), ("chat", 1)])


def test_limiter_capacity_fails_closed():
    limiter = Limiter(60, capacity=1)
    limiter.check([("u", 1)])
    with pytest.raises(Busy):
        limiter.check([("another", 1)])


def test_welcome_escapes_all_user_fields():
    user = SimpleNamespace(id=42, first_name="<b>Eve &", full_name="Eve <script>", username="evil<")
    text = render("{name}|{first_name}|{username}|{id}|{mention}", user)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "@evil&lt;" in text
    assert '<a href="tg://user?id=42">&lt;b&gt;Eve &amp;</a>' in text


@pytest.mark.parametrize(
    "value",
    [
        "<b>Oops",
        "<b><i>x</b></i>",
        "{user.password}",
        "{name!r}",
        "<script>x</script>",
        '<a href="javascript:x">x</a>',
        '<a href="https://x/{name}">x</a>',
        "x < y",
        "<b>x</b",
        "<b/>",
    ],
)
def test_invalid_welcome(value):
    with pytest.raises(ValueError):
        validate_template(value)


def test_config_validation_and_redaction(settings):
    assert settings.owners == {1, 2}
    assert "secret" not in repr(settings)
    assert settings.token not in repr(settings)
    for values in ({}, {"BOT_TOKEN": "bad"}, {"BOT_TOKEN": "123456:" + "a" * 35, "API_ID": "no"}):
        with pytest.raises(ConfigError):
            Settings.load(values)


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1; DROP TABLE af_sudo", "9223372036854775808"])
def test_id_validation(raw):
    with pytest.raises(ValueError):
        positive_id(raw)


def test_channel_validation():
    assert channel_id("-1001234567890") < 0
    with pytest.raises(ValueError):
        channel_id("123456")


async def test_permissions_root_sudo_and_group():
    db = SimpleNamespace(is_sudo=AsyncMock(return_value=True), approved=AsyncMock(return_value=False))
    p = Permissions({1}, db)
    assert p.root(1) and not p.root(2)
    assert await p.staff(2)
    assert not await p.search(2, -1001234567890, "supergroup", True)
    db.approved.return_value = True
    assert await p.search(2, -1001234567890, "supergroup", False)
    assert not await p.search(2, 2, "private", False)
    assert await p.search(1, 1, "private", False)
    assert not await p.search(1, -1, "channel", True)


async def test_search_cache_and_sources(settings):
    f = dict(id=1, title="Film", fingerprint="f")
    db = SimpleNamespace(channels=AsyncMock(return_value=[-1001234567890]), candidates=AsyncMock(return_value=[]))
    source = SimpleNamespace(search=AsyncMock(return_value=([f], False)))
    service = SearchService(db, source, settings)
    query = Query.parse("Film")
    assert (await service.search(query, 10))[0] == [f]
    await service.search(query, 10)
    assert source.search.await_count == 1
    db.channels.return_value = []
    with pytest.raises(Busy):
        await service.search(query, 10)


async def test_partial_failure_not_cached(settings):
    db = SimpleNamespace(channels=AsyncMock(return_value=[-1001234567890]), candidates=AsyncMock(return_value=[]))
    source = SimpleNamespace(search=AsyncMock(return_value=([], True)))
    service = SearchService(db, source, settings)
    await service.search(Query.parse("Film"), 10)
    await service.search(Query.parse("Film"), 10)
    assert source.search.await_count == 2
