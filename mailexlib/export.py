"""Exporters: EML, MBOX, PDF, HTML, TXT, attachments and a CSV/JSON index.

Everything works on a list of MessageRow objects (any mix of files and
folders), so "export the selection", "export this folder" and "export every
file I opened" are all the same call.
"""
from __future__ import annotations

import base64
import csv
import datetime as _dt
import html as _html
import json
import mailbox
import mimetypes
import os
import re
import threading
from dataclasses import dataclass, field
from email import policy as _policy
from email.message import EmailMessage
from email.parser import Parser
from email.utils import format_datetime, formataddr
from urllib.parse import quote
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import mapi, rtf
from .message import Attachment, Message, MessageRow
from .ndb import PSTError

FORMATS = {
    "eml": "EML - one .eml file per message (opens in Outlook, Apple Mail, Thunderbird)",
    "mbox": "MBOX - one mailbox file per folder (Thunderbird, Apple Mail import)",
    "pdf": "PDF - one PDF per message (or one combined PDF)",
    "html": "HTML - one web page per message",
    "txt": "Plain text - one .txt per message",
    "attachments": "Attachments only - just the files that were attached",
}

_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def sanitize(name: str, maxlen: int = 120, maxbytes: int = 200) -> str:
    """Make a string safe as a file or folder name on Windows and macOS.

    Length is capped in characters AND in UTF-8 bytes (APFS and ext4 allow 255
    bytes per name, so a CJK or emoji subject must be cut shorter than a Latin
    one); the extension, if there is one, is kept.
    """
    name = (name or "").replace("\x00", "")
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    stem, ext = os.path.splitext(name)
    if len(ext) > 12 or " " in ext:
        stem, ext = name, ""
    if len(stem) > maxlen:
        stem = stem[:maxlen]
    while len((stem + ext).encode("utf-8")) > maxbytes and len(stem) > 1:
        stem = stem[:-1]
    name = (stem.rstrip(" .") + ext) if stem.strip(" .") else ext.lstrip(".")
    if not name:
        name = "untitled"
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    return name


def fs_path(path: str) -> str:
    """The path to hand to open()/makedirs: long-path safe on Windows."""
    if os.name == "nt":
        ap = os.path.abspath(path)
        if len(ap) > 240 and not ap.startswith("\\\\?\\"):
            return "\\\\?\\" + ap
        return ap
    return path


def unique_path(path: str, taken: Optional[set] = None) -> str:
    """Add ' (2)', ' (3)'... until the path is free (on disk and in `taken`)."""
    base, ext = os.path.splitext(path)
    cand = path
    n = 2
    key = os.path.normcase(cand)
    while os.path.exists(fs_path(cand)) or (taken is not None and key in taken):
        cand = f"{base} ({n}){ext}"
        key = os.path.normcase(cand)
        n += 1
    if taken is not None:
        taken.add(key)
    return cand


def local(d: Optional[_dt.datetime]) -> Optional[_dt.datetime]:
    """Outlook shows local time; so do we, in names and on the page."""
    if d is None:
        return None
    try:
        return d.astimezone()
    except (ValueError, OverflowError, OSError):
        return d


def message_basename(msg: Message, row: Optional[MessageRow] = None) -> str:
    d = local(msg.date or (row.date if row else None))
    stamp = d.strftime("%Y-%m-%d %H%M%S") if d else "undated"
    subj = msg.subject or (row.subject if row else "") or "(no subject)"
    return sanitize(f"{stamp} {subj}", 140)


def _guess_mime(att: Attachment) -> Tuple[str, str]:
    mt = (att.mime_type or "").strip().lower()
    if "/" in mt and " " not in mt:
        a, b = mt.split("/", 1)
        if a in ("multipart", "message"):
            # an S/MIME blob or a raw message tagged this way cannot be a leaf part
            return "application", "octet-stream"
        return a, b
    guess, _ = mimetypes.guess_type(att.filename)
    if guess:
        a, b = guess.split("/", 1)
        return a, b
    return "application", "octet-stream"


