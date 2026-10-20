"""Storage accounting: a blob is billed only to its sole referencer.

The shared render cache and shared chapter text must cost every referencing user
nothing; a blob only one user names is that user's to pay for. Sizes come from
the recorded audio_bytes when present, else from the storage backend.
"""

import os
import tempfile
import unittest

from host import quota
from host.db import connect, now
from storage.local import LocalBackend


class TestQuota(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.tmp = tempfile.mkdtemp(prefix="fireside-quota-")
        self.store = LocalBackend(self.tmp)
        self._u("alice")
        self._u("bob")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _u(self, uid):
        self.conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                          (uid, uid, now()))
        self.conn.commit()

    def _book(self, bid, uid, cover_ref=""):
        self.conn.execute(
            "INSERT INTO books(id,user_id,title,source_json,cover_ref,created_at) "
            "VALUES(?,?,?,?,?,?)", (bid, uid, bid, "{}", cover_ref, now()))
        self.conn.commit()

    def _chapter(self, bid, idx, text_ref=None, audio_ref=None, audio_bytes=0):
        self.conn.execute(
            "INSERT INTO chapters(book_id,idx,title,chars,text_ref,audio_ref,audio_bytes) "
            "VALUES(?,?,?,?,?,?,?)",
            (bid, idx, "c", 0, text_ref, audio_ref, audio_bytes))
        self.conn.commit()

    def test_sole_referencer_is_billed(self):
        self._book("b1", "alice")
        self._chapter("b1", 0, audio_ref="A" * 32, audio_bytes=500)
        u = quota.usage(self.conn, self.store, "alice")
        self.assertEqual(u["own_bytes"], 500)
        self.assertEqual(u["shared_bytes"], 0)

    def test_shared_blob_is_free_for_both(self):
        self._book("b1", "alice")
        self._book("b2", "bob")
        self._chapter("b1", 0, audio_ref="A" * 32, audio_bytes=500)
        self._chapter("b2", 0, audio_ref="A" * 32, audio_bytes=500)
        a = quota.usage(self.conn, self.store, "alice")
        b = quota.usage(self.conn, self.store, "bob")
        self.assertEqual(a["own_bytes"], 0)
        self.assertEqual(b["own_bytes"], 0)
        self.assertEqual(a["shared_bytes"], 500)
        self.assertEqual(b["shared_bytes"], 500)

    def test_same_ref_twice_in_one_shelf_counts_once(self):
        self._book("b1", "alice")
        self._chapter("b1", 0, audio_ref="A" * 32, audio_bytes=500)
        self._chapter("b1", 1, audio_ref="A" * 32, audio_bytes=500)
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 500)

    def test_text_and_cover_sized_from_store_when_no_recorded_bytes(self):
        tref = self.store.put(b"x" * 12)
        cref = self.store.put(b"y" * 7)
        self._book("b1", "alice", cover_ref=cref)
        self._chapter("b1", 0, text_ref=tref)
        u = quota.usage(self.conn, self.store, "alice")
        self.assertEqual(u["own_bytes"], 12 + 7)

    def test_shared_text_is_free(self):
        tref = self.store.put(b"same chapter text")
        self._book("b1", "alice")
        self._book("b2", "bob")
        self._chapter("b1", 0, text_ref=tref)
        self._chapter("b2", 0, text_ref=tref)
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 0)
        self.assertEqual(quota.usage(self.conn, self.store, "bob")["own_bytes"], 0)

    def test_drop_back_to_one_referencer_rebills(self):
        # bob deletes his book -> the blob is alice's alone again
        self._book("b1", "alice")
        self._book("b2", "bob")
        self._chapter("b1", 0, audio_ref="A" * 32, audio_bytes=500)
        self._chapter("b2", 0, audio_ref="A" * 32, audio_bytes=500)
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 0)
        self.conn.execute("DELETE FROM chapters WHERE book_id='b2'")
        self.conn.commit()
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 500)

    def test_allowance_and_room(self):
        self.assertEqual(quota.allowance_bytes("platform"), quota.PLATFORM_ALLOWANCE_BYTES)
        self.assertEqual(quota.allowance_bytes(""), 0)
        self.assertTrue(quota.check_room(self.conn, self.store, "alice", "platform", 10))
        self.assertFalse(quota.check_room(self.conn, self.store, "alice", "", 10))

    def test_room_refuses_over_allowance(self):
        self._book("b1", "alice")
        self._chapter("b1", 0, audio_ref="A" * 32,
                      audio_bytes=quota.PLATFORM_ALLOWANCE_BYTES)
        self.assertTrue(quota.check_room(self.conn, self.store, "alice", "platform", 0))
        self.assertFalse(quota.check_room(self.conn, self.store, "alice", "platform", 1))


