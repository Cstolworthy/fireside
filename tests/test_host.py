"""the host skeleton: registry, contract checks, and the health endpoint.

No outside network; one real loopback socket so the endpoint is proven, not
assumed.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host.host import Host, make_handler
from host.pluginhost import PluginError

from tests.test_pluginhost import write_plugin

READER_BODY = '''
class Plugin:
    id = %(id)r
    order = %(order)r
    def matches(self, req): return False
    def read(self, req, fetch, cred): return {"title": "t", "chapters": []}
'''

SPEECH_BODY = '''
class Plugin:
    id = %(id)r
    def list_voices(self, cred): return []
    def synth(self, text, voice, opts, cred): return b""
    def quote(self, ctx): return {}
    def report(self, ctx): return {}
'''

STORAGE_BODY = '''
class Plugin:
    id = %(id)r
    def put(self, data): return "ref"
    def has(self, ref): return False
    def get(self, ref): return b""
    def delete(self, ref): return None
'''

BARE_READER = '''
class Plugin:
    id = "bare"
    def matches(self, req): return False
'''


def reader(root, pid, order):
    write_plugin(root, "reader", pid, order=order, body=READER_BODY % {"id": pid, "order": order})


class TestHostRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "plugins")

    def test_health_lists_every_kind(self):
        reader(self.root, "epub", 10)
        reader(self.root, "web", 1000)
        write_plugin(self.root, "speech", "azure", body=SPEECH_BODY % {"id": "azure"})
        write_plugin(self.root, "storage", "local", body=STORAGE_BODY % {"id": "local"})
        out = Host(self.root).health()
        self.assertTrue(out["ok"])
        self.assertEqual([r["id"] for r in out["readers"]], ["epub", "web"])
        self.assertEqual(out["speech"], ["azure"])
        self.assertEqual(out["storage"], ["local"])

    def test_no_reader_is_not_ok(self):
        write_plugin(self.root, "speech", "azure", body=SPEECH_BODY % {"id": "azure"})
        self.assertFalse(Host(self.root).health()["ok"])

    def test_plugin_that_announces_a_kind_must_implement_it(self):
        write_plugin(self.root, "reader", "bare", order=1, body=BARE_READER)
        with self.assertRaises(PluginError):
            Host(self.root)

    def test_empty_root_is_a_valid_but_empty_host(self):
        h = Host(os.path.join(self.tmp, "nothing"))
        self.assertEqual(h.health()["readers"], [])
        self.assertFalse(h.health()["ok"])


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        reader(root, "epub", 10)
        self.host = Host(root)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.host))
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()

    def test_health_over_the_wire(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health") as r:
            self.assertEqual(r.status, 200)
            body = json.loads(r.read())
        self.assertEqual(body["readers"][0]["id"], "epub")

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
