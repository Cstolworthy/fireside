"""Source registry (v1 shim). See docs/SOURCE-CONTRACT.md.

One source = one object:

  id                    stable slug: part of the book identity and the meter tag.
                        Never rename once books exist under it.
  label                 human name for a picker UI
  matches(req)          -> bool. req = {kind:"upload"|"url", filename, data, url}
  read(req, fetch, cred)-> book, book = {title, chapters:[{title,text}], meta}
  settings()            -> [Field]  declarative; the host renders it, the source
                        ships no HTML
  credential_hosts()    -> [host suffix]; the host attaches the credential ONLY
                        to these hosts, as a Cookie header, and never logs it
  transport() [opt]     -> fetch policy: {"backend": "plain"|"browser", ...}
                        for sites that gate on the TLS fingerprint. Generic
                        fields: headers, retries, backoff, timeout; a named
                        backend may read its own. The host performs the fetch;
                        the source owns the quirk. Absent = plain urllib.

The plugin never opens a socket and never holds the login. It calls the `fetch`
the host hands it. `cred` is a resolved credential ({cookie: ...} for now); the
source cannot tell whether it came from paste-cookie or a later sign-in flow.

The readers themselves live in `plugins/reader/<id>/` now: one folder per site,
carrying its own parse. This package is the v1 compatibility registry the old
`server.py` and the v1 tests still call; it discovers the same plugins the v2
host does, so there is exactly one place a reader is defined.

Adding a site = adding a folder under plugins/reader/. Nothing in this file.
"""

import os
import hashlib

from host.contracts import Field  # noqa: F401  (re-exported for v1 callers)
from host.egress import fetch, host_allowed, bind_transport  # noqa: F401
from host.pluginhost import discover

PLUGINS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")
PLUGIN_ROOT = PLUGINS_ROOT


def _book_dict(book):
    """A plugin returns a host.contracts.Book; the v1 callers want a dict."""
    if isinstance(book, dict):
        return book
    return {"title": getattr(book, "title", "Untitled"),
            "chapters": [{"title": c.title, "text": c.text} for c in getattr(book, "chapters", [])],
            "meta": dict(getattr(book, "meta", {}) or {})}


def get_source(sid):
    """The v1 lookup by id."""
    return REGISTRY.get(sid)


def describe():
    """Every source as plain data, so one Settings page can list them."""
    out = []
    for s in ORDER:
        fields = s.settings() if callable(getattr(s, "settings", None)) else []
        out.append({"id": s.id, "label": s.label,
                    "settings": [(f.dict() if hasattr(f, "dict") else dict(f)) for f in fields],
                    "credential_hosts": list(s.credential_hosts())})
    return out


def choose(req):
    """First source whose matches() is true, in plugin order. The generic web
    source declares the highest order so it is tried last."""
    for s in ORDER:
        if s.matches(req):
            return s
    return None


def request_key(req) -> str:
    """The canonical identity of a request. Same key -> same book (idempotent
    re-ingest). Ordering guarantees only one source consumes a request."""
    if req.get("kind") == "upload":
        return hashlib.sha1(req["data"]).hexdigest()[:12]
    return hashlib.sha1((req.get("url") or "").encode("utf-8")).hexdigest()[:12]


def ingest(req, cred=None, fetcher=None):
    """Run the matching source and return (source, book_id, book-as-dict)."""
    s = choose(req)
    if s is None:
        raise ValueError("no source matches this input")
    base = fetcher or fetch
    tr = s.transport() if hasattr(s, "transport") else None
    book = _book_dict(s.read(req, bind_transport(base, tr), cred))
    book.setdefault("meta", {})
    book["meta"]["source"] = s.id
    return s, request_key(req), book


# Order matters: specific sources first, the generic web fallback LAST
# (its manifest declares the highest `order`).
ORDER = [p.obj for p in discover("reader", PLUGIN_ROOT)]
REGISTRY = {s.id: s for s in ORDER}
