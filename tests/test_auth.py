"""Local accounts: the door, the first admin, and the registration switch.

Every request goes through a cookie jar, so these tests exercise the real
session cookie, not a stubbed user id.
"""

import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host import auth
from host.app import App, make_handler
from tests.test_app import READER_BODY, SPEECH_BODY, STORAGE_BODY
from tests.test_pluginhost import write_plugin


def _open(path, obj, port):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    return req


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        write_plugin(root, "reader", "fake", order=10, body=READER_BODY)
        write_plugin(root, "speech", "fake", body=SPEECH_BODY)
        write_plugin(root, "storage", "mem", body=STORAGE_BODY)
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="mem", require_auth=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.jar = http.cookiejar.CookieJar()
        self.browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.httpd.shutdown()

    def _post(self, path, obj):
        return self.browser.open(_open(path, obj, self.port))

    def _jget(self, path):
        with self.browser.open("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, json.loads(r.read())

    def test_a_session_gates_the_app(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.browser.open("http://127.0.0.1:%d/library?user=x" % self.port)
        self.assertEqual(cm.exception.code, 401)

    def test_policy_is_public_and_reports_a_fresh_server(self):
        s, pol = self._jget("/auth/policy")
        self.assertEqual(s, 200)
        self.assertFalse(pol["has_accounts"])
        self.assertTrue(pol["registration_open"])

    def test_first_account_is_admin(self):
        with self._post("/auth/local/register",
                        {"username": "chris", "password": "longenough"}) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(json.loads(r.read())["admin"])
        s, me = self._jget("/me")
        self.assertTrue(me["authenticated"])
        self.assertTrue(me["admin"])
        s, pol = self._jget("/auth/policy")
        self.assertTrue(pol["has_accounts"])

    def test_wrong_password_says_no_but_not_which(self):
        self._post("/auth/local/register", {"username": "chris", "password": "longenough"})
        self.jar.clear()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/auth/local/login", {"username": "chris", "password": "wrongpass"})
        self.assertEqual(cm.exception.code, 401)
        self.assertIn("invalid username or password",
                      json.loads(cm.exception.read())["error"])

    def test_registration_can_close_and_the_first_account_is_still_let_in(self):
        # a stranger cannot be created once the admin closes the door
        self._post("/auth/local/register", {"username": "chris", "password": "longenough"})
        with self._post("/auth/registration", {"open": False}) as r:
            self.assertEqual(r.status, 200)
        stranger = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            stranger.open(_open("/auth/local/register",
                                {"username": "sam", "password": "longenough"}, self.port))
        self.assertEqual(cm.exception.code, 401)
        self.assertIn("registration is closed", json.loads(cm.exception.read())["error"])
        # but a server with no accounts yet always lets the first one in
        fresh = auth.account_count(self.app.conn)
        self.assertEqual(fresh, 1)

    def test_only_an_admin_may_list_or_close_registration(self):
        # no session: refused
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._jget("/auth/users")
        self.assertEqual(cm.exception.code, 401)
        # a plain account is not an admin
        self._post("/auth/local/register", {"username": "chris", "password": "longenough"})
        self._post("/auth/local/register", {"username": "sam", "password": "longenough"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._jget("/auth/users")
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
