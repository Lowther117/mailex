"""Mailbox statistics over listing rows - no message is opened for them.

Everything comes from what the folder listings already carry (date, sender,
size, flags, class, folder), so the tables appear at once even for a mailbox
of a hundred thousand messages. Attachment counts and sizes are the one thing
a listing does not know; they are filled in from whatever the caller can
supply (the window's background cache, or the command line opening the
messages that have attachments) and the table says how much of the mailbox
that covered.

The output is a list of Table objects, rendered three ways: plain text for
the terminal, one CSV per table in a folder, or a single self-contained HTML
page.
"""
from __future__ import annotations

import csv
import datetime as _dt
import html as _html
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import mapi
from .filters import is_contact_class, is_mail_class

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass
class Table:
    key: str                      # file-name-safe id, e.g. "senders"
    title: str
    columns: List[str]
    rows: List[list] = field(default_factory=list)
    note: str = ""                # a line under the title (coverage caveats and the like)
    bar_column: Optional[int] = None   # index of the numeric column to draw a bar for


@dataclass
class Stats:
    tables: List[Table]
    total: int
    label: str = ""

    def get(self, key: str) -> Optional[Table]:
        return next((t for t in self.tables if t.key == key), None)


def _size(n: float) -> str:
    from .export import human_size
    return human_size(int(n or 0))


def sender_address(row) -> str:
    """The best e-mail address the listing has for the sender, or ''."""
    r = getattr(row, "row", None) or {}
    for pid in (mapi.PR_SENDER_SMTP, mapi.PR_SENT_REPRESENTING_SMTP, mapi.PR_SENDER_EMAIL, mapi.PR_SENT_REPRESENTING_EMAIL):
        v = r.get(pid)
        if isinstance(v, str) and "@" in v:
            return v.strip().lower()
    m = _EMAIL_RE.search(getattr(row, "sender", "") or "")
    return m.group(0).lower() if m else ""


def item_kind(mc: str) -> str:
    u = (mc or "IPM.Note").upper()
    if is_mail_class(u):
        return "e-mail"
    if is_contact_class(u):
        return "contact"
    if u.startswith("IPM.APPOINTMENT") or u.startswith("IPM.SCHEDULE"):
        return "appointment / meeting"
    if u.startswith("IPM.TASK"):
        return "task"
    if u.startswith("IPM.STICKYNOTE"):
        return "note"
    if u.startswith("IPM.ACTIVITY"):
        return "journal"
    if u.startswith("IPM.DISTLIST"):
        return "distribution list"
    return "other"


AttInfoFn = Callable[[object], Optional[Sequence[Tuple[str, int]]]]   # row -> [(name, size)] or None


