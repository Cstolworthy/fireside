"""Server-side credential store: values resolve in-process, never leave."""

import unittest

from host import credentials
from host.db import connect


class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_set_and_get(self):
        credentials.set_credential(self.conn, "u1", "azure", {"key": "k", "region": "eastus"})
        self.assertEqual(credentials.get_credential(self.conn, "u1", "azure"),
                         {"key": "k", "region": "eastus"})

    def test_missing_is_empty(self):
        self.assertEqual(credentials.get_credential(self.conn, "u1", "azure"), {})

    def test_overwrite(self):
        credentials.set_credential(self.conn, "u1", "azure", {"key": "old"})
        credentials.set_credential(self.conn, "u1", "azure", {"key": "new"})
        self.assertEqual(credentials.get_credential(self.conn, "u1", "azure")["key"], "new")

    def test_providers_lists_names_only(self):
        credentials.set_credential(self.conn, "u1", "azure", {"key": "k"})
        credentials.set_credential(self.conn, "u1", "minimax", {"key": "m"})
        self.assertEqual(credentials.providers(self.conn, "u1"), ["azure", "minimax"])

    def test_delete(self):
        credentials.set_credential(self.conn, "u1", "azure", {"key": "k"})
        credentials.delete_credential(self.conn, "u1", "azure")
        self.assertEqual(credentials.providers(self.conn, "u1"), [])


if __name__ == "__main__":
    unittest.main()
