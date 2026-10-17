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

    def test_put_returns_content_address(self):
        ref = self.b.put(b"hello")
        self.assertEqual(ref, hashlib.sha256(b"hello").hexdigest())
        self.assertTrue(self.b.has(ref))

    def test_idempotent_same_bytes_one_blob(self):
        a = self.b.put(b"same")
        c = self.b.put(b"same")
        self.assertEqual(a, c)
        files = []
        for _dirpath, _dirs, names in os.walk(self.b.objects):
            files += [n for n in names if not n.endswith(".tmp")]
        self.assertEqual(len(files), 1)

    def test_get_roundtrip(self):
        ref = self.b.put(b"payload\x00\xff")
        self.assertTrue(self.b.has(ref))
        self.assertEqual(self.b.get(ref), b"payload\x00\xff")

    def test_missing_raises_and_has_false(self):
        self.assertFalse(self.b.has("0" * 64))
        with self.assertRaises(FileNotFoundError):
            self.b.get("0" * 64)

    def test_corrupt_blob_raises_on_read(self):
        ref = self.b.put(b"good")
        with open(self.b._path(ref), "wb") as fh:
            fh.write(b"evil")
        with self.assertRaises(IOError):
            self.b.get(ref)

    def test_delete(self):
        ref = self.b.put(b"gone")
        self.b.delete(ref)
        self.assertFalse(self.b.has(ref))
        self.b.delete(ref)          # deleting a missing blob is not an error

    def test_put_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            self.b.put("a string")

    def test_info_is_local_and_durable(self):
        i = self.b.info()
        self.assertEqual(i["kind"], "local")
        self.assertTrue(i["durable"])


if __name__ == "__main__":
    unittest.main()