def compute(rows: Iterable, label: str = "", att_info: Optional[AttInfoFn] = None, top: int = 20) -> Stats:
    rows = list(rows)
    total = len(rows)
    tables: List[Table] = []
    from .export import local

    # -- overview
    dated = [local(r.date) for r in rows if r.date]
    size_total = sum(r.size or 0 for r in rows)
    unread = sum(1 for r in rows if not r.read)
    with_att = sum(1 for r in rows if r.has_attachments)
    kinds = Counter(item_kind(r.message_class) for r in rows)
    files = {r.pst.name for r in rows}
    folders = {r.folder.path_str for r in rows}
    ov = Table("overview", "Overview", ["Measure", "Value"])
    ov.rows.append(["Messages / items", f"{total:,}"])
    ov.rows.append(["Files", f"{len(files):,}" + (": " + ", ".join(sorted(files)) if len(files) <= 5 else "")])
    ov.rows.append(["Folders with items", f"{len(folders):,}"])
    if dated:
        ov.rows.append(["Earliest", min(dated).strftime("%Y-%m-%d %H:%M")])
        ov.rows.append(["Latest", max(dated).strftime("%Y-%m-%d %H:%M")])
    ov.rows.append(["Undated", f"{total - len(dated):,}"])
    ov.rows.append(["Total size (as the listing reports it)", _size(size_total)])
    ov.rows.append(["Average size", _size(size_total / total) if total else "0 B"])
    ov.rows.append(["Read / unread", f"{total - unread:,} / {unread:,}"])
    ov.rows.append(["With attachments", f"{with_att:,}"])
    for k in ("e-mail", "contact", "appointment / meeting", "task", "note", "journal", "distribution list", "other"):
        if kinds.get(k):
            ov.rows.append([f"Items: {k}", f"{kinds[k]:,}"])
    tables.append(ov)

    # -- per year / per month
    by_year: Dict[int, List[int]] = defaultdict(lambda: [0, 0])
    by_month: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        d = local(r.date)
        if not d:
            continue
        y = by_year[d.year]
        y[0] += 1
        y[1] += r.size or 0
        m = by_month[d.strftime("%Y-%m")]
        m[0] += 1
        m[1] += r.size or 0
    ty = Table("years", "Messages per year", ["Year", "Messages", "Size"], bar_column=1)
    for y in sorted(by_year):
        ty.rows.append([str(y), by_year[y][0], _size(by_year[y][1])])
    if total - len(dated):
        ty.rows.append(["(undated)", total - len(dated), ""])
    tables.append(ty)
    tm = Table("months", "Messages per month", ["Month", "Messages", "Size"], bar_column=1)
    for m in sorted(by_month):
        tm.rows.append([m, by_month[m][0], _size(by_month[m][1])])
    tables.append(tm)

    # -- senders and domains
    senders: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    domains: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    no_addr = 0
    for r in rows:
        if not is_mail_class(r.message_class):
            continue
        s = (r.sender or "").strip() or "(no sender)"
        senders[s][0] += 1
        senders[s][1] += r.size or 0
        addr = sender_address(r)
        if addr:
            dom = addr.rsplit("@", 1)[1]
            domains[dom][0] += 1
            domains[dom][1] += r.size or 0
        else:
            no_addr += 1
    ts = Table("senders", f"Top {top} senders", ["Sender", "Messages", "Size"], bar_column=1)
    for s, (n, sz) in sorted(senders.items(), key=lambda kv: (-kv[1][0], kv[0]))[:top]:
        ts.rows.append([s, n, _size(sz)])
    ts.note = f"{len(senders):,} distinct senders across the e-mail items"
    tables.append(ts)
    td = Table("domains", f"Top {top} sender domains", ["Domain", "Messages", "Size"], bar_column=1)
    for d, (n, sz) in sorted(domains.items(), key=lambda kv: (-kv[1][0], kv[0]))[:top]:
        td.rows.append([d, n, _size(sz)])
    if no_addr:
        td.note = (f"{no_addr:,} e-mail item{'s' if no_addr != 1 else ''} had no address in the listing "
                   "(a PST folder table only names the sender; the address is inside the message)")
    tables.append(td)

    # -- folders
    per_folder: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        key = f"{r.pst.name} / {r.folder.path_str}" if len(files) > 1 else r.folder.path_str
        f = per_folder[key]
        f[0] += 1
        f[1] += r.size or 0
        f[2] += 0 if r.read else 1
    tf = Table("folders", "Per folder", ["Folder", "Messages", "Size", "Unread"], bar_column=1)
    for k in sorted(per_folder):
        n, sz, un = per_folder[k]
        tf.rows.append([k, n, _size(sz), un])
    tables.append(tf)

    # -- attachments
    ta = Table("attachments", "Attachments", ["Measure", "Value"])
    ta.rows.append(["Messages with attachments", f"{with_att:,}"])
    if att_info is not None:
        known = 0
        count = 0
        bytes_ = 0
        by_ext: Counter = Counter()
        for r in rows:
            if not r.has_attachments:
                continue
            info = att_info(r)
            if info is None:
                continue
            known += 1
            for name, sz in info:
                count += 1
                bytes_ += sz or 0
                by_ext[(os.path.splitext(name)[1].lstrip(".").lower() or "(none)")] += 1
        ta.rows.append(["Attachments counted", f"{count:,}"])
        ta.rows.append(["Total attachment size", _size(bytes_)])
        if by_ext:
            ta.rows.append(["Most common types", ", ".join(f"{e} ({n:,})" for e, n in by_ext.most_common(8))])
        if known < with_att:
            ta.note = (f"Counts cover {known:,} of the {with_att:,} messages with attachments - the rest have not "
                       "been read yet")
        else:
            ta.note = "Counts cover every message with attachments"
    else:
        ta.note = "Attachment counts and sizes need the messages read; the listing only knows which messages have some"
    tables.append(ta)

    # -- largest
    tl = Table("largest", f"{top} largest messages", ["Date", "From", "Subject", "Folder", "Size"])
    for r in sorted(rows, key=lambda r: -(r.size or 0))[:top]:
        d = local(r.date)
        tl.rows.append([d.strftime("%Y-%m-%d %H:%M") if d else "", r.sender, r.subject or "(no subject)",
                        r.folder.path_str, _size(r.size)])
    tables.append(tl)

    # -- classes
    tc = Table("classes", "Item classes", ["Message class", "Items"], bar_column=1)
    for mc, n in Counter((r.message_class or "IPM.Note") for r in rows).most_common():
        tc.rows.append([mc, n])
    tables.append(tc)
    return Stats(tables, total, label)


# -- rendering -------------------------------------------------------------------
def _bar(n: float, biggest: float, width: int = 30) -> str:
    if not biggest:
        return ""
    k = int(round(width * float(n) / biggest))
    return "#" * k


