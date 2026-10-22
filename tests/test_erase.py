"""Erasing an account: the rows go, and the bytes that are only theirs go too.

Two promises are under test, and they pull in opposite directions:

- nothing that is only this account's survives (its books, audio, history,
  sign-ins, and the render-cache copy kept for it);
- anything another account still holds is untouched, because deleting it would
  break a reader who did nothing.
"""

import os
import tempfile
import unittest

from host import auth, erase, library
from host.app import App
from host.contracts import Book, Chapter
from host.db import connect
from storage.local import LocalBackend
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


class EraseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-erase-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        for uid in ("u1", "u2"):
            library.create_user(self.conn, uid)

    def _book(self, uid="u1", texts=("one", "two")):
        return library.add_book(
            self.conn, self.storage, uid,
            Book(title="Fire", chapters=[Chapter("Ch %d" % i, t)
                                         for i, t in enumerate(texts)]))

    def _voice(self, bid, idx, voice, data):
        ref = self.storage.put(data)
        library.add_rendition(self.conn, bid, idx, "local", voice, "", 1.0,
                              ref, len(data), None, voice_label=voice)
        self.conn.execute(
            "UPDATE chapters SET audio_ref=?,audio_bytes=?,provider='local',"
            "voice=?,model='',speed=1.0 WHERE book_id=? AND idx=?",
            (ref, len(data), voice, bid, idx))
        self.conn.commit()
        return ref

    def _cache(self, key, ref, provider="local", voice="Alice"):
        """A render-cache row, the way the renderer writes one: it points at the
        same audio a rendition does, so a re-voice is free."""
        self.conn.execute(
            "INSERT INTO render_cache(`key`,audio_ref,audio_bytes,duration_s,"
            "provider,voice,model,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (key, ref, 0, None, provider, voice, "", "now"))
        self.conn.commit()


class TestPurge(EraseCase):
    def test_account_and_everything_it_held_is_gone(self):
        bid = self._book()
        ref = self._voice(bid, 0, "Alice", b"alice-0")
        text_ref = self.conn.execute(
            "SELECT text_ref FROM chapters WHERE book_id=? AND idx=0",
            (bid,)).fetchone()["text_ref"]
        self.conn.execute("INSERT INTO sessions(token,user_id,provider,created_at,"
                          "expires_at) VALUES('t1','u1','local',0,9e18)")
        self.conn.execute("INSERT INTO positions(user_id,book_id,chapter_index,"
                          "offset_seconds,updated_at,owner_device_id) "
                          "VALUES('u1',?,'0','1.0','now','')", (bid,))
        self.conn.execute("INSERT INTO listens(user_id,book_id,seconds,created_at) "
                          "VALUES('u1',?,'9','now')", (bid,))
        self.conn.execute("INSERT INTO subscriptions(user_id,plan,status,provider,"
                          "reference,updated_at,created_at) "
                          "VALUES('u1','platform','active','stripe','sub_1','now','now')")
        self.conn.execute("INSERT INTO user_prefs(user_id,updated_at) "
                          "VALUES('u1','now')")
        self.conn.execute("INSERT INTO credentials(user_id,provider,data,updated_at) "
                          "VALUES('u1','azure','{}','now')")
        self.conn.commit()

        out = erase.purge_user(self.conn, self.storage, "u1")

        self.assertEqual(out["books"], 1)
        self.assertFalse(self.storage.has(ref), "the voiced audio left the disk")
        self.assertFalse(self.storage.has(text_ref), "the imported text left the disk")
        for table in ("users", "books", "sessions", "positions", "listens",
                      "subscriptions", "user_prefs", "credentials",
                      "local_accounts"):
            col = "id" if table == "users" else "user_id"
            n = self.conn.execute(
                "SELECT COUNT(*) AS n FROM %s WHERE %s='u1'" % (table, col)
            ).fetchone()["n"]
            self.assertEqual(n, 0, "%s still holds a row for u1" % table)
        for table in ("chapters", "renditions"):
            n = self.conn.execute(
                "SELECT COUNT(*) AS n FROM %s WHERE book_id=?" % table,
                (bid,)).fetchone()["n"]
            self.assertEqual(n, 0, "%s still holds a row for the book" % table)
        self.assertEqual(library.list_books(self.conn, "u1"), [])
        # the other account is untouched
        self.assertEqual(library.list_books(self.conn, "u2"), [])

    def test_a_blob_another_account_still_holds_survives(self):
        """The same text imported by two people is one blob. Erasing one must
        not take the other reader's copy."""
        u1 = self._book("u1", texts=("shared text",))
        u2 = self._book("u2", texts=("shared text",))
        r1 = self.conn.execute("SELECT text_ref FROM chapters WHERE book_id=?",
                               (u1,)).fetchone()["text_ref"]
        r2 = self.conn.execute("SELECT text_ref FROM chapters WHERE book_id=?",
                               (u2,)).fetchone()["text_ref"]
        self.assertEqual(r1, r2, "content-addressed: same text, same ref")

        erase.purge_user(self.conn, self.storage, "u1")

        self.assertTrue(self.storage.has(r2), "u2's copy is still readable")
        self.assertEqual(len(library.list_books(self.conn, "u2")), 1)

    def test_the_render_cache_copy_of_a_private_voice_is_dropped(self):
        bid = self._book()
        ref = self._voice(bid, 0, "Alice", b"alice-0")
        self._cache("k1", ref, voice="Alice")

        erase.purge_user(self.conn, self.storage, "u1")

        c = self.conn.execute("SELECT COUNT(*) AS n FROM render_cache WHERE `key`='k1'"
                              ).fetchone()["n"]
        self.assertEqual(c, 0, "the free-re-read copy went with the account")
        self.assertFalse(self.storage.has(ref))

    def test_a_cache_copy_shared_with_a_live_reader_stays(self):
        """u2 holds the same voice, so the audio is a live reader's, and the
        cache entry that makes u2's re-read free must stay."""
        bid = self._book("u1")
        ref = self._voice(bid, 0, "Alice", b"alice-0")
        self._cache("k1", ref, voice="Alice")
        # u2 names the same audio with a rendition of its own
        b2 = self._book("u2", texts=("other",))
        self.conn.execute(
            "INSERT INTO renditions(rk,book_id,idx,provider,voice,voice_label,"
            "model,speed,pron,audio_ref,audio_bytes,duration_s,created_at) "
            "VALUES('u2rk',?,0,'local','Alice','Alice','',1.0,'',?,0,NULL,'now')",
            (b2, ref))
        self.conn.commit()

        erase.purge_user(self.conn, self.storage, "u1")

        self.assertTrue(self.storage.has(ref), "u2's live reader keeps the bytes")
        c = self.conn.execute("SELECT COUNT(*) AS n FROM render_cache WHERE `key`='k1'"
                              ).fetchone()["n"]
        self.assertEqual(c, 1, "the shared cache entry is kept")

    def test_a_users_own_connection_goes_and_a_system_one_stays(self):
        self.conn.execute("INSERT INTO connections(id,owner_id,scope,provider,label,"
                          "data,created_at,updated_at) "
                          "VALUES('c1','u1','user','azure','mine','{}','now','now')")
        self.conn.execute("INSERT INTO connections(id,owner_id,scope,provider,label,"
                          "data,created_at,updated_at) "
                          "VALUES('c2','u1','system','azure','sale','{}','now','now')")
        self.conn.commit()
        erase.purge_user(self.conn, self.storage, "u1")
        ids = {r["id"] for r in self.conn.execute("SELECT id FROM connections")}
        self.assertEqual(ids, {"c2"}, "the account's own connection left, the server's stayed")

    def test_admin_guard_helper_counts(self):
        self.conn.execute("INSERT INTO admins(user_id,created_at) VALUES('u1','now')")
        self.conn.commit()
        self.assertEqual(auth.admin_count(self.conn), 1)
        self.conn.execute("INSERT INTO admins(user_id,created_at) VALUES('u2','now')")
        self.conn.commit()
        self.assertEqual(auth.admin_count(self.conn), 2)


class TestEraseHandler(unittest.TestCase):
    """The route's guards: proof before erasure, and the last admin stays."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-erase-http-")
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")

    def _user(self, name="chris", pw="hunter2hunter2"):
        """A plain account. local_register makes the FIRST account admin, so
        the admin row is cleared here unless a test wants it."""
        uid = auth.local_register(self.app.conn, name, pw)
        self.app.conn.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        self.app.conn.commit()
        return uid

    def _admin(self, uid):
        self.app.conn.execute("INSERT OR IGNORE INTO admins(user_id,created_at) "
                              "VALUES(?,'now')", (uid,))
        self.app.conn.commit()

    def test_password_account_must_prove_its_password(self):
        uid = self._user()
        status, out = self.app.account_delete({"_user": uid}, {"password": "wrong"})
        self.assertEqual(status, 403)
        self.assertFalse(out.get("ok"))
        self.conn_has_user(uid)

    def test_password_account_erases_with_the_right_password(self):
        uid = self._user()
        status, out = self.app.account_delete({"_user": uid},
                                              {"password": "hunter2hunter2"})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertFalse(self.user_exists(uid))

    def test_social_only_account_confirms_by_phrase(self):
        self._user("seed")          # so the server has a user, not a first-run state
        self.app.conn.execute("INSERT INTO users(id,name,created_at) "
                              "VALUES('google:9','g','now')")
        self.app.conn.commit()
        status, _ = self.app.account_delete({"_user": "google:9"}, {"confirm": "nope"})
        self.assertEqual(status, 400)
        status, out = self.app.account_delete({"_user": "google:9"},
                                              {"confirm": "delete my account"})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertFalse(self.user_exists("google:9"))

    def test_the_last_admin_cannot_delete_itself(self):
        uid = self._user()
        self._admin(uid)
        status, out = self.app.account_delete({"_user": uid},
                                              {"password": "hunter2hunter2"})
        self.assertEqual(status, 409)
        self.assertEqual(out["error"], "last admin")
        self.assertTrue(self.user_exists(uid))

    def test_a_non_last_admin_can_delete_itself(self):
        a = self._user("chris")
        b = self._user("cat")
        self._admin(a)
        self._admin(b)
        status, out = self.app.account_delete({"_user": a}, {"password": "hunter2hunter2"})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(auth.admin_count(self.app.conn), 1)

    # helpers
    def user_exists(self, uid):
        return self.app.conn.execute("SELECT 1 FROM users WHERE id=?",
                                     (uid,)).fetchone() is not None

    def conn_has_user(self, uid):
        self.assertTrue(self.user_exists(uid))


if __name__ == "__main__":
    unittest.main()
