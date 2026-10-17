import unittest

from host import auth, db


class LocalAccounts(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_register_then_login(self):
        uid = auth.local_register(self.conn, "Chris", "hunter2hunter2")
        self.assertEqual(uid, "local:chris")
        self.assertEqual(auth.local_login(self.conn, "chris", "hunter2hunter2"), uid)

    def test_wrong_password_rejected(self):
        auth.local_register(self.conn, "chris", "hunter2hunter2")
        with self.assertRaises(auth.AuthError):
            auth.local_login(self.conn, "chris", "nope")

    def test_unknown_user_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.local_login(self.conn, "nobody", "hunter2hunter2")

    def test_duplicate_username_rejected(self):
        auth.local_register(self.conn, "chris", "hunter2hunter2")
        with self.assertRaises(auth.AuthError):
            auth.local_register(self.conn, "chris", "another-pass")

    def test_short_password_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.local_register(self.conn, "chris", "short")

    def test_bad_username_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.local_register(self.conn, "no spaces", "hunter2hunter2")

    def test_local_listed_when_enabled(self):
        a = auth.Auth(self.conn)
        self.assertTrue(any(p["id"] == "local" for p in a.providers()))

    def test_local_hidden_when_disabled(self):
        a = auth.Auth(self.conn, local=False)
        self.assertFalse(any(p["id"] == "local" for p in a.providers()))


if __name__ == "__main__":
    unittest.main()
