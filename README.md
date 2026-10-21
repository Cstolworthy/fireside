# reader

Point it at an EPUB, choose a voice, get audio files you can pause, resume, and keep.

## Why files

Browser read-aloud streams speech and can stall partway through a long page. Here
a whole chapter is turned into one audio file **first**, so playback can't drop out.
Close the tab, come back, it picks up where you stopped.

## Run

    python3 server.py
    # then open http://127.0.0.1:8765

Python 3.8+, standard library only. No pip install, no build.

Options: `--port 9000`, `--out ./out`.

### Download and run (no terminal)

- **Windows** - double-click `start_reader.bat`
- **macOS** - double-click `start_reader.command`
  (if macOS instead asks what to open it with, right-click it, *Open With ->
  Terminal*, or run `sh start_reader.command` in Terminal)
- **Linux** - run `./start_reader.sh`

Each one starts the server and opens the page in your browser. The only thing you
need first is Python 3 from <https://www.python.org/downloads/> (on a Mac,
`xcode-select --install` in Terminal is enough). The launchers say so plainly if
it is missing.

### Serve it to more than yourself

    python3 server.py --host 0.0.0.0 --port 8765
    docker compose up          # or: docker build -t reader . && docker run -p 8765:8765 reader

`--host 0.0.0.0` means anyone who can reach that port sees the same shelf and the
same `out/` folder. There are no accounts; it is one bookshelf for whoever shows
up. That is fine on your own machine or LAN. Do not put it on the open internet in
that shape.

## Use

1. Drop an `.epub` (or click to choose), **or** paste a public story link and press
   Load (the page is fetched here and stripped down to the story text).
   AO3 works either way: on the work page use **Download -> EPUB**, then drop that
   file in. That path also handles login-gated works, because the download happens
   in *your* browser.
2. Pick a voice engine, paste your key, **Load voices**, type in the filter box to
   narrow the list, pick one. Azure default region is `eastus`; the region has to
   match the key's resource.
3. Press **Remember settings** to keep the engine, key and **voice** for next time.
   The button shows **unsaved** whenever something changed and has not been saved.
   On the next visit the saved key is used to reload the list and re-select your voice.
4. **Play** a chapter.

Every slow step shows a spinner, a count and an elapsed timer, so a long first
render does not look like a hang. Audio is cached **per voice**: change the voice,
press Play again, and you hear the new one. Going back to a voice you already used
is instant.

## Save the audio

**Render all chapters** works through the whole book in the background and reports
progress chapter by chapter. It skips anything already rendered, so pressing it
twice costs nothing. The line under the buttons says how much of the book exists in
the voice you have selected right now.

Then take it out:

- **Save one file** stitches the chapters into a single audio file, for a phone or
  a podcast app that should just play straight through.
- **Save zip** hands back one file per chapter, numbered, inside a folder named
  after the book.

Both are built once and cached next to the audio, so a second download is instant.
The chapter files underneath are plain `.mp3` (Azure, MiniMax) or `.wav` (offline
tone) in `out/<book>/audio/<voice>/`, so you can also just copy that folder.

## Voice tester

