# reader

Point it at an **EPUB** or a **story link**, choose a voice, get audio files you
can pause and resume.

## Why files

Browser read-aloud streams speech and can stall partway through a long page.
Here a whole chapter is turned into one audio file **first**, so playback can't
drop out. Close the tab, come back, it picks up where you stopped.

## Run it

**Windows, no terminal:** double-click `start_reader.bat`. A page opens at
http://127.0.0.1:8765. (Needs Python 3 once, from python.org, with
"Add python.exe to PATH" ticked.)

**Anywhere with Python 3.8+:** `python3 server.py` then open
http://127.0.0.1:8765. Standard library only. No pip, no build.
Options: `--port 9000`, `--out ./out`, `--open`.

**Docker / unraid:**

    docker compose up -d --build
    # then open http://<host>:8765

## Use

1. Drop an `.epub`, **or** paste a public story link and hit Load.
   - Public pages (story blogs, the like) work pasted.
   - **AO3:** either paste the work link, or use the work page's
     **Download -> EPUB** button and drop the file. The EPUB path also handles
     login-gated works, because the download happens in *your* browser.
2. Pick a voice engine.
3. **Load voices**, pick one.
4. **Play** a chapter. First play renders it (a moment); after that it's cached.
   Resume is automatic per book.

Audio files land in `out/<book>/audio/` (or `/data/...` under Docker).

## Voice engines

| engine  | key | notes |
|---------|-----|-------|
| local   | none | Offline tone, for testing the flow. Not speech. |
| azure   | Azure Speech key + region | Hundreds of neural voices across accents and styles. |
| minimax | key + Group ID | The voice from the demo clip. |

Keys are typed into the page, kept in this browser, and sent only to this local
server. They are not stored on disk here and never leave your machine except to
the provider you picked (and only the text you ask to be spoken).

### Getting an Azure key (about 5 minutes)

1. Sign in at portal.azure.com and pick **Create a resource**.
2. Search **Speech** and create a "Speech" resource (pick the region nearest you).
3. Open it, go to **Keys and Endpoint**.
4. Paste **Key 1** and the region name (e.g. `eastus`) into the page here.

Azure Speech has a free tier; check the current quota on the create screen.

## Notes / limits

- One thing at a time: a book, or a single page. A pasted page is one chapter.
- Books are keyed by file hash (links by URL), so re-loading the same thing
  reuses its folder and its already-rendered audio.
- Rendering sends the text to the provider in ~3.5k-character pieces and joins
  the audio. Long chapters take a while and cost provider quota.
- A page behind a login or built entirely by JavaScript won't fetch; drop an
  EPUB or a saved copy instead.
- `local` engine caps at 4 minutes because it is just a stand-in.