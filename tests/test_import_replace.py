"""Importing a book you already hold: it is a replace, not a second copy.

Two layers. The library half is about what a replacement does to the shelf:
the id stays, so positions and stats still point at this book, and a voice
whose words did not change is kept. The HTTP half is about the question the
host asks first, and the two answers a person can give.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host import library
from host.app import App, make_handler
from host.contracts import Book, Chapter
from host.db import connect
from storage.local import LocalBackend
from tests.test_app import SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin

READER_REPLACE = """
import types

# story-v1.epub and story-v2.epub are the same story, two editions: the
# identity is the name before the dash, so the second one is a replacement.
TEXTS = {
    "story-v1.epub": ["one", "two"],
    "story-v2.epub": ["one", "two changed", "three"],
    "other.epub": ["elsewhere"],
}

class Plugin:
    def matches(self, req):
        return req.get("kind") == "upload"
    def read(self, req, fetch, cred):
        fn = req.get("filename") or ""
        texts = TEXTS.get(fn, ["x"])
        return types.SimpleNamespace(
            title="Story " + fn, id=None,
            meta={"source": "rep", "identity": "rep:" + fn.split("-")[0].split(".")[0],
                  "url": "https://site/" + fn.split(".")[0]},
            cover=None, cover_mime="",
            chapters=[types.SimpleNamespace(title="c%d" % i, text=t)
                      for i, t in enumerate(texts)])
    def settings(self): return []
    def credential_hosts(self): return []
"""

# A reader that recognizes an older row by its own rule, the way a site reader
# recognizes a page the generic web reader imported before the site had one.
READER_LEGACY = READER_REPLACE.replace(
    "    def settings(self): return []",
    "    def same(self, stored, incoming):\n"
    "        return (stored or {}).get('url') == (incoming or {}).get('url')\n"
    "    def settings(self): return []")


def _book(texts, title="Fire"):
    return Book(title=title, chapters=[Chapter("Ch %d" % i, t)
                                       for i, t in enumerate(texts)])


def _voice(conn, storage, bid, idx, text):
    """A chapter as the renderer leaves it: audio on the row and in renditions."""
    ref = storage.put(b"\xff\xfb" + text.encode())
    conn.execute("UPDATE chapters SET audio_ref=?,audio_bytes=?,duration_s=?,"
                 "provider=?,voice=?,model=?,speed=? WHERE book_id=? AND idx=?",
                 (ref, 128, 1.5, "fake", "v1", "", 1.0, bid, idx))
    library.add_rendition(conn, bid, idx, "fake", "v1", "", 1.0, ref, 128, 1.5)
    conn.commit()
    return ref


class TestReplaceBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-rep-")
        self.storage = LocalBackend(self.tmp + "/blobs")
        self.conn = connect(":memory:")
        library.create_user(self.conn, "u1")

    def test_unchanged_words_keep_their_voice(self):
        bid = library.add_book(self.conn, self.storage, "u1", _book(["a", "b", "c"]))
        library.set_details(self.conn, bid, author="Someone")
        _voice(self.conn, self.storage, bid, 0, "a")
        _voice(self.conn, self.storage, bid, 1, "b")
        out = library.replace_book(self.conn, self.storage, bid,
                                   _book(["a", "B", "c"], title="Renamed by the import"))
        self.assertEqual(out["kept_renditions"], 1)
        self.assertEqual(out["dropped_renditions"], 1)
        chs = {c["idx"]: c for c in library.chapters(self.conn, bid)}
        self.assertTrue(chs[0]["audio_ref"], "chapter 0 text did not change")
        self.assertFalse(chs[1]["audio_ref"], "chapter 1 was rewritten, its audio is stale")
        self.assertEqual(len(chs), 3)

    def test_a_book_that_shrank_loses_the_extra_chapters(self):
        bid = library.add_book(self.conn, self.storage, "u1", _book(["a", "b", "c"]))
        _voice(self.conn, self.storage, bid, 2, "c")
        library.replace_book(self.conn, self.storage, bid, _book(["a", "b"]))
        self.assertEqual([c["idx"] for c in library.chapters(self.conn, bid)], [0, 1])
        self.assertEqual(library.renditions(self.conn, bid), [])

    def test_the_book_keeps_its_id_and_its_edited_title(self):
        bid = library.add_book(self.conn, self.storage, "u1", _book(["a"]))
        library.set_details(self.conn, bid, title="My name for it")
        out = library.replace_book(self.conn, self.storage, bid,
                                   _book(["a", "b"], title="What the site calls it"))
        self.assertEqual(out["kept_renditions"], 0)
        self.assertEqual(library.get_book(self.conn, bid)["title"], "My name for it")
        self.assertEqual(len(library.list_books(self.conn, "u1")), 1)

    def test_find_same_reads_identity_then_asks_the_reader(self):
        bid = library.add_book(self.conn, self.storage, "u1", _book(["a"]))
        library.replace_book(self.conn, self.storage, bid, _book(["a"]))
        self.conn.execute("UPDATE books SET source_json=? WHERE id=?",
                          (json.dumps({"source": "web", "url": "https://x/y"}), bid))
        self.conn.commit()

        class P:
            def same(self, stored, incoming):
                return stored.get("url") == incoming.get("url")

        self.assertEqual(
            library.find_same(self.conn, "u1", {"url": "https://x/y"}, P()), bid)
        self.assertIsNone(library.find_same(self.conn, "u1", {"url": "https://x/z"}, P()))
        self.assertIsNone(library.find_same(self.conn, "u2", {"url": "https://x/y"}, P()))


class TestImportReplaceHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "rep", order=10, body=READER_REPLACE)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"), storage_id="mem")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _import(self, filename, **kw):
        body = {"user": "u1", "kind": "upload", "filename": filename}
        body.update(kw)
        return self._post("/import", body)

    def _shelf(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/library?user=u1" % self.port) as r:
            return json.loads(r.read())

    def test_second_import_asks_instead_of_stacking(self):
        s, first = self._import("story-v1.epub")
        self.assertEqual(s, 200)
        s, again = self._import("story-v1.epub")
        self.assertEqual(s, 409)
        self.assertEqual(again["existing"]["book_id"], first["book_id"])
        self.assertEqual(again["existing"]["chapters"], 2)
        self.assertEqual(len(self._shelf()), 1)

    def test_replace_reuses_the_same_book(self):
        s, first = self._import("story-v1.epub")
        bid = first["book_id"]
        s, swapped = self._import("story-v2.epub", replace=True)
        self.assertEqual(s, 200)
        self.assertTrue(swapped["replaced"])
        self.assertEqual(swapped["book_id"], bid)
        self.assertEqual(swapped["chapters"], 3)
        shelf = self._shelf()
        self.assertEqual(len(shelf), 1)
        self.assertEqual(shelf[0]["id"], bid)
        self.assertEqual(shelf[0]["chapters"], 3)

    def test_keep_both_is_still_available(self):
        self._import("story-v1.epub")
        s, second = self._import("story-v2.epub", keep_both=True)
        self.assertEqual(s, 200)
        self.assertNotIn("replaced", second)
        self.assertEqual(len(self._shelf()), 2)

    def test_a_different_book_is_not_a_duplicate(self):
        self._import("story-v1.epub")
        s, other = self._import("other.epub")
        self.assertEqual(s, 200)
        self.assertEqual(len(self._shelf()), 2)

    def test_another_account_is_not_a_duplicate(self):
        self._import("story-v1.epub")
        s, r = self._post("/import", {"user": "u2", "kind": "upload",
                                      "filename": "story-v1.epub"})
        self.assertEqual(s, 200)


class TestLegacyRowHttp(unittest.TestCase):
    """A row imported before readers named an identity is still replaceable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "rep", order=10, body=READER_LEGACY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"), storage_id="mem")
        # A book already on the shelf with no identity, the way an old import sits.
        library.create_user(self.app.conn, "u1")
        bid = library.add_book(self.app.conn, self.app.storage, "u1",
                               Book("Old", [Chapter("c", "text")]))
        self.app.conn.execute(
            "UPDATE books SET source_json=? WHERE id=?",
            (json.dumps({"source": "web", "url": "https://site/story-v1"}), bid))
        self.app.conn.commit()
        self.old = bid
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def test_reader_rule_matches_the_old_row(self):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/import" % self.port,
            data=json.dumps({"user": "u1", "kind": "upload", "filename": "story-v1.epub",
                             "old_url": "https://site/story"}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 409)
            self.assertEqual(json.loads(e.read())["existing"]["book_id"], self.old)
            return
        self.fail("the old row should have been recognized")


