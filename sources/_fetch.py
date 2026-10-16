"""Host-owned HTTP egress for sources. See docs/SOURCE-CONTRACT.md.

The plugin never opens a socket. It calls the `fetch` the host hands it; the host
attaches the credential (only to hosts the source declared), and logs every
request. This module is that fetch, plus the credential-scoping check.

Transport (added v1.10): some sites gate on the TLS ClientHello, not on routing.
archiveofourown.org sits behind Cloudflare and answers a plain urllib/requests
client with HTTP 525 (and a chrome-fingerprinted one with 403), while a browser
gets the page. A source that hits one of those declares `transport()` and the
host performs the fetch that way -- still through this one logged path, still
with the cookie attached only to declared hosts. The source owns the quirk; the
host still owns the socket and the credential.
"""

import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; reader-local/1.0; personal read-aloud)"
DEFAULT_LIMIT = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 30


def host_allowed(url: str, credential_hosts) -> bool:
    """True if `url`'s host is inside the source's declared credential scope.
    Matching is on host or a dot-boundary suffix (archiveofourown.org matches
    www.archiveofourown.org, not notarchiveofourown.org)."""
    if not credential_hosts:
        return False
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for h in credential_hosts:
        h = h.lower().lstrip(".")
        if host == h or host.endswith("." + h):
            return True
    return False


def _http_urllib(url, headers, limit, timeout):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(limit)
        cs = r.headers.get_content_charset()
    return raw.decode(cs or "utf-8", "replace")


def _http_impersonate(url, headers, limit, timeout, impersonate):
    """Fetch with a browser TLS/HTTP fingerprint via curl_cffi. Needed where the
    site's bot check grades the ClientHello. Raises on non-2xx so the retry loop
    (rate-limit and challenge responses are often transient) can back off."""
    try:
        from curl_cffi import requests as creq
    except Exception as e:  # pragma: no cover - dependency-missing path
        raise RuntimeError(
            "this source needs the 'curl_cffi' client to pass the site's bot "
            "check (pip install curl_cffi): %s" % e)
    sess = creq.Session(impersonate=impersonate)
    r = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError("HTTP %d from %s" % (r.status_code, url))
    return r.text[:limit]


def fetch(url: str, cred=None, credential_hosts=(), limit: int = DEFAULT_LIMIT,
          transport=None):
    """Fetch one url and return text. Attaches the credential as a Cookie header
    ONLY when the host is in scope. Never logs the credential. `transport` is the
    source's declared fetch policy (see docs/SOURCE-CONTRACT.md); None = plain
    urllib, same as before."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("only http/https links")
    t = transport or {}
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    }
    headers.update(t.get("headers") or {})
    if cred and host_allowed(url, credential_hosts):
        cookie = cred.get("cookie") or cred.get("session") or ""
        if cookie:
            headers["Cookie"] = cookie
    imp = t.get("impersonate")
    timeout = t.get("timeout", DEFAULT_TIMEOUT)
    retries = int(t.get("retries", 0))
    backoff = float(t.get("backoff", 1.5))
    last = None
    for attempt in range(retries + 1):
        try:
            if imp:
                return _http_impersonate(url, headers, limit, timeout, imp)
            return _http_urllib(url, headers, limit, timeout)
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last


def bind_transport(base_fetch, transport):
    """Wrap a fetch so it carries a source's declared transport, without the
    source call-sites knowing. Returns base_fetch unchanged when transport is
    falsy, so plain sources fetch exactly as before."""
    if not transport:
        return base_fetch

    def bound(url, cred=None, credential_hosts=()):
        return base_fetch(url, cred=cred, credential_hosts=credential_hosts,
                          transport=transport)

    return bound
