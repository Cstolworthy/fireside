import unittest

from host import db


class Translate(unittest.TestCase):
    """The SQLite-isms the query layer relies on, rewritten for MySQL."""

    def test_insert_or_ignore(self):
        self.assertEqual(db.translate("INSERT OR IGNORE INTO x VALUES(?)"),
                         "INSERT IGNORE INTO x VALUES(%s)")

    def test_insert_or_replace(self):
        self.assertEqual(db.translate("INSERT OR REPLACE INTO x(a) VALUES(?)"),
                         "REPLACE INTO x(a) VALUES(%s)")

    def test_begin_immediate(self):
        self.assertEqual(db.translate("BEGIN IMMEDIATE"), "START TRANSACTION")

    def test_plain_statement_only_reparameterised(self):
        self.assertEqual(db.translate("SELECT * FROM t WHERE a=? AND b=?"),
                         "SELECT * FROM t WHERE a=%s AND b=%s")


class Backend(unittest.TestCase):
    def test_sqlite_is_default(self):
        self.assertEqual(db.backend_name(""), "sqlite")

    def test_sqlite_url(self):
        self.assertEqual(db.backend_name("sqlite:///tmp/x.db"), "sqlite")

    def test_mysql_urls(self):
        self.assertEqual(db.backend_name("mysql://u:p@h:3306/f"), "mysql")
        self.assertEqual(db.backend_name("mysql+pymysql://u:p@h/f"), "mysql")


class Schema(unittest.TestCase):
    def test_memory_connection_has_both_new_tables(self):
        c = db.connect(":memory:")
        try:
            c.execute("SELECT * FROM local_accounts").fetchall()
            rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            names = {r["name"] for r in rows}
            self.assertIn("render_cache", names)
            self.assertIn("local_accounts", names)
        finally:
            c.close()

    def test_reserved_word_column_is_usable(self):
        c = db.connect(":memory:")
        try:
            c.execute("INSERT INTO render_cache(`key`,audio_ref) VALUES(?,?)", ("k", "r"))
            r = c.execute("SELECT audio_ref FROM render_cache WHERE `key`=?", ("k",)).fetchone()
            self.assertEqual(r["audio_ref"], "r")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
