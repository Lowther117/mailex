# Mailex

Open any mailbox, export it any way. Mailex reads the formats mail ends up
stored in - Outlook **PST** and **OST** files, **MBOX** (including Thunderbird
profiles and Apple Mail's exported mailboxes), **Maildir**, and loose **EML**,
**EMLX** and Outlook **MSG** files - without Outlook, MAPI or any mail client
installed. Point it at one file or at a folder full of them, browse and preview,
then export what you select - a handful of messages or an entire mailbox - as
**EML**, **MBOX**, **PDF**, **HTML**, **plain text**, or just the
**attachments**. Windows and macOS, one window, dark mode by default.

The PST/OST reader and the compound-file reader behind `.msg` are written from
scratch in Python from Microsoft's published specifications ([MS-PST],
[MS-CFB], [MS-OXMSG]); everything else is the standard library. The PST reader
handles all three on-disk variants - ANSI (Outlook 97-2002), Unicode (2003
onwards) and the 4K-page OST format that Outlook 2013 and later use for cached
Exchange mailboxes - and both "encryption" modes Outlook can apply.

Everything runs on your own machine. Nothing is uploaded anywhere, and the
files you open are only ever read; not a byte of them is changed.

---

## Which file do I use?

| File | When |
|---|---|
| `build-exe.bat` | **Windows.** Double-click once. Installs Python if it is missing, installs every dependency, and produces `dist\Mailex.exe` - one standalone file you can copy anywhere. |
| `build-app.command` | **macOS.** Double-click once. Installs Homebrew and Python if they are missing, installs every dependency, and produces `dist/Mailex.app`. Drag it to Applications. |
| `run.bat` | Windows, run from source without building an exe. Sets up a virtual environment the first time. |
| `run.command` | macOS, run from source without building an app. |
| `python mailex.py` | Any platform, if you already have Python 3.9+ with tkinter and the requirements installed. |
| `Mailex selftest` | Builds synthetic mailboxes in every supported format, reads them back and exports every output format, then writes `mailex-selftest.txt`. The build scripts run this and refuse to claim success if anything fails. |

The repository is deliberately flat - no Windows/Mac subfolders. The extension
already says which operating system a file is for.

**First build on a clean machine takes a few minutes** (Python, Homebrew,
wheels, PyInstaller). After that it is about a minute. Watch
`build-win-log.txt` / `build-mac-log.txt` if you want the detail; on failure the
last forty lines are printed for you.

On macOS, if a freshly written `.command` will not run, it needs its
executable bit and quarantine flag sorted once, from inside the folder:
`chmod +x *.command && xattr -d com.apple.quarantine *.command`.

---

## What it opens

| Source | How Mailex sees it |
|---|---|
| **PST / OST** | Outlook data files. Its folder tree, message counts and all. Both encryption modes are undone transparently; a PST *password* only guards Outlook's own UI, so password-protected files open normally. |
| **MBOX** file | One folder of messages. Any of the mbox flavours (mboxo, mboxrd, mboxcl) that mail clients write. |
| **Thunderbird** | Point *Open folder* at a profile's `Mail` or `ImapMail` folder (or one account inside it): each extensionless mbox file becomes a folder, `Name.sbd` directories become its sub-folders, `.msf` index files are ignored. |
| **Apple Mail export** | An exported `Name.mbox` package (the folder with an `mbox` file inside) opens as one folder. A folder of them opens as one source with a folder each. |
| **Maildir** | A directory with `cur`, `new` and `tmp`. Courier-style `.Folder.Sub` sub-directories and plainly nested Maildirs both become sub-folders; read/unread comes from the file flags. |
| **EML / EMLX** | Single files, or a directory tree of them: the directories become the folders. `.emlx` is Apple Mail's per-message file. |
| **Outlook MSG** | Single files or a directory tree of them, read through the compound-file reader. Attachments, embedded (forwarded) messages, RTF and HTML bodies all come through. |

*Open folder* works out what is inside a folder by itself - PSTs, mbox files,
a Maildir, loose message files, or a mixture - and opens all of it. That is the
bulk route. An OST still in use by Outlook may refuse to open until Outlook is
closed; copy the file somewhere first if you need to.

## Using it

