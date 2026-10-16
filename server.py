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
import threading
import urllib.parse
import urllib.request
import wave
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

READER_VERSION = "1.6"  # shown in the page header; single source

# ---------------------------------------------------------------- epub parsing

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"[ \t\r\f\v]+")
BLANK = re.compile(r"\n{3,}")
CHUNK_TARGET = 3500  # characters per TTS request

# page chrome that survives tag-stripping and would otherwise be read aloud
_CHROME = re.compile(r"(?i)^(skip to (main )?(content|footer)|jump to content|toggle navigation|menu|search|search for:)$")


def _text_of(xhtml: str) -> str:
    """Tags out, readable text back. Block tags become newlines."""
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", xhtml)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</?(p|div|h[1-6]|li|tr|blockquote|section)[^>]*>", "\n", s)
    s = TAG.sub("", s)
    s = html.unescape(s)
    s = WS.sub(" ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    s = "\n".join(ln for ln in lines if ln and not _CHROME.match(ln))
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
    s = "\n".join(ln for ln in lines if ln and not _CHROME.match(ln))
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
        locale = v.get("Locale", "")
        gender = v.get("Gender", "")
        name = _voice_name(v.get("DisplayName") or v.get("ShortName", ""))
        # deliberately no personality in the label: we do not apply a style, so
        # naming one would claim something the audio does not do
        label = "%s (%s, %s)" % (name, locale, gender)
        out.append({
            "id": v.get("ShortName", ""),
            "label": label,
            "locale": locale,
            "gender": gender,
        })
    # English first (that is what gets read here), then everything else; both alphabetical
    out.sort(key=lambda x: (0 if x["locale"].startswith("en") else 1, x["locale"], x["id"]))
    return out


def _voice_name(dn: str) -> str:
    """'Microsoft Jenny Online (Natural) - English (United States)' -> 'Jenny'.

    Azure's DisplayName is a mouthful and its ShortName is a slug; the voice list
    is unusable if you have to read either one 900 times.
    """
    s = (dn or "").strip()
    s = re.sub(r"^Microsoft\s+", "", s, flags=re.I)
    s = s.split(" - ")[0]
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    s = re.sub(r"\s+Online$", "", s, flags=re.I).strip()
    if not s or s == dn:
        # DisplayName was missing and we got a ShortName like en-US-JennyMultilingualNeural
        m = re.match(r"^[a-z]{2}-[A-Za-z]{2,}(-(.+))?$", s or dn or "")
        if m and m.group(2):
            s = re.sub(r"Neural$", "", m.group(2)) or s
    return s or dn


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


def voice_sig(provider: str, voice: str, model: str = "", extra: str = "") -> str:
    """A short folder name for one voice, so audio is cached per voice.

    Without this, changing the voice and pressing play replays whatever voice the
    first render used. The cache has to be keyed by what actually made the sound.
    `extra` carries the pronunciation tag, so a fixed word makes a new render too.
    """
    raw = "%s|%s|%s|%s" % (provider, voice or "none", model or "", extra or "")
    return "%s-%s" % (provider, hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8])


# -------------------------------------------------------------- pronunciations

def pron_path(root: str, book_id: str) -> str:
    return os.path.join(book_dir(root, book_id), "pron.json")


def load_pron(root: str, book_id: str) -> list:
    """This book's say-it-differently list: [{'from': word, 'to': respelling}].

    Stored beside the book rather than inside book.json, so loading the same file
    again does not throw the list away.
    """
    try:
        with open(pron_path(root, book_id), "r", encoding="utf-8") as f:
            rules = json.load(f)
    except Exception:
        return []
    if not isinstance(rules, list):
        return []
    out = []
    for r in rules:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if frm and to:
            out.append({"from": frm, "to": to})
    return out


