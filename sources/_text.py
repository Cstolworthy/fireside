"""Text helpers shared by the source parsers. No network, no state, no imports
from the rest of the app, so a parser can be tested on a plain string."""

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


def text_of(xhtml: str) -> str:
    """EPUB xhtml -> readable text. Tags out, block tags become newlines."""
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", xhtml)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</?(p|div|h[1-6]|li|tr|blockquote|section)[^>]*>", "\n", s)
    s = TAG.sub("", s)
    s = html.unescape(s)
    s = WS.sub(" ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    s = "\n".join(ln for ln in lines if ln and not _CHROME.match(ln))
    return BLANK.sub("\n\n", s).strip()


def clean_fragment(frag: str) -> str:
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
