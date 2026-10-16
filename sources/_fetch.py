"""Host-owned HTTP egress for sources. See docs/SOURCE-CONTRACT.md.

The plugin never opens a socket. It calls the `fetch` the host hands it; the host
attaches the credential (only to hosts the source declared), and logs every
request. This module is that fetch, plus the credential-scoping check.
"""

import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; reader-local/1.0; personal read-aloud)"
DEFAULT_LIMIT = 8 * 1024 * 1024


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


def fetch(url: str, cred=None, credential_hosts=(), limit: int = DEFAULT_LIMIT):
    """Fetch one url and return (text, headers). Attaches the credential as a
    Cookie header ONLY when the host is in scope. Never logs the credential."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("only http/https links")
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    }
    if cred and host_allowed(url, credential_hosts):
        cookie = cred.get("cookie") or cred.get("session") or ""
        if cookie:
            headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(limit)
        cs = r.headers.get_content_charset()
    return raw.decode(cs or "utf-8", "replace")
