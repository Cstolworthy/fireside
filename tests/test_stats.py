"""Per-user listening stats: chars voiced, books listened, time heard."""

import unittest

from host import stats
from host.db import connect


class TestStats(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def _job(self, user, provider, done_chars, status="done"):
        self._n = getattr(self, "_n", 0) + 1
        self.conn.execute(
            "INSERT INTO jobs(id,user_id,book_id,kind,provider,status,current,"
            "done_chars,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("j%d" % self._n, user, "b1", "render", provider, status, 1,
             done_chars, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        self.conn.commit()

    def test_characters_sum_by_provider_and_only_for_done_jobs(self):
        self._job("u1", "azure", 100)
        self._job("u1", "azure", 50)
        self._job("u1", "minimax", 25)
        self._job("u1", "azure", 999, status="error")     # a failed run is not counted
        self._job("u2", "azure", 777)                     # another user's run is not mine
        s = stats.user_stats(self.conn, "u1")
        self.assertEqual(s["chars_total"], 175)
        self.assertEqual(s["chars_by_provider"], {"azure": 150, "minimax": 25})

    def test_listen_log_is_append_only_and_anonymous_ticks_are_dropped(self):
        stats.add_listen(self.conn, "u1", "b1", 10)
        stats.add_listen(self.conn, "u1", "b1", 5)
        stats.add_listen(self.conn, "u1", "b2", 2)
        stats.add_listen(self.conn, "u1", "b1", 0)        # zero is not a tick
        stats.add_listen(self.conn, "u1", "b1", -4)       # nor is negative
        stats.add_listen(self.conn, "", "b1", 9)          # nor anonymous
        s = stats.user_stats(self.conn, "u1")
        self.assertEqual(s["listen_seconds"], 17.0)
        self.assertEqual(s["books_listened"], 2)          # b1 and b2

    def test_empty_user_is_all_zero_never_a_guess(self):
        s = stats.user_stats(self.conn, "nobody")
        self.assertEqual(s, {"chars_total": 0, "chars_by_provider": {},
                             "books_listened": 0, "listen_seconds": 0.0})


if __name__ == "__main__":
    unittest.main()