def save_pron(root: str, book_id: str, rules) -> list:
    """Tidy and store the list: drop blanks, drop duplicate words, keep order."""
    clean, seen = [], set()
    for r in rules or []:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if frm and to and frm.lower() not in seen:
            seen.add(frm.lower())
            clean.append({"from": frm, "to": to})
    os.makedirs(book_dir(root, book_id), exist_ok=True)
    with open(pron_path(root, book_id), "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=1)
    return clean


def pron_token(rules) -> str:
    """Short tag folded into the audio cache key. Empty when there is no list, so a
    book that never had one keeps whatever it already rendered."""
    if not rules:
        return ""
    raw = "|".join("%s>%s" % (r["from"].lower(), r["to"])
                   for r in sorted(rules, key=lambda r: r["from"].lower()))
    return "p" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


def _pron_regex(rules):
    """One alternation, longest word first, whole-word where the word has word edges."""
    alts = []
    for r in sorted(rules or [], key=lambda r: -len(r["from"])):
        frm = r["from"]
        e = re.escape(frm)
        if re.match(r"\w", frm[0]):
            e = r"(?<!\w)" + e
        if re.match(r"\w", frm[-1]):
            e = e + r"(?!\w)"
        alts.append(e)
    return re.compile("|".join(alts), re.I) if alts else None


def apply_pron(text: str, rules) -> str:
    """Swap whole words for their say-as respelling, case-insensitive. The
    replacement goes in literally, never read back as a regex."""
    pat = _pron_regex(rules)
    if not text or pat is None:
        return text
    repl = {r["from"].lower(): r["to"] for r in rules}
    return pat.sub(lambda m: repl.get(m.group(0).lower(), m.group(0)), text)


def chapter_uses_pron(text: str, rules) -> bool:
    pat = _pron_regex(rules)
    return bool(pat and text and pat.search(text))


TESTER_MAX_FILES = 240


def _tester_dir(root: str) -> str:
    d = os.path.join(root, "_tester")
    os.makedirs(d, exist_ok=True)
    return d


def _prune_tester(d: str) -> None:
    """Keep the voice-tester pool bounded; drop the oldest once it gets big."""
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if not f.endswith(".part")]
        if len(files) <= TESTER_MAX_FILES:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[: len(files) - TESTER_MAX_FILES]:
            os.remove(p)
    except OSError:
        pass


# --------------------------------------------------------------------- storage

# render progress, so a long chapter shows movement instead of a hung button
PROGRESS = {}
PROGRESS_LOCK = threading.Lock()


def _prog_set(book_id, idx, **kw):
    with PROGRESS_LOCK:
        PROGRESS.setdefault((book_id, idx), {}).update(kw)


def _prog_get(book_id, idx):
    with PROGRESS_LOCK:
        return dict(PROGRESS.get((book_id, idx), {}))


def book_dir(root: str, book_id: str) -> str:
    return os.path.join(root, book_id)


