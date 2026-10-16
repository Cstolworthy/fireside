"""Source registry. See docs/SOURCE-CONTRACT.md.

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
"""

import hashlib

from ._fetch import fetch, host_allowed, bind_transport  # noqa: F401  (re-exported: host egress)


class Field:
    """One row of a source's settings screen. The host renders the widget; the
    source only declares the shape, same reason a provider never holds the login."""

    __slots__ = ("key", "label", "type", "help", "placeholder")

    def __init__(self, key, label, type="text", help="", placeholder=""):
        self.key = key
        self.label = label
        self.type = type            # "text" | "secret" | "select"
        self.help = help
        self.placeholder = placeholder

    def dict(self):
        return {"key": self.key, "label": self.label, "type": self.type,
                "help": self.help, "placeholder": self.placeholder}


def get_source(sid):
    """The one lookup that replaces the old inline source handling."""
    return REGISTRY.get(sid)


def describe():
    """The whole registry as plain data, so the host can render one Settings
    page listing every source and the fields it declares. A source ships no HTML;
    it only declares the shape and the host draws the widget."""
    out = []
    for s in ORDER:
        out.append({"id": s.id, "label": s.label,
                    "settings": [f.dict() for f in s.settings()],
                    "credential_hosts": list(s.credential_hosts())})
    return out


def choose(req):
    """First source whose matches() is true, in registration order. The generic
    web source is the fallback and is registered last."""
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
    """Run the matching source and return (source, book_id, book). The host calls
    this; it owns egress and the credential, the source owns matching + parsing."""
    s = choose(req)
    if s is None:
        raise ValueError("no source matches this input")
    base = fetcher or fetch
    tr = s.transport() if hasattr(s, "transport") else None
    book = s.read(req, bind_transport(base, tr), cred)
    book.setdefault("meta", {})
    book["meta"]["source"] = s.id
    return s, request_key(req), book


from .epub import Epub   # noqa: E402
from .ao3 import Ao3     # noqa: E402
from .web import Web     # noqa: E402

# Order matters: specific sources first, the generic web fallback LAST.
ORDER = [Epub(), Ao3(), Web()]
REGISTRY = {s.id: s for s in ORDER}
