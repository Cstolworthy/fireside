"""The v2 host, served: HTTP routes over the registry, library, jobs, ledger.

One deployment-agnostic server. Running at home and running hosted differ only
in auth mode and in whose credential is used; the routes are identical. Nothing
here imports a plugin by name - it goes through the registry.

Endpoints (JSON unless noted):
  GET  /health                              what this deployment loaded
  GET  /plugins                             readers + speech + storage, with fields
  GET  /library?user=                        the user's books
  POST /import   {user, kind, url|data_b64, filename}   reader -> book
  POST /render   {user, book, provider, voice, model, chapters?}   -> {job_id}
  GET  /job?id=                              job status
  GET  /progress?user=&book=                 per-chapter title, chars, rendered status
  GET  /text?book=&idx=                      the chapter text (story page, no audio)
  GET  /chapter?book=&idx=                   raw audio bytes (verify-before-play)
  POST /speak    {text, connection_id|provider, voice, model, speed}  free-text
                synth, straight back as audio (the voice sandbox; nothing stored)
  GET  /position?user=&book=                 current position
  POST /position {user, book, chapter_index, offset_seconds, device}
  POST /claim    {user, book, device}        take ownership; stops the other
  GET  /stop?user=&book=&device=             has this device been told to stop
  POST /ack      {user, book, device}        confirm the stop
"""

import base64
import ipaddress
import json
import os
import re
import socket
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from host.host import Host
from host import accounting, auth, connections, credentials, db, jobs, library, playback
from sources._fetch import fetch as _fetch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(BASE_DIR, "ui")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
HOST_VERSION = "2.0"


