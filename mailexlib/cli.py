"""Command-line entry: GUI by default, plus export / list / stats / info / selftest sub-commands."""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

from .paths import APP, VERSION, default_save_dir

USAGE = f"""{APP} {VERSION} - open any mailbox, export it any way

  mailex                                  open the window
  mailex SOURCE...                        open the window with these loaded
  mailex export SOURCE... [options]       export without the window
  mailex list SOURCE...                   print the folder tree with message counts
  mailex stats SOURCE... [options]        mailbox statistics as text, CSV or HTML
  mailex info SOURCE...                   print what kind of mailbox it is
  mailex selftest [FILE ...]              build synthetic mailboxes in every format, read and export them

SOURCE is a file - Outlook .pst / .ost, an .mbox, a single .eml / .emlx / .msg -
or a folder. A folder is worked out from what is in it: PST/OST files, MBOX
files (a Thunderbird Mail folder with its .sbd sub-folders, Apple Mail .mbox
packages), a Maildir (cur/new/tmp), or a tree of loose .eml / .msg / .emlx
files whose directories become the folders. Everything found is opened.

export options:
  -f, --format FMT      eml (default), mbox, pdf, html, txt, attachments, vcf
  -o, --out DIR         destination (default: <Downloads>/Mailex export)
  --folder PATH         only this folder (and its subfolders), e.g. "Inbox" or "Inbox/Projects"
  --search TEXT         only messages whose subject, sender or recipients contain every word of TEXT
  --search-body         ... or whose body text does (opens every message: slower)
  --since DATE          only messages dated on or after DATE  (2019, 2019-03 or 2019-03-15)
  --until DATE          only messages dated on or before DATE (the end of that year / month / day)
  --has-attachments     only messages with attachments
  --attachment-type T   only messages with an attachment of that kind: documents, images, archives, other
                        (opens the messages that have attachments; with -f attachments only those files are saved)
  --email-only          skip calendar, contact, task and note items
  --flat                do not recreate the folder structure
  --no-attachments      leave attachments out
  --single-pdf          PDF: everything in one document
  --att-ext EXT,EXT     attachments only: save just these extensions, e.g. pdf,docx,xlsx
  --skip-inline-images  attachments only: leave out inline pictures (signature logos and the like)
  --no-contacts-vcard   do not add a .vcf beside each exported contact (and contacts.vcf / contacts.csv)

stats options:
  --csv DIR             also write one CSV per table into DIR
  --html FILE           also write a single self-contained HTML page
  --folder PATH         only this folder (and its subfolders)
  --quick               do not open messages for attachment counts and sizes
"""



def _find_sources(sources: List[str]) -> List[Tuple[str, str]]:
    """(kind, path) for everything openable in the given files and folders."""
    from .sources import find_sources
    out: List[Tuple[str, str]] = []
    for s in sources:
        if os.path.isdir(s):
            found = find_sources(s)
            if not found:
                print(f"nothing openable under: {s}", file=sys.stderr)
            out.extend(found)
        elif os.path.isfile(s):
            out.append(("file", s))
        else:
            print(f"not found: {s}", file=sys.stderr)
    return out


def _open(kind: str, path: str):
    from .sources import open_found, open_source
    return open_source(path) if kind == "file" else open_found(kind, path)


def _collect_rows(sources: List[str], folder: str):
    """Open every source and gather the listing rows (optionally under one folder)."""
    files = _find_sources(sources)
    psts = []
    rows = []
    for kind, f in files:
        try:
            pst = _open(kind, f)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot open {f}: {exc}", file=sys.stderr)
            continue
        psts.append(pst)
        for w in pst.warnings:
            print(f"{pst.name}: {w}", file=sys.stderr)
        roots = [pst.root]
        if folder:
            want = [p.strip().lower() for p in folder.replace("\\", "/").split("/") if p.strip()]
            roots = [fo for fo in pst.root.walk() if [p.lower() for p in fo.path[-len(want):]] == want]
            if not roots:
                print(f"{pst.name}: no folder called {folder!r}", file=sys.stderr)
        seen = set()
        for r in roots:
            for row in pst.all_message_rows(r):
                if row.nid not in seen:
                    seen.add(row.nid)
                    rows.append(row)
    return psts, rows


def _add_filter_args(ap: argparse.ArgumentParser):
    from .filters import ATT_TYPES
    ap.add_argument("--search", default="")
    ap.add_argument("--search-body", action="store_true")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--has-attachments", action="store_true")
    ap.add_argument("--attachment-type", default="all", choices=list(ATT_TYPES))
    ap.add_argument("--email-only", action="store_true")


def _apply_filters(a, rows):
    from .filters import RowFilter, attachments_of, body_of, parse_date_bound
    since = parse_date_bound(a.since)
    until = parse_date_bound(a.until, end=True)
    if a.since and since is None:
        print(f"--since {a.since!r} is not a date I understand (use 2019, 2019-03 or 2019-03-15)", file=sys.stderr)
        return None
    if a.until and until is None:
        print(f"--until {a.until!r} is not a date I understand (use 2019, 2019-03 or 2019-03-15)", file=sys.stderr)
        return None
    flt = RowFilter(text=a.search, search_body=a.search_body, since=since, until=until,
                    has_attachments=a.has_attachments, attachment_type=a.attachment_type,
                    only_mail=getattr(a, "email_only", False))
    if not flt.active and not flt.needs_body:
        return rows
    return flt.apply(rows, body=body_of if flt.needs_body else None,
                     atts=attachments_of if flt.needs_attachment_names else None)


