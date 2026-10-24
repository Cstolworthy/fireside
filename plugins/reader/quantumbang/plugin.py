"""v2 reader plugin: quantumbang.org. One story, published as N numbered pages.

A Quantum Bang story is split across pages whose slug carries the part:

    hidden-resources-for-young-men-of-quality-1-5-meyarimcfarland
                                              ^^^  part 1 of 5

Strip that marker and you get the landing page, which lists every part. So the
family key of a page is its slug minus any `-N/M-` segment, and every page in a
story shares one. Give this reader any page of the story (landing page or a
part) and it gathers the family, fetches each part once, and returns one book
with one chapter per part, in order.

It does NOT follow rel="next": on this site that chain walks out of the story
into whatever else the tag links to. The slug family is the guard, so a story
whose parts are not all linked still stops where the family stops, and a blog
post that links to an unrelated page never gets crawled.

The part pages repeat the same metadata block (Title:, Author:, Fandom:, ...)
at the top of each. It is read once, into the book's meta, and dropped from
every chapter: nobody wants warnings read aloud between parts.
"""

import os
import re
import sys
import urllib.parse

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.contracts import to_book
from host.textutil import best_fragment

HOSTS = ("quantumbang.org",)
MAX_PARTS = 60                     # a real story, not a crawl

_HREF = re.compile(r"""(?is)<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""")
# -12-15- anywhere in a slug: part 12 of 15. Bounded so a year or an id cannot
# pass for a part number.
_PART = re.compile(r"-(?P<n>\d{1,2})-(?P<m>\d{1,2})-")
# A metadata line: "Title: ...", "Content Rating: PG-13", "Relationship(s): Gen"
_FIELD = re.compile(r"^[A-Z][A-Za-z0-9 ()/.'-]{1,40}:\s*\S")
MAX_HEADER_PARAGRAPHS = 16


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def is_quantumbang(url: str) -> bool:
    h = _host(url)
    if not any(h == x or h.endswith("." + x) for x in HOSTS):
        return False
    return urllib.parse.urlparse(url).scheme in ("http", "https")


def slug_of(url: str) -> str:
    """The path as a family key: no scheme, query, fragment or trailing slash."""
    path = urllib.parse.urlparse(url).path or ""
    return urllib.parse.unquote(path).strip("/").lower()


def split_slug(slug: str):
    """(family, part, of) for a page slug. Landing pages have no part: (slug, None, None)."""
    for m in _PART.finditer(slug):
        n, total = int(m.group("n")), int(m.group("m"))
        if not (1 <= n <= total <= MAX_PARTS):
            continue
        family = (slug[:m.start()] + "-" + slug[m.end():]).strip("-")
        return family, n, total
    return slug, None, None


def _links(page: str, base_url: str) -> list:
    """Every absolute same-host link on the page, deduped, in page order."""
    base_host = _host(base_url)
    out, seen = [], set()
    for raw in _HREF.findall(page):
        url = urllib.parse.urljoin(base_url, raw.strip())
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or _host(url) != base_host:
            continue
        clean = urllib.parse.urlunparse(u._replace(query="", fragment=""))
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def parts_in_page(page: str, url: str) -> dict:
    """{(n, of): url} for every page of this story's family linked from `page`."""
    family, n, of = split_slug(slug_of(url))
    found = {}
    if n is not None:
        found[(n, of)] = url
    for link in _links(page, url):
        fam, pn, pm = split_slug(slug_of(link))
        if fam != family or pn is None:
            continue
        found.setdefault((pn, pm), link)
    return found


def _best_run(found: dict):
    """The part set that is most complete: one `of` value, parts 1..of.

    A page can link two sizes of the same slug (a series landing page links a
    3-parter and a 5-parter). Taking the largest consistent run is what keeps
    the book in order and stops a stray part from a different size joining."""
    by_total = {}
    for (n, of) in found:
        by_total.setdefault(of, set()).add(n)
    best = None
    for of, ns in sorted(by_total.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if all(i in ns for i in range(1, of + 1)):
            best = (of, [found[(i, of)] for i in range(1, of + 1)])
            break
    if best is None:
        return None
    return best


def split_header(text: str):
    """(fields, body). The repeated metadata block at the top of a part, peeled off.

    Only a run of at least three `Key: value` paragraphs counts as a header, so
    a story paragraph that happens to contain a colon is never eaten."""
    paras = text.split("\n\n")
    fields = {}
    end = 0
    for i, p in enumerate(paras):
        if i >= MAX_HEADER_PARAGRAPHS:
            break
        line = p.strip()
        if not line:
            end = i + 1
            continue
        if not _FIELD.match(line):
            break
        key, _, val = line.partition(":")
        fields[key.strip()] = val.strip()
        end = i + 1
    if len(fields) < 3:
        return {}, text
    return fields, "\n\n".join(paras[end:]).strip()


def parse_parts(pages: list):
    """[(part_url, page_html)] -> book dict. Parts already in order."""
    chapters, meta, first_title = [], {}, ""
    for i, (url, page) in enumerate(pages):
        text = best_fragment(page)
        title_hint = ""
        m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", page)
        if m:
            title_hint = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if len(text) < 120:
            if len(pages) == 1:
                raise ValueError("no readable text on that page (login wall, or it builds itself in JS)")
            raise ValueError("part %d of that story has no readable text" % (i + 1))
        fields, body = split_header(text)
        if fields and not first_title:
            first_title = fields.get("Title", "")
            for k in ("Author", "Fandom", "Summary"):
                if fields.get(k):
                    meta[k.lower()] = fields[k]
        total = len(pages)
        ctitle = ("Part %d of %d" % (i + 1, total)) if total > 1 else (title_hint[:120] or "Part 1")
        chapters.append({"title": ctitle, "text": body})
    if not chapters:
        raise ValueError("that story has no parts this reader could find")
    title = first_title
    if not title:
        t = re.search(r"(?is)<title[^>]*>(.*?)</title>", pages[0][1])
        title = re.sub(r"<[^>]+>", "", t.group(1)).strip() if t else ""
        title = re.split(r"\s+[–—]\s+", title)[0].strip()[:160]
    if not title:
        title = "Untitled"
    meta.update({"source": "quantumbang", "url": pages[0][0], "parts": len(chapters)})
    return {"title": title[:200], "chapters": chapters, "meta": meta}


class Quantumbang:
    id = "quantumbang"
    label = "Quantum Bang story"

    def matches(self, req) -> bool:
        return req.get("kind") == "url" and is_quantumbang(req.get("url") or "")

    def read(self, req, fetch, cred=None):
        url = req["url"]
        first = fetch(url, cred=cred, credential_hosts=self.credential_hosts())
        pages = {slug_of(url): (url, first)}

        found = parts_in_page(first, url)
        run = _best_run(found) if len(found) > 1 else None
        if run:
            total, urls = run
            for part_url in urls[1:] if urls[0] == url else urls:
                key = slug_of(part_url)
                if key in pages:
                    continue
                pages[key] = (part_url,
                              fetch(part_url, cred=cred,
                                    credential_hosts=self.credential_hosts()))
            ordered = [pages[slug_of(u)] for u in urls]
        else:
            ordered = [(url, first)]

        book = parse_parts(ordered)
        if len(book["chapters"]) > 1:
            book["meta"]["page_count"] = len(pages)
        return to_book(book)

    def settings(self):
        return []

    def credential_hosts(self):
        return []