class App:
    def __init__(self, plugins_root=None, db_path=None, storage_id="local",
                 sale_connection=None, rate_limiter=None, hosted=False,
                 require_auth=False):
        self.host = Host(plugins_root)
        self.db_path = db_path
        self.conn = db.connect(db_path)
        library.backfill_renditions(self.conn)   # a book voiced before renditions still lists its voice
        self.storage_id = storage_id
        self.sale_connection = sale_connection
        self.rate_limiter = rate_limiter
        self.hosted = hosted
        self.require_auth = require_auth
        self.auth = auth.Auth(self.conn)

    def _uid(self, q, body):
        """The signed-in user wins over any user id the client passed in."""
        return ((q or {}).get("_user") or (body or {}).get("user")
                or (q or {}).get("user") or "")

    @property
    def storage(self):
        return self.host.storage[self.storage_id].obj

    # ---- registry -------------------------------------------------------------

    def health(self, q, body):
        h = self.host.health()
        h["hosted"] = self.hosted
        h["storage_backend"] = self.storage_id
        h["db_backend"] = db.backend_name()
        return 200, h

    def _fields(self, o):
        fn = getattr(o, "settings", None)
        if not callable(fn):
            return []
        try:
            raw = fn()
        except Exception:
            return []
        out = []
        for f in raw:                            # one shape for the UI: name + kind
            d = f.dict() if hasattr(f, "dict") else dict(f)
            if "name" not in d and "key" in d:
                d["name"] = d["key"]
            if "kind" not in d and "type" in d:
                d["kind"] = d["type"]
            out.append(d)
        return out

    def plugins(self, q, body):
        out = {"readers": [], "speech": [], "storage": []}
        for p in self.host.readers:
            out["readers"].append({"id": p.id, "label": p.label, "order": p.order,
                                   "settings": self._fields(p.obj)})
        for pid, p in self.host.speech.items():
            o = p.obj
            out["speech"].append({"id": pid, "label": p.label,
                                  "needs_key": bool(getattr(o, "needs_key", True)),
                                  "ext": getattr(o, "ext", ""),
                                  "settings": self._fields(o)})
        for pid, p in self.host.storage.items():
            out["storage"].append({"id": pid, "info": p.obj.info()})
        return 200, out

    def voices(self, q, body):
        uid = self._uid(q, body)
        provider = body.get("provider") or q.get("provider") or ""
        cid = body.get("connection_id") or q.get("connection_id") or ""
        cred = body.get("cred") or {}
        if cid:
            c = connections.get(self.conn, cid)
            if not connections.may_use(c, uid, auth.is_admin(self.conn, uid)):
                return 403, {"error": "no such connection"}
            resolved = connections.resolve(self.conn, uid, cid)
            if resolved is None:
                return 403, {"error": "connection is switched off"}
            provider, cred = c["provider"], resolved
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        raw = self.host.speech[provider].obj.list_voices(cred)
        return 200, {"voices": [v.dict() if hasattr(v, "dict") else dict(v) for v in raw]}

    def ui(self, q, body):
        """Serve the browser client. The host owns its own UI; plugins ship none."""
        with open(os.path.join(UI_DIR, "index.html"), "r", encoding="utf-8") as f:
            page = f.read().replace("__HOST_VERSION__", HOST_VERSION)
        return 200, {"_html": page}

    def favicon(self, q, body):
        """The browser-tab icon: the same mark the install screen shows."""
        with open(os.path.join(ASSETS_DIR, "favicon.ico"), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": "image/x-icon"}

    # ---- library --------------------------------------------------------------

    def library_list(self, q, body):
        return 200, library.list_books(self.conn, self._uid(q, None))

    def import_book(self, q, body):
        user = self._uid(q, body)
        req = {"kind": body.get("kind", "url"), "url": body.get("url"),
               "filename": body.get("filename"),
               "data": base64.b64decode(body["data_b64"]) if body.get("data_b64") else None}
        plugin = self._pick_reader(req)
        if plugin is None:
            return 400, {"error": "no reader matched"}
        cred = self._source_cred(user, plugin.id, body.get("cred"))
        book = plugin.obj.read(req, self._make_fetch(plugin.obj, cred), cred)
        bid = library.add_book(self.conn, self.storage, user, book)
        self._maybe_cover(bid, book)
        return 200, {"book_id": bid, "title": getattr(book, "title", ""),
                     "chapters": len(getattr(book, "chapters", []))}

    # ---- cover art -----------------------------------------------------------
    # A book's cover is its own image when the reader handed one over (an epub
    # almost always carries it), otherwise it is fetched by ISBN. Both paths are
    # generic: the host has no branch on which reader produced the book.

    COVER_MAX_BYTES = 8 * 1024 * 1024
    _COVER_URLS = ("https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false",)

    def _maybe_cover(self, book_id, book):
        data = getattr(book, "cover", None)
        mime = getattr(book, "cover_mime", "") or "image/jpeg"
        if not data:
            meta = getattr(book, "meta", None) or {}
            isbn = (meta.get("isbn") if isinstance(meta, dict) else "") or ""
            got = self._cover_by_isbn(isbn)
            if got:
                data, mime = got
        if not data:
            return
        try:
            library.set_cover(self.conn, self.storage, book_id, data, mime)
        except Exception:               # a cover is a nicety; never fail an import for it
            pass

    def _cover_by_isbn(self, isbn):
        """Fetch a cover for an ISBN from a keyless public source, or None."""
        isbn = re.sub(r"[^0-9Xx]", "", isbn or "")
        if not re.fullmatch(r"\d{9}[\dXx]|\d{13}", isbn or ""):
            return None
        for tpl in self._COVER_URLS:
            url = tpl.format(isbn=isbn)
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "fireside/2 (cover lookup)"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    ctype = (r.headers.get("Content-Type") or "").split(";")[0]
                    data = r.read(self.COVER_MAX_BYTES)
                if data and len(data) > 100:
                    return data, (ctype if ctype.startswith("image/") else "image/jpeg")
            except Exception:
                continue
        return None

    def cover_get(self, q, body):
        got = library.cover(self.conn, self.storage, q.get("book", ""))
        if not got:
            return 404, {"error": "no cover"}
        data, mime = got
        return 200, {"_raw": data, "_mime": mime, "checksum": ""}

    def put_cover(self, q, body):
        book_id = body.get("book", "")
        if not library.get_book(self.conn, book_id):
            return 404, {"error": "no such book"}
        url = (body.get("url") or "").strip()
        if url:
            got = self._cover_from_url(url)
            if not got:
                return 400, {"error": "could not fetch an image from that URL"}
            data, mime = got
            library.set_cover(self.conn, self.storage, book_id, data, mime)
            return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}
        raw = body.get("data_b64") or ""
        try:
            data = base64.b64decode(raw) if raw else b""
        except Exception:
            return 400, {"error": "bad image data"}
        if not data:
            return 400, {"error": "no image"}
        if len(data) > self.COVER_MAX_BYTES:
            return 413, {"error": "image is too large"}
        library.set_cover(self.conn, self.storage, book_id, data,
                          body.get("mime") or "image/jpeg")
        return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}

    def clear_cover(self, q, body):
        book_id = body.get("book", "")
        if not library.get_book(self.conn, book_id):
            return 404, {"error": "no such book"}
        library.clear_cover(self.conn, book_id)
        return 200, {"ok": True, "cover": ""}

    # a cover URL is user input, so it only ever reaches the public internet:
    # http(s), an image content-type (or a recognised image signature), and a
    # host that does not resolve into a private / loopback / link-local range.
    _IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"),
                    (b"\x89PNG\r\n\x1a\n", "image/png"),
                    (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
                    (b"RIFF", "image/webp"))

    def _url_allowed(self, url):
        """True only for an http(s) URL whose host resolves to a public address.
        A cover URL is user input, so it must never reach this box's own
        network or a file on it."""
        try:
            p = urllib.parse.urlsplit(url)
        except Exception:
            return False
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        try:
            infos = socket.getaddrinfo(p.hostname, p.port or
                                       (443 if p.scheme == "https" else 80))
        except Exception:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True

    def _cover_from_url(self, url):
        if not self._url_allowed(url):
            return None
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "fireside/2 (cover fetch)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                data = r.read(self.COVER_MAX_BYTES + 1)
        except Exception:
            return None
        if not data or len(data) > self.COVER_MAX_BYTES:
            return None
        sniff = ""
        for magic, m in self._IMAGE_MAGIC:
            if data.startswith(magic):
                sniff = m
                break
        if ctype.startswith("image/"):
            mime = ctype
        elif sniff:
            mime = sniff
        else:
            return None
        return data, mime

    def cover_auto(self, q, body):
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b:
            return 404, {"error": "no such book"}
        isbn = b.get("isbn") or body.get("isbn") or (b.get("meta") or {}).get("isbn") or ""
        got = self._cover_by_isbn(isbn)
        if not got:
            return 404, {"error": "no cover found" + (" for that ISBN" if isbn else "")}
        data, mime = got
        library.set_cover(self.conn, self.storage, book_id, data, mime)
        return 200, {"ok": True, "cover": library.get_book(self.conn, book_id)["cover"]}

    # ---- book details ---------------------------------------------------------
    # An ISBN is the key to a book's public metadata. Given one, the settings
    # panel can fill in author, description, publisher and date without the
    # reader having typed any of it. OpenLibrary is keyless, so this works on a
    # fresh install. Nothing here overwrites the book's own title.

    DETAILS_MAX_CHARS = 4000
    _OL_EDITION = "https://openlibrary.org/isbn/{isbn}.json"
    _OL_KEY = "https://openlibrary.org{key}.json"

    def _get_json(self, url):
        req = urllib.request.Request(
            url, headers={"User-Agent": "fireside/2 (book details)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read(4 * 1024 * 1024).decode("utf-8", "replace"))

    @staticmethod
    def _norm_isbn(isbn):
        return re.sub(r"[^0-9Xx]", "", str(isbn or ""))

    @staticmethod
    def _valid_isbn(isbn):
        return bool(re.fullmatch(r"\d{9}[\dXx]|\d{13}", isbn or ""))

    def _details_by_isbn(self, isbn):
        """Public metadata for an ISBN from OpenLibrary, or None."""
        isbn = self._norm_isbn(isbn)
        if not self._valid_isbn(isbn):
            return None
        try:
            ed = self._get_json(self._OL_EDITION.format(isbn=isbn))
        except Exception:
            return None
        out = {"isbn": isbn}
        title = ed.get("title") or ""
        if title and ed.get("subtitle"):
            title += ": " + ed["subtitle"]
        if title:
            out["title"] = title
        names = []
        for a in (ed.get("authors") or [])[:6]:
            key = a.get("key") if isinstance(a, dict) else None
            if not key:
                continue
            try:
                n = self._get_json(self._OL_KEY.format(key=key)).get("name")
            except Exception:
                n = ""
            if n:
                names.append(n)
        if not names and ed.get("by_statement"):
            names = [ed["by_statement"]]
        if names:
            out["author"] = ", ".join(names)
        pubs = [p for p in (ed.get("publishers") or []) if p][:3]
        if pubs:
            out["publisher"] = ", ".join(pubs)
        if ed.get("publish_date"):
            out["published"] = str(ed["publish_date"])
        desc = ed.get("description")
        if not desc:
            works = ed.get("works") or []
            if works and isinstance(works[0], dict) and works[0].get("key"):
                try:
                    desc = self._get_json(
                        self._OL_KEY.format(key=works[0]["key"])).get("description")
                except Exception:
                    desc = None
        if isinstance(desc, dict):
            desc = desc.get("value")
        if desc:
            out["description"] = str(desc).strip()[:self.DETAILS_MAX_CHARS]
        return out

    def fetch_details(self, q, body):
        """Look a book up by ISBN and store what we learn. An ISBN in the body
        is saved first (the panel just typed one); otherwise the stored one is
        used. The cover is fetched too, but only when the book has none."""
        book_id = body.get("book", "")
        b = library.get_book(self.conn, book_id)
        if not b:
            return 404, {"error": "no such book"}
        isbn = self._norm_isbn(body.get("isbn"))
        if isbn:
            library.set_details(self.conn, book_id, isbn=isbn)
            b = library.get_book(self.conn, book_id)
        else:
            isbn = b.get("isbn") or ""
        if not self._valid_isbn(isbn):
            return 400, {"error": "that does not look like an ISBN"}
        details = self._details_by_isbn(isbn)
        if not details:
            return 404, {"error": "no details found for that ISBN"}
        fields = {k: details[k] for k in
                  ("author", "publisher", "published", "description")
                  if details.get(k)}
        if not b.get("title") and details.get("title"):
            fields["title"] = details["title"]
        if fields:
            library.set_details(self.conn, book_id, **fields)
        if not b.get("cover"):
            got = self._cover_by_isbn(isbn)
            if got:
                try:
                    library.set_cover(self.conn, self.storage, book_id, got[0], got[1])
                except Exception:
                    pass
        return 200, {"ok": True, "book": self._book_payload(book_id)}

    def _book_payload(self, book_id):
        b = library.get_book(self.conn, book_id) or {}
        return {"id": book_id, "title": b.get("title", ""),
                "author": b.get("author", ""), "cover": b.get("cover", ""),
                "isbn": b.get("isbn", ""), "publisher": b.get("publisher", ""),
                "published": b.get("published", ""),
                "description": b.get("description", "")}

    def _pick_reader(self, req):
        for p in self.host.readers:
            try:
                if p.obj.matches(req):
                    return p
            except Exception:
                continue
        return None

    def _make_fetch(self, o, cred):
        hosts = tuple(o.credential_hosts() or []) if callable(
            getattr(o, "credential_hosts", None)) else ()
        tr = o.transport() if callable(getattr(o, "transport", None)) else None

        def f(url, **kw):
            return _fetch(url, cred=cred, credential_hosts=hosts, transport=tr)
        return f

    def _source_cred(self, user, source_id, client_cred=None):
        """The credential a source reads with. The server holds it; a value the
        client sent for this source wins over a stored one (a fresh paste), and
        otherwise the stored value fills in. A reader's login never has to live
        in a browser."""
        cred = dict(client_cred or {})
        stored = credentials.get_credential(self.conn, user, "source:" + source_id)
        for k, v in (stored or {}).items():
            if v not in (None, ""):
                cred.setdefault(k, v)
        return cred

    # ---- render ---------------------------------------------------------------

    def render(self, q, body):
        user = self._uid(q, body)
        book_id = body["book"]
        cid = body.get("connection_id") or ""
        cred = None
        provider = body.get("provider", "")
        if cid:
            c = connections.get(self.conn, cid)
            if not connections.may_use(c, user, auth.is_admin(self.conn, user)):
                return 403, {"error": "no such connection"}
            cred = connections.resolve(self.conn, user, cid)
            if cred is None:
                return 403, {"error": "connection is switched off"}
            provider = c["provider"]
        voice = body.get("voice", "")
        voice_label = body.get("voice_label", "")
        model = body.get("model", "")
        speed = float(body.get("speed", 1.0) or 1.0)
        only = body.get("chapters")
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        if cred is None:
            cred = body.get("cred") or credentials.get_credential(self.conn, user, provider)
        if self.rate_limiter is not None and not self.rate_limiter.allow(user):
            return 429, {"error": "rate limited"}
        speech = self.host.speech[provider].obj
        rows = library.chapters(self.conn, book_id)
        est = sum(c["chars"] or 0 for c in rows)
        todo = [c["idx"] for c in rows]
        if only is not None:
            todo = [i for i in todo if i in set(only)]
        sale = accounting.sale_connection_matches(self.sale_connection, provider, voice, model)
        res_id = None
        if sale:
            quote = speech.quote({"chars_est": est, "cred": cred})
            if not getattr(quote, "eligible", True):
                return 402, {"error": "not eligible",
                             "reason": getattr(quote, "refusal_reason", "")}
            price = float(getattr(quote, "price", 0.0) or 0.0)
            if price > 0:
                try:
                    res_id = accounting.reserve(self.conn, user, price,
                                                getattr(quote, "currency", "USD"))
                except accounting.Insufficient as e:
                    return 402, {"error": "insufficient balance", "detail": str(e)}
        jid = jobs.start_job(self.conn, user, book_id, speech.id, voice, model,
                             total=len(todo))
        opts = {"model": model, "speed": speed}

        def _work():
            # A render can run for minutes; it gets its own DB connection so it
            # never blocks the request thread and the client can poll /job.
            c = db.connect(self.db_path)
            try:
                jobs.render_book(c, self.storage, speech, user, book_id, voice,
                                 model, speed, cred=cred, opts=opts, only=only,
                                 job_id=jid, voice_label=voice_label)
                if sale and res_id:
                    rep = speech.report({"chars_actual": est, "cred": cred, "job_id": jid})
                    accounting.settle(c, res_id, rep)
            finally:
                c.close()

        threading.Thread(target=_work, daemon=True).start()
        return 200, {"job_id": jid, "total": len(todo)}

    def job(self, q, body):
        j = jobs.job(self.conn, q.get("id", ""))
        return (200, j) if j else (404, {"error": "no job"})

    def progress(self, q, body):
        book_id = q.get("book", "")
        b = library.get_book(self.conn, book_id) or {}
        ch = library.chapters(self.conn, book_id)
        rends = {}
        for r in library.renditions(self.conn, book_id):
            rends.setdefault(r["idx"], []).append({
                "provider": r["provider"], "voice": r["voice"],
                "voice_label": r["voice_label"], "model": r["model"],
                "audio_ref": r["audio_ref"], "audio_bytes": r["audio_bytes"]})
        return 200, {"book": self._book_payload(book_id),
                     "chapters": [
            {"idx": c["idx"], "title": c["title"], "chars": c["chars"],
             "rendered": bool(c["audio_ref"]),
             "audio_ref": c["audio_ref"], "audio_bytes": c["audio_bytes"],
             "provider": c["provider"], "voice": c["voice"], "model": c["model"],
             "renditions": rends.get(c["idx"], [])}
            for c in ch]}

    def text(self, q, body):
        """The chapter's text, for the now-playing page. Not the audio path."""
        try:
            idx = int(q["idx"])
            t = library.chapter_text(self.conn, self.storage, q["book"], idx)
        except (KeyError, ValueError):
            return 404, {"error": "no chapter"}
        rows = [c for c in library.chapters(self.conn, q["book"]) if c["idx"] == idx]
        return 200, {"text": t, "title": rows[0]["title"] if rows else ""}

    def chapter(self, q, body):
        idx = int(q["idx"])
        book_id = q["book"]
        voice = q.get("voice") or ""
        provider = q.get("provider") or ""
        model = q.get("model") or ""
        if voice or provider:
            # a named voice: serve that rendition, and say so if it is not there
            r = library.rendition(self.conn, book_id, idx, provider, voice, model)
            if not r:
                return 409, {"error": "not rendered in that voice"}
            data = self.storage.get(r["audio_ref"])
            return 200, {"_raw": data, "_mime": "audio/mpeg", "bytes": len(data),
                         "checksum": r["audio_ref"]}
        rows = [r for r in library.chapters(self.conn, book_id) if r["idx"] == idx]
        if not rows:
            return 404, {"error": "no chapter"}
        r = rows[0]
        if not r["audio_ref"]:
            return 409, {"error": "not rendered"}
        data = self.storage.get(r["audio_ref"])
        return 200, {"_raw": data, "_mime": "audio/mpeg", "bytes": len(data),
                     "checksum": r["audio_ref"]}

    # ---- voice sandbox --------------------------------------------------------
    # Say a short bit of free text in one voice, right now, and hand the audio
    # straight back. Not a render: nothing is stored, nothing is metered, and the
    # only limit is the provider's own. The sandbox page drives this once per
    # voice for the scan button.

    SPEAK_MAX_CHARS = 1000

    def speak(self, q, body):
        uid = self._uid(q, body)
        text = (body.get("text") or "").strip()
        if not text:
            return 400, {"error": "nothing to say"}
        if len(text) > self.SPEAK_MAX_CHARS:
            return 400, {"error": "text is longer than %d characters" % self.SPEAK_MAX_CHARS}
        cid = body.get("connection_id") or ""
        provider = body.get("provider", "")
        cred = None
        if cid:
            c = connections.get(self.conn, cid)
            if not connections.may_use(c, uid, auth.is_admin(self.conn, uid)):
                return 403, {"error": "no such connection"}
            cred = connections.resolve(self.conn, uid, cid)
            if cred is None:
                return 403, {"error": "connection is switched off"}
            provider = c["provider"]
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        if cred is None:
            cred = body.get("cred") or credentials.get_credential(self.conn, uid, provider)
        voice = body.get("voice", "")
        model = body.get("model", "")
        speed = float(body.get("speed", 1.0) or 1.0)
        if self.rate_limiter is not None and not self.rate_limiter.allow(uid):
            return 429, {"error": "rate limited"}
        speech = self.host.speech[provider].obj
        audio = speech.synth(text, voice, {"model": model, "speed": speed}, cred)
        data = getattr(audio, "data", None) or getattr(audio, "bytes", None) or b""
        if not data:
            return 502, {"error": "the voice server returned no audio"}
        return 200, {"_raw": data, "_mime": getattr(audio, "mime", "audio/mpeg"),
                     "bytes": len(data), "chars": len(text)}

    # ---- playback -------------------------------------------------------------

    def get_position(self, q, body):
        return 200, playback.get_position(self.conn, self._uid(q, None), q["book"]) or {}

    def last_position(self, q, body):
        return 200, playback.last_position(self.conn, self._uid(q, None))

    def put_position(self, q, body):
        return 200, playback.put_position(self.conn, self._uid(q, body), body["book"],
                                          body["chapter_index"], body["offset_seconds"],
                                          body.get("device", ""))

    def claim(self, q, body):
        return 200, playback.claim(self.conn, self._uid(q, body), body["book"], body["device"])

    def stop(self, q, body):
        return 200, {"stop": playback.stop_pending(self.conn, self._uid(q, None), q["book"],
                                                   q["device"])}

    def ack(self, q, body):
        playback.ack_stop(self.conn, self._uid(q, body), body["book"], body["device"])
        return 200, {"ok": True}

    # ---- credentials (hosted; values never leave) -----------------------------

    def put_credential(self, q, body):
        credentials.set_credential(self.conn, self._uid(q, body), body["provider"],
                                   body.get("data") or {})
        return 200, {"ok": True, "providers": credentials.providers(self.conn, self._uid(q, body))}

    # ---- source credentials (server-side, never returned) ---------------------

    def _source_field_defs(self, sid):
        for p in self.host.readers:
            if p.id == sid:
                return self._fields(p.obj)
        return []

    def source_creds(self, q, body):
        """Every reader that declares settings, with its fields and whether the
        signed-in user has a stored value. A value is never returned."""
        uid = self._uid(q, body)
        out = []
        for p in self.host.readers:
            fields = self._fields(p.obj)
            if not fields:
                continue
            stored = credentials.get_credential(self.conn, uid, "source:" + p.id)
            fs = []
            for f in fields:
                name = f.get("name") or f.get("key") or ""
                kind = f.get("kind") or f.get("type") or "text"
                sensitive = self._sensitive(name, kind)
                fs.append({"name": name, "label": f.get("label") or name,
                           "kind": kind, "help": f.get("help") or "",
                           "placeholder": f.get("placeholder") or "",
                           "sensitive": sensitive,
                           "set": bool(stored.get(name)) if stored else False,
                           "value": ""})
            out.append({"id": p.id, "label": p.label, "fields": fs})
        return 200, {"sources": out}

    def source_cred_save(self, q, body):
        """Store a source's credential server-side for the signed-in user. An
        empty field leaves the stored value alone, so re-saving a form that does
        not re-type a secret never blanks it."""
        uid = self._uid(q, body)
        sid = body.get("id") or ""
        defs = {f.get("name") or f.get("key"): (f.get("kind") or f.get("type") or "text")
                for f in self._source_field_defs(sid)}
        if not defs:
            return 400, {"error": "unknown source"}
        cur = credentials.get_credential(self.conn, uid, "source:" + sid)
        for k, v in (body.get("data") or {}).items():
            if k in defs and v not in (None, ""):
                cur[k] = v
        credentials.set_credential(self.conn, uid, "source:" + sid, cur)
        return 200, {"ok": True}

    def source_cred_delete(self, q, body):
        uid = self._uid(q, body)
        credentials.delete_credential(self.conn, uid, "source:" + (body.get("id") or ""))
        return 200, {"ok": True}

    # ---- connections (voice servers, kept on the server) ---------------------

    def _field_defs(self, provider):
        p = self.host.speech.get(provider)
        return self._fields(p.obj) if p else []

    def _sensitive(self, name, kind):
        return kind == "password" or name in ("key", "token", "cookie", "password", "secret")

    def _conn_view(self, c, uid, is_admin):
        """A connection as the client may see it: never a secret value."""
        fields = []
        for f in self._field_defs(c["provider"]):
            name = f.get("name") or f.get("key") or ""
            kind = f.get("kind") or f.get("type") or "text"
            merged = dict(c["data"]); merged.update(c.get("override") or {})
            val = merged.get(name)
            sensitive = self._sensitive(name, kind)
            fields.append({"name": name, "label": f.get("label") or name, "kind": kind,
                           "sensitive": sensitive, "set": val not in (None, ""),
                           "value": "" if sensitive else (val or "")})
        return {"id": c["id"], "scope": c["scope"], "provider": c["provider"],
                "label": c["label"], "mine": c["mine"], "system": c["system"],
                "enabled": c["enabled"],
                "editable": connections.may_edit(c, uid, is_admin), "fields": fields}

    def connections_list(self, q, body):
        uid = self._uid(q, body)
        is_admin = auth.is_admin(self.conn, uid)
        out = [self._conn_view(c, uid, is_admin)
               for c in connections.list_for(self.conn, uid)]
        prefs = connections.get_prefs(self.conn, uid)
        return 200, {"connections": out, "admin": is_admin,
                     "default_connection_id": prefs["default_connection_id"],
                     "default_voice": prefs["default_voice"]}

    def _writable_fields(self, provider, uid, is_admin):
        """The field-name -> kind map for a provider (who may write what is
        decided by the caller)."""
        return {f.get("name") or f.get("key"): (f.get("kind") or f.get("type") or "text")
                for f in self._field_defs(provider)}

    def connection_save(self, q, body):
        uid = self._uid(q, body)
        is_admin = auth.is_admin(self.conn, uid)
        cid = body.get("id") or None
        scope = body.get("scope") or connections.PERSONAL
        provider = body.get("provider") or ""
        if cid:
            cur = connections.get(self.conn, cid)
            if not connections.may_edit(cur, uid, is_admin):
                return 403, {"error": "not yours to edit"}
            provider, scope = provider or cur["provider"], cur["scope"]
            # an admin may share a server with the household, or stop sharing it
            if is_admin and body.get("scope") in (connections.SYSTEM, connections.PERSONAL):
                scope = body["scope"]
        elif scope == connections.SYSTEM and not is_admin:
            return 403, {"error": "only an admin adds a shared server"}
        if provider not in self.host.speech:
            return 400, {"error": "unknown provider"}
        defs = self._writable_fields(provider, uid, is_admin)
        data = {k: v for k, v in (body.get("data") or {}).items()
                if k in defs and (is_admin or not self._sensitive(k, defs[k]))}
        cid = connections.upsert(self.conn, uid, provider, body.get("label"),
                                 data, scope=scope, conn_id=cid)
        return 200, {"ok": True, "id": cid}

    def connection_delete(self, q, body):
        uid = self._uid(q, body)
        cur = connections.get(self.conn, body.get("id") or "")
        if not connections.may_edit(cur, uid, auth.is_admin(self.conn, uid)):
            return 403, {"error": "not yours to remove"}
        connections.delete(self.conn, cur["id"])
        return 200, {"ok": True}

    def connection_override(self, q, body):
        """A user's own switch and non-secret edits for one connection."""
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        cur = connections.get(self.conn, body.get("id") or "")
        if not connections.may_use(cur, uid, auth.is_admin(self.conn, uid)):
            return 403, {"error": "no such connection"}
        defs = self._writable_fields(cur["provider"], uid, False)
        data = {k: v for k, v in (body.get("data") or {}).items()
                if k in defs and not self._sensitive(k, defs[k])}
        connections.set_override(self.conn, uid, cur["id"],
                                 enabled=body.get("enabled"), data=data)
        return 200, {"ok": True}

    def prefs_set(self, q, body):
        uid = self._uid(q, body)
        if not uid:
            return 401, {"error": "auth required"}
        kw = {}
        if "default_connection_id" in body:
            kw["connection_id"] = body.get("default_connection_id") or ""
        if "default_voice" in body:
            kw["voice"] = body.get("default_voice") or ""
        connections.set_prefs(self.conn, uid, **kw)
        return 200, {"ok": True}

    # ---- auth (social sign-in; the session lives in sqlite) ------------------

    def auth_providers(self, q, body):
        return 200, {"providers": self.auth.providers()}

    def auth_policy(self, q, body):
        """What a stranger may do before they have a session."""
        return 200, {
            "providers": self.auth.providers(),
            "has_accounts": auth.account_count(self.conn) > 0,
            "registration_open": auth.registration_open(self.conn),
        }

    def auth_users(self, q, body):
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        return 200, {"users": auth.local_accounts(self.conn),
                     "registration_open": auth.registration_open(self.conn),
                     "you": uid}

    def set_registration(self, q, body):
        uid = (q or {}).get("_user") or ""
        if not auth.is_admin(self.conn, uid):
            return 403, {"error": "admin only"}
        v = auth.set_registration(self.conn, bool((body or {}).get("open")))
        return 200, {"ok": True, "registration": v}

    def me(self, q, body):
        uid = (q or {}).get("_user") or ""
        return 200, {"user": uid, "authenticated": bool(uid),
                     "admin": auth.is_admin(self.conn, uid)}


GET_ROUTES = {
    "/": "ui", "/index.html": "ui", "/favicon.ico": "favicon",
    "/health": "health", "/plugins": "plugins", "/library": "library_list",
    "/job": "job", "/progress": "progress", "/chapter": "chapter",
    "/position": "get_position", "/last": "last_position", "/stop": "stop", "/text": "text",
    "/connections": "connections_list", "/source-creds": "source_creds",
    "/cover": "cover_get",
    "/auth/providers": "auth_providers", "/me": "me", "/auth/policy": "auth_policy",
    "/auth/users": "auth_users",
}
POST_ROUTES = {
    "/import": "import_book", "/render": "render", "/voices": "voices",
    "/speak": "speak",
    "/cover": "put_cover", "/cover/auto": "cover_auto", "/cover/clear": "clear_cover",
    "/book/details": "fetch_details",
    "/position": "put_position", "/claim": "claim", "/ack": "ack",
    "/credential": "put_credential", "/auth/registration": "set_registration",
    "/connections": "connection_save", "/connections/delete": "connection_delete",
    "/source-creds": "source_cred_save", "/source-creds/delete": "source_cred_delete",
    "/connections/override": "connection_override", "/prefs": "prefs_set",
}

# reachable without a session even when auth is required: the page, its health,
# the sign-in surface, and the browser's cache of who it is.
PUBLIC_ROUTES = {"/", "/index.html", "/favicon.ico", "/health", "/auth/providers", "/auth/policy", "/me"}

# auth sub-routes the handler serves directly (they set cookies / redirect),
# so they are not in the GET/POST dispatch tables above. Declared here so the
# browser client's served-route guard can see them too.
AUTH_ROUTES = {"/auth/logout"}


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fireside-host/2"

        # ---- cookies + redirects ---------------------------------------------
        def _cookie(self, name):
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return ""

        def _base_url(self):
            env = os.environ.get("FIRESIDE_BASE_URL")
            if env:
                return env.rstrip("/")
            return "http://" + (self.headers.get("Host") or "127.0.0.1:8788")

        def _cookie_str(self, name, value, path="/", max_age=None):
            c = "%s=%s; Path=%s; HttpOnly; SameSite=Lax" % (name, value, path)
            if max_age is not None:
                c += "; Max-Age=%d" % max_age
            if self._base_url().startswith("https"):
                c += "; Secure"
            return c

        def _redirect(self, location, cookies=None):
            self.send_response(302)
            self.send_header("Location", location)
            for c in (cookies or []):
                self.send_header("Set-Cookie", c)
            self.end_headers()

        def _token(self):
            tok = self._cookie(auth.SESSION_COOKIE)
            if not tok:
                authz = self.headers.get("Authorization", "")
                if authz.startswith("Bearer "):
                    tok = authz[7:]
            return tok

        def _auth_route(self, parts, parsed):
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            pid, action = parts[1], parts[2]
            if pid not in app.auth.table:
                return self.send_error(404)
            try:
                redirect_uri = self._base_url() + "/auth/%s/callback" % pid
                if action == "start":
                    state = auth.new_state()
                    url = app.auth.authorize_url(pid, redirect_uri, state)
                    return self._redirect(url, [self._cookie_str(
                        auth.STATE_COOKIE, state, path="/auth", max_age=600)])
                if action == "callback":
                    if q.get("error"):
                        return self.send_error(400, "oauth error: %s" % q["error"])
                    if not q.get("state") or q["state"] != self._cookie(auth.STATE_COOKIE):
                        return self.send_error(400, "bad oauth state")
                    uid, token = app.auth.login(pid, q.get("code", ""), redirect_uri)
                    return self._redirect("/", [
                        self._cookie_str(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL),
                        self._cookie_str(auth.STATE_COOKIE, "", path="/auth", max_age=0)])
            except auth.AuthError as e:
                return self._json(401, {"error": str(e)})
            return self.send_error(404)

        def _dispatch(self, routes):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            path = u.path.rstrip("/") or "/"
            parts = path.strip("/").split("/")
            if (self.command == "GET" and len(parts) == 3 and parts[0] == "auth"):
                return self._auth_route(parts, u)
            if path == "/auth/logout" and self.command == "POST":
                auth.delete_session(app.conn, self._token())
                return self._redirect("/", [self._cookie_str(
                    auth.SESSION_COOKIE, "", max_age=0)])
            if path in ("/auth/local/register", "/auth/local/login") \
                    and self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    return self.send_error(400)
                try:
                    if path.endswith("register"):
                        uid = auth.local_register(app.conn, body.get("username", ""),
                                                  body.get("password", ""))
                    else:
                        uid = auth.local_login(app.conn, body.get("username", ""),
                                               body.get("password", ""))
                except auth.AuthError as e:
                    return self._json(401, {"error": str(e)})
                token = auth.create_session(app.conn, uid, "local")
                return self._json(200, {"ok": True, "user": uid,
                                        "admin": auth.is_admin(app.conn, uid)},
                                  cookie=self._cookie_str(
                                      auth.SESSION_COOKIE, token,
                                      max_age=auth.SESSION_TTL))
            name = routes.get(path)
            if name is None:
                return self.send_error(404)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = {}
            if self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n).decode("utf-8"))
                    except ValueError:
                        return self.send_error(400)
            q["_user"] = auth.session_user(app.conn, self._token()) or ""
            if app.require_auth and path not in PUBLIC_ROUTES and not q["_user"]:
                return self._json(401, {"error": "auth required"})
            try:
                status, payload = getattr(app, name)(q, body)
            except Exception as e:
                return self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})
            if isinstance(payload, dict) and "_html" in payload:
                raw = payload["_html"].encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif isinstance(payload, dict) and "_raw" in payload:
                raw = payload["_raw"]
                self.send_response(status)
                self.send_header("Content-Type", payload.get("_mime", "application/octet-stream"))
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Checksum", payload.get("checksum", ""))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._json(status, payload)

        def _json(self, status, payload, cookie=None):
            b = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._dispatch(GET_ROUTES)

        def do_POST(self):
            self._dispatch(POST_ROUTES)

        def log_message(self, *args):
            pass

    return Handler


