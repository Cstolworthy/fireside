"""End-to-end over loopback: import -> render -> chapter -> playback handoff.

Uses fake plugins in a temp root, so it proves the wiring with no network.
"""

import hashlib
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
        return req.get(\"kind\") == \"upload\"
    def read(self, req, fetch, cred):
        return types.SimpleNamespace(title=\"T\", meta={}, id=None,
            chapters=[types.SimpleNamespace(title=\"c0\", text=\"hello world\"),
                      types.SimpleNamespace(title=\"c1\", text=\"second chapter\")])
    def settings(self):
        return []
    def credential_hosts(self):
        return []
"""

SPEECH_BODY = """
from host.contracts import Audio, Quote, Report
class Plugin:
    id = \"fake\"
    ext = \"mp3\"
    mime = \"audio/mpeg\"
    needs_key = False
    def list_voices(self, cred):
        return [{\"id\": \"vv\", \"label\": \"Voice V\"}]
    def synth(self, text, voice, opts, cred):
        return Audio(data=(\"A:\" + text).encode())
    def cost_per_1m_chars(self, cred):
        return 12.0
    def quote(self, ctx):
        return Quote(chars_est=ctx.get(\"chars_est\", 0), price=0.0)
    def report(self, ctx):
        return Report(charge=\"charged\", cost=0.0, currency=\"USD\")
"""

STORAGE_BODY = """
import hashlib
class Plugin:
    def __init__(self):
        self._d = {}
    def put(self, data):
        r = hashlib.sha256(data).hexdigest()
        self._d[r] = data
        return r
    def has(self, ref):
        return ref in self._d
    def get(self, ref):
        return self._d[ref]
    def delete(self, ref):
        self._d.pop(ref, None)
    def info(self):
        return {\"kind\": \"memory\", \"durable\": False, \"url_ttl\": None}
"""


class TestApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, r.read()

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def _jget(self, path):
        s, b = self._get(path)
        return s, json.loads(b)

    def _wait_job(self, jid, timeout=10):
        import time
        end = time.time() + timeout
        while time.time() < end:
            s, job = self._jget("/job?id=%s" % jid)
            if job["status"] in ("done", "error"):
                return job
            time.sleep(0.05)
        self.fail("job %s did not finish in %ss" % (jid, timeout))

    def test_health(self):
        s, body = self._jget("/health")
        self.assertEqual(s, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["storage"], ["mem"])

    def test_full_flow(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload",
                                        "filename": "x.epub"})
        self.assertEqual(s, 200)
        bid = imp["book_id"]
        self.assertEqual(imp["chapters"], 2)

        s, lib = self._jget("/library?user=u1")
        self.assertEqual(len(lib), 1)

        s, r = self._post("/render", {"user": "u1", "book": bid,
                                      "provider": "fake", "voice": "v"})
        self.assertEqual(s, 200)
        job = self._wait_job(r["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["current"], 2)

        s, prog = self._jget("/progress?user=u1&book=%s" % bid)
        self.assertTrue(all(c["rendered"] for c in prog["chapters"]))

        s, audio = self._get("/chapter?book=%s&idx=0" % bid)
        self.assertEqual(audio, b"A:hello world")

    def test_playback_handoff_over_http(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        bid = imp["book_id"]
        self._post("/claim", {"user": "u1", "book": bid, "device": "pc"})
        self._post("/position", {"user": "u1", "book": bid, "chapter_index": 1,
                                 "offset_seconds": 3.5, "device": "pc"})
        self._post("/claim", {"user": "u1", "book": bid, "device": "phone"})
        s, stop = self._jget("/stop?user=u1&book=%s&device=pc" % bid)
        self.assertTrue(stop["stop"])
        s, pos = self._jget("/position?user=u1&book=%s" % bid)
        self.assertEqual(pos["chapter_index"], 1)

    def test_not_rendered_is_409(self):
        s, imp = self._post("/import", {"user": "u1", "kind": "upload"})
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/chapter?book=%s&idx=0" % imp["book_id"])
        self.assertEqual(cm.exception.code, 409)

    def test_voices_endpoint(self):
        s, d = self._post("/voices", {"provider": "fake"})
        self.assertEqual(s, 200)
        self.assertEqual(d["voices"][0]["id"], "vv")
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/voices", {"provider": "nope"})
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()

