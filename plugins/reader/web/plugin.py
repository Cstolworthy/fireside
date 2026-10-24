"""v2 reader plugin: web. Any public http/https link is one chapter.

The fallback reader, so its manifest declares the highest `order`. It carries
no site knowledge; the page -> container reading it needs lives in
host/textutil.py, shared with every reader that reads article pages.
"""
import os
import sys
import urllib.parse

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.contracts import to_book
from host.textutil import best_fragment, first_heading, page_title


def parse_web(page: str, url: str) -> dict:
    """A fetched page -> {'title','chapters'}. One page is its own chapter."""
    title = page_title(page) or "Untitled"
    text = best_fragment(page)
    if len(text) < 120:
        raise ValueError("no readable text on that page (login wall, or it builds itself in JS)")
    ch = first_heading(page)[:120]
    return {"title": title, "chapters": [{"title": ch or title[:120] or "Page", "text": text}]}


def canonical(url: str) -> str:
    """The same page, spelled one way: no fragment, no trailing slash, lower
    host. Query kept (it often names the story), case kept on the path."""
    u = urllib.parse.urlparse((url or "").strip())
    path = (u.path or "/").rstrip("/") or "/"
    return urllib.parse.urlunparse(
        (u.scheme.lower(), (u.netloc or "").lower(), path, "", u.query, ""))


def identity_of(url: str) -> str:
    return "web:" + canonical(url)


class Web:
    id = "web"
    label = "Web page"

    def matches(self, req) -> bool:
        if req.get("kind") != "url":
            return False
        return urllib.parse.urlparse(req.get("url") or "").scheme in ("http", "https")

    def read(self, req, fetch, cred=None) -> dict:
        url = req["url"]
        page = fetch(url, cred=cred, credential_hosts=self.credential_hosts())
        book = parse_web(page, url)
        book["meta"] = {"source": self.id, "url": url,
                        "identity": identity_of(url)}
        return to_book(book)

    def same(self, stored, incoming) -> bool:
        """Is the book already on the shelf the same page as this import?

        Books imported before readers named their identity carry only a url, so
        this is what lets a plain re-paste replace the copy instead of stacking
        a second one."""
        a, b = (stored or {}).get("url"), (incoming or {}).get("url")
        return bool(a and b and canonical(a) == canonical(b))

    def settings(self):
        return []

    def credential_hosts(self):
        return []
