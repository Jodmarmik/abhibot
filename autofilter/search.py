"""Pure query parsing/ranking and bounded in-memory coordination."""

import asyncio
import re
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from difflib import SequenceMatcher

ALIASES = {"la casa de papel": "money heist", "money heist": "money heist"}
LANGUAGES = {"hindi", "english", "tamil", "telugu", "malayalam", "kannada", "bengali", "multi", "dual"}


def normalize(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[_\W]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"season\s*(\d{1,2})", lambda m: f"s{int(m[1]):02}", text)
    text = re.sub(r"episode\s*(\d{1,3})", lambda m: f"e{int(m[1]):02}", text)
    text = re.sub(r"\bs(\d{1,2})e(\d{1,3})\b", r"s\1 e\2", text)
    text = re.sub(r"\bs(\d{1,2})\b", lambda m: f"s{int(m[1]):02}", text)
    text = re.sub(r"\be(\d{1,3})\b", lambda m: f"e{int(m[1]):02}", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"[_\s]+", " ", text).strip()
    for alias, canonical in ALIASES.items():
        text = text.replace(alias, canonical)
    return text


@dataclass(frozen=True)
class Query:
    text: str
    title: str
    tags: frozenset[str]

    @classmethod
    def parse(cls, value, max_length=120):
        if not 2 <= len(value.strip()) <= max_length:
            raise ValueError(f"Enter a title between 2 and {max_length} characters.")
        text = normalize(value)
        words = text.split()
        tags = frozenset(
            w for w in words if w in LANGUAGES or re.fullmatch(r"(?:19|20)\d{2}|s\d{2}|e\d{2,3}|\d{3,4}p|4k|2160p", w)
        )
        title = " ".join(w for w in words if w not in tags)
        if len(title) < 2 or not any(c.isalnum() for c in title):
            raise ValueError("Include a movie or series title in your search.")
        return cls(text, title, tags)


def rank(query, files, limit):
    def score(f):
        text = normalize(f["title"])
        words = set(text.split())
        similarity = SequenceMatcher(None, query.title, text).ratio()
        coverage = len(set(query.title.split()) & words) / len(set(query.title.split()))
        return coverage * 100 + similarity * 30 + sum(25 if tag in words else -15 for tag in query.tags)

    seen, result = set(), []
    for f in sorted(files, key=lambda f: (-score(f), f["id"])):
        if f["fingerprint"] not in seen:
            seen.add(f["fingerprint"])
            result.append(f)
        if len(result) >= limit:
            break
    return result


def page(items, number, size):
    pages = max(1, (len(items) + size - 1) // size)
    if not 0 <= number < pages:
        raise ValueError("That result page is unavailable.")
    return items[number * size : (number + 1) * size], pages


class Cache:
    def __init__(self, size, ttl, clock=time.monotonic):
        self.size, self.ttl, self.clock = size, ttl, clock
        self.data = OrderedDict()

    def get(self, key):
        entry = self.data.get(key)
        if not entry:
            return None
        expiry, value = entry
        if self.clock() >= expiry:
            self.data.pop(key, None)
            return None
        self.data.move_to_end(key)
        return value

    def put(self, key, value):
        self.data[key] = (self.clock() + self.ttl, value)
        self.data.move_to_end(key)
        while len(self.data) > self.size:
            self.data.popitem(last=False)

    def clear(self):
        self.data.clear()


class Busy(ValueError):
    pass


class Limiter:
    def __init__(self, window, capacity=10000, clock=time.monotonic):
        self.window, self.capacity, self.clock = window, capacity, clock
        self.events = OrderedDict()

    def check(self, limits):
        # No await: checking and consuming all buckets is atomic on the event loop.
        now = self.clock()
        while self.events:
            key, (last, _) = next(iter(self.events.items()))
            if now - last < self.window:
                break
            self.events.pop(key)
        for key, limit in limits:
            _, events = self.events.get(key, (now, deque()))
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= limit:
                raise Busy("Too many requests. Please try again shortly.")
        new_keys = sum(key not in self.events for key, _ in limits)
        if len(self.events) + new_keys > self.capacity:
            raise Busy("The bot is busy. Please try again shortly.")
        for key, _ in limits:
            _, events = self.events.pop(key, (now, deque()))
            events.append(now)
            self.events[key] = (now, events)


class SearchService:
    def __init__(self, db, source, settings):
        self.db, self.source, self.settings = db, source, settings
        self.cache = Cache(settings.cache_size, settings.cache_ttl)
        self.gate = asyncio.Semaphore(settings.concurrency)
        self.pending = set()

    async def search(self, query, limit):
        sources = await self.db.channels("source")
        stores = await self.db.channels("storage")
        allowed = sorted(set(sources) & set(stores))
        if not allowed:
            raise Busy("No authorized sources are ready. Please contact an owner.")
        key = (query.text, tuple(allowed), limit)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if key in self.pending or self.gate.locked():
            raise Busy("Search is busy. Please try again shortly.")
        self.pending.add(key)
        try:
            async with self.gate, asyncio.timeout(self.settings.timeout):
                existing = await self.db.candidates(query.title, self.settings.search_limit * 5)
                fresh, unavailable = await self.source.search(query, allowed)
                merged = {f["id"]: f for f in existing + fresh}
                results = rank(query, list(merged.values()), limit)
                value = (results, unavailable)
                if not unavailable:
                    self.cache.put(key, value)
                return value
        except TimeoutError:
            raise Busy("Search timed out. Please try again shortly.") from None
        finally:
            self.pending.discard(key)
