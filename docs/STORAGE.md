# fireside v2 - storage contract

Status: step 3 of the build order. The third plugin contract, and the one every
other layer sits on.

## The job of a storage backend

One interface, three questions: **has**, **get**, **put**. A backend answers
them for opaque blobs keyed by content hash. It never knows what is inside a
blob and never parses it.

    blob_ref = {storage_ref, bytes, checksum}

`checksum` is the content address (sha256 hex). `bytes` is the exact length.
`storage_ref` is the backend's own handle for the bytes - a path, a URL, a
bucket key. A caller that only needs to *name* a blob uses `blob_ref`; only the
storage layer resolves `storage_ref`.

Required operations:

- `put(bytes) -> blob_ref` - store and return the ref; idempotent, storing the
  same bytes twice is one blob.
- `has(checksum) -> bool`
- `get(checksum) -> bytes` - raise loudly if missing or if the stored bytes do
  not match `checksum`.

## Local is the default and the truth

The default backend is the local filesystem. No server, no account, no key: the
repo run by itself stores under a data dir and is fully functional. Every other
backend (S3, Azure blob, a shared NAS) is an optional override.

The blast radius of a broken storage backend is the whole app, so the contract
is small on purpose and the local backend is the reference implementation every
test runs against.

## Two stores, never merged

- **Global content store**: raw source bytes and rendered audio, addressed by
  `checksum`, shared across users and books. Two books that resolve to the same
  bytes share one blob.
- **Per-user store**: the library manifest, reading position, and offline
  selections, keyed by the user's internal id. Never shared.

Sharing happens at the storage layer, never at the account.

### The leak to guard

A global cache keyed on the *source URL alone* leaks auth-scoped content: two
users hitting the same AO3 chapter URL with one logged in and one not must not
collide, and a logged-in fetch must never be served to an anonymous one. Key
the global store on the *bytes actually fetched* (content address), and keep any
url -> hash freshness map separate from the per-user auth context. The freshness
map is a hint, never a permission.

## What is not storage's job

- no rendering, no fetching, no format awareness;
- no policy about who may read a blob - that is the host's call at a layer above;
- no eviction of derived data without an explicit host instruction (source bytes
  are append-only; see the freshness note).

## Backend plugins

Backends are discovered the same way readers and speech engines are: a directory
with a `plugin.json`, discovered by the host and ordered. `local` ships in the
core and is always available as the fallback.