if __name__ == "__main__":
    unittest.main()


class TestGraceWindow(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.tmp = tempfile.mkdtemp(prefix="fireside-quota2-")
        self.store = LocalBackend(self.tmp)
        for uid in ("alice", "bob"):
            self.conn.execute("INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                              (uid, uid, now()))
        self.conn.commit()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _book_ch(self, bid, uid, ref, nbytes=500):
        self.conn.execute(
            "INSERT OR REPLACE INTO books(id,user_id,title,source_json,created_at) VALUES(?,?,?,?,?)",
            (bid, uid, bid, "{}", now()))
        self.conn.execute(
            "INSERT INTO chapters(book_id,idx,title,chars,audio_ref,audio_bytes) "
            "VALUES(?,?,?,?,?,?)", (bid, 0, "c", 0, ref, nbytes))
        self.conn.commit()

    def test_never_shared_is_not_held(self):
        self._book_ch("b1", "alice", "A" * 32)
        r = quota.sweep(self.conn, self.store, ts=1000)
        self.assertEqual(r["new_holds"], [])
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 500)

    def test_drop_to_one_opens_window_not_a_bill(self):
        self._book_ch("b1", "alice", "A" * 32)
        self._book_ch("b2", "bob", "A" * 32)
        quota.sweep(self.conn, self.store, ts=1000)
        self.conn.execute("DELETE FROM chapters WHERE book_id='b2'")
        self.conn.commit()
        r = quota.sweep(self.conn, self.store, ts=2000)
        self.assertEqual(r["new_holds"], ["A" * 32])
        u = quota.usage(self.conn, self.store, "alice")
        self.assertEqual(u["own_bytes"], 0)
        self.assertEqual(u["pending_bytes"], 500)
        p = quota.pending_for(self.conn, self.store, "alice")
        self.assertEqual(p[0]["purge_at"], 2000 + quota.GRACE_SECONDS)

    def test_reshare_clears_window(self):
        self._book_ch("b1", "alice", "A" * 32)
        self._book_ch("b2", "bob", "A" * 32)
        quota.sweep(self.conn, self.store, ts=1000)
        self.conn.execute("DELETE FROM chapters WHERE book_id='b2'")
        self.conn.commit()
        quota.sweep(self.conn, self.store, ts=2000)
        self._book_ch("b2", "bob", "A" * 32)
        quota.sweep(self.conn, self.store, ts=3000)
        self.assertEqual(quota.pending_for(self.conn, self.store, "alice"), [])
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["shared_bytes"], 500)

    def test_adopt_ends_window_and_bills(self):
        self._book_ch("b1", "alice", "A" * 32)
        self._book_ch("b2", "bob", "A" * 32)
        quota.sweep(self.conn, self.store, ts=1000)
        self.conn.execute("DELETE FROM chapters WHERE book_id='b2'")
        self.conn.commit()
        quota.sweep(self.conn, self.store, ts=2000)
        self.assertEqual(quota.adopt(self.conn, ["A" * 32]), 1)
        u = quota.usage(self.conn, self.store, "alice")
        self.assertEqual(u["own_bytes"], 500)
        self.assertEqual(u["pending_bytes"], 0)

    def test_expired_window_is_reported(self):
        self._book_ch("b1", "alice", "A" * 32)
        self._book_ch("b2", "bob", "A" * 32)
        quota.sweep(self.conn, self.store, ts=1000)
        self.conn.execute("DELETE FROM chapters WHERE book_id='b2'")
        self.conn.commit()
        quota.sweep(self.conn, self.store, ts=2000)          # detects the drop, opens the window
        r = quota.sweep(self.conn, self.store, ts=2000 + quota.GRACE_SECONDS + 1)
        self.assertIn("A" * 32, r["expired"])

    def test_orphan_is_swept_and_blob_deleted(self):
        ref = self.store.put(b"audio")
        self._book_ch("b1", "alice", ref)
        self._book_ch("b2", "bob", ref)
        quota.sweep(self.conn, self.store, ts=1000)
        self.conn.execute("DELETE FROM chapters WHERE book_id IN ('b1','b2')")
        self.conn.commit()
        r = quota.sweep(self.conn, self.store, ts=2000)
        self.assertIn(ref, r["orphans"])
        self.assertFalse(self.store.has(ref))

    def test_purge_clears_refs_and_blob(self):
        ref = self.store.put(b"audio")
        self._book_ch("b1", "alice", ref)
        quota.sweep(self.conn, self.store, ts=1000)
        quota.purge(self.conn, self.store, [ref])
        self.assertFalse(self.store.has(ref))
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
