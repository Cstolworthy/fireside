"""Cover art: the epub's own cover, a reader that hands one over, an upload.

No network is touched: a reader that carries a cover of its own never reaches
the ISBN lookup, and a book with no ISBN returns "no cover" instead of calling
out.
"""

import base64
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

from host.app import App, make_handler
from sources.epub import parse_epub
from tests.test_app import SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin

# One reader, two shapes: a filename containing "cover" makes it hand over a
# cover and an ISBN, anything else hands over neither.
READER_DUAL = """
import types
COVER = b"\\x89PNG\\r\\n\\x1a\\n" + b"Z" * 200
class Plugin:
    def matches(self, req):
        return req.get("kind") == "upload"
    def read(self, req, fetch, cred):
        fn = req.get("filename") or ""
        clo = "cover" in fn
        return types.SimpleNamespace(title="Covered" if clo else "Plain", id=None,
            meta={"source": "dual", "isbn": ("9780306406157" if clo else "")},
            cover=(COVER if clo else None), cover_mime=("image/png" if clo else ""),
            chapters=[types.SimpleNamespace(title="c0", text="hi")])
    def settings(self): return []
    def credential_hosts(self): return []
"""


def _epub_with_cover():
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    z.writestr("META-INF/container.xml",
               '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
    z.writestr("OEBPS/content.opf",
               '<?xml version="1.0"?><package><metadata '
               'xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<dc:title>Covered Book</dc:title>'
               '<dc:identifier id="bookid">urn:isbn:9780306406157</dc:identifier>'
               '<meta name="cover" content="cov"/></metadata><manifest>'
               '<item id="cov" href="cover.jpg" media-type="image/jpeg"/>'
               '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
               '</manifest><spine><itemref idref="c1"/></spine></package>')
    z.writestr("OEBPS/cover.jpg", b"\xff\xd8\xff" + b"C" * 400)
    z.writestr("OEBPS/c1.xhtml", "<html><body><h1>One</h1><p>" + ("hello " * 20) + "</p></body></html>")
    z.close()
    return buf.getvalue()


class TestEpubCover(unittest.TestCase):
    def test_parse_epub_pulls_isbn_and_cover(self):
        b = parse_epub(_epub_with_cover(), "t.epub")
        self.assertEqual(b["isbn"], "9780306406157")
        self.assertTrue(b["cover"].startswith(b"\xff\xd8\xff"))
        self.assertEqual(b["cover_mime"], "image/jpeg")

    def test_epub_without_cover_still_parses(self):
        buf = io.BytesIO()
        z = zipfile.ZipFile(buf, "w")
        z.writestr("content.opf",
                   '<package><metadata><dc:title xmlns:dc="x">T</dc:title></metadata>'
                   '<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>'
                   '</manifest><spine><itemref idref="c"/></spine></package>')
        z.writestr("c.xhtml", "<html><body><h1>Hi</h1><p>" + ("word " * 20) + "</p></body></html>")
        z.close()
        b = parse_epub(buf.getvalue(), "t.epub")
        self.assertEqual(b["isbn"], "")
        self.assertNotIn("cover", b)


class TestCoverHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "dual", order=10, body=READER_DUAL)
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
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, r.read()

    def _jget(self, path):
        s, b = self._get(path)
        return s, json.loads(b)

    def test_reader_cover_is_stored_and_served(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload", "filename": "with-cover.epub"})
        bid = imp["book_id"]
        s, data = self._get("/cover?book=%s" % bid)
        self.assertEqual(s, 200)
        self.assertTrue(data.startswith(b"\x89PNG"))
        s, lib = self._jget("/library?user=u1")
        self.assertTrue(lib[0]["cover"])
        s, prog = self._jget("/progress?user=u1&book=%s" % bid)
        self.assertTrue(prog["book"]["cover"])
        self.assertEqual(prog["book"]["isbn"], "9780306406157")

    def test_upload_a_cover(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload", "filename": "plain.txt"})
        bid = imp["book_id"]
        png = b"\x89PNG\r\n\x1a\n" + b"Q" * 300
        s, r = self._post("/cover", {"book": bid, "mime": "image/png",
                                     "data_b64": base64.b64encode(png).decode()})
        self.assertEqual(s, 200)
        self.assertTrue(r["cover"])
        s, data = self._get("/cover?book=%s" % bid)
        self.assertEqual(data, png)

    def test_no_cover_is_404(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload", "filename": "plain.txt"})
        bid = imp["book_id"]
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/cover?book=%s" % bid)
        self.assertEqual(cm.exception.code, 404)

    def test_auto_without_isbn_is_404(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload", "filename": "plain.txt"})
        bid = imp["book_id"]
        req = urllib.request.Request(
            "http://127.0.0.1:%d/cover/auto" % self.port,
            data=json.dumps({"book": bid}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