def _fmt(name: str, addr: str) -> str:
    try:
        return formataddr((name, addr))
    except UnicodeEncodeError:
        # non-ASCII in the address itself (EAI): write it plainly
        n = name.replace('"', "'")
        return f'"{n}" <{addr}>' if n else addr


def _addr(name: str, addr: str) -> str:
    name = (name or "").strip().replace("\r", " ").replace("\n", " ")
    addr = (addr or "").strip()
    if addr and "@" in addr:
        if name and name.lower() != addr.lower():
            return _fmt(name, addr)
        return addr
    if name:
        # no routable address: keep the name, mark the address as unknown
        return _fmt(name, "unknown@invalid")
    if addr:
        return _fmt(addr, "unknown@invalid")
    return ""


def _recipient_header(msg: Message, kind: str, fallback_display: str) -> str:
    recips = msg.recipients_of(kind)
    if recips:
        parts = [_addr(r.name, r.address) for r in recips]
        return ", ".join(p for p in parts if p)
    if fallback_display.strip():
        names = [n.strip() for n in re.split(r";", fallback_display) if n.strip()]
        return ", ".join(_addr(n, "") for n in names)
    return ""


_HEADERS_WE_SET = {"from", "to", "cc", "bcc", "subject", "date", "message-id", "in-reply-to", "references",
                   "content-type", "content-transfer-encoding", "mime-version", "content-disposition",
                   "content-id", "content-length", "content-class"}