def cmd_export(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="mailex export", add_help=True)
    ap.add_argument("sources", nargs="+")
    ap.add_argument("-f", "--format", default="eml", choices=["eml", "mbox", "pdf", "html", "txt", "attachments", "vcf"])
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--folder", default="")
    _add_filter_args(ap)
    ap.add_argument("--flat", action="store_true")
    ap.add_argument("--no-attachments", action="store_true")
    ap.add_argument("--single-pdf", action="store_true")
    ap.add_argument("--att-ext", default="")
    ap.add_argument("--skip-inline-images", action="store_true")
    ap.add_argument("--no-contacts-vcard", action="store_true")
    a = ap.parse_args(argv)
    from .export import ExportOptions, Exporter
    from .filters import parse_extensions

    out = a.out or os.path.join(default_save_dir(), "Mail export")
    psts, rows = _collect_rows(a.sources, a.folder)
    if not psts:
        print("nothing to export", file=sys.stderr)
        return 2
    rows = _apply_filters(a, rows)
    if rows is None:
        return 2
    print(f"{len(rows)} messages from {len(psts)} file(s) -> {out} as {a.format}")
    opts = ExportOptions(fmt=a.format, out_dir=out, mirror_folders=not a.flat, include_attachments=not a.no_attachments,
                         pdf_single_file=a.single_pdf, only_email=a.email_only,
                         contacts_vcard=not a.no_contacts_vcard,
                         attachment_extensions=parse_extensions(a.att_ext) or None,
                         attachment_types={a.attachment_type} if a.attachment_type != "all" else None,
                         skip_inline_images=a.skip_inline_images)
    last = [-1]

    def progress(i, n, label):
        pct = int(i * 100 / n) if n else 100
        if pct // 5 != last[0]:
            last[0] = pct // 5
            print(f"  {pct:3d}%  {label[:70]}")

    res = Exporter(opts, progress).run(rows)
    for p in psts:
        p.close()
    print(res.summary())
    if res.warnings and not res.exported:
        for w in res.warnings[:3]:
            print("  " + w)
    if res.errors:
        print(f"{len(res.errors)} errors - see export-log.txt in the output folder")
        for e in res.errors[:10]:
            print("  " + e)
    return 0 if not res.failed else 1


def cmd_stats(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="mailex stats", add_help=True)
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--folder", default="")
    ap.add_argument("--csv", default="")
    ap.add_argument("--html", default="")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args(argv)
    from . import stats as _stats
    psts, rows = _collect_rows(a.sources, a.folder)
    if not psts:
        print("nothing to report on", file=sys.stderr)
        return 2

    def att_info(row):
        try:
            # the same rule as the window: inline pictures (signature logos) are not counted
            return [(x.filename, x.size or len(x.data or b"")) for x in row.open().attachments()
                    if not (x.hidden or x.is_inline)]
        except Exception:  # noqa: BLE001
            return None

    label = ", ".join(p.name for p in psts) + (f" / {a.folder}" if a.folder else "")
    st = _stats.compute(rows, label, att_info=None if a.quick else att_info)
    print(_stats.render_text(st))
    if a.csv:
        paths = _stats.write_csv_dir(st, a.csv)
        print(f"{len(paths)} CSV files written to {a.csv}")
    if a.html:
        with open(a.html, "w", encoding="utf-8") as fh:
            fh.write(_stats.render_html(st))
        print(f"HTML written to {a.html}")
    for p in psts:
        p.close()
    return 0


def cmd_list(argv: List[str]) -> int:
    if not argv:
        print("usage: list FILE_OR_FOLDER", file=sys.stderr)
        return 2
    rc = 0
    for kind, f in _find_sources(argv):
        try:
            pst = _open(kind, f)
        except Exception as exc:  # noqa: BLE001
            print(f"{f}: {exc}")
            rc = 1
            continue
        print(f"{pst.name}  [{pst.description}]")
        total = 0
        for folder in pst.root.walk():
            n = len(folder.messages())
            total += n
            depth = len(folder.path) - 1
            print(f"  {'  ' * depth}{folder.name or '(root)'}  ({n})" + (f"  ! {folder.error}" if folder.error else ""))
        print(f"  total: {total} messages")
        pst.close()
    return rc


def cmd_info(argv: List[str]) -> int:
    if not argv:
        print("usage: info FILE_OR_FOLDER", file=sys.stderr)
        return 2
    for kind, f in _find_sources(argv):
        try:
            pst = _open(kind, f)
        except Exception as exc:  # noqa: BLE001
            print(f"{f}: {exc}")
            continue
        print(f"{f}\n  {pst.description}\n  store name: {pst.store_name or '(none)'}\n  size: {pst.size:,} bytes")
        for w in pst.warnings:
            print(f"  warning: {w}")
        pst.close()
    return 0


def main(argv: List[str]) -> int:
    if argv and argv[0].lower() in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv and argv[0].lower() in ("selftest", "--selftest", "-t"):
        from .selftest import run
        return run(verbose=True, extra_files=[a for a in argv[1:] if os.path.isfile(a)])
    if argv and argv[0].lower() == "export":
        return cmd_export(argv[1:])
    if argv and argv[0].lower() == "stats":
        return cmd_stats(argv[1:])
    if argv and argv[0].lower() == "list":
        return cmd_list(argv[1:])
    if argv and argv[0].lower() == "info":
        return cmd_info(argv[1:])
    if argv and argv[0].lower() in ("-v", "--version", "version"):
        print(f"{APP} {VERSION}")
        return 0
    paths = [a for a in argv if os.path.exists(a)]
    unknown = [a for a in argv if not os.path.exists(a)]
    if unknown:
        print(USAGE)
        print(f"unknown argument(s): {' '.join(unknown)}", file=sys.stderr)
        return 2
    from .ui import launch
    launch(paths or None)
    return 0
