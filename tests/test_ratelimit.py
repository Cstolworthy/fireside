"""Token bucket: the gate before our key runs."""

import unittest

from host.ratelimit import RateLimiter


class TestRateLimiter(unittest.TestCase):
    def test_burst_then_deny(self):
        rl = RateLimiter(rate_per_min=60, burst=3)
        self.assertTrue(rl.allow("u", now=0))
        self.assertTrue(rl.allow("u", now=0))
        self.assertTrue(rl.allow("u", now=0))
        self.assertFalse(rl.allow("u", now=0))

    def test_refills_over_time(self):
        rl = RateLimiter(rate_per_min=60, burst=1)     # 1 per second
        self.assertTrue(rl.allow("u", now=0))
        self.assertFalse(rl.allow("u", now=0.5))
        self.assertTrue(rl.allow("u", now=1.1))

    def test_per_key(self):
        rl = RateLimiter(rate_per_min=60, burst=1)
        self.assertTrue(rl.allow("a", now=0))
        self.assertTrue(rl.allow("b", now=0), "other users are unaffected")


if __name__ == "__main__":
    unittest.main()