def build_eml(msg: Message, include_attachments: bool = True, depth: int = 0) -> EmailMessage:
    """Build a standards-compliant MIME message from a PST message."""
    em = EmailMessage(policy=_policy.SMTP)

    # Original transport headers first (Received:, X-Mailer, Thread-Index...)
    raw_headers = msg.transport_headers
    if raw_headers.strip():
        try:
            parsed = Parser(policy=_policy.compat32).parsestr(raw_headers.replace("\r\n", "\n"), headersonly=True)
            for k, v in parsed.items():
                # only real header names survive; mangled header blocks (common in
                # converted archives) produce things like "13" or "2.0" as names
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,60}", k) or k.lower() in _HEADERS_WE_SET:
                    continue
                try:
                    em[k] = re.sub(r"\s*\n\s*", " ", str(v)).strip()
                except Exception:  # noqa: BLE001 - bad header, drop it
                    continue
        except Exception:  # noqa: BLE001
            pass

    def put(k, v):
        if v:
            try:
                em[k] = v
            except Exception:  # noqa: BLE001
                em.add_header(k, re.sub(r"[\r\n]+", " ", str(v)))

    put("From", _addr(msg.sender_name, msg.sender_email))
    put("To", _recipient_header(msg, "To", msg.display_to))
    put("Cc", _recipient_header(msg, "Cc", msg.display_cc))
    put("Bcc", _recipient_header(msg, "Bcc", msg.display_bcc))
    put("Subject", msg.subject)
    d = msg.sent_date or msg.date
    if d:
        put("Date", format_datetime(d))
    mid = msg.internet_message_id
    if mid:
        if not mid.startswith("<"):
            mid = f"<{mid}>"
        put("Message-ID", mid)
    put("In-Reply-To", msg.in_reply_to)
    put("References", msg.references)
    put("X-Mailex-Message-Class", msg.message_class)
    if msg.folder is not None and depth == 0:
        put("X-Mailex-Folder", msg.folder.path_str)
    if msg.flags & mapi.MSGFLAG_UNSENT:
        put("X-Unsent", "1")
    imp = msg.importance
    if imp == 2:
        put("Importance", "High")
        put("X-Priority", "1")
    elif imp == 0:
        put("Importance", "Low")
        put("X-Priority", "5")

    text = msg.body_text
    html = msg.best_html()
    if not text.strip() and not html.strip():
        rtf_bytes = msg.body_rtf
        if rtf_bytes:
            text = rtf.rtf_to_text(rtf_bytes)
    if not text.strip() and html.strip():
        text = rtf.html_to_text(html)

    em.set_content(text or "", subtype="plain", charset="utf-8")
    inline: List[Attachment] = []
    others: List[Attachment] = []
    if include_attachments:
        for att in msg.attachments():
            if att.is_embedded_message or att.method in (mapi.ATTACH_BY_REFERENCE, mapi.ATTACH_BY_REF_ONLY,
                                                          mapi.ATTACH_BY_REF_RESOLVE):
                others.append(att)
            elif att.content_id and html and (f"cid:{att.content_id}" in html):
                inline.append(att)
            else:
                others.append(att)
    if html.strip():
        em.add_alternative(html, subtype="html", charset="utf-8")
        if inline:
            html_part = em.get_payload()[-1]
            for att in inline:
                data = att.data
                if data is None:
                    others.append(att)
                    continue
                a, b = _guess_mime(att)
                html_part.add_related(data, maintype=a, subtype=b, cid=f"<{att.content_id}>",
                                      filename=att.filename, disposition="inline")
    for att in others:
        if att.is_embedded_message:
            sub = att.embedded_message
            if sub is not None and depth < 8:
                try:
                    sub_em = build_eml(sub, include_attachments, depth + 1)
                    em.add_attachment(sub_em, filename=sanitize(att.filename) if att.filename.lower().endswith(".eml") else None)
                    continue
                except (PSTError, Exception) as exc:  # noqa: BLE001
                    msg.warnings.append(f"embedded message {att.filename!r}: {exc}")
        data = att.data
        if data is None:
            if att.method in (mapi.ATTACH_BY_REFERENCE, mapi.ATTACH_BY_REF_ONLY, mapi.ATTACH_BY_REF_RESOLVE):
                msg.warnings.append(f"attachment {att.filename!r} was a link to a file, not stored in the PST")
            else:
                msg.warnings.append(f"attachment {att.filename!r} had no data ({att.error or 'empty'})")
            continue
        a, b = _guess_mime(att)
        em.add_attachment(data, maintype=a, subtype=b, filename=att.filename)
    return em


def eml_bytes(msg: Message, include_attachments: bool = True) -> bytes:
    """The message as an .eml. Sources that had real RFC 822 bytes (MBOX, EML,
    Maildir) are passed through untouched; PST and MSG messages are rebuilt."""
    if include_attachments:
        raw = msg.raw_eml() if hasattr(msg, "raw_eml") else None
        if raw:
            return raw
    return build_eml(msg, include_attachments).as_bytes()


# ---------------------------------------------------------------------------
# text / html renderings
# ---------------------------------------------------------------------------
def header_lines(msg: Message) -> List[Tuple[str, str]]:
    out = [("From", msg.sender), ]
    to = ", ".join(r.formatted() for r in msg.recipients_of("To")) or msg.display_to
    cc = ", ".join(r.formatted() for r in msg.recipients_of("Cc")) or msg.display_cc
    bcc = ", ".join(r.formatted() for r in msg.recipients_of("Bcc")) or msg.display_bcc
    out.append(("To", to))
    if cc:
        out.append(("Cc", cc))
    if bcc:
        out.append(("Bcc", bcc))
    out.append(("Subject", msg.subject))
    d = local(msg.date)
    out.append(("Date", d.strftime("%a, %d %b %Y %H:%M:%S %z") if d else ""))
    if msg.folder is not None:
        out.append(("Folder", msg.folder.path_str))
    atts = [a for a in msg.attachments() if not a.hidden]
    if atts:
        out.append(("Attachments", "; ".join(f"{a.filename} ({human_size(a.size or len(a.data or b''))})" for a in atts)))
    return out


