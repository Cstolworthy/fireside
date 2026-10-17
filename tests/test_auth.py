"""Social sign-in: sessions, provider config, and the OAuth callback.

No network: the token and identity calls are served by an injected fetch, so the
flow is proven end to end without touching a real provider.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host import auth, db
from host.app import App, make_handler
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


def fake_fetch(routes):
    def f(req, timeout=20):
        for needle, body in routes.items():
            if needle in req.full_url:
                return json.dumps(body).encode()
        raise AssertionError("unexpected request: " + req.full_url)
    return f


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def test_roundtrip(self):
        tok = auth.create_session(self.conn, "github:1", "github")
        self.assertEqual(auth.session_user(self.conn, tok), "github:1")

    def test_unknown_and_empty(self):
        self.assertIsNone(auth.session_user(self.conn, ""))
        self.assertIsNone(auth.session_user(self.conn, "nope"))

    def test_expiry(self):
        tok = auth.create_session(self.conn, "github:2", "github", ttl=-1)
        self.assertIsNone(auth.session_user(self.conn, tok))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM sessions").fetchone()["c"], 0)

    def test_logout(self):
        tok = auth.create_session(self.conn, "github:3", "github")
        auth.delete_session(self.conn, tok)
        self.assertIsNone(auth.session_user(self.conn, tok))


class TestProviders(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.a = auth.Auth(self.conn, local=False)
        self._env = {}
        for k in ("FIRESIDE_OAUTH_GITHUB_CLIENT_ID", "FIRESIDE_OAUTH_GITHUB_SECRET",
                  "FIRESIDE_OAUTH_GOOGLE_CLIENT_ID", "FIRESIDE_OAUTH_GOOGLE_SECRET"):
            self._env[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_unconfigured_offers_nothing(self):
        self.assertEqual(self.a.providers(), [])

    def test_configured_only(self):
        os.environ["FIRESIDE_OAUTH_GITHUB_CLIENT_ID"] = "cid"
        os.environ["FIRESIDE_OAUTH_GITHUB_SECRET"] = "sec"
        self.assertEqual([p["id"] for p in self.a.providers()], ["github"])

    def test_authorize_url(self):
        os.environ["FIRESIDE_OAUTH_GITHUB_CLIENT_ID"] = "cid"
        os.environ["FIRESIDE_OAUTH_GITHUB_SECRET"] = "sec"
        url = self.a.authorize_url("github", "http://h/auth/github/callback", "st")
        self.assertTrue(url.startswith("https://github.com/login/oauth/authorize?"))
        self.assertIn("client_id=cid", url)
        self.assertIn("state=st", url)
        self.assertIn("redirect_uri=http", url)

    def test_authorize_unconfigured_raises(self):
        with self.assertRaises(auth.AuthError):
            self.a.authorize_url("google", "http://h/cb", "st")


class TestLogin(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.a = auth.Auth(self.conn)
        os.environ["FIRESIDE_OAUTH_GITHUB_CLIENT_ID"] = "cid"
        os.environ["FIRESIDE_OAUTH_GITHUB_SECRET"] = "sec"
        self.a.fetch = fake_fetch({
            "login/oauth/access_token": {"access_token": "T"},
            "api.github.com/user": {"id": 7, "login": "ren", "name": "Ren"},
            "api.github.com/user/emails": [{"email": "ren@x", "primary": True}],
        })

    def tearDown(self):
        os.environ.pop("FIRESIDE_OAUTH_GITHUB_CLIENT_ID", None)
        os.environ.pop("FIRESIDE_OAUTH_GITHUB_SECRET", None)

    def test_login_creates_user_and_session(self):
        uid, tok = self.a.login("github", "code", "http://h/auth/github/callback")
        self.assertEqual(uid, "github:7")
        self.assertEqual(auth.session_user(self.conn, tok), "github:7")
        row = self.conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
        self.assertEqual(row["name"], "Ren")

    def test_no_token_raises(self):
        self.a.fetch = fake_fetch({"login/oauth/access_token": {"error": "bad_code"}})
        with self.assertRaises(auth.AuthError):
            self.a.login("github", "code", "http://h/cb")


class TestAuthOverHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        os.environ["FIRESIDE_OAUTH_GITHUB_CLIENT_ID"] = "cid"
        os.environ["FIRESIDE_OAUTH_GITHUB_SECRET"] = "sec"
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem", require_auth=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        os.environ.pop("FIRESIDE_OAUTH_GITHUB_CLIENT_ID", None)
        os.environ.pop("FIRESIDE_OAUTH_GITHUB_SECRET", None)

    def _get(self, path, cookie=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_providers_listed(self):
        s, b = self._get("/auth/providers")
        self.assertEqual(s, 200)
        self.assertEqual([p["id"] for p in json.loads(b)["providers"]], ["local", "github"])

    def test_local_register_and_use(self):
        body = json.dumps({"username": "chris", "password": "hunter2hunter2"}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/auth/local/register" % self.port, data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            cookie = r.headers.get("Set-Cookie", "")
            self.assertIn("fireside_session=", cookie)
        tok = cookie.split("fireside_session=")[1].split(";")[0]
        s, b = self._get("/me", cookie="fireside_session=" + tok)
        self.assertTrue(json.loads(b)["authenticated"])
        s, b = self._get("/library", cookie="fireside_session=" + tok)
        self.assertEqual(s, 200)

    def test_local_login_wrong_password_is_401(self):
        body = json.dumps({"username": "chris", "password": "hunter2hunter2"}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/auth/local/register" % self.port, data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
        bad = json.dumps({"username": "chris", "password": "wrongwrong"}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/auth/local/login" % self.port, data=bad,
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

    def test_me_unauthenticated(self):
        s, b = self._get("/me")
        self.assertEqual(s, 200)
        self.assertFalse(json.loads(b)["authenticated"])

    def test_gate_blocks_without_session(self):
        s, b = self._get("/library?user=")
        self.assertEqual(s, 401)

    def test_gate_passes_with_session(self):
        tok = auth.create_session(self.app.conn, "github:7", "github")
        s, b = self._get("/library", cookie="fireside_session=" + tok)
        self.assertEqual(s, 200)

    def test_start_redirects_to_provider(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener.open("http://127.0.0.1:%d/auth/github/start" % self.port)
        self.assertEqual(ctx.exception.code, 302)
        loc = ctx.exception.headers.get("Location")
        self.assertTrue(loc.startswith("https://github.com/login/oauth/authorize?"))
        self.assertIn("state=", loc)
