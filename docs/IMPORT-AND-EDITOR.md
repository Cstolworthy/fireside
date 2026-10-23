# Import and chapter editor

Status: features agreed with Chris 2026-10-23. Not built. LOE 5.5 days, behind the Play release work.

This is the spec of record for DOCX import and the pre-import editing window. If a build
choice here disagrees with the code, the code is wrong until this file is updated on purpose.

## Why this exists

Automated extraction from a DOCX guesses. Best-effort text pull will drag in running
headers, page numbers, the copyright page, the table of contents, and the "also by this
author" ad at the back. None of that can be guessed away reliably, so it must not be
guessed away silently.

The principle: **the preview window is the feature, the extraction is only the feeder.**
A user must be able to see exactly what is about to become narration, and change it,
before a single character is billed.

## Flow

1. Drop a DOCX on the shelf.
2. Progress shows while extraction runs (same job/progress channel as render, per-chapter).
3. Editable chapter preview opens.
4. User edits, resolves flags, saves.
5. Import commits: chapters become the book, characters are only spent at render time as
   today.

## File support

- **DOCX first.** Target ~4.5 days with extraction, preview, flagging, editable text and
  split, plus tests.
- **PDF after, separate work.** 3-4 days. Library constraint: PyMuPDF is AGPL and is not
  acceptable inside a hosted commercial product, so PDF extraction uses pypdf/pdfminer.
  Scanned PDFs with no text layer must be refused with a clear message, not silently
  imported empty.
- No reflow/markup reconstruction. Plain text only.

## Features

### F1. Extraction with block-level structure

- Split into chapters on DOCX headings (Heading 1 / Heading 2 style runs, then fallback
  heuristics). Chapter boundaries are visible and re-cuttable by the user, not fixed at
  import.
- Preserve paragraph breaks. Drop nothing else.

### F2. Junk flagging (flag, never silently delete)

Flag engine marks suspicious text. Rules in the first pass:

- a line repeated across multiple pages (running header / footer / book title)
- a standalone number (page number)
- an all-caps short line
- orphan one- or two-word lines
- leading/trailing sections that look like front/back matter: copyright page, TOC,
  "also by this author"

Flagged text is **shown, highlighted, never removed by the machine**. Removing it is the
user's decision, one flag at a time or all at once.

### F3. One editor, chapter picked from the list

- Chapter list on one side, one chapter of text loaded into a single editor.
- Deleting and retyping both work. That means plain-text editing, not rich text: no bold,
  italic, or link markup. A rich-text editor is a week of work and buys nothing for
  narration.

### F4. Inline highlight (accepted, it is the key surface)

- Highlighting inside editable text cannot live in a plain `<textarea>`.
- Build: an overlay `div` positioned behind a transparent textarea, same font, size,
  line-height and padding, with flagged spans drawn under the live text.
- **Marks are identified by node id, never by character offset.** Every deletion above a
  mark would otherwise shift it silently. This is the detail that would otherwise bite us
  in month two.

### F5. Explicit save, dirty guard

- Editor has an explicit Save. No autosave.
- Navigating away from a dirty chapter warns that edits will be lost. Navigating away from
  a dirty book warns before leaving the window.

### F6. Resolve flags individually and in bulk

- Each flag can be rejected (keep the text) or accepted (drop the text).
- Rejecting one flag then hitting "accept all" must still apply every other flag.
- **Accept all is global**: it applies to every flag in every chapter, including chapters
  the user never opened, minus the set explicitly rejected.
- Rejection set is per book and persists in the draft until import commits.

### F7. Chapter split editing

- User can move a boundary and merge/split chapters in the preview. Only the split for
  now; no attempt to re-serialize DOCX.

## Non-goals

- Round-tripping the DOCX.
- Rich text, images, footnotes, tables as tables.
- OCR.
- Auto-deciding junk without showing it.

## Build order and LOE

Agreed order: **Play release work first, then DOCX import.** Import is what users hit
first, but Play is what makes the app exist at all, and it is mostly paperwork plus a day
of packaging.

Play first:
- release `.aab`, upload keystore + Play App Signing
- one 512px icon
- Data safety form + content rating
- in-app delete path
- target API level

Then DOCX import, 5.5 days:
- 1.5 extraction (DOCX -> chapters -> blocks, headings, page-repeat detection)
- 1 preview endpoint (serve the extracted draft, accept the draft edits/saves)
- 2 preview window (overlay editor, chapter list, save model, dirty guard)
- 1 flag engine + tests

Then PDF import, 3-4 days, as its own change.

## Tests that matter

- A DOCX with running headers and page numbers flags them and leaves them in the text
  until accepted.
- A deletion above a flag does not move the flag (node ids, not offsets).
- Reject one flag, accept all: that one survives, every other flag is applied, including
  in unopened chapters.
- Save, leave clean chapter, come back: no warning. Edit, navigate: warning shown.
- A chapter split in the preview survives into the imported book.
- A scanned PDF is refused with a readable reason.
