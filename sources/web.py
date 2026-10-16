"""Generic web-page source: any public http/https link is one chapter.
The fallback source, so it is registered LAST. Matches any http(s) url."""

import re
import urllib.parse

from ._text import clean_fragment


def parse_web(page: str, url: str) -> dict:
    """A fetched page -> {'title','chapters'}. One page is its own chapter."""
    title = "Untitled"
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    if m:
        t = clean_fragment(m.group(1))
        if t:
            title = t[:160]

    frag = None
    for pat in (r"(?is)<article\b[^>]*>(.*?)</article>",
                r"(?is)<main\b[^>]*>(.*?)</main>"):
        m = re.search(pat, page)
        if m:
            frag = m.group(1)
            break
    if not frag:
        m = re.search(r"(?is)<body\b[^>]*>(.*?)</body>", page)
        frag = m.group(1) if m else page

    text = clean_fragment(frag)
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
        return book

    def settings(self):
        return []

    def credential_hosts(self):
        return []
