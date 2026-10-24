"""Text helpers shared by the parsers. No network, no state, no imports from
the rest of the app, so a parser can be tested on a plain string.

Two layers live here: element -> text (a parser's own markup), and page -> the
one container worth reading (any site whose story sits in an article body). The
second is generic HTML reading, not knowledge of a site, so readers share it
instead of each growing a copy."""

import html
import re

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"[ \t\r\f\v]+")
BLANK = re.compile(r"\n{3,}")
# page chrome that survives tag-stripping and would otherwise be read aloud
_CHROME = re.compile(
    r"(?i)^(skip to (main )?(content|footer)|jump to content|toggle navigation|menu|search|search for:)$")

# chrome that never belongs in the reading text (web pages)
_KILL = re.compile(
    r"(?is)<(script|style|noscript|svg|iframe|template|form|nav|header|footer|aside)\b[^>]*>.*?</\1>")
_BLOCK = re.compile(r"(?i)</?(p|div|h[1-6]|li|tr|blockquote|section|article|main|figure|figcaption)\b[^>]*>")
_ANYTAG = re.compile(r"<[^>]+>")


def _readable_text(s: str) -> str:
    """Normalize whitespace without discarding paragraph separators."""
    s = html.unescape(s)
    s = WS.sub(" ", s)
    lines = [line.strip() for line in s.split("\n")]
    # The empty lines distinguish separate paragraphs from soft line breaks.
    # Dropping them collapses EPUB paragraph boundaries before TTS receives text.
    return BLANK.sub("\n\n", "\n".join(
        line for line in lines if not _CHROME.match(line)
    )).strip()


def text_of(xhtml: str) -> str:
    """EPUB XHTML -> readable text, retaining paragraph and line breaks."""
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", xhtml)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    # Block tags separate paragraphs, unlike <br> which is a single line break.
    s = re.sub(r"(?i)</?(p|div|h[1-6]|li|tr|blockquote|section)[^>]*>", "\n\n", s)
    return _readable_text(TAG.sub("", s))


def clean_fragment(frag: str) -> str:
    """Page HTML -> readable text, retaining paragraph and line breaks."""
    s = _KILL.sub(" ", frag)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = _BLOCK.sub("\n\n", s)
    return _readable_text(_ANYTAG.sub("", s))


# ---- page -> the container worth reading ---------------------------------------

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


def strip_furniture(page: str) -> str:
    """Drop comment threads and related-post widgets, whole."""
    for pattern in _THROW_OUT:
        page = _drop(page, pattern)
    return page


def best_fragment(page: str) -> str:
    """The candidate container holding the most text, as readable text.

    Longest wins, so a page with six tiny <article> widgets and one real body
    still reads right. Returns "" when nothing plausible is there."""
    page = strip_furniture(page)
    best = ""
    for pattern in _CANDIDATES:
        for m in re.finditer(pattern, page):
            text = clean_fragment(_element(page, m.start(), m.group("tag")))
            if len(text) > len(best):
                best = text
        if len(best) >= 120:
            return best
    return best


def page_title(page: str) -> str:
    """<title> as readable text, or ""."""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    return clean_fragment(m.group(1))[:160] if m else ""


def first_heading(page: str) -> str:
    """The first <h1> as readable text, or ""."""
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", page)
    return clean_fragment(m.group(1))[:200] if m else ""
