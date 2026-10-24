"""v2 reader plugin: web. Self-contained: the parse lives here.

Nothing above host/pluginhost.py imports a plugin by name, and this package
imports no other plugin. It uses the host's shared text helpers and the `fetch`
the host hands it -- never a socket of its own.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.contracts import Field, to_book


import re
import urllib.parse

from host.textutil import clean_fragment

_TAGNAME = re.compile(r"(?is)<(/?)([a-z][a-z0-9]*)\b[^>]*?(/?)>")

# Containers worth reading, in rough order of trust. The real text is not
# always the first match: WordPress with a related-posts plugin emits a stack
# of tiny <article> widgets after the story, and taking the first one is how a
# 115k-word page came back as "no readable text".
_CANDIDATES = (
    r"(?is)<(?P<tag>article)\b[^>]*>",
    r"""(?is)<(?P<tag>[a-z][a-z0-9]*)\b[^>]*class="[^"]*\b(?:entry-content|post-content|"""
    r"""article-content|story-content|content-body|chapter-content)\b[^"]*"[^>]*>""",
    r"(?is)<(?P<tag>main)\b[^>]*>",
    r"""(?is)<(?P<tag>[a-z][a-z0-9]*)\b[^>]*id="(?:content|main|primary)"[^>]*>""",
    r"(?is)<(?P<tag>body)\b[^>]*>",
)

# Page furniture that survives tag-stripping and would otherwise be read aloud
# after the chapter. Removed whole (nested tags included), not just opened.
_THROW_OUT = (
    r"""(?is)<(?P<tag>[a-z][a-z0-9]*)\b[^>]*id="(?:comments|respond|disqus_thread)"[^>]*>""",
    r"""(?is)<(?P<tag>[a-z][a-z0-9]*)\b[^>]*class="[^"]*\b(?:commentlist|comment-list|comments-area)\b[^"]*"[^>]*>""",
    r"""(?is)<(?P<tag>[a-z][a-z0-9]*)\b[^>]*class="[^"]*\b(?:related-posts|rltdpstsplgn-posts|sharedaddy)\b[^"]*"[^>]*>""",
)


def _element(page: str, start: int, tag: str) -> str:
    """Slice from an opening tag to its own closing tag, counting depth.

    A regex cannot do this: nested <div>s end the lazy match at the first
    inner close, which truncates exactly the pages worth reading."""
    depth = 0
    for m in _TAGNAME.finditer(page, start):
        if m.group(2).lower() != tag.lower() or m.group(3):
            continue
        if m.group(1):
            depth -= 1
            if depth == 0:
                return page[start:m.end()]
        else:
            depth += 1
    return page[start:]


def _drop(page: str, pattern: str) -> str:
    out, last = [], 0
    for m in re.finditer(pattern, page):
        if m.start() < last:
            continue
        block = _element(page, m.start(), m.group("tag"))
        out.append(page[last:m.start()])
        last = m.start() + len(block)
    out.append(page[last:])
    return "".join(out)


def _clean_page(page: str) -> str:
    for pattern in _THROW_OUT:
        page = _drop(page, pattern)
    return page


def _best_fragment(page: str) -> str:
    """The candidate container holding the most text. Longest wins, so a page
    with six tiny <article> widgets and one real body still reads right."""
    best = ""
    for pattern in _CANDIDATES:
        for m in re.finditer(pattern, page):
            text = clean_fragment(_element(page, m.start(), m.group("tag")))
            if len(text) > len(best):
                best = text
        if len(best) >= 120:
            return best
    return best


def parse_web(page: str, url: str) -> dict:
    """A fetched page -> {'title','chapters'}. One page is its own chapter."""
    title = "Untitled"
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    if m:
        t = clean_fragment(m.group(1))
        if t:
            title = t[:160]

    text = _best_fragment(_clean_page(page))
    if len(text) < 120:
        raise ValueError("no readable text on that page (login wall, or it builds itself in JS)")

    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", page)
    ch = clean_fragment(h1.group(1))[:120] if h1 else ""
    return {"title": title, "chapters": [{"title": ch or title[:120] or "Page", "text": text}]}


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
        book["meta"] = {"source": self.id, "url": url}
        return to_book(book)

    def settings(self):
        return []

    def credential_hosts(self):
        return []
