"""A token-bucket rate limiter, for the gate before our key goes on a hosted box.

Hosting with the server's provider key means every request spends our money, so
the gate is checked before a run starts. Keyed by user id. Pure and clock-
injectable so it is testable without sleeping.
"""

import time


class RateLimiter:
    def __init__(self, rate_per_min, burst=None):
        self.rate_per_min = float(rate_per_min)
        self.capacity = float(burst if burst is not None else max(1.0, rate_per_min))
        self._state = {}                 # key -> (tokens, last_ts)

    def allow(self, key, now=None):
        now = time.monotonic() if now is None else now
        tokens, last = self._state.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * (self.rate_per_min / 60.0))
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        return True
