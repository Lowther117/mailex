# PST Exporter

Opens Outlook **PST** and **OST** files directly and gets the mail out of them,
without Outlook, without MAPI, and without any compiled library. Point it at
one file, or at a folder full of them, browse and preview, then export what you
select - a handful of messages or an entire mailbox - as **EML**, **MBOX**,
**PDF**, **HTML**, **plain text**, or just the **attachments**. Windows and
macOS, one window, dark mode by default.

The file reader is written from scratch in Python from Microsoft's published
[MS-PST] specification. It handles all three on-disk variants - ANSI (Outlook
97-2002), Unicode (Outlook 2003 onwards) and the 4K-page OST format that
Outlook 2013 and later use for cached Exchange mailboxes - and both of the
"encryption" modes Outlook can apply to a file.

Everything runs on your own machine. Nothing is uploaded anywhere, and the
PST/OST is only ever read; not a byte of it is changed.

---

## Which file do I use?

| File | When |
|---|---|
| `build-exe.bat` | **Windows.** Double-click once. Installs Python if it is missing, installs every dependency, and produces `dist\PSTExporter.exe` - one standalone file you can copy anywhere. |
| `build-app.command` | **macOS.** Double-click once. Installs Homebrew and Python if they are missing, installs every dependency, and produces `dist/PSTExporter.app`. Drag it to Applications. |
| `run.bat` | Windows, run from source without building an exe. Sets up a virtual environment the first time. |
| `run.command` | macOS, run from source without building an app. |
| `python pst_exporter.py` | Any platform, if you already have Python 3.9+ with tkinter and the requirements installed. |
| `PSTExporter selftest` | Builds three small PST files (one per encryption mode), reads them back and exports every format, then writes `pst-exporter-selftest.txt`. The build scripts run this and refuse to claim success if anything fails. |

The repository is deliberately flat - no Windows/Mac subfolders. The extension
already says which operating system a file is for.

**First build on a clean machine takes a few minutes** (Python, Homebrew,
wheels, PyInstaller). After that it is about a minute. Watch
`build-win-log.txt` / `build-mac-log.txt` if you want the detail; on failure the
last forty lines are printed for you.

On macOS, if a freshly written `.command` will not run, it needs its
executable bit and quarantine flag sorted once:
`chmod +x *.command && xattr -d com.apple.quarantine *.command`.

---

## Using it

**Open** one or more files with *Open files…*, or *Open folder…* to find
every `.pst` and `.ost` under a folder however deep it is nested - that is the
bulk route. Each file appears in the left pane with its folder tree and
message counts. Files still in use by Outlook may refuse to open until Outlook
is closed; copy the file somewhere first if you need to.

**Browse.** Click a folder to list its messages. Tick *Subfolders* to include
everything beneath it, *E-mail only* to hide calendar, contact, task and note
items. *Search* filters the list by subject, sender and recipients; click a
column heading to sort. A single click previews the message - headers, body
and attachments - and any attachment can be saved on its own from the preview.
Double-clicking a message saves it as `.eml`.

**Select.** Click, Shift-click and Ctrl-click (Cmd-click on a Mac) pick
messages; Ctrl+A selects everything listed. Then:

| Button | Exports |
|---|---|
| *Export selected…* | the selected messages (or everything listed, if nothing is selected) |
| *Export folder…* | the folder chosen in the left pane, with all of its subfolders |
| *Export everything…* | every message in every open file |

All three open the same dialog, where you pick the format, where the files go,
and a few options. Progress is shown as it goes and can be cancelled; one bad
message never stops the batch.

### Formats

| Format | What you get |
|---|---|
| **EML** | One `.eml` per message: a standards-compliant MIME message with the original transport headers preserved where the PST kept them, text and HTML bodies, attachments embedded, inline images wired up by Content-ID, and forwarded messages nested as `message/rfc822`. Opens in Outlook, Apple Mail, Thunderbird, and anything else that reads mail. |
| **MBOX** | One mailbox file per PST folder (or one for the lot), for importing into Thunderbird or Apple Mail. |
| **PDF** | One PDF per message with a header block and the body text, or - tick the option - every message in one combined PDF with a page per message. A system font with wide Unicode coverage is used when one is present (Segoe UI or Arial on Windows, Arial Unicode on macOS), so accented and most non-Latin text prints as text (CJK needs a font that has it - Arial Unicode on a Mac does, Segoe UI on Windows does not). Attachments are saved in a folder beside each PDF. |
| **HTML** | A standalone web page per message: header table, the original HTML body with inline images embedded as data URIs (so the page works on its own), links to the attachments saved beside it. |
| **Plain text** | Headers and body text, attachments saved beside it. Bodies that only exist as HTML or RTF are converted. |
| **Attachments only** | Just the files, one folder per message (or pooled). Forwarded messages come out as `.eml`. |

Every export also writes `index.csv` and `index.json` at the top of the
export folder listing each message, where it came from and what it was written
as, and `export-log.txt` if anything could not be exported (typically
attachments that were links to files on the original computer, which the PST
never contained).

By default the PST's own folder tree is recreated under the export folder,
with one folder per file when several are exported at once, and file names are
`date time subject` so they sort chronologically. Untick *Keep the folder
structure* to put everything in one place. Exports go to your Downloads folder
unless you choose somewhere else, or set a default under *File > Default save
folder*.

