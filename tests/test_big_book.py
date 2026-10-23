"""A big book: import a real-sized EPUB and account for all of its text.

Every other test in this repo imports "hello world" twice, so a book with 60
chapters and 1.8M characters (a real shelf, in one file) had never been through
the import path at all. The bytes come from tools/make_big_epub.py, seeded, so
the assertions are on counts that a rerun reproduces exactly.
"""

import base64
import os
import sys
import time
import unittest

from host.app import App
from sources.epub import parse_epub
from tools.make_big_epub import build

CHARS = 1_800_000
CHAPTERS = 60


class TestGeneratedEpub(unittest.TestCase):
    def test_same_seed_same_bytes(self):
        self.assertEqual(build(20_000, 4, 3), build(20_000, 4, 3))
        self.assertNotEqual(build(20_000, 4, 3), build(20_000, 4, 4))

    def test_mimetype_is_first_and_stored(self):
        import io
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(build(2_000, 2, 1)))
        first = z.infolist()[0]
        self.assertEqual(first.filename, "mimetype")
        self.assertEqual(first.compress_type, zipfile.ZIP_STORED)

    def test_parses_to_the_asked_size(self):
        b = parse_epub(build(CHARS, CHAPTERS, 7), "big.epub")
        self.assertEqual(len(b["chapters"]), CHAPTERS)
        total = sum(len(c["text"]) for c in b["chapters"])
        self.assertGreaterEqual(total, CHARS * 0.95)
        self.assertLess(total, CHARS * 1.10)


class TestImportBigBook(unittest.TestCase):
    def setUp(self):
        self.app = App(db_path=":memory:")
        self.raw = build(CHARS, CHAPTERS, 7)

    def test_import_a_million_and_a_half_characters(self):
        t0 = time.time()
        s, out = self.app.import_book({}, {
            "user": "u1", "kind": "upload", "filename": "big.epub",
            "data_b64": base64.b64encode(self.raw).decode()})
        took = time.time() - t0
        self.assertEqual(s, 200, out)
        self.assertEqual(out["chapters"], CHAPTERS)
        rows = self.app.conn.execute(
            "SELECT chars FROM chapters WHERE book_id=?", (out["book_id"],)).fetchall()
        self.assertEqual(len(rows), CHAPTERS)
        self.assertGreaterEqual(sum(r["chars"] for r in rows), CHARS * 0.95)
        # A generous ceiling: this is a smoke test for a pathological import,
        # not a benchmark, and CI boxes are slow.
        self.assertLess(took, 60.0)

    def test_the_text_counts_against_storage(self):
        s, out = self.app.import_book({}, {
            "user": "u1", "kind": "upload", "filename": "big.epub",
            "data_b64": base64.b64encode(self.raw).decode()})
        self.assertEqual(s, 200, out)
        s, st = self.app.storage_get({"user": "u1"}, {})
        self.assertEqual(s, 200)
        self.assertGreaterEqual(st["own_bytes"], CHARS * 0.95)
        self.assertLessEqual(st["own_bytes"], st["allowance_bytes"])


if __name__ == "__main__":
    unittest.main()
