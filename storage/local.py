"""Local filesystem blob store - the reference storage backend.

Content-addressed: a blob's identity is the sha256 of its bytes, so put() of the
same bytes twice is one blob and get() can verify what it returns. This is the
default backend and the one every test runs against; any other backend (S3,
Azure blob, a NAS) is an optional override. See docs/STORAGE.md.
"""

import hashlib
import os


class LocalBackend:
    """put/has/get/delete over a directory of content-addressed files."""

    def __init__(self, root=None):
        if root is None:
            root = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "data", "blobs"
            )
        self.root = os.path.abspath(root)
        self.objects = os.path.join(self.root, "objects")

    def _path(self, ref):
        return os.path.join(self.objects, ref[:2], ref)

    def put(self, data):
        """Store bytes, return the content-address ref (sha256 hex). Idempotent."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("put expects bytes")
        b = bytes(data)
        ref = hashlib.sha256(b).hexdigest()
        p = self._path(ref)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(b)
            os.replace(tmp, p)
        return ref

    def has(self, ref):
        return os.path.isfile(self._path(ref))

    def get(self, ref):
        p = self._path(ref)
        if not os.path.isfile(p):
            raise FileNotFoundError(ref)
        with open(p, "rb") as fh:
            b = fh.read()
        got = hashlib.sha256(b).hexdigest()
        if got != ref:
            raise IOError("checksum mismatch: stored %s != asked %s" % (got, ref))
        return b

    def delete(self, ref):
        p = self._path(ref)
        if os.path.isfile(p):
            os.remove(p)

    def info(self):
        return {"kind": "local", "durable": True, "url_ttl": None}
