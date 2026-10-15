#!/usr/bin/env python3
"""reader - turn an EPUB into audio files you can play, with a voice you pick.

Run it:      python3 server.py            -> open http://127.0.0.1:8765
Options:     python3 server.py --port 9000 --out ./out

Why files, not a live read-aloud: a browser speech engine streams and can stall
mid-page. Here the whole chapter is turned into one audio file first, so playback
can't drop out. Pause anywhere, come back, it resumes.

No third-party packages. An EPUB is a zip; it's read with the stdlib.

TTS providers
  local   - offline. A spoken-length tone. No key, no network. For trying the UI.
  azure   - Azure AI Speech. Needs a key + region. Great voice menu (hundreds).

Input: an .epub file, or any public http/https story link (fetched + cleaned here).
  minimax - MiniMax T2A. Needs a key + group id. The voice used in the demo clip.

Keys stay on this machine (sent to this local server only, stored in your browser).
Nothing is uploaded anywhere except the text you ask to be spoken, to the provider
you chose.
"""

import argparse
import hashlib
import html
import json
import os
import re
import struct
import sys
import urllib.parse
import urllib.request
import wave
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- epub parsing

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"[ \t\r\f\v]+")
BLANK = re.compile(r"\n{3,}")
CHUNK_TARGET = 3500  # characters per TTS request