def serve(app=None, port=8788, host="127.0.0.1"):
    app = app or App()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return app, httpd


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 for this machine; 0.0.0.0 to share on the LAN")
    ap.add_argument("--plugins", default=None)
    ap.add_argument("--data", default=None,
                    help="data dir for the DB and blobs (default: $FIRESIDE_DATA)")
    ap.add_argument("--hosted", action="store_true")
    ap.add_argument("--require-auth", action="store_true",
                    help="reject requests with no session (default: on)")
    ap.add_argument("--open", action="store_true",
                    help="serve without requiring a session (development only)")
    args = ap.parse_args()
    if args.data:                      # one knob sets DB + blob root alike
        os.environ["FIRESIDE_DATA"] = args.data
    # A session is required unless the operator explicitly opens the server
    # (--open / FIRESIDE_OPEN=1). --require-auth / --hosted still force it on.
    require = args.require_auth or args.hosted or (not args.open)
    if _truthy(os.environ.get("FIRESIDE_REQUIRE_AUTH")):
        require = True
    if _truthy(os.environ.get("FIRESIDE_OPEN")):
        require = False
    app, httpd = serve(App(plugins_root=args.plugins, hosted=args.hosted,
                           require_auth=require), args.port, args.host)
    print("fireside v2 host on http://%s:%d" % (args.host, httpd.server_address[1]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


