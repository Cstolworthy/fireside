"""Azure Blob backend: contract behavior with the network faked out.

No real Azure calls here. urllib.request.urlopen is replaced with an in-memory
blob store, so these tests prove the backend's logic (content addressing,
checksum verification, 404 handling, container creation, signing is applied,
SAS skips signing) without a subscription. The signing string itself is checked
structurally; the end-to-end proof is a live round-trip, run separately.

The whole point of the stdlib implementation is that this file imports with no
packages installed - the same way CI runs.
"""

import hashlib
import io
import unittest
import urllib.error

from storage import azure_blob
from storage.azure_blob import AzureBlobBackend, parse_connection_string

CONN = ("DefaultEndpointsProtocol=https;AccountName=acct;"
        "AccountKey=" + ("a" * 44) + "==;EndpointSuffix=core.windows.net")


class FakeAzure:
    """A tiny in-memory stand-in for the blob service."""

    def __init__(self):
        self.blobs = {}
        self.container = False
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        method = req.get_method()
        self.calls.append((method, url, dict(req.headers)))
        if "restype=container" in url:
            self.container = True
            return _resp(201)
        name = url.split("?")[0].rsplit("/", 1)[-1]
        if method == "PUT":
            self.blobs[name] = req.data
            return _resp(201)
        if method == "HEAD":
            if name in self.blobs:
                return _resp(200, {"Content-Length": str(len(self.blobs[name]))})
            raise urllib.error.HTTPError(url, 404, "not found", {},
                                         io.BytesIO(b""))
        if method == "GET":
            if name in self.blobs:
                return _resp(200, {}, self.blobs[name])
            raise urllib.error.HTTPError(url, 404, "not found", {},
                                         io.BytesIO(b""))
        if method == "DELETE":
            if name in self.blobs:
                del self.blobs[name]
                return _resp(202)
            raise urllib.error.HTTPError(url, 404, "not found", {},
                                         io.BytesIO(b""))
        raise AssertionError("unexpected method %s" % method)


def _resp(status, headers=None, body=b""):
    class R:
        def __init__(self):
            self.status = status
            self.headers = headers or {}

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return R()


class TestConnectionString(unittest.TestCase):
    def test_parses_fields(self):
        d = parse_connection_string(CONN)
        self.assertEqual(d["AccountName"], "acct")
        self.assertEqual(d["EndpointSuffix"], "core.windows.net")

    def test_endpoint_default(self):
        b = AzureBlobBackend(conn=CONN)
        self.assertEqual(b.endpoint, "https://acct.blob.core.windows.net")
        self.assertTrue(b.configured)

    def test_absent_config_is_unconfigured(self):
        b = AzureBlobBackend(conn="")
        self.assertFalse(b.configured)

    def test_sas_without_key_is_configured(self):
        b = AzureBlobBackend(account="acct", sas="sv=2021;sig=x")
        self.assertTrue(b.configured)

    def test_name_is_content_sharded(self):
        b = AzureBlobBackend(conn=CONN)
        ref = "ab" + "c" * 62
        self.assertEqual(b._name(ref), "objects/ab/" + ref)


class TestContract(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAzure()
        self._orig = azure_blob.urllib.request.urlopen
        azure_blob.urllib.request.urlopen = self.fake
        self.b = AzureBlobBackend(conn=CONN)

    def tearDown(self):
        azure_blob.urllib.request.urlopen = self._orig

    def test_put_returns_sha256_and_is_idempotent(self):
        ref = self.b.put(b"hello world")
        self.assertEqual(ref, hashlib.sha256(b"hello world").hexdigest())
        before = len(self.fake.blobs)
        self.assertEqual(self.b.put(b"hello world"), ref)
        self.assertEqual(len(self.fake.blobs), before)      # one blob, not two

    def test_put_creates_container_once(self):
        self.b.put(b"a")
        self.b.put(b"b")
        creates = [c for c in self.fake.calls if "restype=container" in c[1]]
        self.assertEqual(len(creates), 1)

    def test_put_sets_blockblob_header(self):
        self.b.put(b"x")
        put = [c for c in self.fake.calls
               if c[0] == "PUT" and "restype=container" not in c[1]][0]
        hdrs = {k.lower(): v for k, v in put[2].items()}
        self.assertEqual(hdrs.get("x-ms-blob-type"), "BlockBlob")

    def test_has_size_get_delete_round_trip(self):
        ref = self.b.put(b"payload")
        self.assertTrue(self.b.has(ref))
        self.assertEqual(self.b.size(ref), len(b"payload"))
        self.assertEqual(self.b.get(ref), b"payload")
        self.b.delete(ref)
        self.assertFalse(self.b.has(ref))
        self.assertEqual(self.b.size(ref), 0)

    def test_get_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.b.get("0" * 64)

    def test_get_verifies_checksum(self):
        ref = self.b.put(b"payload")
        # corrupt the stored bytes under the same name
        for name in list(self.fake.blobs):
            self.fake.blobs[name] = b"tampered"
        with self.assertRaises(IOError):
            self.b.get(ref)

    def test_delete_is_idempotent(self):
        self.b.delete("0" * 64)          # missing -> no raise

    def test_signed_request_carries_authorization(self):
        self.b.put(b"x")
        put = [c for c in self.fake.calls
               if c[0] == "PUT" and "restype=container" not in c[1]][0]
        auth = {k.lower(): v for k, v in put[2].items()}.get("authorization", "")
        self.assertTrue(auth.startswith("SharedKey acct:"))

    def test_sas_skips_authorization_and_appends_token(self):
        b = AzureBlobBackend(account="acct", sas="sv=2021&sig=abc")
        b.put(b"x")
        put = [c for c in self.fake.calls
               if c[0] == "PUT" and "restype=container" not in c[1]][0]
        hdrs = {k.lower(): v for k, v in put[2].items()}
        self.assertNotIn("authorization", hdrs)
        self.assertIn("sig=abc", put[1])


class TestPluginTree(unittest.TestCase):
    def test_azure_discovered_and_exposes_contract(self):
        import os
        from host.pluginhost import discover
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "plugins")
        d = discover("storage", root)
        self.assertIn("azure", d)
        o = d["azure"].obj
        for m in ("put", "has", "get", "delete", "size", "info"):
            self.assertTrue(callable(getattr(o, m, None)), "azure missing %s" % m)


if __name__ == "__main__":
    unittest.main()
