# Mailex

Open any mailbox, export it any way. Mailex reads the formats mail ends up
stored in - Outlook **PST** and **OST** files, **MBOX** (including Thunderbird
profiles and Apple Mail's exported mailboxes), **Maildir**, and loose **EML**,
**EMLX** and Outlook **MSG** files - without Outlook, MAPI or any mail client
installed. Point it at one file or at a folder full of them, browse and preview,
then export what you select - a handful of messages or an entire mailbox - as
**EML**, **MBOX**, **PDF**, **HTML**, **plain text**, **vCard** for the
contacts, or just the **attachments**. Or drag messages straight out of the
list into a folder. A **content index** makes every message *and every
attachment* - Word, Excel, PowerPoint, PDF, ZIP and the rest - searchable in
milliseconds across all your mailboxes at once. Windows and macOS, one window,
dark mode by default.

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
| `python mailex.py` | Any platform, if you already have Python 3.9+ with tkinter and the requirements installed (`pip install -r requirements.txt`; `tkinterdnd2` is optional - without it everything works except drag and drop). |
| `Mailex selftest` | Builds synthetic mailboxes in every supported format, reads them back, exports every output format, indexes and searches them, then writes `mailex-selftest.txt`. The build scripts run this and refuse to claim success if anything fails. |

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
bulk route. Dropping a file or folder onto the window does the same. An OST
still in use by Outlook may refuse to open until Outlook is closed; copy the
file somewhere first if you need to.

## Using it

**Browse.** Each opened source appears in the left pane with its folder tree
and message counts. Click a folder to list its messages. Tick *Subfolders* to
include everything beneath it, *E-mail only* to hide calendar, contact, task
and note items; click a column heading to sort. A single click previews the
message - headers, body and attachments - and any attachment can be saved on
its own from the preview. Contacts preview as a card (name, company, e-mail
addresses, phones, addresses, notes) rather than as raw text. Double-clicking
a message saves it as `.eml`.

**Filter.** The row under the toolbar narrows the list without re-reading the
folder, and whatever it leaves listed is what *Export selected…* takes when
nothing is selected:

| Control | What it does |
|---|---|
| *Search* | Every word you type must appear in the subject, sender or recipients. |
| *Search bodies too* | ... or in the message text. Bodies are read in the background (the status line counts them off, "Reading bodies 1,240 / 38,000…") and the list fills in as matches turn up; the first 64 KB of each body is kept in memory for the rest of the session so the next search is instant. Opening a new file starts that cache afresh. |
| *From* / *To* | A year (`2019`), a month (`2019-03`) or a day (`2019-03-15`), in local time as the list shows it. *From* is the start of that period and *To* its end, so `2019` to `2019` is the whole year. Undated items only show when both are blank. A date that cannot be read gets a red border and is ignored. |
| *Has attachments* | Only messages with attachments. |
| The box beside it | Narrow that to *Documents* (PDF, Word, Excel, PowerPoint, OpenDocument, text…), *Images*, *Archives* or *Other files*. This needs the attachment names, which are read in the background like bodies. Inline pictures - signature logos, embedded images - do not count. |
| *Contacts* | Only contact items. |
| *Clear* | Back to the plain listing. |

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

**Drag and drop.** Select messages and drag them out of the list onto the
desktop, a Finder or Explorer window, or any folder: they land as `.eml` files
(contacts as `.vcf`), named `date time subject` like an export. The files are
written to a temporary folder the moment the drag starts and cleared when
Mailex quits, so dragging a thousand messages would be a mistake - a single
drag takes up to 200 and the status line says so; use *Export selected…*
beyond that. Dropping a mailbox file or folder onto the window opens it. Both
directions come from the `tkinterdnd2` package, which the build scripts and
`requirements.txt` include; running from a bare Python without it, the window
simply behaves as before - `pip install tkinterdnd2` turns it on.

### Formats

| Format | What you get |
|---|---|
| **EML** | One `.eml` per message. Messages that arrived as real RFC 822 bytes (MBOX, EML, Maildir) are written out **byte for byte**. PST and MSG messages are rebuilt into standards-compliant MIME: the original transport headers where the file kept them, text and HTML bodies, attachments embedded, inline images wired up by Content-ID, forwarded messages nested as `message/rfc822`. Opens in Outlook, Apple Mail, Thunderbird, and anything else that reads mail. |
| **MBOX** | One mailbox file per folder (or one for the lot), for importing into Thunderbird or Apple Mail. Same passthrough rule as EML. |
| **PDF** | One PDF per message with a header block and the body text, or - tick the option - every message in one combined PDF with a page per message. A system font with wide Unicode coverage is used when one is present (Segoe UI or Arial on Windows, Arial Unicode on macOS), so accented and most non-Latin text prints as text (CJK needs a font that has it - Arial Unicode on a Mac does, Segoe UI on Windows does not). Attachments are saved in a folder beside each PDF. |
| **HTML** | A standalone web page per message: header table, the original HTML body with inline images embedded as data URIs (so the page works on its own), links to the attachments saved beside it. |
| **Plain text** | Headers and body text, attachments saved beside it. Bodies that only exist as HTML or RTF are converted. |
| **Attachments only** | Just the files, one folder per message (or pooled). Forwarded messages come out as `.eml`. The dialog can limit this to certain extensions (`pdf, docx, xlsx`), to one kind (documents / images / archives / other), and can skip inline pictures - the signature logos and embedded images that otherwise swamp an attachment dump. |
| **Contacts (vCard + CSV)** | Contact items only; everything else in the selection is skipped and counted as such. One `.vcf` per contact (vCard 3.0: name, organisation, title, phones, e-mail addresses, postal addresses, birthday, web pages, categories, notes - lines folded at 75 octets, commas and semicolons escaped), plus `contacts.vcf` with every card in one file and `contacts.csv` with every field in columns, both at the top of the export folder. Apple Contacts, Outlook and Google Contacts import `contacts.vcf` directly. |

Contacts are not second-class in the other formats either: whenever an export
in any format meets a contact item, it writes the `.vcf` beside that item's
normal output and adds it to `contacts.vcf` and `contacts.csv` at the export
root. Untick *Contacts: also write a .vcf…* in the dialog (or pass
`--no-contacts-vcard`) if you want the old behaviour of a text body only.

Every export also writes `index.csv` and `index.json` at the top of the
export folder listing each message, where it came from and what it was written
as, and `export-log.txt` if anything could not be exported (typically
attachments that were links to files on the original computer, which the PST
never contained, or a file whose named-property map could not be read, in
which case its contacts export without e-mail addresses).

By default the source's own folder tree is recreated under the export folder,
with one folder per source when several are exported at once, and file names
are `date time subject` (in local time, as the mail client would show it) so
they sort chronologically. Untick *Keep the folder structure* to put everything
in one place. Exports go to your Downloads folder unless you choose somewhere
else, or set a default under *File > Default save folder*.

### Extras

- **Statistics** (View menu, Ctrl+T) sums up the listed folder or everything
  open, from the listings alone so it is instant: messages per year and per
  month with a bar, the top 20 senders and top 20 sender domains, per-folder
  counts and sizes, the 20 largest messages, read/unread, and how many items
  are e-mail, contacts, appointments, tasks or notes. Attachment counts and
  sizes need the messages read - *Read attachment details…* does that for the
  ones that have attachments (or the attachment-type filter will already have)
  and the table says how many it covers. *Save as CSV…* writes one CSV per
  table into a folder you choose (`01-overview.csv`, `02-years.csv`, …);
  *Save as HTML…* writes a single self-contained page.
- **Property inspector** (View menu, Ctrl+I) lists every MAPI property of a
  PST or MSG message - or every header of a MIME one - with its tag, type and
  value, for when you need to know exactly what the file says rather than what
  a mail client shows. Named properties (0x8000 and up) are resolved through
  the file's own name-to-id map, so a contact's `0x8083` shows as
  `PidLidEmail1EmailAddress`, a meeting's `0x8208` as `PidLidLocation`, and
  the common Outlook sets (Address, Common, Appointment, Meeting, Task, Log,
  Note) are named; anything else shows its property set and id.
- **Orphaned messages** (View menu, PST/OST only) scans a file's node index
  for messages that no folder lists any more - things that were deleted but
  whose data is still in the file - and adds them as a pseudo-folder you can
  export from.
- Ctrl+D switches between dark and light.

### The content index

The filter row searches the listing you are looking at. The **index** searches
everything: the full text of every message in every mailbox you have indexed,
and the contents of their attachments - like [Findex](https://github.com/Lowther117/findex)
does for files on disk, with the same query syntax.

- **Index > Index the open mailboxes now** (Ctrl+Shift+I) reads each message
  once - subject, people, body, and every attachment it can get text out of:
  Word/Excel/PowerPoint (`.docx`/`.xlsx`/`.pptx`), OpenDocument, **PDF**, RTF,
  HTML and plain text, saved `.eml`/`.msg` messages, the members of **ZIP**
  archives one level down, and the readable strings of old binary `.doc`/`.xls`
  files. Forwarded (embedded) messages are indexed to any depth. It all goes
  into one SQLite FTS5 database, `mailex-index.db`, beside the app (or wherever
  *Index location…* points). A progress window shows where it is and Cancel
  keeps what has been done so far.
- **Incremental.** A mailbox whose size and modification time have not changed
  is skipped outright; one that has changed only has its *new* messages read,
  and messages that have vanished are dropped. Tick *Index mailboxes
  automatically when they are opened* and the index keeps itself current.
- **Search index** in the filter row (Ctrl+Shift+F) switches the search box to
  the index. Results arrive as you type, the last word matching as a prefix,
  ranked by relevance with subject matches first. They list like any other
  messages - preview, select, drag out, export, statistics all work on them -
  and a *Matched text* column shows where the words were found: `body:`,
  `attachment:`, `subject:`, `sender:`, with the matched words in [brackets].
  A hit from a mailbox that is not open opens it for you. The date, attachment
  and contact filters still narrow the hits; clicking a folder returns to the
  normal listing.
- **Query syntax** (Index > Search syntax…):

  | | |
  |---|---|
  | `invoice 2023` | every word, in any field |
  | `"exact phrase"` | words in that order |
  | `from:bob` `to:alice` | sender / recipients (name or address) |
  | `subject:renewal` `body:total` | one field only |
  | `att:pdf` `att:"q4 report"` | attachment names **and contents** |
  | `file:xlsx` | attachment names only |
  | `in:inbox` `source:work.pst` | folder path / mailbox contains |
  | `has:att` | only messages with attachments |
  | `after:2023-06` `before:2024` | date window (year, year-month or full date) |
  | `!spam` `-newsletter` | must not contain |

  Accents are folded (`cafe` finds *café*), matching is case-insensitive, and
  the index tokeniser is Unicode-aware, so non-Latin text is searchable too.
- **PDFs** are read with a built-in reader that handles the text streams of
  normal PDFs (Flate / ASCII85 / ASCIIHex encoded, literal and hex strings).
  The optional **PyMuPDF** package does much better on complex layouts and
  subset-font PDFs; the build and run scripts install it when a wheel exists
  for the Python in use and carry on without it when not (`Index > What is
  indexed…` says which reader is in use). Scanned images are not OCR'd.
- **Index > What is indexed…** lists every mailbox in the index with its
  message count and whether it is open, missing or partially indexed; *Take the
  selected mailbox out of the index*, *Forget mailboxes whose files are gone*
  and *Clear the whole index…* do what they say.

---

## Command line

The same engine works without the window, which is the way to script it or
run it on a server:

```
Mailex export SOURCE... [-f eml|mbox|pdf|html|txt|attachments|vcf] [-o DIR]
              [--folder "Inbox/Projects"] [--search TEXT] [--search-body]
              [--since DATE] [--until DATE] [--has-attachments]
              [--attachment-type documents|images|archives|other]
              [--email-only] [--flat] [--no-attachments] [--single-pdf]
              [--att-ext pdf,docx] [--skip-inline-images] [--no-contacts-vcard]
Mailex stats SOURCE... [--folder PATH] [--csv DIR] [--html FILE] [--quick]
Mailex list SOURCE...          folder tree with message counts
Mailex info SOURCE...          what kind of mailbox it is
Mailex index SOURCE... [--force] [--db FILE]        add mailboxes to the content index
Mailex index [--stats] [--prune] [--clear] [--remove SOURCE...]
Mailex find QUERY [-n N] [--json] [--export DIR -f FMT]   search the index, optionally export the hits
Mailex selftest [FILE]         self-test, optionally exercising a real file too
```

`SOURCE` is any file or folder from the table above; folders are worked out
the same way as *Open folder*. The filters are the window's, with the same
rules: `--since`/`--until` take `2019`, `2019-03` or `2019-03-15` and mean the
start and end of that period in local time; `--search` needs every word in
the subject, sender or recipients, `--search-body` extends that to the message
text (which means opening every message, so it is slower); `--attachment-type`
opens the messages that have attachments to look at their names, and with
`-f attachments` also limits which files are saved. `stats` prints the same
tables the window shows and opens the messages with attachments for their
counts and sizes unless `--quick` is given. `index` and `find` use the same
index file as the window (or `--db` names another), so an index built
overnight on a server with `Mailex index /mail/*.pst` is what the window
searches in the morning, and `Mailex find 'from:bob att:invoice after:2024'
--export ~/hits -f pdf` pulls matching messages straight out of whichever
mailboxes they came from. From source, `python mailex.py` takes the same
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
  and note items appear too. Contacts are read properly - the standard MAPI
  contact properties plus the e-mail addresses, file-as name, IM address and
  categories that live in *named* properties - and export as vCard and CSV.
  Calendar, task and note items export with whatever text body they carry.
  Tick *E-mail only* to hide them all, *Contacts* to see only contacts.
- **Named properties**: the name-to-id map of a PST (node 0x61) and of a
  `.msg` (the `__nameid_version1.0` storage) is decoded, so properties above
  0x8000 can be looked up by property set and id and are labelled in the
  inspector. A file whose map is missing or damaged still opens; its contacts
  then export without e-mail addresses and `export-log.txt` says so.
- **MSG**: Unicode and ANSI string storage, multi-valued properties, the
  properties stream at every level (message, embedded message, recipient,
  attachment).

Things it does not do: write PST or MSG files, interpret calendar recurrence
patterns (they are exported as text), read Outlook for Mac `.olm` archives, or
recover from a badly damaged PST beyond the orphan scan.

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
checks the same messages come back from each. Both writers emit a real
name-to-id map - numbered differently in the PST and the `.msg` on purpose -
and two contact items, so the named-property decoding and the vCard/CSV
output are proven end to end; the filters, the date parsing, the vCard folding
and escaping, the statistics tables and the command-line flags are checked
directly as well.

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
| `mailexlib/namedprops.py` | The name-to-id map of a PST or .msg; named-property lookup and naming |
| `mailexlib/contacts.py` | Contact items: MAPI properties to one shape, then vCard, CSV and readable text |
| `mailexlib/filters.py` | The listing filters (text, body, dates, attachments, contacts) shared by window and CLI |
| `mailexlib/stats.py` | Mailbox statistics tables and their text, CSV and HTML renderings |
| `mailexlib/indexer.py` | The content index: SQLite FTS5 schema, incremental indexing, query parsing and search, hits back to rows |
| `mailexlib/extract.py` | Text out of attachments: Office, OpenDocument, PDF, RTF, HTML, text, .eml/.msg, ZIP, legacy Office |
| `mailexlib/dnd.py` | Drag and drop in both directions through tkinterdnd2, optional |
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
only), `tkinterdnd2` (for drag and drop only, and optional) and `pymupdf`
(optional, better PDF text for the index; `requirements-optional.txt`) -
everything else, the SQLite FTS5 index included, is the standard library.

## Licence

MIT No Attribution - see `LICENSE`.
