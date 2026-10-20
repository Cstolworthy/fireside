"""Per-user storage accounting: a held book bills its own bytes.

Every distinct blob a user's shelf names (chapter text, chapter audio, the
renditions kept for it, and the cover) counts against that user's storage. There
is no sharing discount and no reference count: if two people each hold the same
book, each of them bills its bytes, because each is holding it in their own
library. Content addressing still stores one physical copy on disk; billing is
per shelf, never per copy.

The launch rule, in one line: drop a book and it consumes storage. The first
`PLATFORM_ALLOWANCE_BYTES` are included with the platform tier; bytes above that
bill. A write that would cross the allowance is refused up front - existing bytes
are never silently removed to make room.

Promote-to-shared (hand a book to the shared catalog so it stops billing you) is
a real later feature and is deliberately NOT implemented here. Nothing in this
module frees a user's storage automatically; only the user deleting their own
book does, and that is handled by the library, not by a background pass.
"""

# ---- policy constants ------------------------------------------------------
# The platform tier's included allowance. Blocks (buy more storage, monthly) are
# not wired yet - the block size is pending; when it lands, allowance adds
# blocks * STORAGE_BLOCK_BYTES and this stays the only place that knows.
PLATFORM_ALLOWANCE_BYTES = 2 * 1000 ** 3        # 2 GB included with the tier
STORAGE_BLOCK_BYTES = 0                          # pending: size of a storage block
DEFAULT_PLAN = "platform"
PLAN_ALLOWANCE = {DEFAULT_PLAN: PLATFORM_ALLOWANCE_BYTES}


def _shelf_refs(conn, user_id):
    """Ref -> recorded bytes (or None) for every distinct blob on the shelf.

    A ref named twice (the same chapter text in two books, or a chapter row and
    its rendition pointing at the same audio) is one blob and appears once.
    `None` means the row carries no size (chapter text, cover art); the caller
    sizes those from the storage backend.
    """
    refs = {}

    def note(ref, nbytes):
        if not ref:
            return
        if ref not in refs or not refs[ref]:
            refs[ref] = nbytes

    q = ("SELECT c.text_ref AS ref, NULL AS nbytes FROM chapters c "
         "JOIN books b ON b.id = c.book_id WHERE b.user_id = ? "
         "AND c.text_ref IS NOT NULL AND c.text_ref <> ''")
    for r in conn.execute(q, (user_id,)):
        note(r["ref"], r["nbytes"])
    q = ("SELECT c.audio_ref AS ref, c.audio_bytes AS nbytes FROM chapters c "
         "JOIN books b ON b.id = c.book_id WHERE b.user_id = ? "
         "AND c.audio_ref IS NOT NULL AND c.audio_ref <> ''")
    for r in conn.execute(q, (user_id,)):
        note(r["ref"], r["nbytes"])
    q = ("SELECT r.audio_ref AS ref, r.audio_bytes AS nbytes FROM renditions r "
         "JOIN books b ON b.id = r.book_id WHERE b.user_id = ? "
         "AND r.audio_ref IS NOT NULL AND r.audio_ref <> ''")
    for r in conn.execute(q, (user_id,)):
        note(r["ref"], r["nbytes"])
    q = ("SELECT cover_ref AS ref, NULL AS nbytes FROM books "
         "WHERE user_id = ? AND cover_ref IS NOT NULL AND cover_ref <> ''")
    for r in conn.execute(q, (user_id,)):
        note(r["ref"], r["nbytes"])
    return refs


def _blob_bytes(storage, ref):
    """Size of a blob, whatever the backend can answer. A backend that exposes
    size() is asked directly; otherwise the blob is read. Never raises: an
    unreadable blob is 0 bytes, not a failed storage screen."""
    if storage is None:
        return 0
    fn = getattr(storage, "size", None)
    if callable(fn):
        try:
            return int(fn(ref) or 0)
        except Exception:
            pass
    try:
        return len(storage.get(ref))
    except Exception:
        return 0


def usage(conn, storage, user_id):
    """This user's storage: the bytes their own shelf holds, counted once per
    distinct blob. `own_bytes` and `total_bytes` are the same number - nothing
    is shared away - kept as two keys so the storage screen reads the same."""
    refs = _shelf_refs(conn, user_id)
    total = 0
    for ref, nbytes in refs.items():
        n = 0
        try:
            n = int(nbytes or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            n = _blob_bytes(storage, ref)
        total += n
    return {"user_id": user_id, "own_bytes": total, "total_bytes": total,
            "blobs": len(refs)}


def allowance_bytes(plan):
    """Included bytes for a plan name.

    An absent or unknown plan gets the platform allowance, not zero: the
    platform tier is where the included storage lives, payments are held for
    launch so every account is on it, and a plan name we do not recognise must
    fail open (generous) rather than lock a user out of their own library.
    """
    p = (plan or "").strip().lower() or DEFAULT_PLAN
    return PLAN_ALLOWANCE.get(p, PLAN_ALLOWANCE[DEFAULT_PLAN])


def check_room(conn, storage, user_id, plan, incoming_bytes):
    """True when adding `incoming_bytes` of storage stays within allowance.

    Read-only: it decides, the caller refuses the write. `incoming_bytes` is the
    size of the bytes the write would add to this user's shelf. It is an upper
    bound - a caller that cannot cheaply know which of its bytes already exist
    passes the full size - so it may refuse a write that would in fact fit. A
    false "no room" is recoverable; a false "room" overspends the box.
    """
    allowed = allowance_bytes(plan)
    used = usage(conn, storage, user_id)["own_bytes"]
    return (used + int(incoming_bytes or 0)) <= allowed
