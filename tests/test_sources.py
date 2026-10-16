#!/usr/bin/env python3
"""Self-contained check of the source registry and the HTTP ingest flow.

No external network: it builds an EPUB in a temp dir, serves one page from a
loopback http.server to prove credential scoping, and runs the real reader
server on a spare port to prove the refactor did not change behavior.
Run: python3 tests/test_sources.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

FAILS = []


def ck(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: %s" % (extra,)) if extra else ""))
    if not cond:
        FAILS.append(name)


def make_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:title>Test Book</dc:title></metadata><manifest>'
                   '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>'
                   '</manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
        for n in (1, 2):
            body = ("<html><body><h1>Chapter %d</h1><p>Fire burns here. Name it Fire. "
                    "%s</p></body></html>" % (n, "word " * 40))
            z.writestr("OEBPS/c%d.xhtml" % n, body)
    return buf.getvalue()


PAGE = ("<html><head><title>A Story</title></head><body><h1>Part One</h1>"
        "<p>%s</p></body></html>" % ("Fire and salt. " * 40))

# A trimmed AO3 work page: title h2, two chapters, each a div id="chapter-N"
# with a div.userstuff body and a blockquote notes block that must NOT be read.
AO3_PAGE = ("""<html><head><title>Test Fic - somebody | Archive of Our Own</title></head><body>
<div id="main"><h2 class="title heading">Test Fic</h2>
<div id="workskin"><div id="chapters">
<div class="chapter preface group"><h3 class="title">Chapter 1: Start</h3></div>
<div id="chapter-1" class="chapter" role="article">
  <div class="chapter preface group"><h3 class="title">Chapter 1: Start</h3>
  <blockquote class="userstuff">a summary, not the body</blockquote></div>
  <div class="userstuff module" role="article"><h3 class="landmark heading" id="work">Chapter Text</h3><p>Fire burns here.</p><p>%s</p></div>
  <div class="chapter notes module"><h3 class="landmark heading">Notes</h3>
  <blockquote class="userstuff">author note, not read aloud</blockquote></div>
</div>
<div id="chapter-2" class="chapter" role="article">
  <div class="chapter preface group"><h3 class="title">Chapter 2: Later</h3></div>
  <div class="userstuff module" role="article"><p>%s</p></div>
