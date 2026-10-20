"""Azure Blob blob store - an optional storage backend behind the same contract
as the local filesystem (docs/STORAGE.md).

Content-addressed, exactly like the local backend: a blob's identity is the
sha256 of its bytes, so put() of the same bytes twice is one blob and get()
verifies what it returns. The difference is only where the bytes live.

stdlib only. Azure's Blob REST API with SharedKey signing is a short amount of
HMAC-SHA256 over a canonical string - not worth a dependency on the whole
azure-storage-blob SDK (and CI installs nothing, so the tests must import this
with no packages present). A SAS token is also accepted, which skips signing.

Config (all optional; absent config means this backend simply is not selected):

    FIRESIDE_AZURE_STORAGE_CONN   connection string:
        DefaultEndpointsProtocol=https;AccountName=..;AccountKey=..;EndpointSuffix=core.windows.net
    FIRESIDE_AZURE_ACCOUNT        account name (alternative to the connection string)
    FIRESIDE_AZURE_KEY            account key (base64), with FIRESIDE_AZURE_ACCOUNT
    FIRESIDE_AZURE_SAS            a container SAS token (alternative to a key)
    FIRESIDE_AZURE_CONTAINER      container name (default "fireside")
    FIRESIDE_AZURE_ENDPOINT       blob endpoint base (default https://<account>.blob.<suffix>)

Blob names shard like the local backend ("objects/<first two hex>/<ref>") so a
bucket listing is browsable, but nothing above this file depends on the layout.
"""

import base64
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
from email.utils import formatdate

API_VERSION = "2021-08-06"


