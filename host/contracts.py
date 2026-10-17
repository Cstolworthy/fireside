"""The three v2 plugin contracts. See docs/V2-ARCHITECTURE.md.

Shapes only. The host and every plugin agree on these, and neither side imports
the other's implementation: dataclasses for the data crossing the boundary, and
a base class per kind that documents the methods the host will call. A plugin is
not required to subclass these - the host checks for the methods it needs - but
subclassing is the shortest way to say "I am this contract".
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---- data crossing the boundary ------------------------------------------------

@dataclass
class Field:
    """One declarative settings widget. The host renders it; a plugin ships no HTML."""
    name: str
    label: str
    kind: str = "text"                 # text | password | number | bool | choice
    default: Any = None
    choices: Optional[List[str]] = None
    help: str = ""


@dataclass
class Chapter:
    title: str
    text: str


@dataclass
class Book:
    title: str
    chapters: List[Chapter] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None           # plugin-owned stable id, if it has one


@dataclass
class Voice:
    id: str
    label: str = ""
    language: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Audio:
    data: bytes
    ext: str = "mp3"
    mime: str = "audio/mpeg"


@dataclass
class Consent:
    ok: bool = True
    reason: str = ""


@dataclass
class Quote:
    """What the plugin says before a run. The host never invents a price."""
    chars_est: int = 0
    price: float = 0.0
    currency: str = "USD"
    eligible: bool = True
    balance: Optional[float] = None
    refusal_reason: str = ""
    authoritative: bool = True


@dataclass
class Report:
    """What the plugin says after a run. `unknown` stays unknown, never zero."""
    charge: str = "unknown"            # charged | unknown | pending
    chars_actual: Optional[int] = None
    cost: Optional[float] = None
    currency: str = ""
    duration_s: Optional[float] = None
    external_balance: Optional[float] = None
    reconcilable: bool = False


@dataclass
class StorageInfo:
    kind: str = "local"
    durable: bool = True
    url_ttl: Optional[int] = None


# ---- one base class per kind ---------------------------------------------------

class Reader:
    """Matches a request first, then reads it. Ordered: lower `order` runs first,
    and the generic HTTP reader declares a high order so it runs last."""
    id = ""
    order = 100

    def matches(self, req: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def read(self, req, fetch, cred) -> Book:
        raise NotImplementedError

    def settings(self) -> List[Field]:
        return []

    def credential_hosts(self) -> List[str]:
        return []

    def transport(self) -> Dict[str, Any]:
        return {}

    def consent(self, req) -> Consent:
        return Consent(True)


class Speech:
    """Declares the fields it needs, then synthesizes. `report` is the only
    authority on what a run cost; None from cost_per_1m_chars means unpriced,
    never free."""
    id = ""
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = True

    def list_voices(self, cred) -> List[Voice]:
        raise NotImplementedError

    def synth(self, text, voice, opts, cred) -> Audio:
        raise NotImplementedError

    def quote(self, ctx) -> Quote:
        raise NotImplementedError

    def report(self, ctx) -> Report:
        raise NotImplementedError

    def cost_per_1m_chars(self, cred) -> Optional[float]:
        return None


class Storage:
    """Opaque blobs behind refs. The host never assumes a path and never assumes
    the bytes are still there."""
    id = ""

    def put(self, key: str, data: bytes) -> str:
        raise NotImplementedError

    def get(self, ref: str) -> bytes:
        raise NotImplementedError

    def delete(self, ref: str) -> None:
        raise NotImplementedError

    def info(self) -> StorageInfo:
        return StorageInfo()