**Browse.** Each opened source appears in the left pane with its folder tree
and message counts. Click a folder to list its messages. Tick *Subfolders* to
include everything beneath it, *E-mail only* to hide calendar, contact, task
and note items. *Search* filters the list by subject, sender and recipients;
click a column heading to sort. A single click previews the message - headers,
body and attachments - and any attachment can be saved on its own from the
preview. Double-clicking a message saves it as `.eml`.

**Select.** Click, Shift-click and Ctrl-click (Cmd-click on a Mac) pick
messages; Ctrl+A selects everything listed. Then:

| Button | Exports |
|---|---|
| *Export selected…* | the selected messages (or everything listed, if nothing is selected) |
| *Export folder…* | the folder chosen in the left pane, with all of its subfolders |
| *Export everything…* | every message in every open source |

All three open the same dialog, where you pick the format, where the files go,
and a few options. Progress is shown as it goes and can be cancelled; one bad
message never stops the batch.

### Formats

| Format | What you get |
|---|---|
| **EML** | One `.eml` per message. Messages that arrived as real RFC 822 bytes (MBOX, EML, Maildir) are written out **byte for byte**. PST and MSG messages are rebuilt into standards-compliant MIME: the original transport headers where the file kept them, text and HTML bodies, attachments embedded, inline images wired up by Content-ID, forwarded messages nested as `message/rfc822`. Opens in Outlook, Apple Mail, Thunderbird, and anything else that reads mail. |
| **MBOX** | One mailbox file per folder (or one for the lot), for importing into Thunderbird or Apple Mail. Same passthrough rule as EML. |
| **PDF** | One PDF per message with a header block and the body text, or - tick the option - every message in one combined PDF with a page per message. A system font with wide Unicode coverage is used when one is present (Segoe UI or Arial on Windows, Arial Unicode on macOS), so accented and most non-Latin text prints as text (CJK needs a font that has it - Arial Unicode on a Mac does, Segoe UI on Windows does not). Attachments are saved in a folder beside each PDF. |
| **HTML** | A standalone web page per message: header table, the original HTML body with inline images embedded as data URIs (so the page works on its own), links to the attachments saved beside it. |
| **Plain text** | Headers and body text, attachments saved beside it. Bodies that only exist as HTML or RTF are converted. |
| **Attachments only** | Just the files, one folder per message (or pooled). Forwarded messages come out as `.eml`. |

Every export also writes `index.csv` and `index.json` at the top of the
export folder listing each message, where it came from and what it was written
as, and `export-log.txt` if anything could not be exported (typically
attachments that were links to files on the original computer, which the PST
never contained).

By default the source's own folder tree is recreated under the export folder,
with one folder per source when several are exported at once, and file names
are `date time subject` (in local time, as the mail client would show it) so
they sort chronologically. Untick *Keep the folder structure* to put everything
in one place. Exports go to your Downloads folder unless you choose somewhere
else, or set a default under *File > Default save folder*.

### Extras

- **Property inspector** (View menu, Ctrl+I) lists every MAPI property of a
  PST or MSG message - or every header of a MIME one - with its tag, type and
  value, for when you need to know exactly what the file says rather than what
  a mail client shows.
- **Orphaned messages** (View menu, PST/OST only) scans a file's node index
  for messages that no folder lists any more - things that were deleted but
  whose data is still in the file - and adds them as a pseudo-folder you can
  export from.
- Ctrl+D switches between dark and light.

---

## Command line

The same engine works without the window, which is the way to script it or
run it on a server:

```
Mailex export SOURCE... [-f eml|mbox|pdf|html|txt|attachments] [-o DIR]
              [--folder "Inbox/Projects"] [--search TEXT] [--email-only]
              [--flat] [--no-attachments] [--single-pdf]
Mailex list SOURCE...          folder tree with message counts
Mailex info SOURCE...          what kind of mailbox it is
Mailex selftest [FILE]         self-test, optionally exercising a real file too
```

`SOURCE` is any file or folder from the table above; folders are worked out
the same way as *Open folder*. From source, `python mailex.py` takes the same
arguments.

---

## What it can and cannot read

- **PST**: ANSI (Outlook 97-2002) and Unicode (2003 onwards). **OST**:
  Unicode, including the 4K-page variant with zlib-compressed blocks that
  Outlook 2013+ writes. Files protected with Windows EFS-style encryption need
  the original user's key and are refused with a message saying so.
- **Bodies**: plain text, HTML and RTF, including HTML that Outlook
  encapsulated inside RTF. Compressed RTF (LZFu) is decoded.