The **Voice tester** tab is for choosing a voice before you commit a book to it.
Type or paste some text (or press *Use this chapter's opening*), pick a voice on
the left, press **Say it**. The same text in each voice, so you can tell them apart.
Each result is cached, so re-hearing one you already tried is instant.

Playback speed is settable. Audio files land in `out/<book>/audio/<voice>/`.

## Voice engines

| engine  | key | notes |
|---------|-----|-------|
| local   | none | Offline tone, for testing the flow. Not speech. |
| azure   | Azure Speech key + region | Hundreds of neural voices across accents and styles. |
| minimax | key + Group ID | The voice from the demo clip. |

Keys are typed into the page, kept in this browser, and sent only to this local
server. They are not stored on disk here and never leave your machine except to
the provider you picked (and only the text you ask to be spoken).

## Notes / limits

- One EPUB at a time. Books are keyed by file hash, so re-dropping the same book
  reuses its folder and its already-rendered audio.
- Rendering a chapter sends its text to the provider in ~3.5k-character pieces and
  joins the audio. Roughly 2 s per piece on Azure, so a 160k-character story is
  about two minutes and ~160k characters of provider quota. A partly-rendered
  chapter is never cached as if it were finished.
- Inputs: `.epub`, or any public http/https page that is plain HTML. Pages built
  entirely in JavaScript, or behind a login, will not parse.
- `local` engine caps at 4 minutes because it is just a stand-in.
- **Save one file** joins the chapters end to end. That works because each chapter
  is already one continuous audio file with no container wrapper beyond the format
  itself; a `.wav` gets its two length fields fixed up on the join so players do not
  stop at the end of chapter one. Chapter boundaries are not re-encoded, so there is
  no extra quality loss.

## Provenance

Agent-authored, human-owned. The commit history is linear, one change per commit;
each shipped version is a tag (`v1.1` through `v1.6`), and nothing has been
rewritten since the first public push. See [AUTHORS](AUTHORS) for the
authorship/ownership split and how the history proves it.


## fireside v2 (host + browser client)

v2 splits the single app into a **host** and a **client**. The host owns the
library, the render jobs, storage, the credit ledger, and a plugin registry; the
browser client is served by the host and talks to it over JSON. The same server
runs at home and hosted - only the auth mode and whose key is used differ.

    python3 -m host.app --host 0.0.0.0 --port 8788
    # then open http://<this-machine>:8788

The v1 app (`python3 server.py`) still runs alongside it, unchanged, until every
v2 piece has replaced it. The v2 HTTP surface is documented at the top of
`host/app.py`; the plugin contracts are in `docs/V2-ARCHITECTURE.md`.

### What the browser client does

- import an EPUB (dropped) or a story link, into your own library;
- pick a voice engine and a voice from those the plugins declare;
- render a book as a job and watch it progress;
- **verify before play**: each chapter is downloaded whole and its SHA-256
  checked against the server's ref before a single note plays, online or offline;
- resume from a single per-book position, and hand playback between devices
  (playing on one stops the other);
- **download for offline** is pull-only: it caches chapters that are already
  converted and reports the rest; it never triggers a render.

The SHA-256 runs in the page itself, so verification works on a plain-HTTP LAN
origin where `crypto.subtle` is unavailable.

### Run it on unraid (or any Docker host)

    docker compose up -d --build

Then open `http://<unraid-ip>:8788`. Rendered audio, the SQLite DB and the
content-addressed blobs live in `./data` (mapped to `/data` in the container), so
a container update never loses them. Plugins are discovered from `plugins/` in
the image; drop a new `plugins/<kind>/<id>/` directory in and rebuild to add a
reader, a speech engine, or a storage backend.

On unraid, the simplest path is the Compose Manager plugin: add this repo as a
stack, set the host port to 8788, and point the `data` mount at a share under
`/mnt/user/appdata/fireside`.

The Community Applications template is at `templates/fireside.xml`. Its default
host path is `/mnt/user/appdata/fireside/data`. Unraid creates that directory as
root, and fireside runs as uid 10001, so on first start the app cannot create
its SQLite file. The container entrypoint handles that for you: it starts as
root only long enough to make the data directory writable by the app user, then
drops to the unprivileged user before the server runs. Nothing to chown by hand.
If you would rather own the files yourself, set `PUID`/`PGID` (the Unraid
convention) and the entrypoint uses those instead of 10001.


## Backends and accounts

State lives in SQLite under `FIRESIDE_DATA` by default - nothing to install, one
file to back up. Point it at MySQL or MariaDB when you want the data somewhere
else (a shared host, an existing database server, a second box):

    FIRESIDE_DB_TYPE=mysql
    FIRESIDE_DB_CONN=user:password@host:3306/fireside

The two variables are independent: `FIRESIDE_DB_TYPE` picks the engine
(`sqlite` by default, or `mysql`), and `FIRESIDE_DB_CONN` is the connection
string for it - a MySQL DSN (`user:pass@host:3306/db`, or a full
`mysql://...` URL) or a SQLite file path. The database is created on first
start if the account may create it. The query layer is the same either way;
the engine is chosen once, at connect. Leave both unset to stay on the default
SQLite file under `FIRESIDE_DATA`. The older single variable
`FIRESIDE_DB=mysql://...` still works and sets both.

Sign-in has two shapes, and a deployment can offer both:

- **Local account** - username and password, stored on this server as a salted
  PBKDF2 hash. This is the door a LAN box needs, and it is on by default; set
  `FIRESIDE_ALLOW_LOCAL=0` to hide it. Register and sign in from the header.
- **Social sign-in** - GitHub / Google / Microsoft, configured by putting the
  client id and secret in the environment. A provider appears only when its
  credentials are present, so an unconfigured server simply offers none. Google
  requires an HTTPS redirect URI: a bare IP or plain http is rejected by Google,
  so it needs a real hostname with TLS in front.

With `FIRESIDE_REQUIRE_AUTH=1` (automatic under `--hosted`) every route but the
page, `/health`, `/auth/providers` and `/me` needs a session.
