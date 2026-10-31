"""Validated deployment settings; credentials are never included in reprs."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


def positive_id(value: str) -> int:
    if not re.fullmatch(r"[1-9]\d{0,18}", value) or int(value) > 2**63 - 1:
        raise ValueError("Expected a positive numeric Telegram user ID.")
    return int(value)


def channel_id(value: str) -> int:
    if not re.fullmatch(r"-100[1-9]\d{5,15}", value) or abs(int(value)) > 2**63 - 1:
        raise ValueError("Expected a numeric channel/supergroup ID starting with -100.")
    return int(value)


def group_id(value: str) -> int:
    if not re.fullmatch(r"-[1-9]\d{0,18}", value) or abs(int(value)) > 2**63 - 1:
        raise ValueError("Expected a negative numeric Telegram group ID.")
    return int(value)


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    api_id: int
    api_hash: str = field(repr=False)
    session_path: str = field(repr=False)
    database_url: str = field(repr=False)
    owners: frozenset[int]
    private_search: bool = False
    search_limit: int = 100
    source_limit: int = 100
    max_sources: int = 20
    page_size: int = 8
    cache_ttl: int = 120
    cache_size: int = 200
    concurrency: int = 4
    user_rate: int = 10
    chat_rate: int = 30
    rate_window: int = 60
    timeout: int = 30
    max_flood_wait: int = 15
    query_length: int = 120
    pool_size: int = 10
    default_welcome: str = "Welcome, {mention}! Send a movie or series title to search."
    default_photo: str = ""

    @classmethod
    def load(cls, environ=None):
        if environ is None:
            load_dotenv(override=False)
            environ = os.environ

        def required(key):
            value = environ.get(key, "").strip()
            if not value:
                raise ConfigError(f"{key} is required.")
            return value

        def number(key, default, low, high):
            try:
                value = int(environ.get(key, str(default)))
            except ValueError:
                raise ConfigError(f"{key} must be an integer.") from None
            if not low <= value <= high:
                raise ConfigError(f"{key} must be between {low} and {high}.")
            return value

        def boolean(key, default):
            value = environ.get(key, str(default)).lower()
            if value not in ("true", "false"):
                raise ConfigError(f"{key} must be true or false.")
            return value == "true"

        token = required("BOT_TOKEN")
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
            raise ConfigError("BOT_TOKEN has an invalid format.")
        api_hash = required("API_HASH")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ConfigError("API_HASH must contain 32 hexadecimal characters.")
        try:
            owners = frozenset(positive_id(x) for x in re.split(r"[,\s]+", required("OWNER_IDS")))
        except ValueError:
            raise ConfigError("OWNER_IDS must contain positive numeric IDs.") from None
        dsn = required("DATABASE_URL")
        try:
            url = urlsplit(dsn)
            if url.scheme not in ("postgres", "postgresql") or not url.hostname or not url.path.strip("/"):
                raise ValueError
            _ = url.port
        except ValueError:
            raise ConfigError("DATABASE_URL must be a PostgreSQL connection URL.") from None
        session_path = str(Path(required("TELETHON_SESSION_PATH")).expanduser())
        if not session_path.endswith(".session"):
            raise ConfigError("TELETHON_SESSION_PATH must end in .session.")
        return cls(
            token=token,
            api_id=number("API_ID", 0, 1, 2**31 - 1),
            api_hash=api_hash,
            session_path=session_path,
            database_url=dsn,
            owners=owners,
            private_search=boolean("PRIVATE_SEARCH", False),
            **{
                name: number(key, default, low, high)
                for name, key, default, low, high in (
                    ("search_limit", "SEARCH_LIMIT", 100, 1, 500),
                    ("source_limit", "SOURCE_SCAN_LIMIT", 100, 1, 500),
                    ("max_sources", "MAX_SOURCES", 20, 1, 100),
                    ("page_size", "PAGE_SIZE", 8, 1, 20),
                    ("cache_ttl", "CACHE_TTL", 120, 1, 3600),
                    ("cache_size", "CACHE_SIZE", 200, 1, 2000),
                    ("concurrency", "SEARCH_CONCURRENCY", 4, 1, 32),
                    ("user_rate", "USER_RATE", 10, 1, 120),
                    ("chat_rate", "CHAT_RATE", 30, 1, 300),
                    ("rate_window", "RATE_WINDOW", 60, 1, 3600),
                    ("timeout", "SEARCH_TIMEOUT", 30, 5, 120),
                    ("max_flood_wait", "MAX_FLOOD_WAIT", 15, 0, 60),
                    ("query_length", "MAX_QUERY_LENGTH", 120, 10, 256),
                    ("pool_size", "DB_POOL_SIZE", 10, 2, 50),
                )
            },
            default_welcome=environ.get("DEFAULT_WELCOME", cls.default_welcome),
            default_photo=environ.get("DEFAULT_PHOTO", ""),
        )
