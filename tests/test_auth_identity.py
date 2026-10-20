"""Identity resolution: one user per verified email, not one per login.

These exercise auth.upsert_user and auth.Auth.identity directly, with a fake
fetch standing in for the provider endpoints. No network, no server.
"""

import json
import os
import tempfile
import unittest

from host import auth, db


class TestIdentityLinking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "db.sqlite"))

    def tearDown(self):
        self.conn.close()

    def _login(self, provider, subject, email="", verified=False):
        return auth.upsert_user(self.conn, provider, {
            "subject": subject, "name": subject, "email": email,
            "email_verified": verified})

    def test_first_login_makes_a_user(self):
        uid = self._login("google", "abc", "a@example.com", True)
        self.assertEqual(uid, "google:abc")
        row = self.conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        self.assertIsNotNone(row)

    def test_same_identity_keeps_its_user(self):
        a = self._login("google", "abc", "a@example.com", True)
        b = self._login("google", "abc", "a@example.com", True)
        self.assertEqual(a, b)

    def test_a_second_provider_joins_on_a_verified_email(self):
        # the whole point: github login on an address google already owns
        first = self._login("google", "abc", "a@example.com", True)
        second = self._login("github", "99", "a@example.com", True)
        self.assertEqual(second, first)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"], 1)

    def test_an_unverified_email_opens_its_own_account(self):
        first = self._login("google", "abc", "a@example.com", True)
        second = self._login("microsoft", "99", "a@example.com", False)
        self.assertNotEqual(second, first)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"], 2)

    def test_no_email_never_links(self):
        first = self._login("google", "abc", "a@example.com", True)
        second = self._login("microsoft", "99", "", True)
        self.assertNotEqual(second, first)

    def test_email_match_is_case_insensitive(self):
        first = self._login("google", "abc", "A@Example.com", True)
        second = self._login("github", "99", "a@example.com", True)
        self.assertEqual(second, first)


class TestExplicitLink(unittest.TestCase):
    """The settings > link an account door, distinct from login-by-email."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "db.sqlite"))
        self.conn.execute("INSERT INTO users(id,name,created_at) VALUES('local:chris','chris','x')")
        self.conn.execute("INSERT INTO users(id,name,created_at) VALUES('local:sam','sam','x')")
        self.conn.commit()

    def _ident(self, subject, email="chris@example.com", verified=True):
        return {"subject": subject, "name": "Chris", "email": email,
                "email_verified": verified}

    def test_link_attaches_to_the_named_user(self):
        auth.link_identity(self.conn, "google", self._ident("g1"), "local:chris")
        self.assertEqual(auth.identity_user(self.conn, "google", "g1"), "local:chris")

    def test_relinking_the_same_user_is_fine(self):
        auth.link_identity(self.conn, "google", self._ident("g1"), "local:chris")
        auth.link_identity(self.conn, "google", self._ident("g1"), "local:chris")
        self.assertEqual(auth.linked_providers(self.conn, "local:chris"), ["google"])

    def test_an_identity_owned_by_another_user_cannot_be_stolen(self):
        auth.link_identity(self.conn, "google", self._ident("g1"), "local:chris")
        with self.assertRaises(auth.AuthError):
            auth.link_identity(self.conn, "google", self._ident("g1"), "local:sam")
        self.assertEqual(auth.identity_user(self.conn, "google", "g1"), "local:chris")

    def test_linked_providers_lists_what_is_attached(self):
        auth.link_identity(self.conn, "google", self._ident("g1"), "local:chris")
        auth.link_identity(self.conn, "simplelogin", self._ident("s1"), "local:chris")
        self.assertEqual(auth.linked_providers(self.conn, "local:chris"),
                         ["google", "simplelogin"])
        self.assertEqual(auth.linked_providers(self.conn, "local:sam"), [])

    def test_login_after_link_lands_on_the_linked_user(self):
        # link a local account to google, then a fresh google login resolves to it
        auth.link_identity(self.conn, "google", self._ident("g1"), "local:chris")
        uid = auth.upsert_user(self.conn, "google", self._ident("g1"))
        self.assertEqual(uid, "local:chris")


class TestProviderIdentity(unittest.TestCase):
    def _auth(self, responses):
        def fetch(req, timeout=20):
            return json.dumps(responses[req.full_url]).encode()
        return auth.Auth(db.connect(":memory:"), fetch=fetch)

    def test_simplelogin_userinfo_is_read(self):
        a = self._auth({"https://app.simplelogin.io/oauth2/userinfo": {
            "sub": "36", "name": "Sam", "email": "sam@simplelogin.co",
            "email_verified": True}})
        ident = a.identity("simplelogin", "tok")
        self.assertEqual(ident["subject"], "36")
        self.assertEqual(ident["email"], "sam@simplelogin.co")
        self.assertTrue(ident["email_verified"])

    def test_github_verified_flag_comes_from_the_emails_endpoint(self):
        a = self._auth({
            "https://api.github.com/user": {"id": 7, "login": "sam", "email": None},
            "https://api.github.com/user/emails": [
                {"email": "old@x.com", "primary": False, "verified": True},
                {"email": "sam@x.com", "primary": True, "verified": True}]})
        ident = a.identity("github", "tok")
        self.assertEqual(ident["email"], "sam@x.com")
        self.assertTrue(ident["email_verified"])

    def test_a_provider_without_creds_offers_no_button(self):
        for k in list(os.environ):
            if k.startswith("FIRESIDE_OAUTH_SIMPLELOGIN"):
                del os.environ[k]
        a = auth.Auth(db.connect(":memory:"))
        ids = [p["id"] for p in a.providers()]
        self.assertNotIn("simplelogin", ids)

    def test_simplelogin_button_appears_with_creds(self):
        os.environ["FIRESIDE_OAUTH_SIMPLELOGIN_CLIENT_ID"] = "cid"
        os.environ["FIRESIDE_OAUTH_SIMPLELOGIN_SECRET"] = "sec"
        try:
            a = auth.Auth(db.connect(":memory:"))
            p = {x["id"]: x for x in a.providers()}
            self.assertIn("simplelogin", p)
            self.assertEqual(p["simplelogin"]["label"], "SimpleLogin")
        finally:
            del os.environ["FIRESIDE_OAUTH_SIMPLELOGIN_CLIENT_ID"]
            del os.environ["FIRESIDE_OAUTH_SIMPLELOGIN_SECRET"]


if __name__ == "__main__":
    unittest.main()
