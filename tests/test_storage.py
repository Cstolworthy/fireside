"""Local storage backend: content addressing, verify-on-read, idempotence."""

import hashlib
import os
import shutil
import tempfile
import unittest

from storage.local import LocalBackend


class TestLocalBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fireside-store-")
        self.b = LocalBackend(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_put_returns_content_addressed_ref(self):
        ref = self.b.put(b"hello")
        self.assertEqual(ref["checksum"], hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(ref["bytes"], 5)
        self.assertTrue(os.path.isfile(ref["storage_ref"]))

    def test_idempotent_same_bytes_one_blob(self):
        a = self.b.put(b"same")
        c = self.b.put(b"same")
        self.assertEqual(a, c)
        files = []
        for dirpath, _dirs, names in os.walk(self.b.objects):
            files += [n for n in names if not n.endswith(".tmp")]
        self.assertEqual(len(files), 1)

    def test_get_roundtrip(self):
        ref = self.b.put(b"payload\x00\xff")
        self.assertTrue(self.b.has(ref["checksum"]))
        self.assertEqual(self.b.get(ref["checksum"]), b"payload\x00\xff")

    def test_missing_raises_and_has_false(self):
        self.assertFalse(self.b.has("0" * 64))
        with self.assertRaises(FileNotFoundError):
            self.b.get("0" * 64)

    def test_corrupt_blob_raises_on_read(self):
        ref = self.b.put(b"good")
        with open(ref["storage_ref"], "wb") as fh:
            fh.write(b"evil")
        with self.assertRaises(IOError):
            self.b.get(ref["checksum"])

    def test_put_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            self.b.put("a string")


if __name__ == "__main__":
    unittest.main()
