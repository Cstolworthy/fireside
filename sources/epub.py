"""EPUB source. Matches any upload; an EPUB is a zip, read with the stdlib."""

import io
import os
import re
import zipfile

from ._text import text_of


def _first_heading(xhtml: str, fallback: str) -> str:
    m = re.search(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>", xhtml)
    if m:
        t = text_of(m.group(1))
        if t:
            return t[:120]
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", xhtml)
    if m:
        t = text_of(m.group(1))
        if t:
            return t[:120]
    return fallback


def parse_epub(data: bytes, name: str) -> dict:
    """bytes of an .epub -> {'title':..., 'chapters':[{'title','text'}...]}"""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()

    opf_path = None
    if "META-INF/container.xml" in names:
        m = re.search(r'full-path="([^"]+)"',
                      z.read("META-INF/container.xml").decode("utf-8", "replace"))
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
        title = text_of(m.group(1)) or title

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
        txt = text_of(raw)
        if len(txt) < 2:
            continue
        chapters.append({"title": _first_heading(raw, "Chapter %d" % (len(chapters) + 1)),
                         "text": txt})

    if not chapters:
        raise ValueError("no readable chapters found")
    return {"title": title, "chapters": chapters}


class Epub:
    id = "epub"
    label = "EPUB file"

    def matches(self, req) -> bool:
        return req.get("kind") == "upload"

    def read(self, req, fetch, cred=None) -> dict:
        book = parse_epub(req["data"], req.get("filename") or "book.epub")
        book["meta"] = {"source": self.id}
        return book

    def settings(self):
        return []

    def credential_hosts(self):
        return []
