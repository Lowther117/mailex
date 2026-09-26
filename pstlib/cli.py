"""Command-line entry: GUI by default, plus export / list / info / selftest sub-commands."""
from __future__ import annotations

import argparse
import os
import sys
from typing import List

from .paths import APP, VERSION, default_save_dir

USAGE = f"""{APP} {VERSION}

  pst_exporter                              open the window
  pst_exporter FILE.pst [FILE.ost ...]      open the window with these files loaded
  pst_exporter export SOURCE... [options]   export without the window (SOURCE = files or folders)
  pst_exporter list FILE                    print the folder tree with message counts
  pst_exporter info FILE                    print what kind of file it is
  pst_exporter selftest [FILE ...]          build synthetic PSTs, read them, export every format

export options:
  -f, --format FMT      eml (default), mbox, pdf, html, txt, attachments
  -o, --out DIR         destination (default: <Downloads>/PST export)
  --folder PATH         only this folder (and its subfolders), e.g. "Inbox" or "Inbox/Projects"
  --search TEXT         only messages whose subject, sender or recipients contain TEXT
  --email-only          skip calendar, contact, task and note items
  --flat                do not recreate the folder structure
  --no-attachments      leave attachments out
  --single-pdf          PDF: everything in one document
"""


def _find_sources(sources: List[str]) -> List[str]:
    out = []
    for s in sources:
        if os.path.isdir(s):
            for base, _d, files in os.walk(s):
                for fn in sorted(files):
                    if fn.lower().endswith((".pst", ".ost")):
                        out.append(os.path.join(base, fn))
        elif os.path.isfile(s):
            out.append(s)
        else:
            print(f"not found: {s}", file=sys.stderr)
    return out


def cmd_export(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="pst_exporter export", add_help=True)
    ap.add_argument("sources", nargs="+")
    ap.add_argument("-f", "--format", default="eml", choices=["eml", "mbox", "pdf", "html", "txt", "attachments"])
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--folder", default="")
    ap.add_argument("--search", default="")
    ap.add_argument("--email-only", action="store_true")
    ap.add_argument("--flat", action="store_true")
    ap.add_argument("--no-attachments", action="store_true")
    ap.add_argument("--single-pdf", action="store_true")
    a = ap.parse_args(argv)
    from .export import ExportOptions, Exporter
    from .message import PSTFile

    files = _find_sources(a.sources)
    if not files:
        print("nothing to export", file=sys.stderr)
        return 2
    out = a.out or os.path.join(default_save_dir(), "PST export")
    psts = []
    rows = []
    for f in files:
        try:
            pst = PSTFile(f)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot open {f}: {exc}", file=sys.stderr)
            continue
        psts.append(pst)
        for w in pst.warnings:
            print(f"{pst.name}: {w}", file=sys.stderr)
        roots = [pst.root]
        if a.folder:
            want = [p.strip().lower() for p in a.folder.replace("\\", "/").split("/") if p.strip()]
            roots = [fo for fo in pst.root.walk() if [p.lower() for p in fo.path[-len(want):]] == want]
            if not roots:
                print(f"{pst.name}: no folder called {a.folder!r}", file=sys.stderr)
        seen = set()
        for r in roots:
            for row in pst.all_message_rows(r):
                if row.nid not in seen:
                    seen.add(row.nid)
                    rows.append(row)
    if a.search:
        terms = a.search.lower().split()
        rows = [r for r in rows if all(t in f"{r.subject} {r.sender} {r.to}".lower() for t in terms)]
    print(f"{len(rows)} messages from {len(psts)} file(s) -> {out} as {a.format}")
    opts = ExportOptions(fmt=a.format, out_dir=out, mirror_folders=not a.flat, include_attachments=not a.no_attachments,
                         pdf_single_file=a.single_pdf, only_email=a.email_only)
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
    if res.errors:
        print(f"{len(res.errors)} errors - see export-log.txt in the output folder")
        for e in res.errors[:10]:
            print("  " + e)
    return 0 if not res.failed else 1


def cmd_list(argv: List[str]) -> int:
    from .message import PSTFile
    if not argv:
        print("usage: pst_exporter list FILE", file=sys.stderr)
        return 2
    rc = 0
    for f in _find_sources(argv):
        try:
            pst = PSTFile(f)
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
    from .message import PSTFile
    if not argv:
        print("usage: pst_exporter info FILE", file=sys.stderr)
        return 2
    for f in _find_sources(argv):
        try:
            pst = PSTFile(f)
        except Exception as exc:  # noqa: BLE001
            print(f"{f}: {exc}")
            continue
        print(f"{f}\n  {pst.description}\n  store name: {pst.store_name or '(none)'}\n  size: {pst.ndb.size:,} bytes")
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
    if argv and argv[0].lower() == "list":
        return cmd_list(argv[1:])
    if argv and argv[0].lower() == "info":
        return cmd_info(argv[1:])
    if argv and argv[0].lower() in ("-v", "--version", "version"):
        print(f"{APP} {VERSION}")
        return 0
    paths = [a for a in argv if os.path.isfile(a)]
    unknown = [a for a in argv if not os.path.isfile(a)]
    if unknown:
        print(USAGE)
        print(f"unknown argument(s): {' '.join(unknown)}", file=sys.stderr)
        return 2
    from .ui import launch
    launch(paths or None)
    return 0