def _text_of(xhtml: str) -> str:
    """Tags out, readable text back. Block tags become newlines."""
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", xhtml)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</?(p|div|h[1-6]|li|tr|blockquote|section)[^>]*>", "\n", s)
    s = TAG.sub("", s)
    s = html.unescape(s)
    s = WS.sub(" ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    s = "\n".join(ln for ln in lines if ln)
    return BLANK.sub("\n\n", s).strip()


def _first_heading(xhtml: str, fallback: str) -> str:
    m = re.search(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>", xhtml)
    if m:
        t = _text_of(m.group(1))
        if t:
            return t[:120]
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", xhtml)
    if m:
        t = _text_of(m.group(1))
        if t:
            return t[:120]
    return fallback


def parse_epub(data: bytes, name: str) -> dict:
    """bytes of an .epub -> {'title':..., 'chapters':[{'title','text'}...]}"""
    import io

    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()

    opf_path = None
    if "META-INF/container.xml" in names:
        root = _text_of_xml(z.read("META-INF/container.xml"))
        m = re.search(r'full-path="([^"]+)"', z.read("META-INF/container.xml").decode("utf-8", "replace"))
        if m:
            opf_path = m.group(1)
    if not opf_path:
        opf_path = next((n for n in names if n.lower().endswith(".opf")), None)
    if not opf_path:
        raise ValueError("no .opf in this epub")

    opf = z.read(opf_path).decode("utf-8", "replace")
    base = os.path.dirname(opf_path)

    title = "Untitled"
    m = re.search(r"(?is)<dc:title[^>]*>(.*?)</dc:title>", opf)
    if m:
        title = _text_of(m.group(1)) or title

    manifest = {}
    for m in re.finditer(r"(?is)<item\b[^>]*/?>", opf):
        tag = m.group(0)
        i = re.search(r'id="([^"]+)"', tag)
        h = re.search(r'href="([^"]+)"', tag)
        mt = re.search(r'media-type="([^"]+)"', tag)
        if i and h:
            manifest[i.group(1)] = (h.group(1), (mt.group(1) if mt else ""))

    order = []
    for m in re.finditer(r"(?is)<itemref\b[^>]*/?>", opf):
        i = re.search(r'idref="([^"]+)"', m.group(0))
        if i and i.group(1) in manifest:
            order.append(manifest[i.group(1)])

    chapters = []
    for idx, (href, mt) in enumerate(order):
        if "html" not in mt and not href.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        p = href if not base else os.path.join(base, href)
        p = p.replace("\\", "/")
        try:
            raw = z.read(p).decode("utf-8", "replace")
        except KeyError:
            continue
        txt = _text_of(raw)
        if len(txt) < 2:
            continue
        chapters.append({"title": _first_heading(raw, "Chapter %d" % (len(chapters) + 1)), "text": txt})

    if not chapters:
        raise ValueError("no readable chapters found")
    return {"title": title, "chapters": chapters}


def _text_of_xml(b: bytes) -> str:
    return b.decode("utf-8", "replace")


# --------------------------------------------------------------- web pages

UA = "Mozilla/5.0 (compatible; reader-local/1.0; personal read-aloud)"

# chrome that never belongs in the reading text
_KILL = re.compile(
    r"(?is)<(script|style|noscript|svg|iframe|template|form|nav|header|footer|aside)\b[^>]*>.*?</\1>")
_BLOCK = re.compile(r"(?i)</?(p|div|h[1-6]|li|tr|blockquote|section|article|main|figure|figcaption)\b[^>]*>")
_ANYTAG = re.compile(r"<[^>]+>")


def _clean_fragment(frag: str) -> str:
    """Page HTML -> just the words, with paragraph breaks kept."""
    s = _KILL.sub(" ", frag)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = _BLOCK.sub("\n", s)
    s = _ANYTAG.sub("", s)
    s = html.unescape(s)
    s = WS.sub(" ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    s = "\n".join(ln for ln in lines if ln)
    return BLANK.sub("\n\n", s).strip()


def fetch_url(url: str, limit: int = 8 * 1024 * 1024) -> str:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("only http/https links")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(limit)
        cs = r.headers.get_content_charset()
    return raw.decode(cs or "utf-8", "replace")


def parse_web(page: str, url: str) -> dict:
    """A fetched page -> {'title','chapters'}. One page is its own chapter."""
    title = "Untitled"
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    if m:
        t = _clean_fragment(m.group(1))
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

    text = _clean_fragment(frag)
    if len(text) < 120:
        raise ValueError("no readable text on that page (login wall, or it builds itself in JS)")

    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", page)
    ch = _clean_fragment(h1.group(1))[:120] if h1 else ""
    return {"title": title, "chapters": [{"title": ch or title[:120] or "Page", "text": text}]}


def chunk_text(text: str, target: int = CHUNK_TARGET):
    """Split on sentence/paragraph edges so each request stays under the provider cap."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > target and cur:
            parts.append(cur.strip())
            cur = ""
        if len(para) > target:
            sents = re.split(r"(?<=[.!?])\s+", para)
            for s in sents:
                if len(cur) + len(s) + 1 > target and cur:
                    parts.append(cur.strip())
                    cur = ""
                cur += s + " "
        else:
            cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts or [text]


# ------------------------------------------------------------------- providers

def voices_azure(key: str, region: str):
    url = "https://%s.tts.speech.microsoft.com/cognitiveservices/voices/list" % region
    req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    out = []
    for v in data:
        if "Neural" not in v.get("VoiceType", ""):
            continue
        out.append({
            "id": v.get("ShortName", ""),
            "label": "%s %s (%s, %s)" % (
                v.get("LocalName", v.get("ShortName", "")),
                "- %s" % v.get("VoiceTag", {}).get("VoicePersonalities", [""])[0] if v.get("VoiceTag", {}).get("VoicePersonalities") else "",
                v.get("Locale", ""),
                v.get("Gender", ""),
            ).replace(" ()", ""),
            "locale": v.get("Locale", ""),
            "gender": v.get("Gender", ""),
        })
    out.sort(key=lambda x: (x["locale"], x["id"]))
    return out


def synth_azure(text: str, voice: str, key: str, region: str, rate: str = "0%") -> bytes:
    url = "https://%s.tts.speech.microsoft.com/cognitiveservices/v1" % region
    ssml = (
        "<speak version='1.0' xml:lang='en-US'>"
        "<voice name='%s'>"
        "<prosody rate='%s'>%s</prosody>"
        "</voice></speak>" % (voice, rate, html.escape(text))
    )
    req = urllib.request.Request(url, data=ssml.encode("utf-8"), headers={
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "reader-local",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def voices_minimax(key: str, group: str):
    # Best-effort. MiniMax voice ids look like English_Gentle-voiced_man.
    url = "https://api.minimax.chat/v1/get_voice?GroupId=%s" % urllib.parse.quote(group)
    req = urllib.request.Request(url, data=b"{}", headers={
        "Authorization": "Bearer %s" % key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
        vs = d.get("system_voice") or d.get("voice_list") or []
        out = [{"id": v.get("voice_id"), "label": v.get("voice_name") or v.get("voice_id"),
                "locale": "", "gender": ""} for v in vs if v.get("voice_id")]
        if out:
            return out
    except Exception:
        pass
    return [
        {"id": "English_Gentle-voiced_man", "label": "English Gentle (voiced man)", "locale": "en", "gender": "male"},
        {"id": "English_CalmWoman", "label": "English Calm (woman)", "locale": "en", "gender": "female"},
        {"id": "English_Trustworth_Man", "label": "English Trustworthy (man)", "locale": "en", "gender": "male"},
        {"id": "English_radiant_girl", "label": "English Radiant (girl)", "locale": "en", "gender": "female"},
    ]


def synth_minimax(text: str, voice: str, key: str, group: str, model: str = "speech-02-hd",
                  speed: float = 1.0) -> bytes:
    url = "https://api.minimax.chat/v1/t2a_v2?GroupId=%s" % urllib.parse.quote(group)
    body = json.dumps({
        "model": model, "text": text, "stream": False,
        "voice_setting": {"voice_id": voice, "speed": speed, "vol": 1.0, "pitch": 0},
        "audio_setting": {"sample_rate": 32000, "format": "mp3"},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer %s" % key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode())
    audio = (d.get("data") or {}).get("audio")
    if not audio:
        raise ValueError("minimax returned no audio: %s" % str(d)[:200])
    return bytes.fromhex(audio)


def synth_local(text: str, out_path: str) -> str:
    """Offline stand-in: a quiet tone read at speaking length. Proves the pipeline."""
    secs = max(1.0, min(240.0, len(text) / 14.0))
    rate = 16000
    frames = int(secs * rate)
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        buf = bytearray()
        for i in range(frames):
            # slow warble so it is obvious this is a stand-in, not real speech
            v = int(3000 * (0.5 + 0.5 * (i % 800) / 800.0))
            buf += struct.pack("<h", v)
        w.writeframes(bytes(buf))
    return out_path


PROVIDERS = {
    "local": {"ext": ".wav", "mime": "audio/wav"},
    "azure": {"ext": ".mp3", "mime": "audio/mpeg"},
    "minimax": {"ext": ".mp3", "mime": "audio/mpeg"},
}


# --------------------------------------------------------------------- storage

def book_dir(root: str, book_id: str) -> str:
    return os.path.join(root, book_id)


def load_book(root: str, book_id: str) -> dict:
    with open(os.path.join(book_dir(root, book_id), "book.json"), "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "reader/1.0"
    root = "out"

    def log_message(self, *a):
        pass  # quiet

    # -- helpers
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": str(msg)}, code)

    def _body(self, limit=80 * 1024 * 1024):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    # -- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            with open(p, "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if u.path.startswith("/audio/"):
            return self._serve_audio(u.path[len("/audio/"):])
        if u.path == "/api/books":
            books = []
            for bid in os.listdir(self.root):
                bd = os.path.join(self.root, bid)
                j = os.path.join(bd, "book.json")
                if os.path.isfile(j):
                    try:
                        b = json.load(open(j, encoding="utf-8"))
                        aud = os.path.join(bd, "audio")
                        rendered = sorted(os.listdir(aud)) if os.path.isdir(aud) else []
                        books.append({"id": bid, "title": b["title"],
                                      "chapters": len(b["chapters"]), "rendered": rendered})
                    except Exception:
                        pass
            return self._json({"ok": True, "books": books})
        self._err("not found", 404)

    def _serve_audio(self, rel):
        rel = rel.replace("..", "")
        path = os.path.join(self.root, rel)
        if not os.path.isfile(path):
            return self._err("no such audio", 404)
        size = os.path.getsize(path)
        mime = "audio/mpeg" if path.endswith(".mp3") else "audio/wav"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                code = 206
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        self.wfile.write(data)

    # -- POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/ingest":
                return self._ingest(q)
            if u.path == "/api/ingest-url":
                return self._ingest_url()
            if u.path == "/api/voices":
                return self._voices()
            if u.path == "/api/render":
                return self._render()
        except Exception as e:
            return self._err("%s: %s" % (type(e).__name__, e), 500)
        self._err("not found", 404)

    def _ingest(self, q):
        data = self._body()
        if not data:
            return self._err("empty upload")
        name = (q.get("name", ["book.epub"])[0])
        book_id = hashlib.sha1(data).hexdigest()[:12]
        d = book_dir(self.root, book_id)
        os.makedirs(os.path.join(d, "audio"), exist_ok=True)
        parsed = parse_epub(data, name)
        with open(os.path.join(d, "book.json"), "w", encoding="utf-8") as f:
            json.dump(parsed, f, ensure_ascii=False)
        return self._json({"ok": True, "id": book_id, "title": parsed["title"],
                           "chapters": [{"i": i, "title": c["title"], "chars": len(c["text"])}
                                        for i, c in enumerate(parsed["chapters"])]})

    def _ingest_url(self):
        body = json.loads(self._body() or b"{}")
        url = (body.get("url") or "").strip()
        if not url:
            return self._err("no url")
        page = fetch_url(url)
        parsed = parse_web(page, url)
        book_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        d = book_dir(self.root, book_id)
        os.makedirs(os.path.join(d, "audio"), exist_ok=True)
        with open(os.path.join(d, "book.json"), "w", encoding="utf-8") as f:
            json.dump(parsed, f, ensure_ascii=False)
        return self._json({"ok": True, "id": book_id, "title": parsed["title"],
                           "chapters": [{"i": i, "title": c["title"], "chars": len(c["text"])}
                                        for i, c in enumerate(parsed["chapters"])]})

    def _voices(self):
        body = json.loads(self._body() or b"{}")
        provider = body.get("provider", "local")
        key = body.get("key", "")
        if provider == "local":
            return self._json({"ok": True, "voices": [
                {"id": "tone", "label": "Offline tone (no speech)", "locale": "", "gender": ""}]})
        if not key:
            return self._err("need a key for %s" % provider)
        if provider == "azure":
            vs = voices_azure(key, body.get("region", "eastus"))
        elif provider == "minimax":
            vs = voices_minimax(key, body.get("group", ""))
        else:
            return self._err("unknown provider")
        return self._json({"ok": True, "voices": vs})

    def _render(self):
        body = json.loads(self._body() or b"{}")
        book_id = body["bookId"]
        idx = int(body["chapter"])
        provider = body.get("provider", "local")
        key = body.get("key", "")
        voice = body.get("voice", "")
        book = load_book(self.root, book_id)
        if idx < 0 or idx >= len(book["chapters"]):
            return self._err("chapter out of range")
        ch = book["chapters"][idx]
        ext = PROVIDERS[provider]["ext"]
        out = os.path.join(book_dir(self.root, book_id), "audio", "ch%03d%s" % (idx, ext))
        if body.get("force") and os.path.exists(out):
            os.remove(out)
        if os.path.exists(out):
            return self._json({"ok": True, "cached": True, "url": "/audio/%s/audio/%s" % (book_id, os.path.basename(out)),
                               "bytes": os.path.getsize(out)})
        if provider == "local":
            synth_local(ch["text"], out)
        else:
            parts = chunk_text(ch["text"])
            blob = b""
            for p in parts:
                if provider == "azure":
                    blob += synth_azure(p, voice, key, body.get("region", "eastus"), body.get("rate", "0%"))
                elif provider == "minimax":
                    blob += synth_minimax(p, voice, key, body.get("group", ""),
                                          body.get("model", "speech-02-hd"), float(body.get("speed", 1.0)))
                else:
                    return self._err("unknown provider")
            with open(out, "wb") as f:
                f.write(blob)
        return self._json({"ok": True, "cached": False, "url": "/audio/%s/audio/%s" % (book_id, os.path.basename(out)),
                           "bytes": os.path.getsize(out)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 for this machine only; 0.0.0.0 to share on the LAN")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    ap.add_argument("--open", action="store_true", help="open a browser window on start")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    Handler.root = a.out
    url = "http://%s:%d" % ("127.0.0.1" if a.host == "0.0.0.0" else a.host, a.port)
    if a.open:
        import threading
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("reader: %s  (out: %s)" % (url, a.out))
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

