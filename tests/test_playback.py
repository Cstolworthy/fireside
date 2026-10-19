"""One owner per book; handoff stops the other device; position is server-stamped."""

import unittest

from host import playback as pb
from host.db import connect


class TestPlayback(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_position_roundtrip(self):
        self.assertIsNone(pb.get_position(self.conn, "u1", "b1"))
        pb.put_position(self.conn, "u1", "b1", 3, 12.5, "pc")
        p = pb.get_position(self.conn, "u1", "b1")
        self.assertEqual(p["chapter_index"], 3)
        self.assertEqual(p["offset_seconds"], 12.5)
        self.assertTrue(p["updated_at"])

    def test_claim_hands_off_and_stops_the_other(self):
        pb.claim(self.conn, "u1", "b1", "pc")
        self.assertEqual(pb.owner(self.conn, "u1", "b1"), "pc")
        res = pb.claim(self.conn, "u1", "b1", "phone")
        self.assertEqual(res["stopped"], "pc")
        self.assertTrue(pb.stop_pending(self.conn, "u1", "b1", "pc"))
        self.assertFalse(pb.stop_pending(self.conn, "u1", "b1", "phone"))
        self.assertEqual(pb.owner(self.conn, "u1", "b1"), "phone")

    def test_ack_clears_stop(self):
        pb.claim(self.conn, "u1", "b1", "pc")
        pb.claim(self.conn, "u1", "b1", "phone")
        pb.ack_stop(self.conn, "u1", "b1", "pc")
        self.assertFalse(pb.stop_pending(self.conn, "u1", "b1", "pc"))

    def test_position_is_per_user_and_book(self):
        pb.put_position(self.conn, "u1", "b1", 1, 1.0, "pc")
        pb.put_position(self.conn, "u2", "b1", 9, 9.0, "phone")
        self.assertEqual(pb.get_position(self.conn, "u1", "b1")["chapter_index"], 1)
        self.assertEqual(pb.get_position(self.conn, "u2", "b1")["chapter_index"], 9)

    def test_release_clears_ownership(self):
        pb.claim(self.conn, "u1", "b1", "pc")
        pb.release(self.conn, "u1", "b1", "pc")
        self.assertIsNone(pb.owner(self.conn, "u1", "b1"))

    def test_last_position_is_the_newest_book(self):
        self.assertEqual(pb.last_position(self.conn, "u1"), {})
        pb.put_position(self.conn, "u1", "b1", 2, 5.0, "pc")
        pb.put_position(self.conn, "u1", "b2", 7, 1.0, "pc")
        # updated_at is second-resolution; pin them so the order is unambiguous
        self.conn.execute("UPDATE positions SET updated_at='2026-01-01T00:00:01Z' WHERE book_id='b1'")
        self.conn.execute("UPDATE positions SET updated_at='2026-01-01T00:00:02Z' WHERE book_id='b2'")
        self.conn.commit()
        last = pb.last_position(self.conn, "u1")
        self.assertEqual(last["book_id"], "b2")
        self.assertEqual(last["chapter_index"], 7)
        self.assertEqual(last["offset_seconds"], 1.0)

    def test_last_position_carries_the_title(self):
        self.conn.execute("INSERT INTO books(id,user_id,title) VALUES('b1','u1','Fire')")
        self.conn.commit()
        pb.put_position(self.conn, "u1", "b1", 0, 0.0, "pc")
        self.assertEqual(pb.last_position(self.conn, "u1")["title"], "Fire")


if __name__ == "__main__":
    unittest.main()
