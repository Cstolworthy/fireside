"""The browser client: the page the host serves, and the routes it depends on.

Two guards that matter:

- every fetch the UI makes must be a route the host actually serves, parsed from
  the shipped HTML against the live route table. A client that calls a phantom
  endpoint is a broken deployment, and this catches it before a browser does.
- the chapter bytes the player verifies (sha256 in JS) must match the ref the
  server reports, so verify-before-play is checking the right thing.

The whole flow runs over loopback against fake plugins; no network.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request

from host.app import App, UI_DIR, GET_ROUTES, POST_ROUTES, AUTH_ROUTES, PUBLIC_ROUTES, make_handler
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


class TestBrowserUi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem")
        self.httpd = urllib.request  # silence linters; server started below
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()

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
        self.fail("job %s did not finish" % jid)

    def test_index_is_served_and_versioned(self):
        s, body = self._get("/")
        html = body.decode("utf-8")
        self.assertEqual(s, 200)
        self.assertIn("fireside", html)
        self.assertIn("sha256hex", html)                 # the verifier ships with it
        self.assertNotIn("__HOST_VERSION__", html)       # placeholder was filled
        self.assertTrue(re.search(r"v\d+\.\d+", html))

    def test_ui_only_calls_served_routes(self):
        html = open(os.path.join(UI_DIR, "index.html"), encoding="utf-8").read()
        served = set(GET_ROUTES) | set(POST_ROUTES) | set(AUTH_ROUTES)
        calls = set(re.findall(r"""(?:fetch|api|jpost)\(\s*['"]([^'"]+)['"]""", html))
        self.assertTrue(calls, "no fetch calls found - did the page change shape?")
        for call in calls:
            base = call.split("?")[0]
            self.assertIn(base, served,
                          "the UI calls %r, which the host does not serve" % base)

    def test_sign_in_chip_is_wired(self):
        # the whole app sits behind a session: a door is served, it talks only to
        # served routes, and the signed-in chip carries the way back out.
        html = open(os.path.join(UI_DIR, "index.html"), encoding="utf-8").read()
        self.assertIn('id="auth"', html)
        self.assertIn('id="lock"', html)
        self.assertIn("/auth/policy", html)
        self.assertIn("/auth/local/login", html)
        self.assertIn("/auth/logout", html)
        self.assertIn("Continue with ", html)
        # the door must actually be shown: body.locked hides the app, so the
        # sign-in card has to be un-hidden or the page is blank for everyone
        # who is not signed in.
        self.assertIn("$('#lock').hidden = false", html)

    def test_favicon_is_served_publicly(self):
        self.assertIn("/favicon.ico", PUBLIC_ROUTES)
        s, body = self._get("/favicon.ico")
        self.assertEqual(s, 200)
        self.assertEqual(body[:4], b"\x00\x00\x01\x00")   # ICO magic

    def test_plugins_declare_speech_settings(self):
        # the fake root above ships no settings; ask the host to load the real
        # plugin tree so we are testing the shipped contracts, not the stubs.
        real = App("plugins", db_path=os.path.join(self.tmp, "db2.sqlite"),
                   storage_id="local")
        s, d = real.plugins({}, {})
        self.assertEqual(s, 200)
        ids = {p["id"] for p in d["speech"]}
        self.assertTrue({"local", "openai", "azure", "minimax"} <= ids)
        by_id = {p["id"]: p for p in d["speech"]}
        self.assertTrue(any(f["name"] == "base_url" for f in by_id["openai"]["settings"]))
        self.assertTrue(any(f["name"] == "key" for f in by_id["azure"]["settings"]))
        self.assertTrue(any(f["name"] == "group" for f in by_id["minimax"]["settings"]))
        self.assertEqual(by_id["local"]["settings"], [])
        # and the AO3 reader declares the cookie field the page renders
        ao3 = next(r for r in d["readers"] if r["id"] == "ao3")
        self.assertTrue(any(f["name"] == "cookie" for f in ao3["settings"]))

    def test_browser_flow_verify_before_play(self):
        # the exact sequence the page runs: identity -> import -> voices ->
        # render -> poll -> progress -> verified chapter bytes -> position/claim
        s, imp = self._post("/import", {"user": "u1", "kind": "upload",
                                        "filename": "x.epub"})
        bid = imp["book_id"]

        s, v = self._post("/voices", {"provider": "fake", "cred": {}})
        self.assertEqual(v["voices"][0]["id"], "vv")

        s, r = self._post("/render", {"user": "u1", "book": bid,
                                      "provider": "fake", "voice": "vv"})
        job = self._wait_job(r["job_id"])
        self.assertEqual(job["status"], "done")

        s, prog = self._jget("/progress?user=u1&book=%s" % bid)
        ch = prog["chapters"][0]
        self.assertTrue(ch["rendered"])
        self.assertTrue(ch["audio_ref"])

        s, audio = self._get("/chapter?book=%s&idx=0" % bid)
        self.assertEqual(s, 200)
        # what the JS sha256 verifier checks, checked here in Python:
        self.assertEqual(hashlib.sha256(audio).hexdigest(), ch["audio_ref"])
        self.assertEqual(len(audio), ch["audio_bytes"])

        self._post("/claim", {"user": "u1", "book": bid, "device": "web-abcd"})
        self._post("/position", {"user": "u1", "book": bid, "chapter_index": 0,
                                 "offset_seconds": 4.0, "device": "web-abcd"})
        s, pos = self._jget("/position?user=u1&book=%s" % bid)
        self.assertEqual(pos["offset_seconds"], 4.0)
        self.assertEqual(pos["owner_device_id"], "web-abcd")
class TestUiDomRefs(unittest.TestCase):
    """A dead $("#id") throws at load and blanks the whole page silently.

    The element the script reaches for must exist in the shipped HTML; #user
    was removed when the free-text "listening as" field became a session label,
    and the leftover reference killed the script for every returning browser.
    """

    def test_index_html_references_only_existing_ids(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        defined = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
        refs = set(re.findall(r"\$\('#([A-Za-z0-9_\-]+)'\)", html))
        self.assertEqual(sorted(refs - defined), [],
                         "index.html references ids that do not exist")


class TestBootSafetyNet(unittest.TestCase):
    """A load-time throw must surface, and every selector form must resolve.

    The blank page had two causes stacked: a dead $('#user') reference that
    threw at load, and a signed-out branch that hid the app but never revealed
    the door. These guards cover both classes plus the generic error net.
    """

    def _html(self):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_page_never_blanks_on_error(self):
        html = self._html()
        self.assertIn("window.addEventListener('error'", html)
        # both auth branches set the door's visibility explicitly
        self.assertIn("$('#lock').hidden = true", html)    # signed in
        self.assertIn("$('#lock').hidden = false", html)   # signed out

    def test_signin_errors_are_generic(self):
        # the brief's rule: never say which half was wrong
        html = self._html()
        self.assertIn("invalid username or password.", html)
        self.assertIn("id=\"lockuser\"", html)
        self.assertIn("id=\"lockpass\"", html)
        self.assertIn("id=\"lockpass2\"", html)  # confirm field exists

    def test_id_refs_cover_all_selector_forms(self):
        html = self._html()
        defined = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
        pats = [r"\$\('#([A-Za-z0-9_\-]+)'\)",
                r"getElementById\('([A-Za-z0-9_\-]+)'\)",
                r"querySelector(?:All)?\('#([A-Za-z0-9_\-]+)'\)"]
        refs = set()
        for pat in pats:
            refs |= set(re.findall(pat, html))
        self.assertEqual(sorted(refs - defined), [],
                         "index.html reaches for ids that do not exist")


if __name__ == "__main__":
    unittest.main()
