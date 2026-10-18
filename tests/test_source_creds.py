"""Source logins live on the server, not in a browser.

The client is a remote, not a credential holder: a source's cookie (AO3's, for
now) is stored server-side, resolved at ingest, and never returned by a listing.
A value the client re-sends for a source still wins, so a fresh paste works.
"""

import json
import os
import tempfile
import unittest

from host import auth, credentials
from host.app import App
from tests.test_app import STORAGE_BODY
from tests.test_pluginhost import write_plugin

CRED_READER = '''
import types
class Plugin:
    id = "fake"
    def matches(self, req):
        return req.get("kind") == "url"
    def read(self, req, fetch, cred):
        return types.SimpleNamespace(title="T", meta={}, id=None,
            chapters=[types.SimpleNamespace(title="c0",
                text="COOKIE=" + (cred.get("cookie") or "none"))])
    def settings(self):
        return [{"name": "cookie", "label": "Cookie", "kind": "secret"}]
    def credential_hosts(self):
        return ["archiveofourown.org"]
'''


class SourceCredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=CRED_READER)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.uid = auth.local_register(self.app.conn, "user1", "longenough")
        self.q = {"_user": self.uid}

    def test_store_is_server_side_and_never_returned(self):
        st, d = self.app.source_creds(self.q, {})
        self.assertEqual(st, 200)
        src = next(s for s in d["sources"] if s["id"] == "fake")
        self.assertFalse(src["fields"][0]["set"])
        self.assertEqual(src["fields"][0]["value"], "")

        st, _ = self.app.source_cred_save(self.q, {"id": "fake", "data": {"cookie": "abc123"}})
        self.assertEqual(st, 200)
        # it really is on the server
        self.assertEqual(credentials.get_credential(self.app.conn, self.uid, "source:fake")["cookie"],
                         "abc123")
        # but a listing reports only that it is set
        st, d = self.app.source_creds(self.q, {})
        src = next(s for s in d["sources"] if s["id"] == "fake")
        self.assertTrue(src["fields"][0]["set"])
        self.assertEqual(src["fields"][0]["value"], "")

    def test_empty_save_does_not_blank_the_secret(self):
        self.app.source_cred_save(self.q, {"id": "fake", "data": {"cookie": "keepme"}})
        self.app.source_cred_save(self.q, {"id": "fake", "data": {"cookie": ""}})
        self.assertEqual(credentials.get_credential(self.app.conn, self.uid, "source:fake")["cookie"],
                         "keepme")

    def test_ingest_uses_the_stored_credential(self):
        self.app.source_cred_save(self.q, {"id": "fake", "data": {"cookie": "xyz"}})
        st, d = self.app.import_book(self.q, {"kind": "url", "url": "http://e/x"})
        self.assertEqual(st, 200)
        from host import library
        txt = library.chapter_text(self.app.conn, self.app.storage, d["book_id"], 0)
        self.assertIn("COOKIE=xyz", txt)

    def test_client_credential_wins_over_stored(self):
        self.app.source_cred_save(self.q, {"id": "fake", "data": {"cookie": "old"}})
        st, d = self.app.import_book(self.q, {"kind": "url", "url": "http://e/y",
                                              "cred": {"cookie": "fresh"}})
        from host import library
        txt = library.chapter_text(self.app.conn, self.app.storage, d["book_id"], 0)
        self.assertIn("COOKIE=fresh", txt)

    def test_delete_clears_it(self):
        self.app.source_cred_save(self.q, {"id": "fake", "data": {"cookie": "gone"}})
        st, _ = self.app.source_cred_delete(self.q, {"id": "fake"})
        self.assertEqual(st, 200)
        self.assertEqual(credentials.get_credential(self.app.conn, self.uid, "source:fake"), {})
        st, d = self.app.source_creds(self.q, {})
        src = next(s for s in d["sources"] if s["id"] == "fake")
        self.assertFalse(src["fields"][0]["set"])

    def test_unknown_source_is_rejected(self):
        st, _ = self.app.source_cred_save(self.q, {"id": "nope", "data": {"cookie": "x"}})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
