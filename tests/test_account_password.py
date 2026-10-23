"""Setting and changing your own password, including from a social-only account.

Two doors, one account: a password account proves the old password before it
gets a new one, and an account that only ever signed in with Google picks a
sign-in name and gets a password. The account doing the changing stays signed
in; every other session dies with the old password.
"""

import unittest

from host import auth, db


class SetPassword(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.uid = auth.local_register(self.conn, "chris", "hunter2hunter2",
                                       email="chris@example.com")

    def tearDown(self):
        self.conn.close()

    def test_change_needs_the_current_password(self):
        with self.assertRaises(auth.AuthError):
            auth.set_password(self.conn, self.uid, "wrong-one", "brandnewpass")
        self.assertEqual(auth.local_login(self.conn, "chris", "hunter2hunter2"), self.uid)

    def test_change_swaps_the_password_and_keeps_the_name(self):
        name = auth.set_password(self.conn, self.uid, "hunter2hunter2", "brandnewpass")
        self.assertEqual(name, "chris")
        self.assertEqual(auth.local_login(self.conn, "chris", "brandnewpass"), self.uid)
        with self.assertRaises(auth.AuthError):
            auth.local_login(self.conn, "chris", "hunter2hunter2")

    def test_short_new_password_is_refused_and_the_old_one_survives(self):
        with self.assertRaises(auth.AuthError):
            auth.set_password(self.conn, self.uid, "hunter2hunter2", "short")
        self.assertEqual(auth.local_login(self.conn, "chris", "hunter2hunter2"), self.uid)

    def test_the_new_password_is_salted_freshly(self):
        before = self.conn.execute(
            "SELECT salt FROM local_accounts WHERE user_id=?", (self.uid,)).fetchone()["salt"]
        auth.set_password(self.conn, self.uid, "hunter2hunter2", "brandnewpass")
        after = self.conn.execute(
            "SELECT salt FROM local_accounts WHERE user_id=?", (self.uid,)).fetchone()["salt"]
        self.assertNotEqual(before, after)


class SocialOnlyAccount(unittest.TestCase):
    """An account that never had a password: it names itself one."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.uid = "google:123"
        self.conn.execute("INSERT INTO users(id,name,created_at) VALUES(?,?,?)",
                          (self.uid, "cat", auth._now()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_it_starts_with_no_password(self):
        self.assertFalse(auth.has_local_account(self.conn, self.uid))

    def test_setting_one_needs_no_current_password(self):
        name = auth.set_password(self.conn, self.uid, "", "brandnewpass",
                                 username="cat")
        self.assertEqual(name, "cat")
        self.assertTrue(auth.has_local_account(self.conn, self.uid))
        self.assertEqual(auth.local_login(self.conn, "cat", "brandnewpass"), self.uid)

    def test_it_needs_a_name(self):
        with self.assertRaises(auth.AuthError):
            auth.set_password(self.conn, self.uid, "", "brandnewpass", username="")

    def test_a_taken_name_is_refused(self):
        auth.local_register(self.conn, "taken", "hunter2hunter2")
        with self.assertRaises(auth.AuthError):
            auth.set_password(self.conn, self.uid, "", "brandnewpass", username="taken")
        self.assertFalse(auth.has_local_account(self.conn, self.uid))

    def test_the_identity_email_is_carried_over_so_reset_works(self):
        self.conn.execute(
            "INSERT INTO identities(provider,subject,user_id,email,email_verified)"
            " VALUES(?,?,?,?,?)", ("google", "123", self.uid, "cat@example.com", 1))
        self.conn.commit()
        auth.set_password(self.conn, self.uid, "", "brandnewpass", username="cat")
        res = auth.request_password_reset(self.conn, "cat@example.com")
        self.assertIsNotNone(res)
        self.assertEqual(res[1], "cat")

    def test_a_second_call_is_a_change(self):
        auth.set_password(self.conn, self.uid, "", "brandnewpass", username="cat")
        with self.assertRaises(auth.AuthError):
            auth.set_password(self.conn, self.uid, "", "anotherpass", username="cat2")
        auth.set_password(self.conn, self.uid, "brandnewpass", "anotherpass")
        self.assertEqual(auth.local_login(self.conn, "cat", "anotherpass"), self.uid)

    def test_no_session_at_all_is_refused(self):
        with self.assertRaises(auth.AuthError):
            auth.set_password(self.conn, "", "", "brandnewpass", username="cat")