def load_book(root: str, book_id: str) -> dict:
    with open(os.path.join(book_dir(root, book_id), "book.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def chapter_path(root: str, book_id: str, book: dict, provider: str, voice: str,
                 model: str, idx: int) -> str:
    """Where one chapter's audio for one voice lives. Same rule the player uses.

    The pronunciation tag rides in the signature only for chapters that actually
    use a listed word, so fixing one name re-renders those chapters and leaves the
    rest of a long book exactly where it was.
    """
    rules = load_pron(root, book_id)
    ch = book["chapters"][idx]
    extra = pron_token(rules) if (rules and chapter_uses_pron(ch["text"], rules)) else ""
    sig = voice_sig(provider, voice, model, extra)
    return os.path.join(book_dir(root, book_id), "audio", sig, "ch%03d%s" % (idx, PROVIDERS[provider]["ext"]))


def render_chapter_file(root: str, book_id: str, book: dict, idx: int, provider: str, body: dict) -> str:
    """Synthesize one chapter to its cached file, or return it if it is already there.

    One code path so the single-chapter Play button and Render-all cannot drift
    apart: both end up with the same bytes in the same file.
    """
    ch = book["chapters"][idx]
    out = chapter_path(root, book_id, book, provider, body.get("voice", ""), body.get("model", ""), idx)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if body.get("force") and os.path.exists(out):
        os.remove(out)
    if os.path.exists(out):
        return out
    text = apply_pron(ch["text"], load_pron(root, book_id))
    if provider == "local":
        synth_local(text, out)
        return out
    parts = chunk_text(text)
    tmp = out + ".part"
    _prog_set(book_id, idx, done=0, total=len(parts), error=None)
    try:
        with open(tmp, "wb") as f:
            for n, p in enumerate(parts, 1):
                if provider == "azure":
                    piece = synth_azure(p, body.get("voice", ""), body.get("key", ""),
                                        body.get("region", "eastus"), body.get("rate", "0%"))
                elif provider == "minimax":
                    piece = synth_minimax(p, body.get("voice", ""), body.get("key", ""), body.get("group", ""),
                                          body.get("model") or "speech-02-hd", float(body.get("speed", 1.0) or 1.0))
                else:
                    raise ValueError("unknown provider")
                f.write(piece)
                f.flush()
                _prog_set(book_id, idx, done=n)
        os.replace(tmp, out)
    except Exception as e:
        _prog_set(book_id, idx, error="%s: %s" % (type(e).__name__, e))
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return out


# Whole-book renders run in the background and report on chapter index -1, so a
# long book does not hold one HTTP request open for ten minutes. One at a time
# per book; a second click joins the run instead of starting a rival one.
RENDER_RUNNING = set()
RENDER_GUARD = threading.Lock()


def _render_all_worker(root: str, book_id: str, provider: str, body: dict) -> None:
    try:
        book = load_book(root, book_id)
        n = len(book["chapters"])
        _prog_set(book_id, -1, done=0, total=n, current=0, error=None, running=True)
        for i in range(n):
            _prog_set(book_id, -1, current=i, error=None)
            render_chapter_file(root, book_id, book, i, provider, body)
            _prog_set(book_id, -1, done=i + 1)
        _prog_set(book_id, -1, current=None, running=False, done=n)
    except Exception as e:
        _prog_set(book_id, -1, running=False, error="%s: %s" % (type(e).__name__, e))
    finally:
        with RENDER_GUARD:
            RENDER_RUNNING.discard(book_id)


def _title_slug(title: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._ -]+", "", title or "book").strip()
    s = re.sub(r"\s+", "_", s)
    return s[:60] or "book"


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
            b = b.replace(b"__READER_VERSION__", READER_VERSION.encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if u.path.startswith("/audio/"):
            return self._serve_audio(u.path[len("/audio/"):])
        if u.path == "/api/progress":
            q = urllib.parse.parse_qs(u.query)
            return self._json({"ok": True, **_prog_get(q.get("book", [""])[0], int(q.get("chapter", ["0"])[0]))})
        if u.path == "/api/rendered":
            q = urllib.parse.parse_qs(u.query)
            return self._rendered(q)
        if u.path == "/api/save":
            q = urllib.parse.parse_qs(u.query)
            return self._save(q)
        if u.path == "/api/pron":
            q = urllib.parse.parse_qs(u.query)
            return self._pron_get(q)
        if u.path == "/api/book":
            q = urllib.parse.parse_qs(u.query)
            bid = (q.get("id", [""])[0])
            try:
                b = load_book(self.root, bid)
            except Exception:
                return self._err("no such book", 404)
            return self._json({"ok": True, "id": bid, "title": b["title"],
                               "chapters": [{"i": i, "title": c["title"], "chars": len(c["text"]),
                                             "preview": c["text"][:700]}
                                            for i, c in enumerate(b["chapters"])]})
        if u.path == "/api/books":
            books = []
            for bid in os.listdir(self.root):
                bd = os.path.join(self.root, bid)
                j = os.path.join(bd, "book.json")
                if os.path.isfile(j):
                    try:
                        b = json.load(open(j, encoding="utf-8"))
                        aud = os.path.join(bd, "audio")
                        rendered = []
                        if os.path.isdir(aud):
                            for dp, _, fns in os.walk(aud):
                                for fn in sorted(fns):
                                    rendered.append(os.path.relpath(os.path.join(dp, fn), aud))
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
            if u.path == "/api/render-all":
                return self._render_all()
            if u.path == "/api/say":
                return self._say()
            if u.path == "/api/pron":
                return self._pron_set()
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
                           "chapters": [{"i": i, "title": c["title"], "chars": len(c["text"]),
                                         "preview": c["text"][:700]}
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
                           "chapters": [{"i": i, "title": c["title"], "chars": len(c["text"]),
                                         "preview": c["text"][:700]}
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

    def _say(self):
        """Synthesize arbitrary text in one voice. The voice-tester page, and a
        fast way to hear a voice before committing a book to it."""
        body = json.loads(self._body() or b"{}")
        text = (body.get("text") or "").strip()
        if not text:
            return self._err("no text")
        rules = body.get("rules") or []
        text = apply_pron(text, rules)
        provider = body.get("provider", "local")
        if provider not in PROVIDERS:
            return self._err("unknown provider")
        voice = body.get("voice", "")
        key = body.get("key", "")
        speed = float(body.get("speed", 1.0) or 1.0)
        job = body.get("job") or ""
        raw = "%s|%s|%s|%s|%s" % (provider, voice, speed, pron_token(rules), text)
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        d = _tester_dir(self.root)
        ext = PROVIDERS[provider]["ext"]
        out = os.path.join(d, h + ext)
        url = "/audio/_tester/%s%s" % (h, ext)
        if os.path.exists(out) and not body.get("force"):
            return self._json({"ok": True, "cached": True, "url": url, "bytes": os.path.getsize(out)})
        if provider == "local":
            synth_local(text, out)
            _prune_tester(d)
            return self._json({"ok": True, "cached": False, "url": url, "bytes": os.path.getsize(out)})
        if not key:
            return self._err("need a key for %s" % provider)
        parts = chunk_text(text)
        tmp = out + ".part"
        if job:
            _prog_set(job, 0, done=0, total=len(parts), error=None)
        try:
            with open(tmp, "wb") as f:
                for n, p in enumerate(parts, 1):
                    if provider == "azure":
                        piece = synth_azure(p, voice, key, body.get("region", "eastus"), body.get("rate", "0%"))
                    else:
                        piece = synth_minimax(p, voice, key, body.get("group", ""),
                                              body.get("model", "speech-02-hd"), speed)
                    f.write(piece)
                    f.flush()
                    if job:
                        _prog_set(job, 0, done=n)
            os.replace(tmp, out)
        except Exception as e:
            if job:
                _prog_set(job, 0, error="%s: %s" % (type(e).__name__, e))
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        _prune_tester(d)
        return self._json({"ok": True, "cached": False, "url": url, "bytes": os.path.getsize(out)})

    def _render(self):
        body = json.loads(self._body() or b"{}")
        book_id = body["bookId"]
        idx = int(body["chapter"])
        provider = body.get("provider", "local")
        if provider not in PROVIDERS:
            return self._err("unknown provider")
        book = load_book(self.root, book_id)
        if idx < 0 or idx >= len(book["chapters"]):
            return self._err("chapter out of range")
        out = chapter_path(self.root, book_id, book, provider, body.get("voice", ""),
                           body.get("model", ""), idx)
        rel = os.path.relpath(out, self.root).replace(os.sep, "/")
        cached = os.path.exists(out)
        render_chapter_file(self.root, book_id, book, idx, provider, body)
        return self._json({"ok": True, "cached": cached, "url": "/audio/" + rel,
                           "voice": body.get("voice", ""), "bytes": os.path.getsize(out)})

    def _render_all(self):
        """Kick off a whole-book render in the background; poll /api/progress?chapter=-1."""
        body = json.loads(self._body() or b"{}")
        book_id = body["bookId"]
        provider = body.get("provider", "local")
        if provider not in PROVIDERS:
            return self._err("unknown provider")
        if provider != "local" and not body.get("key"):
            return self._err("need a key for %s" % provider)
        book = load_book(self.root, book_id)
        with RENDER_GUARD:
            if book_id in RENDER_RUNNING:
                return self._json({"ok": True, "already": True, "total": len(book["chapters"])})
            RENDER_RUNNING.add(book_id)
        _prog_set(book_id, -1, done=0, total=len(book["chapters"]), current=0, error=None, running=True)
        threading.Thread(target=_render_all_worker, args=(self.root, book_id, provider, body),
                         daemon=True).start()
        return self._json({"ok": True, "total": len(book["chapters"])})

    def _rendered(self, q):
        """How much of this book already exists in the chosen voice."""
        bid = q.get("id", [""])[0]
        provider = q.get("provider", ["local"])[0]
        if provider not in PROVIDERS:
            return self._err("unknown provider")
        try:
            book = load_book(self.root, bid)
        except Exception:
            return self._err("no such book", 404)
        present = [i for i in range(len(book["chapters"]))
                   if os.path.isfile(chapter_path(self.root, bid, book, provider,
                                                  q.get("voice", [""])[0], q.get("model", [""])[0], i))]
        return self._json({"ok": True, "total": len(book["chapters"]), "done": len(present),
                           "have": present, "pron": len(load_pron(self.root, bid)),
                           "sig": voice_sig(provider, q.get("voice", [""])[0], q.get("model", [""])[0])})

    def _pron_get(self, q):
        """The saved say-it-differently list, plus which chapters it touches."""
        bid = q.get("id", [""])[0]
        try:
            book = load_book(self.root, bid)
        except Exception:
            return self._err("no such book", 404)
        rules = load_pron(self.root, bid)
        hit = [i for i, c in enumerate(book["chapters"]) if chapter_uses_pron(c["text"], rules)]
        return self._json({"ok": True, "book": bid, "rules": rules, "chapters": hit})

    def _pron_set(self):
        """Save the list and report which chapters use it, so the caller can
        re-render just those."""
        body = json.loads(self._body() or b"{}")
        bid = body.get("bookId", "")
        try:
            book = load_book(self.root, bid)
        except Exception:
            return self._err("no such book", 404)
        rules = save_pron(self.root, bid, body.get("rules"))
        hit = [i for i, c in enumerate(book["chapters"]) if chapter_uses_pron(c["text"], rules)]
        return self._json({"ok": True, "rules": rules, "chapters": hit,
                           "titles": [book["chapters"][i]["title"] for i in hit],
                           "token": pron_token(rules)})

    def _save(self, q):
        """Hand back what has been rendered: as=mp3 stitches the chapters into one
        file, as=zip gives the per-chapter files. Built once, then cached."""
        bid = q.get("id", [""])[0]
        provider = q.get("provider", ["local"])[0]
        if provider not in PROVIDERS:
            return self._err("unknown provider")
        try:
            book = load_book(self.root, bid)
        except Exception:
            return self._err("no such book", 404)
        voice, model = q.get("voice", [""])[0], q.get("model", [""])[0]
        ext = PROVIDERS[provider]["ext"]
        present = [(i, chapter_path(self.root, bid, book, provider, voice, model, i))
                   for i in range(len(book["chapters"]))
                   if os.path.isfile(chapter_path(self.root, bid, book, provider, voice, model, i))]
        if not present:
            return self._err("nothing rendered in this voice yet", 404)
        want = (q.get("as", ["mp3"])[0] or "mp3").lower()
        sigdir = os.path.join(book_dir(self.root, bid), "audio", voice_sig(provider, voice, model))
        title = _title_slug(book["title"])
        if want == "zip":
            out = os.path.join(sigdir, "_book.zip")
            stale = (not os.path.isfile(out)) or any(os.path.getmtime(p) > os.path.getmtime(out)
                                                     for _, p in present)
            if stale:
                tmp = out + ".part"
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                    for i, p in present:
                        z.write(p, "%s/ch%03d%s" % (title, i + 1, ext))
                os.replace(tmp, out)
            return self._send_download(out, "application/zip", title + ".zip")
        out = os.path.join(sigdir, "_book" + ext)
        stale = (not os.path.isfile(out)) or any(os.path.getmtime(p) > os.path.getmtime(out)
                                                 for _, p in present)
        if stale:
            tmp = out + ".part"
            with open(tmp, "wb") as w:
                if ext == ".wav":
                    # a wav carries its length in the header, so a plain concat would
                    # stop players at the end of chapter one. keep one header, fix the
                    # two size fields, then append every file's samples.
                    with open(present[0][1], "rb") as f:
                        head = bytearray(f.read(44))
                    total = sum(os.path.getsize(p) for _, p in present) - 44 * len(present)
                    struct.pack_into("<I", head, 4, 36 + total)
                    struct.pack_into("<I", head, 40, total)
                    w.write(bytes(head))
                    for _, p in present:
                        with open(p, "rb") as f:
                            f.seek(44)
                            while True:
                                b = f.read(1 << 20)
                                if not b:
                                    break
                                w.write(b)
                else:
                    for _, p in present:
                        with open(p, "rb") as f:
                            while True:
                                b = f.read(1 << 20)
                                if not b:
                                    break
                                w.write(b)
            os.replace(tmp, out)
        return self._send_download(out, PROVIDERS[provider]["mime"], title + ext)

    def _send_download(self, path, mime, name):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name.replace('"', ""))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                self.wfile.write(b)


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


