"""Local filesystem blob store - the reference storage backend.

Content-addressed: a blob's identity is the sha256 of its bytes, so put() of the
same bytes twice is one blob and get() can verify what it returns. This is the
default backend and the one every test runs against; any other backend (S3,
Azure blob, a NAS) is an optional override. See docs/STORAGE.md.
"""

import hashlib
import os


class LocalBackend:
    """put/has/get over a directory of content-addressed files."""

    def __init__(self, root=None):
        if root is None:
            root = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "data", "blobs"
            )
        self.root = os.path.abspath(root)
        self.objects = os.path.join(self.root, "objects")

    def _path(self, checksum):
        return os.path.join(self.objects, checksum[:2], checksum)

    def put(self, data):
        """Store bytes, return a blob_ref. Idempotent."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("put expects bytes")
        b = bytes(data)
        chk = hashlib.sha256(b).hexdigest()
        p = self._path(chk)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(b)
            os.replace(tmp, p)
        return {"storage_ref": p, "bytes": len(b), "checksum": chk}

    def has(self, checksum):
        return os.path.isfile(self._path(checksum))

    def get(self, checksum):
        p = self._path(checksum)
        if not os.path.isfile(p):
            raise FileNotFoundError(checksum)
        with open(p, "rb") as fh:
            b = fh.read()
        got = hashlib.sha256(b).hexdigest()
        if got != checksum:
            raise IOError("checksum mismatch: stored %s != asked %s" % (got, checksum))
        return b
