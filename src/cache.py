"""Tiny in-memory cache with a TTL. If two requests ask for the same key at the same time, only one goes to the provider."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Hashable, Tuple


class TTLCache:
    def __init__(self, name: str, max_entries: int = 5000):
        self.name = name
        self._max = max_entries
        self._data: Dict[Hashable, Tuple[float, Any]] = {}
        self._key_locks: Dict[Hashable, threading.Lock] = {}
        self._lock = threading.Lock()

    def get_or_compute(self, key: Hashable, ttl_s: float, factory: Callable[[], Any]) -> Any:
        # errors are not cached, the next call just tries again
        with self._lock:
            entry = self._data.get(key)
            if entry and entry[0] > time.monotonic():
                return entry[1]
            key_lock = self._key_locks.setdefault(key, threading.Lock())

        with key_lock:
            with self._lock:
                entry = self._data.get(key)
                if entry and entry[0] > time.monotonic():
                    return entry[1]
            value = factory()
            with self._lock:
                self._data[key] = (time.monotonic() + ttl_s, value)
                if len(self._data) > self._max:
                    self._purge()
            return value

    def _purge(self) -> None:
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
            self._data.pop(k, None)
            self._key_locks.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._key_locks.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


SHORT_TERM_CACHE = TTLCache("short_term")
SEASONAL_CACHE = TTLCache("seasonal")
