"""In-memory sliding-window rate limits. Nothing is written to disk."""

import math
import threading
import time
from collections import deque


class RateLimiter:
    """Allow at most `limit` events per `window` seconds for each key."""

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        events = self._events.get(key)
        if events is None:
            return deque()
        while events and now - events[0] >= self.window:
            events.popleft()
        if not events:
            del self._events[key]  # don't keep empty keys around
        return events

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, self.clock())) >= self.limit

    def hit(self, key: str) -> bool:
        """Record one event. Returns False (and records nothing) if the key is over its limit."""
        with self._lock:
            now = self.clock()
            events = self._prune(key, now)
            if len(events) >= self.limit:
                return False
            self._events.setdefault(key, events).append(now)
            return True

    def retry_after(self, key: str) -> int:
        """Seconds until the key may try again (0 if it may now)."""
        with self._lock:
            now = self.clock()
            events = self._prune(key, now)
            if len(events) < self.limit:
                return 0
            return max(1, math.ceil(self.window - (now - events[0])))