if __name__ == "__main__":
    unittest.main()


class TestReaderIdentities(unittest.TestCase):
    """Each reader names the same work the same way, whatever link you hand it."""

    def test_web_ignores_the_spelling_of_the_same_page(self):
        from plugins.reader.web.plugin import identity_of
        a = identity_of("https://Example.com/story/")
        b = identity_of("https://example.com/story#chapter-2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, identity_of("https://example.com/story/2"))

    def test_ao3_identity_is_the_work_not_the_chapter(self):
        from plugins.reader.ao3.plugin import work_id
        self.assertEqual(work_id("https://archiveofourown.org/works/12345/chapters/9"), "12345")
        self.assertEqual(work_id("https://archiveofourown.org/works/12345?view_full_work=true"),
                         "12345")
        self.assertEqual(work_id("https://archiveofourown.org/tags/Harry%20Potter"), "")

    def test_quantumbang_family_covers_every_part_and_the_landing_page(self):
        from plugins.reader.quantumbang.plugin import identity_of
        base = "https://quantumbang.org/hidden-resources/"
        self.assertEqual(identity_of(base + "-1-5-"), identity_of(base))
        self.assertEqual(identity_of(base + "-3-5-"), identity_of(base + "-1-5-"))
        self.assertNotEqual(identity_of(base), identity_of("https://quantumbang.org/other-story/"))

    def test_epub_identity_prefers_the_isbn(self):
        from plugins.reader.epub.plugin import identity_of
        self.assertEqual(identity_of(b"one", "a.epub", "9780306406157"),
                         identity_of(b"two", "b.epub", "9780306406157"))
        self.assertNotEqual(identity_of(b"one"), identity_of(b"two"))
        self.assertEqual(identity_of(b"one"), identity_of(b"one"))

    def test_quantumbang_recognizes_a_page_the_web_reader_imported(self):
        from plugins.reader.quantumbang.plugin import Quantumbang
        stored = {"source": "web", "url": "https://quantumbang.org/hidden/-1-5-/"}
        incoming = {"source": "quantumbang", "url": "https://quantumbang.org/hidden/"}
        self.assertTrue(Quantumbang().same(stored, incoming))
        self.assertFalse(Quantumbang().same(
            {"source": "web", "url": "https://example.com/x"}, incoming))