def human_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def render_txt(msg: Message) -> str:
    lines = [f"{k}: {v}" for k, v in header_lines(msg)]
    body = msg.best_text()
    return "\n".join(lines) + "\n\n" + body.replace("\r\n", "\n").replace("\r", "\n") + "\n"


_CSS = """
body{font-family:Segoe UI,Helvetica,Arial,sans-serif;margin:0;background:#f4f5f7;color:#111}
.hdr{background:#fff;border-bottom:1px solid #d8dbe0;padding:14px 20px}
.hdr table{border-collapse:collapse}.hdr th{text-align:left;padding:2px 12px 2px 0;color:#555;font-weight:600;vertical-align:top;white-space:nowrap}
.hdr td{padding:2px 0;vertical-align:top}.hdr h1{font-size:18px;margin:0 0 8px}
.body{background:#fff;margin:14px 20px;padding:16px;border:1px solid #d8dbe0}
pre{white-space:pre-wrap;word-wrap:break-word;font-family:Consolas,Menlo,monospace;font-size:13px}
.atts a{display:inline-block;margin:2px 8px 2px 0}
"""


def render_html(msg: Message, attachment_links: Optional[Dict[int, str]] = None, embed_images: bool = True) -> str:
    """A standalone HTML page. cid: images are inlined as data: URIs."""
    html = msg.best_html()
    if html.strip():
        body = html
        if embed_images:
            for att in msg.attachments():
                if att.content_id and f"cid:{att.content_id}" in body:
                    data = att.data
                    if data:
                        a, b = _guess_mime(att)
                        uri = f"data:{a}/{b};base64,{base64.b64encode(data).decode('ascii')}"
                        body = body.replace(f"cid:{att.content_id}", uri)
        # strip an outer document wrapper so it sits inside ours
        m = re.search(r"<body[^>]*>(.*)</body>", body, re.I | re.S)
        inner = m.group(1) if m else re.sub(r"</?(html|head|body)[^>]*>", "", body, flags=re.I)
        # keep any <style> from the original head
        styles = "".join(re.findall(r"<style[^>]*>.*?</style>", body, re.I | re.S)) if m else ""
        body_html = styles + inner
    else:
        body_html = "<pre>" + _html.escape(msg.best_text()) + "</pre>"
    rows = []
    for k, v in header_lines(msg):
        if k == "Attachments":
            continue
        rows.append(f"<tr><th>{_html.escape(k)}</th><td>{_html.escape(v)}</td></tr>")
    atts = [a for a in msg.attachments() if not a.hidden]
    att_html = ""
    if atts:
        items = []
        for a in atts:
            label = _html.escape(f"{a.filename} ({human_size(a.size or len(a.data or b''))})")
            if attachment_links and a.nid in attachment_links:
                href = quote(attachment_links[a.nid], safe="/")
                items.append(f'<a href="{_html.escape(href)}">{label}</a>')
            else:
                items.append(f"<span>{label}</span>")
        att_html = '<tr><th>Attachments</th><td class="atts">' + " ".join(items) + "</td></tr>"
    title = _html.escape(msg.subject or "(no subject)")
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{title}</title><style>{_CSS}</style></head>"
            f"<body><div class=\"hdr\"><h1>{title}</h1><table>{''.join(rows)}{att_html}</table></div>"
            f"<div class=\"body\">{body_html}</div></body></html>")


# ---------------------------------------------------------------------------
# the bulk exporter
# ---------------------------------------------------------------------------
@dataclass
class ExportOptions:
    fmt: str = "eml"
    out_dir: str = ""
    mirror_folders: bool = True          # recreate the PST folder tree under out_dir
    per_file_subfolder: bool = True      # one top-level folder per PST/OST when several are exported
    include_attachments: bool = True     # EML/MBOX: embed; HTML/PDF/TXT: save alongside
    attachments_subfolder: bool = True   # attachments-only: one folder per message
    pdf_single_file: bool = False        # PDF: everything in one document
    only_email: bool = False             # skip calendar/contact/task items
    write_index: bool = True             # index.csv + index.json at the export root
    embed_images: bool = True            # HTML: inline cid: images as data URIs


