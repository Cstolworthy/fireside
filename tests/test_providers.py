#!/usr/bin/env python3
"""Self-contained check of the speech-provider registry and the HTTP flow.

No network, no fixture: it builds a two-chapter EPUB in a temp dir and runs the
real server on a spare port, so it proves the refactor did not change behavior.
Run: python3 tests/test_providers.py
"""
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

FAILS = []


def ck(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: %s" % (extra,)) if extra else ""))
    if not cond:
        FAILS.append(name)


def make_epub() -> bytes:
    """Two chapters, enough structure for parse_epub to walk it."""
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
    import providers
    from providers import REGISTRY, get_provider, Audio

    ck("registry has local/azure/minimax", set(REGISTRY) == {"local", "azure", "minimax"}, set(REGISTRY))
    ck("unknown provider -> None", get_provider("bogus") is None)
    ck("augmentable", hasattr(providers, "REGISTRY") and "minimax" in REGISTRY)
    ck("local needs no key", REGISTRY["local"].needs_key is False)
    ck("azure needs a key", REGISTRY["azure"].needs_key is True)
    ck("minimax needs a key", REGISTRY["minimax"].needs_key is True)
    ck("azure price 15/1M", REGISTRY["azure"].cost_per_1m_chars({}) == 15.0)
    ck("minimax unpriced (None, not 0)", REGISTRY["minimax"].cost_per_1m_chars({}) is None)
    ck("local priced 0", REGISTRY["local"].cost_per_1m_chars({}) == 0.0)
    v = REGISTRY["local"].list_voices({})
    ck("local voice list", len(v) == 1 and v[0].id == "tone" and v[0].dict()["id"] == "tone")
    ck("local chunks=1", REGISTRY["local"].chunks("hello world") == 1)
    ck("azure chunks>1", REGISTRY["azure"].chunks("hello there friend. " * 500) > 1)
    a = REGISTRY["local"].synth("hello there", "tone", {}, {})
    ck("local synth -> Audio wav, cost 0",
       isinstance(a, Audio) and a.ext == ".wav" and a.mime == "audio/wav"
       and a.cost_usd == 0.0 and a.bytes[:4] == b"RIFF")


def live_tests():
    tmp = tempfile.mkdtemp(prefix="reader_prov_")
    out = os.path.join(tmp, "out")
    os.makedirs(out)
    port = 8891
    base = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen([sys.executable, "server.py", "--port", str(port), "--out", out],
                            cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/api/books", timeout=2)
                break
            except Exception:
                time.sleep(0.2)

        st, b = req(base, "/", method="GET")
        ck("index served, version substituted", st == 200 and b"__READER_VERSION__" not in b)

        st, b = req(base, "/api/voices", {"provider": "local"}, "POST")
        j = json.loads(b)
        ck("voices local", st == 200 and j["voices"][0]["id"] == "tone", j)
        st, _ = req(base, "/api/voices", {"provider": "azure"}, "POST")
        ck("azure voices want a key", st == 400)
        st, _ = req(base, "/api/voices", {"provider": "bogus"}, "POST")
        ck("unknown provider 400", st == 400)

        st, b = req(base, "/api/ingest?name=test.epub", raw=make_epub(),
                    ctype="application/epub+zip", method="POST")
        j = json.loads(b)
        ck("ingest", st == 200 and j.get("ok") and len(j["chapters"]) == 2, b[:120])
        bid = j["id"]

        st, b = req(base, "/api/render", {"bookId": bid, "chapter": 0, "provider": "local",
                                          "voice": "tone"}, "POST")
        j = json.loads(b)
        aud = os.path.join(out, j["url"][len("/audio/"):])
        ck("render local", st == 200 and j.get("ok") and os.path.isfile(aud), j)
        ck("rendered file is wav", open(aud, "rb").read(4) == b"RIFF")

        st, b = req(base, "/api/progress?book=%s&chapter=0" % bid)
        ck("progress reports done", json.loads(b).get("done") == 1, b[:120])

        st, b = req(base, "/api/rendered?id=%s&provider=local&voice=tone" % bid)
        ck("rendered 1 of 2", json.loads(b)["done"] == 1, b[:120])

        st, b = req(base, "/api/say", {"text": "hello there friend", "provider": "local"}, "POST")
        j = json.loads(b)
        ck("say local reports cost 0", st == 200 and j.get("cost_usd") == 0.0, j)

        st, b = req(base, "/api/pron", {"bookId": bid, "rules": [{"from": "Fire", "to": "fyer"}]}, "POST")
        ck("pron set", st == 200 and json.loads(b).get("ok"), b[:120])
        st, b = req(base, "/api/render-all", {"bookId": bid, "provider": "local", "voice": "tone"}, "POST")
        ck("render-all kicks", st == 200 and json.loads(b)["total"] == 2, b[:120])
        for _ in range(200):
            st, b = req(base, "/api/progress?book=%s&chapter=-1" % bid)
            jp = json.loads(b)
            if not jp.get("running"):
                break
            time.sleep(0.25)
        ck("render-all finishes", jp.get("done") == 2 and not jp.get("running"), jp)

        st, b = req(base, "/api/save?id=%s&provider=local&voice=tone&as=zip" % bid, method="GET")
        ck("save zip", st == 200 and b[:2] == b"PK", (st, b[:8]))
        st, b = req(base, "/api/save?id=%s&provider=local&voice=tone&as=wav" % bid, method="GET")
        ok = st == 200 and b[:4] == b"RIFF" and int.from_bytes(b[4:8], "little") == 36 + int.from_bytes(b[40:44], "little")
        ck("save one wav, header patched", ok, (st, b[:12]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unit_tests()
    live_tests()
    print("FAILED:", FAILS if FAILS else "none")
    sys.exit(1 if FAILS else 0)