- **Attachments**: ordinary files, inline images, embedded (forwarded)
  messages to any depth, and OLE objects (saved as raw storage). PST
  attachments that were *links* to files have no content and are reported as
  such.
- **Items**: everything in a PST folder is listed, so calendar, contact, task
  and note items appear too and export with whatever text body they carry.
  Tick *E-mail only* to hide them.
- **MSG**: Unicode and ANSI string storage, multi-valued properties, the
  properties stream at every level (message, embedded message, recipient,
  attachment).

Things it does not do: write PST or MSG files, decode named properties
(calendar recurrence patterns and the like are exported as text, not
interpreted), read Outlook for Mac `.olm` archives, or recover from a badly
damaged PST beyond the orphan scan.

---

## How it works

Three readers feed one shape:

1. **PST/OST** (`mailexlib/ndb.py`, `ltp.py`, `message.py`) - the three layers
   of the specification: node and block B-trees, blocks with their ciphers and
   compression, data and sub-node trees; heap-on-node, BTH, property and table
   contexts; the store, folders, messages, recipients and attachments. Pages
   and blocks are read on demand through small LRU caches, so a multi-gigabyte
   OST opens in milliseconds and memory stays flat.
2. **MSG** (`mailexlib/cfb.py`, `msgfile.py`) - a compound-file reader (FAT,
   DIFAT, mini-FAT, the directory tree) and the `.msg` property streams on top
   of it. Messages share the same property-based base class as PST messages,
   so bodies, senders and dates are handled once.
3. **MIME** (`mailexlib/sources.py`) - MBOX, Maildir, Thunderbird and Apple
   Mail layouts, EML/EMLX trees, built on the standard library's `email`
   package, with the original bytes kept for passthrough export.

`mailexlib/synth.py` is the mirror image: small PST and compound-file *writers*
used only by the self-test, so every reader is exercised end to end on any
machine without shipping anybody's real mailbox. The self-test builds a
mailbox as a PST in all three encryption modes, then as EML, EMLX, MBOX, a
Thunderbird folder, an Apple Mail package, a Maildir and `.msg` files, and
checks the same messages come back from each.

The readers were also verified against real files: the EDRM Enron PST
(Unicode, compressible encryption), several Outlook 2016 OST files (4K-page,
compressed blocks) from the `pst-extractor` project's test data, and the
`msg-extractor` project's sample `.msg` files - every message, recipient and
attachment in them opens and exports.

---

## Files

| File | Purpose |
|---|---|
| `mailex.py` | Entry point from source (window, or CLI sub-commands) |
| `mailex_app.py` | Entry point for the frozen builds |
| `mailexlib/ndb.py` | PST storage layer: header, B-trees, blocks, ciphers, data and sub-node trees |
| `mailexlib/ltp.py` | Heap-on-node, BTH, property and table contexts |
| `mailexlib/mapi.py` | Property tags, types, code pages, value decoding |
| `mailexlib/message.py` | PST store, folders, messages, recipients, attachments; the shared message base class |
| `mailexlib/cfb.py` | Compound file (OLE2) reader |
| `mailexlib/msgfile.py` | Outlook .msg on top of it |
| `mailexlib/sources.py` | MBOX, Maildir, Thunderbird, Apple Mail, EML/EMLX/MSG trees; opening and detection |
| `mailexlib/rtf.py` | Compressed RTF, RTF to text, HTML de-encapsulation, HTML to text |
| `mailexlib/export.py` | EML, MBOX, HTML, text and attachment exporters; the bulk runner |
| `mailexlib/pdfout.py` | PDF rendering (ReportLab) |
| `mailexlib/ui.py` | The window |
| `mailexlib/theme.py` | Shared light/dark theme (identical across all of my apps) |
| `mailexlib/paths.py` | App folder, settings, default save folder |
| `mailexlib/synth.py` | Test-only PST and compound-file writers |
| `mailexlib/selftest.py` | The self-test |
| `mailexlib/cli.py` | Command-line handling |
| `mailexlib/_crypt_tables.py` | The three 256-byte cipher tables from the PST specification |

Python 3.9 to 3.14. Dependencies: `reportlab` and `pillow` (for PDF output
only - everything else is the standard library).

## Licence

MIT No Attribution - see `LICENSE`.
