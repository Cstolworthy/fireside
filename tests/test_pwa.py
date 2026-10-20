"""The PWA surface: manifest, service worker, and install icons.

They must be reachable without a session (Chrome fetches them before one
exists) and served with the right content types, or the app will not install.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from host.app import App, make_handler
from tests.test_pluginhost import write_plugin

READER_BODY = """
import types
class Plugin:
    def matches(self, req):
        return req.get("kind") == "upload"
    def read(self, req, fetch, cred):
        return types.SimpleNamespace(title="T", meta={}, id=None, chapters=[])
    def settings(self):
        return []
    def credential_hosts(self):
        return []
"""

STORAGE_BODY = """
class Plugin:
    def __init__(self):
        self._d = {}
    def put(self, data):
        import hashlib
        r = hashlib.sha256(data).hexdigest(); self._d[r] = data; return r
    def has(self, ref):
        return ref in self._d
    def get(self, ref):
        return self._d[ref]
    def delete(self, ref):
        self._d.pop(ref, None)
    def info(self):
        return {"kind": "memory", "durable": False, "url_ttl": None}
"""


class TestPwa(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem", require_auth=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_manifest_is_public_and_valid(self):
        # require_auth is on, yet the manifest still loads: no session needed.
        s, ct, body = self._get("/manifest.webmanifest")
        self.assertEqual(s, 200)
        self.assertIn("manifest+json", ct)
        m = json.loads(body)
        self.assertEqual(m["short_name"], "fireside")
        self.assertEqual(m["display"], "standalone")
        sizes = {i["sizes"] for i in m["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        self.assertTrue(any(i.get("purpose") == "maskable" for i in m["icons"]))

    def test_service_worker_is_public_root_scope(self):
        s, ct, body = self._get("/sw.js")
        self.assertEqual(s, 200)
        self.assertIn("javascript", ct)
        self.assertIn(b"addEventListener('fetch'", body)
        self.assertIn(b"/manifest.webmanifest", body)

    def test_icons_render(self):
        for path in ("/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"):
            s, ct, body = self._get(path)
            self.assertEqual(s, 200, path)
            self.assertEqual(ct, "image/png", path)
            self.assertTrue(body.startswith(b"\x89PNG"), path)


if __name__ == "__main__":
    unittest.main()