def render_text(stats: Stats) -> str:
    out: List[str] = []
    if stats.label:
        out.append(stats.label)
        out.append("")
    for t in stats.tables:
        out.append(t.title)
        out.append("-" * len(t.title))
        if t.note:
            out.append(t.note)
        if not t.rows:
            out.append("(nothing)")
            out.append("")
            continue
        cols = list(t.columns)
        rows = [[_cell(c) for c in r] for r in t.rows]
        if t.bar_column is not None:
            biggest = max((float(r[t.bar_column]) for r in t.rows if isinstance(r[t.bar_column], (int, float))), default=0)
            cols = cols + [""]
            rows = [r + [_bar(orig[t.bar_column], biggest) if isinstance(orig[t.bar_column], (int, float)) else ""]
                    for r, orig in zip(rows, t.rows)]
        widths = [min(60, max(len(str(cols[i])), max(len(r[i]) for r in rows))) for i in range(len(cols))]
        numeric = [all(isinstance(orig[i], (int, float)) for orig in t.rows) if i < len(t.columns) else False
                   for i in range(len(cols))]

        def fmt(vals):
            cells = []
            for i, v in enumerate(vals):
                v = v[:widths[i]] if len(v) > widths[i] else v
                cells.append(v.rjust(widths[i]) if numeric[i] else v.ljust(widths[i]))
            return "  ".join(cells).rstrip()
        out.append(fmt([str(c) for c in cols]))
        for r in rows:
            out.append(fmt(r))
        out.append("")
    return "\n".join(out) + "\n"


def _cell(v) -> str:
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.1f}"
    return str(v)


def write_csv_dir(stats: Stats, folder: str) -> List[str]:
    """One CSV per table, numbered so they sort in reading order. Returns the paths."""
    os.makedirs(folder, exist_ok=True)
    paths = []
    for i, t in enumerate(stats.tables, 1):
        path = os.path.join(folder, f"{i:02d}-{t.key}.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(t.columns)
            for r in t.rows:
                w.writerow(r)
        paths.append(path)
    return paths


_CSS = """
body{font-family:Segoe UI,Helvetica,Arial,sans-serif;margin:0;background:#f4f5f7;color:#111}
h1{font-size:20px;margin:0;padding:18px 24px;background:#fff;border-bottom:1px solid #d8dbe0}
.sub{padding:0 24px 10px;background:#fff;color:#555;font-size:13px;border-bottom:1px solid #d8dbe0}
section{background:#fff;margin:16px 24px;padding:14px 18px;border:1px solid #d8dbe0}
h2{font-size:15px;margin:0 0 6px}.note{color:#555;font-size:12px;margin:0 0 8px}
table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;color:#555;font-weight:600;padding:4px 10px 4px 0;border-bottom:1px solid #d8dbe0}
td{padding:3px 10px 3px 0;vertical-align:top;border-bottom:1px solid #eef0f3}td.n{text-align:right;white-space:nowrap}
.bar{display:inline-block;height:10px;background:#1f6feb;vertical-align:middle;border-radius:2px}
td.b{width:32%}
"""


def render_html(stats: Stats) -> str:
    parts = [f"<!doctype html><html><head><meta charset=\"utf-8\"><title>Mailbox statistics</title><style>{_CSS}</style></head><body>",
             "<h1>Mailbox statistics</h1>",
             f"<div class=\"sub\">{_html.escape(stats.label)} &middot; generated {_dt.datetime.now():%Y-%m-%d %H:%M} by Mailex</div>"]
    for t in stats.tables:
        parts.append(f"<section><h2>{_html.escape(t.title)}</h2>")
        if t.note:
            parts.append(f"<p class=\"note\">{_html.escape(t.note)}</p>")
        heads = "".join(f"<th>{_html.escape(str(c))}</th>" for c in t.columns)
        if t.bar_column is not None:
            heads += "<th></th>"
        parts.append(f"<table><tr>{heads}</tr>")
        biggest = 0.0
        if t.bar_column is not None:
            biggest = max((float(r[t.bar_column]) for r in t.rows if isinstance(r[t.bar_column], (int, float))), default=0.0)
        for r in t.rows:
            cells = []
            for i, v in enumerate(r):
                cls = " class=\"n\"" if isinstance(v, (int, float)) else ""
                cells.append(f"<td{cls}>{_html.escape(_cell(v))}</td>")
            if t.bar_column is not None:
                v = r[t.bar_column]
                pct = (100.0 * float(v) / biggest) if (biggest and isinstance(v, (int, float))) else 0
                cells.append(f"<td class=\"b\"><span class=\"bar\" style=\"width:{pct:.1f}%\"></span></td>")
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("</table></section>")
    parts.append("</body></html>")
    return "".join(parts)
