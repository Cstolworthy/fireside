import os
import unittest
from unittest.mock import patch

import re

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

    def test_on_conflict_becomes_on_duplicate_key(self):
        sql = ("INSERT INTO positions(a,b,c) VALUES(?,?,?) "
               "ON CONFLICT(a,b) DO UPDATE SET c=excluded.c")
        self.assertEqual(
            db.translate(sql),
            "INSERT INTO positions(a,b,c) VALUES(%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE c=VALUES(c)")

    def test_on_conflict_rewrites_every_excluded_column(self):
        sql = ("INSERT INTO positions(user_id,book_id,chapter_index,offset_seconds,"
               "updated_at,owner_device_id) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(user_id,book_id) DO UPDATE SET "
               "chapter_index=excluded.chapter_index,"
               "offset_seconds=excluded.offset_seconds,updated_at=excluded.updated_at")
        self.assertEqual(
            db.translate(sql),
            "INSERT INTO positions(user_id,book_id,chapter_index,offset_seconds,"
            "updated_at,owner_device_id) VALUES(%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE chapter_index=VALUES(chapter_index),"
            "offset_seconds=VALUES(offset_seconds),updated_at=VALUES(updated_at)")

    def test_on_conflict_leaves_non_excluded_rhs_alone(self):
        sql = ("INSERT INTO t(a,b) VALUES(?,?) "
               "ON CONFLICT(a) DO UPDATE SET b=b+1, c=excluded.c")
        self.assertEqual(
            db.translate(sql),
            "INSERT INTO t(a,b) VALUES(%s,%s) "
            "ON DUPLICATE KEY UPDATE b=b+1,c=VALUES(c)")


class Backend(unittest.TestCase):
    def test_sqlite_is_default(self):
        self.assertEqual(db.backend_name(""), "sqlite")

    def test_sqlite_url(self):
        self.assertEqual(db.backend_name("sqlite:///tmp/x.db"), "sqlite")

    def test_mysql_urls(self):
        self.assertEqual(db.backend_name("mysql://u:p@h:3306/f"), "mysql")
        self.assertEqual(db.backend_name("mysql+pymysql://u:p@h/f"), "mysql")


class EnvVars(unittest.TestCase):
    """Engine (FIRESIDE_DB_TYPE) and connection (FIRESIDE_DB_CONN) are separate."""

    def test_type_and_connection_are_independent(self):
        with patch.dict(os.environ, {"FIRESIDE_DB_TYPE": "mysql",
                                     "FIRESIDE_DB_CONN": "u:p@h:3306/f"}):
            self.assertEqual(db.db_type(), "mysql")
            self.assertEqual(db.db_conn(), "u:p@h:3306/f")
            self.assertEqual(db.db_url(), "mysql://u:p@h:3306/f")

    def test_mysql_accepts_full_dsn_too(self):
        with patch.dict(os.environ, {"FIRESIDE_DB_TYPE": "mysql",
                                     "FIRESIDE_DB_CONN": "mysql://u:p@h/f"}):
            self.assertEqual(db.db_url(), "mysql://u:p@h/f")

    def test_sqlite_type_with_a_path(self):
        with patch.dict(os.environ, {"FIRESIDE_DB_TYPE": "sqlite",
                                     "FIRESIDE_DB_CONN": "/tmp/f.db"}):
            self.assertEqual(db.db_type(), "sqlite")
            self.assertEqual(db.backend_name(), "sqlite")
            self.assertEqual(db.db_url(), "sqlite:///tmp/f.db")

    def test_sqlite_default_has_no_conn(self):
        with patch.dict(os.environ, {"FIRESIDE_DB_TYPE": "sqlite",
                                     "FIRESIDE_DB_CONN": ""}):
            self.assertEqual(db.db_type(), "sqlite")
            self.assertEqual(db.db_conn(), "")
            self.assertEqual(db.db_url(), "")

    def test_legacy_single_variable_still_works(self):
        with patch.dict(os.environ, {"FIRESIDE_DB": "mysql://u:p@h/f"}):
            os.environ.pop("FIRESIDE_DB_TYPE", None)
            os.environ.pop("FIRESIDE_DB_CONN", None)
            self.assertEqual(db.db_type(), "mysql")
            self.assertEqual(db.db_url(), "mysql://u:p@h/f")

    def test_new_variables_win_over_legacy(self):
        with patch.dict(os.environ, {"FIRESIDE_DB": "mysql://old:p@h/f",
                                     "FIRESIDE_DB_TYPE": "sqlite",
                                     "FIRESIDE_DB_CONN": "/tmp/new.db"}):
            self.assertEqual(db.db_type(), "sqlite")
            self.assertEqual(db.db_url(), "sqlite:///tmp/new.db")


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


class SchemaPortability(unittest.TestCase):
    """The one schema must parse on MySQL as well as SQLite.

    MySQL rejects a literal DEFAULT on TEXT/BLOB/JSON columns (errno 1101), so a
    column like `source_json TEXT DEFAULT ''` boots on SQLite and crash-loops on
    MySQL. This guard keeps the schema honest for both engines.
    """

    def test_text_columns_have_no_literal_default(self):
        bad = re.findall(r"\b(TEXT|BLOB|GEOMETRY|JSON)\s+DEFAULT\b", db.SCHEMA, re.I)
        self.assertEqual(bad, [], "MySQL rejects DEFAULT on %s column(s)" % bad)


class MySQLTls(unittest.TestCase):
    """A hosted MySQL needs TLS; a LAN self-host must not be forced into it.

    Azure Database for MySQL runs with require_secure_transport=ON, so a
    plaintext connection is refused before auth. pymysql only speaks TLS when
    handed an SSL context, so `mysql_ssl_kwargs()` is the single decision point.
    The default is deliberately empty: nothing changes for an existing
    plaintext deployment.
    """

    def tearDown(self):
        os.environ.pop("FIRESIDE_DB_SSL", None)

    def test_unset_keeps_the_plaintext_default(self):
        self.assertEqual(db.mysql_ssl_kwargs(), {})

    def test_require_turns_on_tls(self):
        import ssl
        os.environ["FIRESIDE_DB_SSL"] = "1"
        kw = db.mysql_ssl_kwargs()
        self.assertIsInstance(kw["ssl"], ssl.SSLContext)
        self.assertEqual(kw["ssl"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kw["ssl"].check_hostname)

    def test_noverify_encrypts_without_verifying(self):
        import ssl
        os.environ["FIRESIDE_DB_SSL"] = "noverify"
        ctx = db.mysql_ssl_kwargs()["ssl"]
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)