def parse_connection_string(s):
    """Split a storage connection string into a dict. Last value wins."""
    out = {}
    for part in (s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class AzureBlobBackend:
    """put/has/get/delete over an Azure blob container, content-addressed."""

    def __init__(self, conn=None, account=None, key=None, sas=None,
                 container=None, endpoint=None):
        conn = conn if conn is not None else os.environ.get("FIRESIDE_AZURE_STORAGE_CONN", "")
        fields = parse_connection_string(conn)
        self.account = (account or fields.get("AccountName")
                        or os.environ.get("FIRESIDE_AZURE_ACCOUNT") or "").strip()
        self.key = (key or fields.get("AccountKey")
                    or os.environ.get("FIRESIDE_AZURE_KEY") or "").strip()
        self.sas = (sas if sas is not None
                    else os.environ.get("FIRESIDE_AZURE_SAS", "")).strip().lstrip("?")
        self.container = (container or os.environ.get("FIRESIDE_AZURE_CONTAINER")
                          or "fireside").strip()
        suffix = fields.get("EndpointSuffix") or "core.windows.net"
        base = (endpoint or os.environ.get("FIRESIDE_AZURE_ENDPOINT") or "").strip()
        if base:
            self.endpoint = base.rstrip("/")
        elif self.account:
            self.endpoint = "https://%s.blob.%s" % (self.account, suffix)
        else:
            self.endpoint = ""
        self._container_ready = False

    # ---- config / health ------------------------------------------------------

    @property
    def configured(self):
        return bool(self.endpoint and self.container and (self.key or self.sas))

    def info(self):
        return {"kind": "azure", "durable": True, "url_ttl": None,
                "container": self.container}

    # ---- naming ---------------------------------------------------------------

    def _name(self, ref):
        return "objects/%s/%s" % (ref[:2], ref)

    def _url(self, ref=""):
        u = "%s/%s" % (self.endpoint, urllib.parse.quote(self.container))
        if ref:
            u += "/" + urllib.parse.quote(self._name(ref))
        return u

    # ---- auth -----------------------------------------------------------------

    def _sign(self, method, path, headers, content_length):
        """Azure SharedKey StringToSign -> Authorization header value.

        Path is /<container>/<blob>. Query parameters are added by _request when
        present (only container create uses one).
        """
        xms = sorted((k.lower(), v) for k, v in headers.items()
                     if k.lower().startswith("x-ms-"))
        canon_headers = "".join("%s:%s\n" % (k, v) for k, v in xms)
        canon_res = "/%s%s" % (self.account, path)
        length = str(content_length) if content_length else ""
        sts = "\n".join([
            method,
            headers.get("Content-Encoding", ""),
            headers.get("Content-Language", ""),
            length,
            headers.get("Content-MD5", ""),
            headers.get("Content-Type", ""),
            "",                                   # Date is empty; x-ms-date is used
            headers.get("If-Modified-Since", ""),
            headers.get("If-Match", ""),
            headers.get("If-None-Match", ""),
            headers.get("If-Unmodified-Since", ""),
            headers.get("Range", ""),
        ]) + "\n" + canon_headers + canon_res
        digest = hmac.new(base64.b64decode(self.key),
                          sts.encode("utf-8"), hashlib.sha256).digest()
        return "SharedKey %s:%s" % (self.account,
                                    base64.b64encode(digest).decode("ascii"))

    def _request(self, method, path, query="", data=None, content_type=None):
        """One signed blob request. Returns (status, headers, body)."""
        url = "%s/%s%s" % (self.endpoint, urllib.parse.quote(self.container), path)
        if query:
            url += ("&" if "?" in url else "?") + query
        if self.sas:
            url += ("&" if "?" in url else "?") + self.sas
        headers = {"x-ms-date": formatdate(usegmt=True),
                   "x-ms-version": API_VERSION}
        if content_type:
            headers["Content-Type"] = content_type
        if not self.sas:
            headers["Authorization"] = self._sign(
                method, "/%s%s" % (urllib.parse.quote(self.container), path),
                headers, len(data) if data else 0)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def _ensure_container(self):
        if self._container_ready:
            return
        status, _, _ = self._request("PUT", "", query="restype=container")
        if status not in (201, 409):              # 409 = already exists
            raise IOError("azure: cannot create container %r (HTTP %s)"
                          % (self.container, status))
        self._container_ready = True

    # ---- contract -------------------------------------------------------------

    def put(self, data):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("put expects bytes")
        b = bytes(data)
        ref = hashlib.sha256(b).hexdigest()
        if self.has(ref):
            return ref
        self._ensure_container()
        status, _, _ = self._put_blob(ref, b)
        if status not in (201, 409):
            raise IOError("azure: put %s failed (HTTP %s)" % (ref, status))
        return ref

    def _put_blob(self, ref, b):
        """PUT a BlockBlob. x-ms-blob-type is required, so this is its own
        signed request rather than going through _request()."""
        path = "/" + self._name(ref)
        url = "%s/%s%s" % (self.endpoint, urllib.parse.quote(self.container), path)
        if self.sas:
            url += ("&" if "?" in url else "?") + self.sas
        headers = {"x-ms-date": formatdate(usegmt=True),
                   "x-ms-version": API_VERSION,
                   "x-ms-blob-type": "BlockBlob",
                   "Content-Type": "application/octet-stream"}
        if not self.sas:
            headers["Authorization"] = self._sign(
                "PUT", "/%s%s" % (urllib.parse.quote(self.container), path),
                headers, len(b))
        req = urllib.request.Request(url, data=b, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def has(self, ref):
        status, _, _ = self._request("HEAD", "/" + self._name(ref))
        return status == 200

    def size(self, ref):
        status, headers, _ = self._request("HEAD", "/" + self._name(ref))
        if status != 200:
            return 0
        try:
            return int(headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return 0

    def get(self, ref):
        status, _, body = self._request("GET", "/" + self._name(ref))
        if status == 404:
            raise FileNotFoundError(ref)
        if status != 200:
            raise IOError("azure: get %s failed (HTTP %s)" % (ref, status))
        got = hashlib.sha256(body).hexdigest()
        if got != ref:
            raise IOError("checksum mismatch: stored %s != asked %s" % (got, ref))
        return body

    def delete(self, ref):
        status, _, _ = self._request("DELETE", "/" + self._name(ref))
        if status not in (202, 404):
            raise IOError("azure: delete %s failed (HTTP %s)" % (ref, status))
