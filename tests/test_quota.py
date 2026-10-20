"""Storage accounting: a held book bills its own bytes.

Every distinct blob on a user's shelf counts against that user, once, with no
sharing discount. Two users holding the same book each bill its bytes. Sizes come
from the recorded audio_bytes when present, else from the storage backend.
"""

import os
import shutil
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
        for uid in ("alice", "bob"):
            self._u(uid)

    def tearDown(self):
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

    def _rendition(self, bid, idx, audio_ref, audio_bytes=0):
        self.conn.execute(
            "INSERT INTO renditions(rk,book_id,idx,audio_ref,audio_bytes,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("rk%d" % idx, bid, idx, audio_ref, audio_bytes, now()))
        self.conn.commit()

    def test_own_shelf_is_billed(self):
        self._book("b1", "alice")
        self._chapter("b1", 0, audio_ref="A" * 32, audio_bytes=500)
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 500)

    def test_two_holders_each_bill_the_whole_blob(self):
        # the same book held by two people is not a sharing discount: each pays
        self._book("b1", "alice")
        self._book("b2", "bob")
        self._chapter("b1", 0, audio_ref="A" * 32, audio_bytes=500)
        self._chapter("b2", 0, audio_ref="A" * 32, audio_bytes=500)
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 500)
        self.assertEqual(quota.usage(self.conn, self.store, "bob")["own_bytes"], 500)

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

    def test_renditions_are_billed_too(self):
        self._book("b1", "alice")
        self._chapter("b1", 0, text_ref=self.store.put(b"text"))
        self._rendition("b1", 0, "A" * 32, audio_bytes=500)
        self._rendition("b1", 1, "B" * 32, audio_bytes=250)
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 4 + 750)

    def test_chapter_audio_and_rendition_same_ref_not_double_counted(self):
        self._book("b1", "alice")
        self._chapter("b1", 0, audio_ref="A" * 32, audio_bytes=500)
        self._rendition("b1", 0, "A" * 32, audio_bytes=500)
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 500)

    def test_bob_deleting_his_book_does_not_change_alice(self):
        self._book("b1", "alice")
        self._book("b2", "bob")
        self._chapter("b1", 0, audio_ref="A" * 32, audio_bytes=500)
        self._chapter("b2", 0, audio_ref="A" * 32, audio_bytes=500)
        self.conn.execute("DELETE FROM chapters WHERE book_id='b2'")
        self.conn.commit()
        self.assertEqual(quota.usage(self.conn, self.store, "alice")["own_bytes"], 500)
        self.assertEqual(quota.usage(self.conn, self.store, "bob")["own_bytes"], 0)

    def test_allowance_and_room(self):
        self.assertEqual(quota.allowance_bytes("platform"), quota.PLATFORM_ALLOWANCE_BYTES)
        # an account with no plan is on the platform tier for launch (payments held)
        self.assertEqual(quota.allowance_bytes(""), quota.PLATFORM_ALLOWANCE_BYTES)
        self.assertTrue(quota.check_room(self.conn, self.store, "alice", "platform", 10))
        self.assertTrue(quota.check_room(self.conn, self.store, "alice", "", 10))

    def test_room_refuses_over_allowance(self):
        self._book("b1", "alice")
        self._chapter("b1", 0, audio_ref="A" * 32,
                      audio_bytes=quota.PLATFORM_ALLOWANCE_BYTES)
        self.assertTrue(quota.check_room(self.conn, self.store, "alice", "platform", 0))
        self.assertFalse(quota.check_room(self.conn, self.store, "alice", "platform", 1))

    def test_storage_contract_default_size(self):
        from host.contracts import Storage

        class Backend(Storage):
            def __init__(self):
                self.blobs = {}

            def put(self, data, ref="R" * 64):
                self.blobs[ref] = bytes(data)
                return ref

            def has(self, ref):
                return ref in self.blobs

            def get(self, ref):
                return self.blobs[ref]

            def delete(self, ref):
                self.blobs.pop(ref, None)

        b = Backend()
        ref = b.put(b"abc")
        self.assertEqual(b.size(ref), 3)
        self.assertEqual(b.size("nope"), 0)


if __name__ == "__main__":
    unittest.main()


class TestStorageHttp(unittest.TestCase):
    """The storage screen and the write gates, over the real HTTP routes."""

    def setUp(self):
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from host.app import App, make_handler
        from tests.test_pluginhost import write_plugin
        from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY

        self.tmp = tempfile.mkdtemp(prefix="fireside-quota-http-")
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"), storage_id="mem")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self._urlopen = urllib.request.urlopen
        self._request = urllib.request.Request
        self._json = json
        self._saved = dict(quota.PLAN_ALLOWANCE)
        quota.PLAN_ALLOWANCE["platform"] = 100

    def tearDown(self):
        self.httpd.shutdown()
        quota.PLAN_ALLOWANCE.clear()
        quota.PLAN_ALLOWANCE.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path, obj):
        req = self._request("http://127.0.0.1:%d%s" % (self.port, path),
                            data=self._json.dumps(obj).encode(),
                            headers={"Content-Type": "application/json"})
        try:
            with self._urlopen(req) as r:
                return r.status, self._json.loads(r.read())
        except Exception as e:                      # HTTPError carries the body
            body = getattr(e, "read", lambda: b"")()
            return getattr(e, "code", 0), self._json.loads(body or b"{}")

    def _get(self, path):
        with self._urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, self._json.loads(r.read())

    def test_storage_screen_reports_own_bytes_and_allowance(self):
        self._post("/import", {"user": "u1", "kind": "upload", "filename": "b.txt"})
        s, u = self._get("/storage?user=u1")
        self.assertEqual(s, 200)
        self.assertEqual(u["own_bytes"], len("hello world") + len("second chapter"))
        self.assertEqual(u["allowance_bytes"], 100)
        self.assertEqual(u["remaining_bytes"], 100 - u["own_bytes"])
        self.assertNotIn("pending", u)
        self.assertNotIn("shared_bytes", u)

    def test_import_over_allowance_is_refused(self):
        quota.PLAN_ALLOWANCE["platform"] = 10         # smaller than the book text
        s, r = self._post("/import", {"user": "u1", "kind": "upload", "filename": "b.txt"})
        self.assertEqual(s, 507)
        self.assertIn("storage", r["error"])
        s, lib = self._get("/library?user=u1")
        self.assertEqual(lib, [])                     # refused, nothing written

    def test_render_over_allowance_is_refused(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload", "filename": "b.txt"})
        self.assertEqual(s, 200)
        s, r = self._post("/render", {"user": "u1", "book": imp["book_id"],
                                      "provider": "fake", "voice": "vv"})
        self.assertEqual(s, 507)                      # audio would blow the tiny cap
        self.assertIn("storage", r["error"])
