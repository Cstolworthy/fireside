"""Per-user pronunciation fixes: the words a voice gets wrong.

A rule is {"from": word, "to": respelling}. `from` is the word exactly as it
appears in the text; `to` is how it should sound, spelled out for the engine
(Hermione -> Her-my-oh-nee, Qui-Gon -> Kwai-Gon). The host applies the list to
the chapter text before any speech engine sees it, so every engine gets the fix,
including the local one that cannot take phonemes.

Rules are per user and global across their books, stored as JSON in
user_prefs.pron.
"""

import hashlib
import json
import re

from host.db import now


def load(conn, uid):
    """This user's list, tidied: [{'from': word, 'to': respelling}].

    Anything malformed is dropped rather than raised: one bad row must not take
    down a render.
    """
    row = conn.execute("SELECT pron FROM user_prefs WHERE user_id=?",
                       (uid or "",)).fetchone()
    raw = (row["pron"] if row else "") or ""
    try:
        rules = json.loads(raw) if raw else []
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


def save(conn, uid, rules):
    """Tidy and store the list: drop blanks, drop duplicate words, keep order.

    Updates the existing prefs row in place so the other columns (default
    voice/connection) survive; only inserts when the user has no row yet.
    """
    clean, seen = [], set()
    for r in rules or []:
        frm = str((r or {}).get("from", "")).strip()
        to = str((r or {}).get("to", "")).strip()
        if frm and to and frm.lower() not in seen:
            seen.add(frm.lower())
            clean.append({"from": frm, "to": to})
    blob = json.dumps(clean, ensure_ascii=False)
    row = conn.execute("SELECT user_id FROM user_prefs WHERE user_id=?",
                       (uid,)).fetchone()
    if row:
        conn.execute("UPDATE user_prefs SET pron=?,updated_at=? WHERE user_id=?",
                     (blob, now(), uid))
    else:
        conn.execute(
            "INSERT INTO user_prefs(user_id,pron,updated_at) VALUES(?,?,?)",
            (uid, blob, now()))
    conn.commit()
    return clean


def _regex(rules):
    """One alternation, longest word first, whole-word where the word has edges."""
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


def apply(text, rules):
    """Swap whole words for their say-as respelling, case-insensitive. The
    replacement goes in literally, never read back as a regex."""
    pat = _regex(rules)
    if not text or pat is None:
        return text
    repl = {r["from"].lower(): r["to"] for r in rules}
    return pat.sub(lambda m: repl.get(m.group(0).lower(), m.group(0)), text)


def token_for(text, rules):
    """A short tag for just the rules that match `text`, for the audio cache key.

    Only the matching rules go in, so adding a rule for a word that lives in one
    chapter does not invalidate chapters where that word never appears. Empty
    means no rule applies, and the chapter keeps whatever audio it already had.
    """
    pat = _regex(rules)
    if not pat or not text:
        return ""
    found = {m.group(0).lower() for m in pat.finditer(text)}
    use = [r for r in rules if r["from"].lower() in found]
    if not use:
        return ""
    raw = "|".join("%s>%s" % (r["from"].lower(), r["to"])
                   for r in sorted(use, key=lambda r: r["from"].lower()))
    return "p" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