### Extras

- **Property inspector** (View menu, Ctrl+I) lists every MAPI property of the
  current message with its tag, type and value - useful when you need to know
  exactly what the file says rather than what a mail client shows.
- **Orphaned messages** (View menu) scans a file's node index for messages that
  no folder lists any more - things that were deleted but whose data is still
  in the file - and adds them as a pseudo-folder you can export from.
- Ctrl+D switches between dark and light.

---

## Command line

The same engine works without the window, which is the way to script it or
run it on a server:

```
PSTExporter export SOURCE... [-f eml|mbox|pdf|html|txt|attachments] [-o DIR]
                   [--folder "Inbox/Projects"] [--search TEXT] [--email-only]
                   [--flat] [--no-attachments] [--single-pdf]
PSTExporter list FILE          folder tree with message counts
PSTExporter info FILE          what kind of file it is
PSTExporter selftest [FILE]    self-test, optionally exercising a real file too
```

`SOURCE` can be files or folders; folders are searched recursively for
`.pst`/`.ost`. From source, `python pst_exporter.py` takes the same arguments.

---

## What it can and cannot read

- **PST**: ANSI (Outlook 97-2002, 2 GB limit era) and Unicode (2003 onwards).
- **OST**: Unicode, including the 4K-page variant with zlib-compressed blocks
  that Outlook 2013+ writes. OST files are a cache of an Exchange or
  Outlook.com mailbox, so the same reader covers them.
- **Encryption**: Outlook's "compressible" and "high" encryption are
  substitution ciphers documented in the specification and are undone
  transparently. A PST **password** only protects Outlook's own UI - the data
  is not encrypted with it - so password-protected files open normally.
  Files protected with Windows EFS-style encryption need the original user's
  key and are refused with a message saying so.
- **Bodies**: plain text, HTML and RTF, including HTML that Outlook
  encapsulated inside RTF. Compressed RTF (LZFu) is decoded.
- **Attachments**: ordinary files, inline images, embedded (forwarded)
  messages to any depth, and OLE objects (saved as raw storage). Attachments
  that were *links* to files have no content in the PST and are reported as
  such.
- **Items**: everything in a folder is listed, so calendar, contact, task and
  note items appear too and export with whatever text body they carry. Tick
  *E-mail only* to hide them.

Things it does not do: write PST files, read the name-to-ID map for named
properties (calendar recurrence patterns and the like are exported as text,
not decoded), or recover from a badly damaged file beyond the orphan scan.

---

## How the reader works

Three layers, as in the specification:

1. **NDB** (`pstlib/ndb.py`) - the header, the two B-trees (nodes and
   blocks), raw blocks with their padding and trailers, the two ciphers, data
   trees (XBLOCK/XXBLOCK) that stitch large values together, and sub-node
   trees. Pages and blocks are read on demand through small LRU caches, so a
   multi-gigabyte OST opens in milliseconds and memory stays flat.
2. **LTP** (`pstlib/ltp.py`) - heap-on-node, the BTH B-tree-on-heap, property
   contexts (a node's property bag) and table contexts (folder listings,
   recipient and attachment tables), with MAPI value decoding in
   `pstlib/mapi.py`.
3. **Messaging** (`pstlib/message.py`) - the store, folders, message rows,
   messages, recipients and attachments as ordinary Python objects.

`pstlib/synth.py` is the mirror image: a small PST *writer* used only by the
self-test, so the reader is exercised end to end on any machine without
shipping anybody's real mailbox. It writes multi-level B-trees, multi-block
heaps, XBLOCK data trees, sub-node trees and embedded messages, in all three
encryption modes.

The reader was also verified against real files: the EDRM Enron PST (Unicode,
compressible encryption) and several Outlook 2016 OST files (4K-page, compressed
blocks) from the `pst-extractor` project's test data - every message,
recipient and attachment in them opens and exports.

---

## Files

| File | Purpose |
|---|---|
| `pst_exporter.py` | Entry point from source (window, or CLI sub-commands) |
| `pst_exporter_app.py` | Entry point for the frozen builds |
| `pstlib/ndb.py` | Storage layer: header, B-trees, blocks, ciphers, data and sub-node trees |
| `pstlib/ltp.py` | Heap-on-node, BTH, property and table contexts |
| `pstlib/mapi.py` | Property tags, types, code pages, value decoding |
| `pstlib/message.py` | Store, folders, messages, recipients, attachments |
| `pstlib/rtf.py` | Compressed RTF, RTF to text, HTML de-encapsulation, HTML to text |
| `pstlib/export.py` | EML, MBOX, HTML, text and attachment exporters; the bulk runner |
| `pstlib/pdfout.py` | PDF rendering (ReportLab) |
| `pstlib/ui.py` | The window |
| `pstlib/theme.py` | Shared light/dark theme (identical across all of my apps) |
| `pstlib/paths.py` | App folder, settings, default save folder |
| `pstlib/synth.py` | Test-only PST writer |
| `pstlib/selftest.py` | The self-test |
| `pstlib/cli.py` | Command-line handling |
| `pstlib/_crypt_tables.py` | The three 256-byte cipher tables from the specification |

Python 3.9 to 3.14. Dependencies: `reportlab` and `pillow` (for PDF output
only - everything else is the standard library).

## Licence

MIT No Attribution - see `LICENSE`.
