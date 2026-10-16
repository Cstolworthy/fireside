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