</div>
</div></div></div></body></html>""" % ("the middle " * 30, "the end " * 30))


class PageHandler(BaseHTTPRequestHandler):
    seen_cookie = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        PageHandler.seen_cookie = self.headers.get("Cookie")
        b = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def req(base, path, data=None, method="GET", raw=None, ctype="application/json"):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    r = urllib.request.Request(base + path, data=body, method=method,
                               headers={"Content-Type": ctype} if body else {})
    try:
        with urllib.request.urlopen(r, timeout=90) as x:
            return x.status, x.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def unit_tests():
    import sources
    from sources import (ORDER, REGISTRY, get_source, choose, request_key,
                         ingest, Field, describe)
    from sources._fetch import host_allowed, fetch, bind_transport
    from sources.web import parse_web
    from sources.epub import parse_epub
    from sources.ao3 import parse_ao3, is_work_url, full_work_url

    ck("registry has epub/ao3/web", set(REGISTRY) == {"epub", "ao3", "web"}, set(REGISTRY))
    ck("unknown source -> None", get_source("bogus") is None)
    ck("augmentable", hasattr(sources, "REGISTRY") and "web" in REGISTRY)
    ck("web registered last (fallback)", ORDER[-1].id == "web", [s.id for s in ORDER])

    up = {"kind": "upload", "filename": "b.epub", "data": b"PK\x03\x04x"}
    u = {"kind": "url", "url": "https://example.com/story?p=1"}
    ck("upload -> epub", choose(up).id == "epub")
    ck("http url -> web", choose(u).id == "web")
    ao3u = {"kind": "url", "url": "https://archiveofourown.org/works/12345"}
    ck("ao3 work url -> ao3 (not web)", choose(ao3u).id == "ao3")
    ck("ao3 tag url -> web", choose({"kind": "url", "url": "https://archiveofourown.org/tags/Good%20Omens/works"}).id == "web")
    ck("ftp url -> no source", choose({"kind": "url", "url": "ftp://x/y"}) is None)
    ck("request_key stable + idempotent", request_key(u) == request_key(u))
    ck("request_key upload == sha1(bytes)[:12]", request_key(up) == __import__("hashlib").sha1(up["data"]).hexdigest()[:12])

    # parsers on plain input
    b = parse_epub(make_epub(), "b.epub")
    ck("epub title", b["title"] == "Test Book", b["title"])
    ck("epub chapters", [c["title"] for c in b["chapters"]] == ["Chapter 1", "Chapter 2"])
    w = parse_web(PAGE, "https://x/y")
    ck("web title", w["title"] == "A Story", w["title"])
    ck("web one chapter", len(w["chapters"]) == 1 and w["chapters"][0]["title"] == "Part One")

    # AO3: one work -> one book, N chapters; summary/notes are not read
    ck("ao3 work url matches", is_work_url("https://archiveofourown.org/works/12345"))
    ck("ao3 subdomain matches", is_work_url("https://www.archiveofourown.org/works/12345/chapters/9"))
    ck("ao3 non-work does not match", not is_work_url("https://archiveofourown.org/tags/x/works"))
    ck("ao3 other host does not match", not is_work_url("https://notao3.example/works/1"))
    fw = full_work_url("https://archiveofourown.org/works/12345")
    ck("full work url asks for all chapters", "view_full_work=true" in fw and "view_adult=true" in fw, fw)
    # transport: AO3 declares a browser backend; bind_transport is inert when absent
    a3t = get_source("ao3").transport() if hasattr(get_source("ao3"), "transport") else {}
    ck("ao3 declares a browser backend", a3t.get("backend") == "browser", a3t)
    ck("plain source has no transport", not hasattr(get_source("web"), "transport"))
    ck("bind_transport inert when None", bind_transport(fetch, None) is fetch)
    # Chris's rule (2026-10-16): the system hosts integrations, it does not learn
    # one. The egress core must carry no vendor vocabulary; a client library lives
    # behind a named backend in _backends.py.
    import inspect
    from sources import _fetch as _fmod, _backends as _bmod
    fsrc = inspect.getsource(_fmod)
    ck("core egress names no vendor", not any(w in fsrc for w in ("curl_cffi", "impersonate")))
    ck("backends registry is generic", set(_bmod.BACKENDS) == {"plain", "browser"}, set(_bmod.BACKENDS))
    try:
        fetch("https://example.com/", transport={"backend": "nope"})
        ck("unknown backend rejected", False)
    except ValueError:
        ck("unknown backend rejected", True)
    except Exception as e:
        ck("unknown backend rejected", False, "wrong error: %r" % (e,))
    a = parse_ao3(AO3_PAGE, "https://archiveofourown.org/works/12345")
    ck("ao3 title", a["title"] == "Test Fic", a["title"])
    ck("ao3 N chapters, one book", [c["title"] for c in a["chapters"]] == ["Chapter 1: Start", "Chapter 2: Later"],
       [c["title"] for c in a["chapters"]])
    ck("ao3 body read, summary skipped", a["chapters"][0]["text"].startswith("Fire burns here.")
       and "summary" not in a["chapters"][0]["text"], a["chapters"][0]["text"][:60])
    ck("ao3 notes skipped", "author note" not in a["chapters"][0]["text"])
    ck("ao3 landmark heading stripped (live bug 10-16)", "Chapter Text" not in a["chapters"][0]["text"])
    ck("ao3 chapter 2 text", a["chapters"][1]["text"].startswith("the end"))

    # credential scoping
    ck("scope: apex", host_allowed("https://archiveofourown.org/works/1", ["archiveofourown.org"]))
    ck("scope: subdomain", host_allowed("https://www.archiveofourown.org/works/1", ["archiveofourown.org"]))
    ck("scope: no suffix bleed", not host_allowed("https://notarchiveofourown.org/x", ["archiveofourown.org"]))
    ck("scope: empty -> never", not host_allowed("https://archiveofourown.org/x", []))

    # a source declares Fields, ships no html
    f = Field("cookie", "AO3 session cookie", "secret", help="paste from your browser", placeholder="")
    ck("Field dict shape", f.dict() == {"key": "cookie", "label": "AO3 session cookie",
                                        "type": "secret", "help": "paste from your browser",
                                        "placeholder": ""})
    ck("epub/web declare no settings", REGISTRY["epub"].settings() == [] and REGISTRY["web"].settings() == [])
    afields = REGISTRY["ao3"].settings()
    ck("ao3 declares a secret cookie field", len(afields) == 1 and afields[0].key == "cookie"
       and afields[0].type == "secret")
    ck("ao3 scopes its cookie to AO3", REGISTRY["ao3"].credential_hosts() == ["archiveofourown.org"])
    desc = describe()
    ck("describe() lists every source", [d["id"] for d in desc] == [s.id for s in ORDER], [d["id"] for d in desc])
    ck("describe() carries fields as plain data", any(d["id"] == "ao3" and d["settings"][0]["key"] == "cookie" for d in desc))

    # live fetch: cookie attached ONLY inside scope
    srv = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/" % port
        PageHandler.seen_cookie = None
        fetch(url, cred={"cookie": "session=abc"}, credential_hosts=["127.0.0.1"])
        ck("cookie sent in scope", PageHandler.seen_cookie == "session=abc", PageHandler.seen_cookie)
        PageHandler.seen_cookie = None
        fetch(url, cred={"cookie": "session=abc"}, credential_hosts=["example.com"])
        ck("cookie withheld out of scope", PageHandler.seen_cookie is None, PageHandler.seen_cookie)
        PageHandler.seen_cookie = None
        got = fetch(url, cred={"cookie": "session=abc"}, credential_hosts=[])
        ck("fetch returns text", "Part One" in got or "Fire" in got)
        ck("no cookie with empty scope", PageHandler.seen_cookie is None, PageHandler.seen_cookie)
    finally:
        srv.shutdown()

    # ingest() with a supplied fetcher: cred reaches the source, source id lands in meta
    captured = {}

    class Stub:
        id = "stub"

        def matches(self, r):
            return r.get("kind") == "url"

        def read(self, r, fetch, cred=None):
            captured["cred"] = cred
            return {"title": "T", "chapters": [{"title": "c", "text": "hello world " * 20}]}

        def settings(self):
            return []

        def credential_hosts(self):
            return ["example.com"]

    saved = list(ORDER)
    ORDER.insert(0, Stub())
    try:
        s2, bid, book = ingest(u, cred={"cookie": "k"})
        ck("ingest picks the stub (precedence)", s2.id == "stub")
        ck("cred flows to the source", captured.get("cred") == {"cookie": "k"})
        ck("source id lands in meta", book["meta"]["source"] == "stub")
    finally:
        ORDER[:] = saved


def http_flow():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    out = tempfile.mkdtemp(prefix="reader_src_")
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py"),
                             "--port", str(port), "--out", out],
                            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/", timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        st, body = req(base, "/api/ingest?name=b.epub", method="POST",
                       raw=make_epub(), ctype="application/epub+zip")
        j = json.loads(body)
        ck("http ingest 200", st == 200, st)
        ck("http ingest reports source", j.get("source") == "epub", j.get("source"))
        ck("http ingest chapters", [c["title"] for c in j["chapters"]] == ["Chapter 1", "Chapter 2"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    unit_tests()
    http_flow()
    print()
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")

