"""AO3 work source.

Matches an archiveofourown.org /works/<id> link and scrapes the work into one
book (one book, N chapters -- each chapter is its own audio file downstream,
same as the EPUB path).

This one is heavier than epub/web: a locked work needs the reader's own session
cookie, so the cookie lives host-side and is sent only to archiveofourown.org
(see docs/SOURCE-CONTRACT.md). The source never opens a socket; it calls the
`fetch` the host hands it.

The parse targets AO3's own markup (each chapter is a div id="chapter-N", the
body is a div.userstuff). That is HTML, not an API, so a re-skin can break it.
"""

import re
import urllib.parse

from ._text import clean_fragment

_CHAP = re.compile(r'(?is)<div\b[^>]*\bid="chapter-\d+"[^>]*>')
_TITLE = re.compile(r'(?is)<h[1-6][^>]*class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</h[1-6]>')
_H = re.compile(r'(?is)<h[1-6][^>]*>(.*?)</h[1-6]>')
_BODY = re.compile(r'(?is)<div\b[^>]*class="[^"]*\buserstuff\b[^"]*"[^>]*>(.*?)</div>')
# AO3 injects a screen-reader landmark per chapter body: <h3 class="landmark
# heading" id="work">Chapter Text</h3>. Tags strip but the words survive and
# would be read aloud at the top of every chapter, so drop the heading first.
_LANDMARK = re.compile(r'(?is)<h[1-6][^>]*\blandmark\b[^>]*>.*?</h[1-6]>')
_WSKIN = re.compile(r'(?is)<div\b[^>]*\bid="workskin"[^>]*>(.*)')
_WTITLE = re.compile(r'(?is)<h2[^>]*class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</h2>')


def is_work_url(url: str) -> bool:
    u = urllib.parse.urlparse(url or "")
    host = (u.hostname or "").lower()
    if not (host == "archiveofourown.org" or host.endswith(".archiveofourown.org")):
        return False
    return bool(re.match(r"^/works/\d+", u.path))


def full_work_url(url: str) -> str:
    """Ask AO3 for the whole work on one page and past the adult interstitial.
    view_adult=true is what the 'Proceed' link on a mature work sets."""
    u = urllib.parse.urlparse(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query)
         if k not in ("view_adult", "view_full_work")]
    q += [("view_adult", "true"), ("view_full_work", "true")]
    return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))


def parse_ao3(page: str, url: str) -> dict:
    """An AO3 work page -> {'title', 'chapters':[{'title','text'}...]}"""
    # a login page or the adult gate means we do not have the work body
    if 'id="workskin"' not in page and "chapter-" not in page:
        if re.search(r'(?i)id="login"|name="user\[login\]"', page):
            raise ValueError("AO3 says log in for this work. Paste your AO3 session "
                             "cookie in Source settings, or use the work's Download -> EPUB.")
        if "view_adult" in page and "This work could have adult content" in page:
            raise ValueError("AO3 showed the adult gate. Try the link again.")
        raise ValueError("no work body on that page (not a work, or a login wall)")

    title = "Untitled"
    m = _WTITLE.search(page)
    if m:
        t = clean_fragment(m.group(1))
        if t:
            title = t[:200]

    starts = [mm.start() for mm in _CHAP.finditer(page)]
    chapters = []
    for i, pos in enumerate(starts):
        seg = page[pos:starts[i + 1]] if i + 1 < len(starts) else page[pos:]
        bm = _BODY.search(seg)
        if not bm:
            continue
        text = clean_fragment(_LANDMARK.sub(" ", bm.group(1)))
        if len(text) < 2:
            continue
        tm = _TITLE.search(seg) or _H.search(seg)
        ctitle = clean_fragment(tm.group(1))[:160] if tm else ""
        chapters.append({"title": ctitle or ("Chapter %d" % (len(chapters) + 1)),
                         "text": text})

    if not chapters:
        # single-chapter fallback: take the whole workskin body
        wm = _WSKIN.search(page)
        region = wm.group(1) if wm else page
        bm = _BODY.search(region)
        if bm:
            text = clean_fragment(_LANDMARK.sub(" ", bm.group(1)))
            if len(text) >= 2:
                tm = _TITLE.search(region) or _H.search(region)
                ctitle = clean_fragment(tm.group(1))[:160] if tm else ""
                chapters.append({"title": ctitle or title[:120] or "Chapter 1", "text": text})

    if not chapters:
        raise ValueError("could not find any chapter text (AO3 markup may have changed)")
    return {"title": title, "chapters": chapters}


class Ao3:
    id = "ao3"
    label = "AO3 work"

    def matches(self, req) -> bool:
        return req.get("kind") == "url" and is_work_url(req.get("url"))

    def read(self, req, fetch, cred=None) -> dict:
        url = req["url"]
        page = fetch(full_work_url(url), cred=cred, credential_hosts=self.credential_hosts())
        book = parse_ao3(page, url)
        book["meta"] = {"source": self.id, "url": url}
        return book

    def settings(self):
        from . import Field
        return [Field(
            "cookie", "AO3 session cookie", "secret",
            help="Only needed for locked works. Log in to AO3 in your browser, then "
                 "DevTools > Application > Cookies > archiveofourown.org and copy the "
                 "sessionid value (or the whole cookie string). Never shared, only sent to AO3.",
            placeholder="sessionid=...")]

    def transport(self):
        # AO3 sits behind Cloudflare, which grades the TLS ClientHello: a plain
        # urllib/requests client gets HTTP 525, a chrome fingerprint gets 403,
        # a safari fingerprint gets the page. Declared here (not hardcoded in the
        # host) so the source owns the quirk; the host still performs the fetch
        # through its one logged, credential-scoped path. Retries+backoff because
        # the gate is also rate-sensitive: a burst flips a good request to 525.
        return {"impersonate": "safari17_0", "retries": 4, "backoff": 1.5,
                "timeout": 60}

    def credential_hosts(self):
        return ["archiveofourown.org"]

