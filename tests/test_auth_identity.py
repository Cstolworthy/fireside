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

    def test_a_second_provider_cannot_take_an_existing_email(self):
        # a verified email proves the mailbox, not the account: a second
        # provider does NOT silently join the account that owns the address.
        first = self._login("google", "abc", "a@example.com", True)
        with self.assertRaises(auth.AccountExistsError) as cm:
            self._login("github", "99", "a@example.com", True)
        self.assertEqual(cm.exception.user_id, first)
        # nothing was attached, and no second user appeared
        self.assertIsNone(auth.identity_user(self.conn, "github", "99"))
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

    def test_email_match_is_case_insensitive_and_still_refused(self):
        self._login("google", "abc", "A@Example.com", True)
        with self.assertRaises(auth.AccountExistsError):
            self._login("github", "99", "a@example.com", True)

    def test_a_password_accounts_email_routes_the_social_login(self):
        # a password account stores the address it signed up with, unverified.
        # A matching verified social login must not make a silent duplicate -
        # it collides, so the caller can hold a claim and ask for the password.
        auth.local_register(self.conn, "chris", "hunter2hunter2",
                            email="chris@example.com")
        with self.assertRaises(auth.AccountExistsError) as cm:
            self._login("google", "g1", "chris@example.com", True)
        self.assertEqual(cm.exception.user_id, "local:chris")
        self.assertIsNone(auth.identity_user(self.conn, "google", "g1"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"], 1)

    def test_a_password_account_without_email_still_makes_a_fresh_account(self):
        # grandfathering: an account created before addresses were collected
        # carries none, so a social login makes its own account, as before.
        auth.local_register(self.conn, "chris", "hunter2hunter2")
        uid = self._login("google", "g1", "chris@example.com", True)
        self.assertEqual(uid, "google:g1")


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


class TestClaimProof(unittest.TestCase):
    """A colliding social login links only after the account is proven."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.connect(os.path.join(self.tmp, "db.sqlite"))
        self.uid = auth.local_register(self.conn, "chris", "hunter2hunter2")

    def _ident(self, subject, email="chris@example.com", verified=True):
        return {"subject": subject, "name": "Chris", "email": email,
                "email_verified": verified}

    def _collide(self, provider="microsoft", subject="m1"):
        """Seed the account's email, then attempt a second provider login."""
        auth.link_identity(self.conn, "google", self._ident("g1"), self.uid)
        try:
            auth.upsert_user(self.conn, provider, self._ident(subject))
        except auth.AccountExistsError as e:
            return auth.create_claim(self.conn, e.provider, e.ident, e.user_id)
        raise AssertionError("collision should have raised AccountExistsError")

    def test_collision_attaches_nothing(self):
        auth.link_identity(self.conn, "google", self._ident("g1"), self.uid)
        with self.assertRaises(auth.AccountExistsError):
            auth.upsert_user(self.conn, "microsoft", self._ident("m1"))
        self.assertIsNone(auth.identity_user(self.conn, "microsoft", "m1"))

    def test_social_login_collides_with_a_local_email_and_links_after_proof(self):
        # the whole point of collecting the address: a returning person who
        # clicks Continue with Google lands back on their password account,
        # after proving it, instead of a silent empty second account.
        self.conn.execute("UPDATE local_accounts SET email='chris@example.com'"
                          " WHERE username='chris'")
        self.conn.commit()
        try:
            auth.upsert_user(self.conn, "google", self._ident("g1"))
        except auth.AccountExistsError as e:
            token = auth.create_claim(self.conn, e.provider, e.ident, e.user_id)
        else:
            raise AssertionError("collision should have raised AccountExistsError")
        self.assertIsNone(auth.identity_user(self.conn, "google", "g1"))
        got = auth.complete_claim(self.conn, token, "hunter2hunter2")
        self.assertEqual(got, self.uid)
        self.assertEqual(auth.identity_user(self.conn, "google", "g1"), self.uid)

    def test_right_password_links_the_identity(self):
        token = self._collide()
        got = auth.complete_claim(self.conn, token, "hunter2hunter2")
        self.assertEqual(got, self.uid)
        self.assertEqual(auth.identity_user(self.conn, "microsoft", "m1"), self.uid)
        # the claim is consumed
        with self.assertRaises(auth.AuthError):
            auth.take_claim(self.conn, token)

    def test_wrong_password_links_nothing(self):
        token = self._collide()
        with self.assertRaises(auth.AuthError):
            auth.complete_claim(self.conn, token, "not-the-password")
        self.assertIsNone(auth.identity_user(self.conn, "microsoft", "m1"))

    def test_expired_claim_is_refused(self):
        token = self._collide()
        self.conn.execute("UPDATE login_claims SET expires_at=0 WHERE token=?", (token,))
        self.conn.commit()
        with self.assertRaises(auth.AuthError):
            auth.complete_claim(self.conn, token, "hunter2hunter2")
        self.assertIsNone(auth.identity_user(self.conn, "microsoft", "m1"))

    def test_passwordless_account_cannot_be_confirmed_here(self):
        # a social-only account owns the email; there is no password to prove
        # with, so the door is to sign in the way it already does
        self.conn.execute("DELETE FROM local_accounts")
        self.conn.execute("DELETE FROM users")
        self.conn.commit()
        owner = auth.upsert_user(self.conn, "google", self._ident("g1"))
        try:
            auth.upsert_user(self.conn, "microsoft", self._ident("m1"))
        except auth.AccountExistsError as e:
            token = auth.create_claim(self.conn, e.provider, e.ident, e.user_id)
        self.assertFalse(auth.has_local_account(self.conn, owner))
        with self.assertRaises(auth.AuthError):
            auth.complete_claim(self.conn, token, "hunter2hunter2")

    def test_login_after_linking_lands_on_the_account(self):
        token = self._collide()
        auth.complete_claim(self.conn, token, "hunter2hunter2")
        uid = auth.upsert_user(self.conn, "microsoft", self._ident("m1"))
        self.assertEqual(uid, self.uid)

    def test_claim_methods_offers_password_and_providers(self):
        token = self._collide()
        claim = auth.take_claim(self.conn, token)
        m = auth.claim_methods(self.conn, claim["user_id"])
        self.assertTrue(m["has_password"])          # chris registered a password
        self.assertEqual(m["providers"], ["google"])  # and linked google

    def test_proof_via_the_accounts_own_provider_links(self):
        # a social-only account: no password, so prove it by signing in through
        # the provider it already uses. That IS the proof.
        self.conn.execute("DELETE FROM local_accounts")
        self.conn.execute("DELETE FROM users")
        self.conn.commit()
        owner = auth.upsert_user(self.conn, "google", self._ident("g1"))
        try:
            auth.upsert_user(self.conn, "microsoft", self._ident("m1"))
        except auth.AccountExistsError as e:
            token = auth.create_claim(self.conn, e.provider, e.ident, e.user_id)
        m = auth.claim_methods(self.conn, owner)
        self.assertFalse(m["has_password"])
        self.assertEqual(m["providers"], ["google"])
        got = auth.complete_claim_via_identity(
            self.conn, token, "google", self._ident("g1"))
        self.assertEqual(got, owner)
        self.assertEqual(auth.identity_user(self.conn, "microsoft", "m1"), owner)
        # the claim is consumed
        with self.assertRaises(auth.AuthError):
            auth.take_claim(self.conn, token)

    def test_proof_via_an_unlinked_identity_links_nothing(self):
        token = self._collide()
        with self.assertRaises(auth.AuthError):
            auth.complete_claim_via_identity(
                self.conn, token, "google", self._ident("not-linked"))
        self.assertIsNone(auth.identity_user(self.conn, "microsoft", "m1"))

    def test_proof_via_another_account_is_refused(self):
        # someone else's google identity proves a different account, not this one
        other = auth.upsert_user(self.conn, "github", self._ident("h1", "other@x.com"))
        token = self._collide()
        with self.assertRaises(auth.AuthError):
            auth.complete_claim_via_identity(
                self.conn, token, "github", self._ident("h1", "other@x.com"))
        self.assertIsNone(auth.identity_user(self.conn, "microsoft", "m1"))


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

    def test_simplelogin_email_without_a_flag_is_still_trusted(self):
        # SimpleLogin does not always carry an explicit verified field; the
        # account's own confirmed address is still the join key the door uses.
        a = self._auth({"https://app.simplelogin.io/oauth2/userinfo": {
            "sub": "37", "name": "Sam", "email": "sam@simplelogin.co"}})
        ident = a.identity("simplelogin", "tok")
        self.assertTrue(ident["email_verified"])

    def test_facebook_identity_with_an_address_is_verified(self):
        a = self._auth({"https://graph.facebook.com/me?fields=id,name,email": {
            "id": "1024", "name": "Sam", "email": "sam@example.com"}})
        ident = a.identity("facebook", "tok")
        self.assertEqual(ident["subject"], "1024")
        self.assertEqual(ident["email"], "sam@example.com")
        self.assertTrue(ident["email_verified"])

    def test_facebook_without_an_address_links_nothing(self):
        a = self._auth({"https://graph.facebook.com/me?fields=id,name,email": {
            "id": "1024", "name": "Sam"}})
        ident = a.identity("facebook", "tok")
        self.assertEqual(ident["subject"], "1024")
        self.assertFalse(ident["email_verified"])

    def test_x_identity_is_the_nested_shape_and_has_no_email(self):
        a = self._auth({"https://api.x.com/2/users/me": {
            "data": {"id": "77", "name": "Sam", "username": "sam"}}})
        ident = a.identity("x", "tok")
        self.assertEqual(ident["subject"], "77")
        self.assertEqual(ident["name"], "Sam")
        self.assertEqual(ident["email"], "")
        self.assertFalse(ident["email_verified"])

    def test_x_authorize_url_carries_pkce_and_facebook_does_not(self):
        os.environ["FIRESIDE_OAUTH_X_CLIENT_ID"] = "cid"
        os.environ["FIRESIDE_OAUTH_X_SECRET"] = "sec"
        a = auth.Auth(db.connect(":memory:"))
        verifier, _ch = auth.new_pkce()
        url = a.authorize_url("x", "https://readfireside.com/auth/x/callback",
                              "st", verifier)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("code_challenge=", url)
        with self.assertRaises(auth.AuthError):
            a.authorize_url("x", "https://readfireside.com/auth/x/callback", "st")

    def test_simplelogin_without_an_email_links_nothing(self):
        a = self._auth({"https://app.simplelogin.io/oauth2/userinfo": {
            "sub": "38", "name": "Sam"}})
        ident = a.identity("simplelogin", "tok")
        self.assertFalse(ident["email_verified"])

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
            self.assertEqual(p["simplelogin"]["label"], "Proton (SimpleLogin)")
        finally:
            del os.environ["FIRESIDE_OAUTH_SIMPLELOGIN_CLIENT_ID"]
            del os.environ["FIRESIDE_OAUTH_SIMPLELOGIN_SECRET"]


if __name__ == "__main__":
    unittest.main()