@dataclass
class ExportResult:
    exported: int = 0
    skipped: int = 0
    failed: int = 0
    files_written: int = 0
    attachments_written: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    index: List[dict] = field(default_factory=list)
    out_dir: str = ""
    cancelled: bool = False

    def summary(self) -> str:
        parts = [f"{self.exported} message{'s' if self.exported != 1 else ''} exported"]
        if self.attachments_written:
            parts.append(f"{self.attachments_written} attachments saved")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.cancelled:
            parts.append("cancelled")
        return ", ".join(parts)


ProgressFn = Callable[[int, int, str], None]


class Exporter:
    def __init__(self, options: ExportOptions, progress: Optional[ProgressFn] = None,
                 cancel: Optional[threading.Event] = None):
        self.o = options
        self.progress = progress or (lambda a, b, c: None)
        self.cancel = cancel or threading.Event()
        self.result = ExportResult(out_dir=options.out_dir)
        self._taken: set = set()
        self._mboxes: Dict[str, mailbox.mbox] = {}
        self._pdf = None

    # -- paths ----------------------------------------------------------------
    def _target_dir(self, row: MessageRow, multi_file: bool, create: bool = True) -> str:
        parts = [self.o.out_dir]
        if multi_file and self.o.per_file_subfolder:
            parts.append(sanitize(os.path.splitext(row.pst.name)[0]))
        if self.o.mirror_folders:
            path = row.folder.path
            # drop the store root and the "Top of ..." / IPM_SUBTREE wrappers, they add nothing
            path = [p for p in path[1:] if p.upper() not in ("IPM_SUBTREE", "ROOT - MAILBOX") and not p.startswith("Top of ")]
            parts.extend(sanitize(p, 80) for p in path)
        d = os.path.join(*parts)
        if create:
            os.makedirs(fs_path(d), exist_ok=True)
        return d

    # -- main loop ------------------------------------------------------------
    def run(self, rows: Iterable[MessageRow]) -> ExportResult:
        rows = list(rows)
        if self.o.only_email:
            rows = [r for r in rows if _is_mail_class(r.message_class)]
        total = len(rows)
        files = {id(r.pst) for r in rows}
        multi_file = len(files) > 1
        os.makedirs(self.o.out_dir, exist_ok=True)
        try:
            for i, row in enumerate(rows):
                if self.cancel.is_set():
                    self.result.cancelled = True
                    break
                label = row.subject or f"message 0x{row.nid:x}"
                self.progress(i, total, label)
                try:
                    self._export_one(row, multi_file)
                    self.result.exported += 1
                except Exception as exc:  # noqa: BLE001 - one bad message must not stop the batch
                    self.result.failed += 1
                    self.result.errors.append(f"{row.folder.path_str} / {label}: {type(exc).__name__}: {exc}")
            self.progress(total, total, "finishing")
        finally:
            self._finish()
        return self.result

    def _finish(self):
        for mb in self._mboxes.values():
            try:
                mb.flush()
                mb.close()
            except Exception:  # noqa: BLE001
                pass
        self._mboxes.clear()
        if self._pdf is not None:
            try:
                self._pdf.finish()
                self.result.files_written += 1
            except Exception as exc:  # noqa: BLE001
                self.result.errors.append(f"combined PDF: {exc}")
            self._pdf = None
        if self.o.write_index and self.result.index:
            try:
                self._write_index()
            except Exception as exc:  # noqa: BLE001
                self.result.errors.append(f"index: {exc}")
        if self.result.errors or self.result.warnings:
            try:
                with open(os.path.join(self.o.out_dir, "export-log.txt"), "w", encoding="utf-8") as fh:
                    fh.write(f"Mailex log - {_dt.datetime.now():%Y-%m-%d %H:%M}\n\n")
                    if self.result.errors:
                        fh.write("ERRORS\n" + "\n".join(self.result.errors) + "\n\n")
                    if self.result.warnings:
                        fh.write("WARNINGS\n" + "\n".join(self.result.warnings) + "\n")
            except Exception:  # noqa: BLE001
                pass

    def _write_index(self):
        cols = ["file", "folder", "date", "from", "to", "subject", "size", "attachments", "message_id", "message_class", "exported_as"]
        with open(os.path.join(self.o.out_dir, "index.csv"), "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for rec in self.result.index:
                w.writerow(rec)
        with open(os.path.join(self.o.out_dir, "index.json"), "w", encoding="utf-8") as fh:
            json.dump(self.result.index, fh, indent=1, ensure_ascii=False, default=str)

    def _record(self, msg: Message, row: MessageRow, path: str):
        self.result.index.append({
            "file": row.pst.name,
            "folder": row.folder.path_str,
            "date": msg.date.isoformat() if msg.date else "",
            "from": msg.sender,
            "to": ", ".join(r.formatted() for r in msg.recipients_of("To")) or msg.display_to,
            "subject": msg.subject,
            "size": msg.size,
            "attachments": len([a for a in msg.attachments() if not a.hidden]),
            "message_id": msg.internet_message_id,
            "message_class": msg.message_class,
            "exported_as": os.path.relpath(path, self.o.out_dir) if path else "",
        })
        for w in msg.warnings:
            self.result.warnings.append(f"{row.folder.path_str} / {msg.subject}: {w}")

    # -- per message ----------------------------------------------------------
    def _export_one(self, row: MessageRow, multi_file: bool):
        msg = row.open()
        fmt = self.o.fmt
        needs_dir = not (fmt == "mbox" or (fmt == "pdf" and self.o.pdf_single_file and not self.o.include_attachments))
        out_dir = self._target_dir(row, multi_file, create=needs_dir)
        base = message_basename(msg, row)
        if fmt == "eml":
            path = unique_path(os.path.join(out_dir, base + ".eml"), self._taken)
            data = eml_bytes(msg, self.o.include_attachments)
            with open(fs_path(path), "wb") as fh:
                fh.write(data)
            self.result.files_written += 1
        elif fmt == "mbox":
            name = sanitize(row.folder.name or "mailbox", 80) if self.o.mirror_folders else "mailbox"
            if self.o.mirror_folders:
                # the mbox sits in the parent folder, named after this folder
                parent = os.path.dirname(out_dir) if os.path.normpath(out_dir) != os.path.normpath(self.o.out_dir) else out_dir
                path = os.path.join(parent, name + ".mbox")
            else:
                path = os.path.join(out_dir, "mailbox.mbox")
            mb = self._mboxes.get(path)
            if mb is None:
                os.makedirs(fs_path(os.path.dirname(path)), exist_ok=True)
                mb = mailbox.mbox(fs_path(path), create=True)
                self._mboxes[path] = mb
                self.result.files_written += 1
            # mailbox rewrites "\n" as os.linesep, so hand it LF-only bytes or
            # Windows gets "\r\r\n" on every line
            raw = msg.raw_eml() if (self.o.include_attachments and hasattr(msg, "raw_eml")) else None
            if raw:
                mb.add(raw.replace(b"\r\n", b"\n"))
            else:
                em = build_eml(msg, self.o.include_attachments)
                mb.add(em.as_bytes(policy=em.policy.clone(linesep="\n")))
        elif fmt == "txt":
            path = unique_path(os.path.join(out_dir, base + ".txt"), self._taken)
            with open(fs_path(path), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render_txt(msg))
            self.result.files_written += 1
            if self.o.include_attachments:
                self._save_attachments(msg, os.path.splitext(path)[0] + " - attachments")
        elif fmt == "html":
            path = unique_path(os.path.join(out_dir, base + ".html"), self._taken)
            links = None
            if self.o.include_attachments:
                folder = os.path.splitext(path)[0] + " - attachments"
                saved = self._save_attachments(msg, folder)
                links = {nid: os.path.relpath(p, out_dir).replace(os.sep, "/") for nid, p in saved.items()}
            with open(fs_path(path), "w", encoding="utf-8") as fh:
                fh.write(render_html(msg, links, self.o.embed_images))
            self.result.files_written += 1
        elif fmt == "pdf":
            from . import pdfout
            if self.o.pdf_single_file:
                if self._pdf is None:
                    path = unique_path(os.path.join(self.o.out_dir, "messages.pdf"), self._taken)
                    self._pdf = pdfout.PdfBook(fs_path(path))
                self._pdf.add_message(msg)
                path = self._pdf.path
            else:
                path = unique_path(os.path.join(out_dir, base + ".pdf"), self._taken)
                pdfout.write_message_pdf(msg, fs_path(path))
                self.result.files_written += 1
            if self.o.include_attachments:
                folder = (os.path.splitext(path)[0] + " - attachments") if not self.o.pdf_single_file \
                    else os.path.join(out_dir, base + " - attachments")
                self._save_attachments(msg, folder)
        elif fmt == "attachments":
            folder = os.path.join(out_dir, base) if self.o.attachments_subfolder else out_dir
            saved = self._save_attachments(msg, folder)
            path = folder if saved else ""
        else:
            raise ValueError(f"unknown format {fmt!r}")
        self._record(msg, row, path)

    def _save_attachments(self, msg: Message, folder: str) -> Dict[int, str]:
        saved: Dict[int, str] = {}
        atts = [a for a in msg.attachments()]
        if not atts:
            return saved
        made = False
        for att in atts:
            if att.is_embedded_message:
                sub = att.embedded_message
                if sub is None:
                    continue
                if not made:
                    os.makedirs(fs_path(folder), exist_ok=True)
                    made = True
                name = sanitize(att.filename if att.filename.lower().endswith(".eml") else (sub.subject or "message") + ".eml")
                path = unique_path(os.path.join(folder, name), self._taken)
                try:
                    with open(fs_path(path), "wb") as fh:
                        fh.write(eml_bytes(sub, True))
                    saved[att.nid] = path
                    self.result.attachments_written += 1
                except Exception as exc:  # noqa: BLE001
                    msg.warnings.append(f"embedded message {name!r}: {exc}")
                continue
            data = att.data
            if data is None:
                if att.method in (mapi.ATTACH_BY_REFERENCE, mapi.ATTACH_BY_REF_ONLY, mapi.ATTACH_BY_REF_RESOLVE):
                    msg.warnings.append(f"attachment {att.filename!r} was a link to a file, not stored in the PST")
                elif att.error:
                    msg.warnings.append(f"attachment {att.filename!r}: {att.error}")
                continue
            if not made:
                os.makedirs(fs_path(folder), exist_ok=True)
                made = True
            path = unique_path(os.path.join(folder, sanitize(att.filename)), self._taken)
            with open(fs_path(path), "wb") as fh:
                fh.write(data)
            saved[att.nid] = path
            self.result.attachments_written += 1
        return saved


def _is_mail_class(mc: str) -> bool:
    mc = (mc or "IPM.Note").upper()
    return mc.startswith("IPM.NOTE") or mc.startswith("REPORT.") or mc == "IPM" or mc.startswith("IPM.POST")


def export(rows: Iterable[MessageRow], options: ExportOptions, progress: Optional[ProgressFn] = None,
           cancel: Optional[threading.Event] = None) -> ExportResult:
    return Exporter(options, progress, cancel).run(rows)
