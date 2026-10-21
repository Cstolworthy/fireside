import time
import unittest

from host import auth, db


class PasswordReset(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.uid = auth.local_register(self.conn, "chris", "hunter2hunter2",
                                       email="chris@example.com")

    def tearDown(self):
        self.conn.close()

    def test_mint_returns_a_token_for_a_known_address(self):
        res = auth.request_password_reset(self.conn, "chris@example.com")
        self.assertIsNotNone(res)
        token, username = res
        self.assertEqual(username, "chris")
        self.assertTrue(token)

    def test_address_is_matched_case_insensitively(self):
        res = auth.request_password_reset(self.conn, "  CHRIS@Example.COM ")
        self.assertIsNotNone(res)

    def test_unknown_address_mints_nothing(self):
        self.assertIsNone(auth.request_password_reset(self.conn, "nobody@example.com"))
        self.assertIsNone(auth.request_password_reset(self.conn, ""))

    def test_only_the_hash_is_stored(self):
        token, _ = auth.request_password_reset(self.conn, "chris@example.com")
        rows = self.conn.execute("SELECT token_hash FROM password_resets").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["token_hash"], token)

    def test_reset_sets_the_new_password(self):
        token, _ = auth.request_password_reset(self.conn, "chris@example.com")
        auth.reset_password(self.conn, token, "brandnewpass")
        self.assertEqual(auth.local_login(self.conn, "chris", "brandnewpass"), self.uid)
        with self.assertRaises(auth.AuthError):
            auth.local_login(self.conn, "chris", "hunter2hunter2")

    def test_a_token_is_single_use(self):
        token, _ = auth.request_password_reset(self.conn, "chris@example.com")
        auth.reset_password(self.conn, token, "brandnewpass")
        with self.assertRaises(auth.AuthError):
            auth.reset_password(self.conn, token, "anotherpass1")

    def test_a_bogus_token_is_refused(self):
        with self.assertRaises(auth.AuthError):
            auth.reset_password(self.conn, "not-a-real-token", "brandnewpass")

    def test_short_password_is_refused_and_the_token_survives(self):
        token, _ = auth.request_password_reset(self.conn, "chris@example.com")
        with self.assertRaises(auth.AuthError):
            auth.reset_password(self.conn, token, "short")
        # the token was not consumed by a refused attempt
        auth.reset_password(self.conn, token, "brandnewpass")

    def test_expired_token_is_refused(self):
        token, _ = auth.request_password_reset(self.conn, "chris@example.com")
        self.conn.execute("UPDATE password_resets SET expires_at=?",
                          (int(time.time()) - 1,))
        self.conn.commit()
        with self.assertRaises(auth.AuthError):
            auth.reset_password(self.conn, token, "brandnewpass")

    def test_a_new_request_retires_the_older_link(self):
        first, _ = auth.request_password_reset(self.conn, "chris@example.com")
        second, _ = auth.request_password_reset(self.conn, "chris@example.com")
        with self.assertRaises(auth.AuthError):
            auth.reset_password(self.conn, first, "brandnewpass")
        auth.reset_password(self.conn, second, "brandnewpass")

    def test_reset_revokes_every_session(self):
        tok_a = auth.create_session(self.conn, self.uid, "local")
        tok_b = auth.create_session(self.conn, self.uid, "local")
        reset, _ = auth.request_password_reset(self.conn, "chris@example.com")
        auth.reset_password(self.conn, reset, "brandnewpass")
        self.assertIsNone(auth.session_user(self.conn, tok_a))
        self.assertIsNone(auth.session_user(self.conn, tok_b))


# ---- the same door over HTTP ------------------------------------------------

import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from host import mailer
from host.app import App, make_handler


class ResetOverHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = App(os.path.join(self.tmp, "plugins"), storage_id="local",
                       db_path=os.path.join(self.tmp, "db.sqlite"))
        auth.local_register(self.app.conn, "chris", "hunter2hunter2",
                            email="chris@example.com")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.sent = []
        self._send, self._conf = mailer.send, mailer.configured
        mailer.configured = lambda: True
        mailer.send = lambda to, subject, text, html=None, fetch=None: (
            self.sent.append((to, subject, text)) or True)

    def tearDown(self):
        mailer.send, mailer.configured = self._send, self._conf
        self.httpd.shutdown()

    def _post(self, path, obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_forgot_mails_a_link_for_a_known_address(self):
        s, body = self._post("/auth/forgot", {"email": "chris@example.com"})
        self.assertEqual(s, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "chris@example.com")
        self.assertIn("/?reset=", self.sent[0][2])

    def test_unknown_address_answers_the_same_and_mails_nothing(self):
        s, body = self._post("/auth/forgot", {"email": "nobody@example.com"})
        self.assertEqual(s, 200)                 # no "does this account exist?"
        self.assertTrue(body["ok"])
        self.assertEqual(self.sent, [])

    def test_full_reset_over_http(self):
        self._post("/auth/forgot", {"email": "chris@example.com"})
        text = self.sent[0][2]
        token = re.search(r"/\?reset=([A-Za-z0-9_\-]+)", text).group(1)
        s, body = self._post("/auth/reset", {"token": token, "password": "brandnewpass"})
        self.assertEqual(s, 200)
        self.assertEqual(body["user"], "local:chris")
        self.assertEqual(auth.local_login(self.app.conn, "chris", "brandnewpass"),
                         "local:chris")

    def test_bad_token_is_refused_over_http(self):
        s, body = self._post("/auth/reset", {"token": "nope", "password": "brandnewpass"})
        self.assertEqual(s, 401)
        self.assertIn("error", body)

    def test_no_mailer_configured_is_said_not_hidden(self):
        mailer.configured = lambda: False
        mailer.send = self._send
        s, body = self._post("/auth/forgot", {"email": "chris@example.com"})
        self.assertEqual(s, 200)
        self.assertFalse(body["mail"])
