"""Mailbox sources other than PST: MBOX, EML, Maildir, Thunderbird, Apple Mail, .msg.

Every source produces the same three kinds of object the PST reader does - a
folder tree, cheap message rows for listing, and fully opened messages - so
the window, the preview and every exporter work on all of them unchanged.

  MboxSource        one mbox file (any of the mboxo/mboxrd/mboxcl flavours)
  MailDirSource     a directory of mbox files: Thunderbird's Mail/ and ImapMail/
                    folders with their .sbd sub-directories, or any folder of
                    .mbox files, including Apple Mail's "Name.mbox" packages
  MaildirSource     a Maildir (cur/new/tmp), with .Folder or nested sub-folders
  FilesSource       a directory tree of loose .eml / .msg / .emlx files; the
                    directories become the folders
  single files      a .eml, .emlx or .msg opened on its own
"""
from __future__ import annotations

import datetime as _dt
import email
import os
import re
from email import policy as _policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import mapi, rtf
from .message import Recipient

MAIL_EXTENSIONS = (".pst", ".ost", ".mbox", ".mbx", ".eml", ".emlx", ".msg")

FILE_TYPES = [
    ("All mailbox files", "*.pst *.ost *.mbox *.mbx *.eml *.emlx *.msg *.PST *.OST *.MBOX *.EML *.MSG"),
    ("Outlook data files (PST, OST)", "*.pst *.ost *.PST *.OST"),
    ("MBOX mailboxes", "*.mbox *.mbx *.MBOX"),
    ("E-mail messages (EML, EMLX)", "*.eml *.emlx *.EML"),
    ("Outlook messages (MSG)", "*.msg *.MSG"),
    ("All files", "*.*"),
]


# ---------------------------------------------------------------------------
# the common shapes
# ---------------------------------------------------------------------------
class MailSource:
    """What every opened thing looks like to the rest of the program."""

    kind = "generic"
    kind_label = "Mailbox"
    is_ost = False
    sender_from_message = False      # True when the listing must open messages to get the sender

    def __init__(self, path: str):
        self.path = path
        self.name = os.path.basename(path.rstrip("/\\")) or path
        self.store_name = ""
        self.warnings: List[str] = []
        self.root: "GenericFolder" = GenericFolder(self, 1, os.path.splitext(self.name)[0] or self.name)
        self._next_id = 2

    @property
    def description(self) -> str:
        return self.kind_label

    @property
    def size(self) -> int:
        try:
            if os.path.isdir(self.path):
                total = 0
                for base, _dirs, files in os.walk(self.path):
                    for fn in files:
                        try:
                            total += os.path.getsize(os.path.join(base, fn))
                        except OSError:
                            pass
                return total
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def all_message_rows(self, root: Optional["GenericFolder"] = None) -> Iterator["GenericRow"]:
        for f in (root or self.root).walk():
            yield from f.messages()

    def folders_with_mail(self):
        for f in self.root.walk():
            if f.messages():
                yield f

    def folder(self, nid: int):
        for f in self.root.walk():
            if f.nid == nid:
                return f
        return None


class GenericFolder:
    def __init__(self, source: MailSource, nid: int, name: str, parent: Optional["GenericFolder"] = None):
        self.pst = source
        self.source = source
        self.nid = nid
        self.name = name
        self.parent = parent
        self.content_count = 0
        self.unread_count = 0
        self.has_subfolders = False
        self.container_class = "IPM.Note"
        self.error: Optional[str] = None
        self._subfolders: List[GenericFolder] = []
        self._rows: Optional[List[GenericRow]] = None
        self._loader = None          # callable returning the rows, run once

    @property
    def path(self) -> List[str]:
        parts = []
        f: Optional[GenericFolder] = self
        while f is not None:
            parts.append(f.name)
            f = f.parent
        parts.reverse()
        return parts

    @property
    def path_str(self) -> str:
        return "/".join(self.path)

    def add_subfolder(self, name: str) -> "GenericFolder":
        sf = GenericFolder(self.source, self.source.new_id(), name, self)
        self._subfolders.append(sf)
        self.has_subfolders = True
        return sf

    def subfolders(self) -> List["GenericFolder"]:
        return self._subfolders

    def walk(self, _seen=None) -> Iterator["GenericFolder"]:
        yield self
        for sf in self._subfolders:
            yield from sf.walk()

    def messages(self) -> List["GenericRow"]:
        if self._rows is None:
            try:
                self._rows = list(self._loader()) if self._loader else []
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                self._rows = []
            self.content_count = len(self._rows)
            self.unread_count = sum(1 for r in self._rows if not r.read)
        return self._rows

    def total_messages(self) -> int:
        return sum(len(f.messages()) for f in self.walk())

    def __repr__(self):
        return f"GenericFolder({self.path_str!r})"


class GenericRow:
    """The listing entry: cheap to make, opens the full message on demand."""

    __slots__ = ("folder", "nid", "subject", "sender", "date", "size", "has_attachments", "message_class",
                 "to", "read", "importance", "row", "_filled", "_opener")

    def __init__(self, folder: GenericFolder, nid: int, opener, subject: str = "", sender: str = "",
                 date: Optional[_dt.datetime] = None, size: int = 0, has_attachments: bool = False,
                 to: str = "", read: bool = True, importance: int = 1, message_class: str = "IPM.Note"):
        self.folder = folder
        self.nid = nid
        self._opener = opener
        self.subject = subject
        self.sender = sender
        self.date = date
        self.size = size
        self.has_attachments = has_attachments
        self.to = to
        self.read = read
        self.importance = importance
        self.message_class = message_class
        self.row = {}
        self._filled = False

    @property
    def pst(self) -> MailSource:
        return self.folder.source

    @property
    def source(self) -> MailSource:
        return self.folder.source

    def open(self):
        return self._opener(self)

    def fill_from_message(self, force_sender: bool = False) -> None:
        try:
            m = self.open()
        except Exception:  # noqa: BLE001
            return
        if force_sender or not self.sender:
            self.sender = m.sender_name or m.sender_email or self.sender
        if not self.subject:
            self.subject = m.subject
        if not self.to:
            self.to = m.display_to
        if self.date is None:
            self.date = m.date
        if not self.has_attachments:
            self.has_attachments = m.has_attachments

    def __repr__(self):
        return f"GenericRow({self.subject!r})"


# ---------------------------------------------------------------------------
# a message that came from MIME bytes
# ---------------------------------------------------------------------------
def _hdr(value) -> str:
    """A header as clean text, whatever encoding games it plays."""
    if value is None:
        return ""
    try:
        s = str(value)
    except Exception:  # noqa: BLE001
        try:
            s = str(make_header(decode_header(str(value))))
        except Exception:  # noqa: BLE001
            s = repr(value)
    return re.sub(r"[\r\n]+", " ", s).replace("\x00", "").strip()


def _parse_date(value: str, fallback: Optional[_dt.datetime] = None) -> Optional[_dt.datetime]:
    if value:
        try:
            d = parsedate_to_datetime(value)
            if d is not None:
                if d.tzinfo is None:
                    d = d.replace(tzinfo=_dt.timezone.utc)
                return d
        except Exception:  # noqa: BLE001
            pass
    return fallback


def parse_bytes(raw: bytes) -> EmailMessage:
    try:
        return email.message_from_bytes(raw, policy=_policy.default)
    except Exception:  # noqa: BLE001
        # the compat32 policy tolerates almost anything
        return email.message_from_bytes(raw, policy=_policy.compat32)


class MimeAttachment:
    def __init__(self, message: "MimeMessage", part, index: int, inline: bool):
        self.message = message
        self.part = part
        self.nid = index
        self.error: Optional[str] = None
        self._inline = inline
        self._data: Optional[bytes] = None

    @property
    def method(self) -> int:
        return mapi.ATTACH_EMBEDDED_MSG if self.is_embedded_message else mapi.ATTACH_BY_VALUE

    @property
    def is_embedded_message(self) -> bool:
        return self.part.get_content_type() == "message/rfc822"

    @property
    def filename(self) -> str:
        try:
            name = self.part.get_filename()
        except Exception:  # noqa: BLE001
            name = None
        if name:
            return _hdr(name)
        if self.is_embedded_message:
            emb = self.embedded_message
            if emb is not None and emb.subject:
                return emb.subject + ".eml"
            return f"message-{self.nid}.eml"
        ct = self.part.get_content_type()
        ext = {"text/plain": ".txt", "text/html": ".html", "image/png": ".png", "image/jpeg": ".jpg",
               "image/gif": ".gif", "application/pdf": ".pdf", "text/calendar": ".ics"}.get(ct, "")
        return f"attachment-{self.nid}{ext}"

    @property
    def mime_type(self) -> str:
        return self.part.get_content_type()

    @property
    def content_id(self) -> str:
        cid = self.part.get("Content-ID") or ""
        return _hdr(cid).strip("<>")

    @property
    def hidden(self) -> bool:
        return self._inline

    @property
    def is_inline(self) -> bool:
        return self._inline or bool(self.content_id)

    @property
    def size(self) -> int:
        d = self.data
        return len(d) if d else 0

    @property
    def data(self) -> Optional[bytes]:
        if self.is_embedded_message:
            return None
        if self._data is None:
            try:
                d = self.part.get_payload(decode=True)
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                d = None
            if d is None:
                try:
                    d = self.part.get_content()
                    if isinstance(d, str):
                        d = d.encode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    d = b""
            self._data = d or b""
        return self._data

    @property
    def embedded_message(self) -> Optional["MimeMessage"]:
        if not self.is_embedded_message:
            return None
        payload = self.part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if inner is None:
            try:
                raw = self.part.get_payload(decode=True) or b""
                return MimeMessage(self.message.source, raw, folder=self.message.folder, embedded=True)
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                return None
        return MimeMessage(self.message.source, None, folder=self.message.folder, embedded=True, parsed=inner)

    def __repr__(self):
        return f"MimeAttachment({self.filename!r})"


class MimeMessage:
    """Implements the same interface as the PST Message, from RFC 822 bytes."""

    def __init__(self, source: MailSource, raw: Optional[bytes], folder=None, row=None, nid: int = 0,
                 embedded: bool = False, parsed=None, read: Optional[bool] = None,
                 mtime: Optional[_dt.datetime] = None):
        self.pst = source
        self.source = source
        self.folder = folder
        self.row = row
        self.nid = nid or (row.nid if row else 0)
        self.embedded = embedded
        self.raw = raw
        self.em = parsed if parsed is not None else parse_bytes(raw or b"")
        self.warnings: List[str] = []
        self._read = read
        self._mtime = mtime
        self._attachments: Optional[List[MimeAttachment]] = None
        self._recipients: Optional[List[Recipient]] = None
        self._bodies: Optional[Tuple[str, str]] = None
        self.cpid = None

    # -- headers ------------------------------------------------------------
    def header(self, name: str) -> str:
        try:
            return _hdr(self.em.get(name))
        except Exception:  # noqa: BLE001
            return ""

    @property
    def subject(self) -> str:
        return self.header("Subject")

    @property
    def message_class(self) -> str:
        return "IPM.Note"

    @property
    def is_email(self) -> bool:
        return True

    def _from(self) -> Tuple[str, str]:
        v = self.header("From") or self.header("Sender") or self.header("Return-Path")
        if not v:
            return "", ""
        pairs = getaddresses([v])
        if pairs:
            name, addr = pairs[0]
            return name.strip(), addr.strip()
        return v, ""

    @property
    def sender_name(self) -> str:
        return self._from()[0] or self._from()[1]

    @property
    def sender_email(self) -> str:
        return self._from()[1]

    @property
    def sender(self) -> str:
        name, addr = self._from()
        if addr and name and name.lower() != addr.lower():
            return f"{name} <{addr}>"
        return addr or name

    @property
    def date(self) -> Optional[_dt.datetime]:
        d = _parse_date(self.header("Date"))
        if d is None:
            for h in ("Delivery-Date", "X-Original-Date", "Resent-Date"):
                d = _parse_date(self.header(h))
                if d:
                    break
        if d is None:
            m = re.search(r"; *([A-Za-z]{3}, *\d{1,2} [A-Za-z]{3} \d{4} [\d:]+ *[+-]?\d*)", self.header("Received"))
            if m:
                d = _parse_date(m.group(1))
        return d or self._mtime

    @property
    def sent_date(self) -> Optional[_dt.datetime]:
        return _parse_date(self.header("Date")) or self.date

    def _addr_header(self, name: str) -> str:
        return self.header(name)

    @property
    def display_to(self) -> str:
        return "; ".join(r.name or r.address for r in self.recipients_of("To"))

    @property
    def display_cc(self) -> str:
        return "; ".join(r.name or r.address for r in self.recipients_of("Cc"))

    @property
    def display_bcc(self) -> str:
        return "; ".join(r.name or r.address for r in self.recipients_of("Bcc"))

    @property
    def internet_message_id(self) -> str:
        return self.header("Message-ID")

    @property
    def in_reply_to(self) -> str:
        return self.header("In-Reply-To")

    @property
    def references(self) -> str:
        return self.header("References")

    @property
    def transport_headers(self) -> str:
        try:
            items = self.em.items()
        except Exception:  # noqa: BLE001
            return ""
        return "\r\n".join(f"{k}: {_hdr(v)}" for k, v in items) + "\r\n"

    @property
    def flags(self) -> int:
        return (mapi.MSGFLAG_READ if self.read else 0) | (mapi.MSGFLAG_HASATTACH if self.has_attachments else 0)

    @property
    def read(self) -> bool:
        if self._read is not None:
            return self._read
        status = self.header("Status")
        if status:
            return "R" in status.upper()
        moz = self.header("X-Mozilla-Status")
        if moz:
            try:
                return bool(int(moz, 16) & 0x0001)
            except ValueError:
                pass
        return True

    @property
    def deleted(self) -> bool:
        """Thunderbird marks deleted-but-not-yet-compacted messages with a flag."""
        moz = self.header("X-Mozilla-Status")
        if moz:
            try:
                return bool(int(moz, 16) & 0x0008)
            except ValueError:
                pass
        return False

    @property
    def importance(self) -> int:
        imp = self.header("Importance").lower()
        if imp.startswith("high"):
            return 2
        if imp.startswith("low"):
            return 0
        pr = self.header("X-Priority")[:1]
        if pr in ("1", "2"):
            return 2
        if pr in ("4", "5"):
            return 0
        return 1

    @property
    def size(self) -> int:
        if self.raw is not None:
            return len(self.raw)
        try:
            return len(self.em.as_bytes())
        except Exception:  # noqa: BLE001
            return 0

    # -- bodies -------------------------------------------------------------
    def _inline_text_parts(self) -> Tuple[list, list]:
        """(plain parts, html parts) that are body text rather than attachments -
        Apple Mail splits a body around inline pictures, so there can be several."""
        plain, html = [], []
        if not self.em.is_multipart():
            ct = self.em.get_content_type()
            if ct == "text/html":
                html.append(self.em)
            elif ct.startswith("text/") and ct not in ("text/calendar", "text/x-vcard", "text/vcard"):
                plain.append(self.em)
            return plain, html
        try:
            parts = list(_leaf_parts(self.em))
        except Exception:  # noqa: BLE001
            return plain, html
        for part in parts:
            ct = part.get_content_type()
            if ct not in ("text/plain", "text/html"):
                continue
            if part.get_content_disposition() == "attachment" or part.get_filename():
                continue
            (html if ct == "text/html" else plain).append(part)
        return plain, html

    def _body_parts(self) -> Tuple[str, str]:
        if self._bodies is not None:
            return self._bodies
        plain, html_parts = self._inline_text_parts()
        text = "\n\n".join(t for t in (_part_text(p) for p in plain) if t)
        html = "\n".join(t for t in (_part_text(p) for p in html_parts) if t)
        self._bodies = (text or "", html or "")
        return self._bodies

    @property
    def body_text(self) -> str:
        return self._body_parts()[0].replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")

    @property
    def body_html(self) -> str:
        return self._body_parts()[1]

    @property
    def body_rtf(self) -> bytes:
        return b""

    def best_html(self) -> str:
        return self.body_html if self.body_html.strip() else ""

    def best_text(self) -> str:
        t = self.body_text
        if t.strip():
            return t
        h = self.body_html
        if h.strip():
            return rtf.html_to_text(h)
        return ""

    # -- sub-objects --------------------------------------------------------
    def recipients(self) -> List[Recipient]:
        if self._recipients is None:
            out = []
            for kind in ("To", "Cc", "Bcc"):
                vals = []
                try:
                    vals = [str(v) for v in self.em.get_all(kind, [])]
                except Exception:  # noqa: BLE001
                    pass
                if not vals:
                    v = self.header(kind)
                    vals = [v] if v else []
                for name, addr in getaddresses(vals):
                    if not (name or addr):
                        continue
                    out.append(Recipient.simple(kind, name, addr))
            self._recipients = out
        return self._recipients

    def recipients_of(self, kind: str) -> List[Recipient]:
        return [r for r in self.recipients() if r.kind == kind]

    def attachments(self) -> List[MimeAttachment]:
        if self._attachments is None:
            out: List[MimeAttachment] = []
            plain, html_parts = self._inline_text_parts()
            body_parts = set(id(p) for p in plain + html_parts)
            html = self.body_html
            i = 0
            try:
                parts = list(_leaf_parts(self.em))
            except Exception:  # noqa: BLE001
                parts = []
            for part in parts:
                if id(part) in body_parts:
                    continue
                i += 1
                cid = _hdr(part.get("Content-ID") or "").strip("<>")
                inline = bool(cid) and f"cid:{cid}" in html
                out.append(MimeAttachment(self, part, i, inline))
            self._attachments = out
        return self._attachments

    @property
    def has_attachments(self) -> bool:
        return any(not a.hidden for a in self.attachments())

    def all_properties(self) -> Iterator[tuple]:
        try:
            for k, v in self.em.items():
                yield None, k, None, _hdr(v)
        except Exception:  # noqa: BLE001
            return

    def raw_eml(self) -> Optional[bytes]:
        """The original bytes, so an EML export is byte-for-byte what came in."""
        if self.raw is not None:
            return self.raw
        try:
            return self.em.as_bytes()
        except Exception:  # noqa: BLE001
            return None

    def __repr__(self):
        return f"MimeMessage({self.subject!r})"


def _leaf_parts(msg):
    """Non-container parts, treating an embedded message/rfc822 as one leaf."""
    if not msg.is_multipart():
        return
    for part in msg.iter_parts():
        if part.get_content_type() == "message/rfc822":
            yield part
        elif part.is_multipart():
            yield from _leaf_parts(part)
        else:
            yield part


def _part_text(part) -> str:
    """Decoded text of a text part, surviving charsets Python has never heard of."""
    try:
        if hasattr(part, "get_content"):
            v = part.get_content()
            if isinstance(v, str):
                return v
    except Exception:  # noqa: BLE001 - odd charset, broken transfer encoding...
        pass
    return _payload_text(part)


def _payload_text(part) -> str:
    try:
        raw = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None:
        p = part.get_payload()
        return p if isinstance(p, str) else ""
    cs = (part.get_content_charset() or "utf-8").strip("\"' =3D")
    cs = {"iso-8859-8-i": "iso-8859-8", "x-user-defined": "latin-1", "unknown-8bit": "latin-1"}.get(cs.lower(), cs)
    for c in (cs, "utf-8", "cp1252"):
        try:
            return raw.decode(c)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", "replace")


def row_from_bytes(folder: GenericFolder, nid: int, raw: bytes, opener, read: Optional[bool] = None,
                   mtime: Optional[_dt.datetime] = None) -> GenericRow:
    """A listing row from the header block only (cheap for big mailboxes)."""
    head_end = raw.find(b"\r\n\r\n")
    n2 = raw.find(b"\n\n")
    if head_end < 0 or (0 <= n2 < head_end):
        head_end = n2
    head = raw[:head_end if head_end >= 0 else min(len(raw), 65536)]
    em = parse_bytes(head + b"\r\n\r\n")
    m = MimeMessage(folder.source, None, folder=folder, nid=nid, parsed=em, read=read, mtime=mtime)
    subject = m.subject
    if m.deleted:
        subject = "(deleted in Thunderbird) " + subject
    ct = m.header("Content-Type").lower()
    has_att = ("multipart/mixed" in ct) or ("multipart/related" in ct) or ("application/" in ct) or \
              ("name=" in ct and "multipart/alternative" not in ct)
    return GenericRow(folder, nid, opener, subject=subject, sender=m.sender_name or m.sender_email, date=m.date,
                      size=len(raw), has_attachments=has_att, to=m.display_to, read=m.read, importance=m.importance)


# ---------------------------------------------------------------------------
# MBOX
# ---------------------------------------------------------------------------
_NOT_MAIL_EXT = (".msf", ".dat", ".json", ".sqlite", ".txt", ".log", ".html", ".htm", ".xml", ".ini", ".db", ".plist",
                 ".jpg", ".jpeg", ".png", ".gif", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".mp3", ".mp4",
                 ".mov", ".py", ".js", ".css", ".exe", ".dll", ".app", ".pkg", ".dmg", ".iso", ".csv")


def looks_like_mbox(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(5)
        return head == b"From "
    except OSError:
        return False


def could_be_mbox(path: str) -> bool:
    """Only extension-less files (Thunderbird) and .mbox/.mbx are worth sniffing:
    reading every file to check would pull cloud-only files down from iCloud or OneDrive."""
    low = os.path.basename(path).lower()
    if low.startswith("."):
        return False
    if low.endswith((".mbox", ".mbx")):
        return True
    if "." in low:
        return False
    try:
        if os.path.getsize(path) == 0:
            return os.path.isdir(path + ".sbd")
    except OSError:
        return False
    return looks_like_mbox(path)


def is_compound_file(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False


class _MboxFolder:
    """One mbox file: an offset index built once, then a fresh read per message.

    No handle stays open (a Thunderbird profile can have hundreds of folders and
    macOS allows 256 open files), nothing is opened for writing, and the
    preview, listing and export threads never share a file position.
    """

    def __init__(self, source: MailSource, folder: GenericFolder, path: str):
        self.source = source
        self.folder = folder
        self.path = path
        self._index: Optional[List[Tuple[int, int, int]]] = None   # (envelope start, body start, end)

    def index(self) -> List[Tuple[int, int, int]]:
        if self._index is None:
            starts: List[Tuple[int, int]] = []
            with open(self.path, "rb") as fh:
                pos = 0
                prev_blank = True
                for line in fh:
                    if line.startswith(b"From ") and prev_blank:
                        starts.append((pos, pos + len(line)))
                    prev_blank = line in (b"\n", b"\r\n")
                    pos += len(line)
                size = pos
            out = []
            for i, (env, body) in enumerate(starts):
                end = starts[i + 1][0] if i + 1 < len(starts) else size
                out.append((env, body, end))
            self._index = out
        return self._index

    def _read(self, i: int, head_only: bool = False) -> bytes:
        _env, start, end = self.index()[i]
        length = end - start
        if head_only:
            length = min(length, 65536)
        with open(self.path, "rb") as fh:
            fh.seek(start)
            data = fh.read(length)
        if not head_only:
            # the blank separator line before the next message is not part of this one
            if data.endswith(b"\r\n\r\n"):
                data = data[:-2]
            elif data.endswith(b"\n\n"):
                data = data[:-1]
        return data

    def rows(self) -> Iterator[GenericRow]:
        for i in range(len(self.index())):
            nid = self.source.new_id()
            try:
                head = self._read(i, head_only=True)
            except OSError:
                continue
            _env, start, end = self.index()[i]

            def opener(row, i=i):
                return MimeMessage(self.source, self._read(i), folder=self.folder, row=row)

            row = row_from_bytes(self.folder, nid, head, opener)
            row.size = end - start
            yield row

    def close(self):
        pass


class MboxSource(MailSource):
    kind = "mbox"
    kind_label = "MBOX"

    def __init__(self, path: str):
        super().__init__(path)
        self.root.name = _mbox_display_name(path)
        self._boxes: List[_MboxFolder] = []
        box = _MboxFolder(self, self.root, path)
        self._boxes.append(box)
        self.root._loader = box.rows

    @property
    def description(self) -> str:
        return "MBOX mailbox file"

    def close(self):
        for b in self._boxes:
            b.close()


def _mbox_display_name(path: str) -> str:
    base = os.path.basename(path)
    if base.lower() in ("mbox", "mbox.txt") and path.lower().endswith((".mbox/mbox", ".mbox\\mbox")):
        base = os.path.basename(os.path.dirname(path))
    if base.lower().endswith((".mbox", ".mbx")):
        base = base[:-5] if base.lower().endswith(".mbox") else base[:-4]
    return base or "mailbox"


class ApplePackageSource(MailSource):
    """One Apple Mail `Name.mbox` directory, exported or live."""

    kind = "mbox"
    kind_label = "Apple Mail mailbox"

    def __init__(self, path: str):
        super().__init__(path)
        self.root.name = os.path.basename(path.rstrip("/\\"))[:-5] or "Mailbox"
        attach_apple_package(self, self.root, path)

    @property
    def description(self) -> str:
        return "Apple Mail mailbox" + (" (export)" if os.path.isfile(os.path.join(self.path, "mbox")) else "")


class MailDirSource(MailSource):
    """A directory of mbox files - Thunderbird's layout with .sbd sub-directories,
    or any folder that holds .mbox files (Apple Mail packages included)."""

    kind = "mboxdir"
    kind_label = "Mail folder"

    def __init__(self, path: str):
        super().__init__(path)
        self._boxes: List[_MboxFolder] = []
        self._build(path, self.root)

    @property
    def description(self) -> str:
        return f"folder of mailbox files ({len(self._boxes)} mailboxes)"

    def _attach_mbox(self, folder: GenericFolder, file_path: str):
        box = _MboxFolder(self, folder, file_path)
        self._boxes.append(box)
        folder._loader = box.rows

    def _build(self, d: str, folder: GenericFolder):
        try:
            entries = sorted(os.listdir(d), key=str.lower)
        except OSError as exc:
            folder.error = str(exc)
            return
        made: Dict[str, GenericFolder] = {}
        for e in entries:
            p = os.path.join(d, e)
            low = e.lower()
            if os.path.isdir(p):
                continue
            if not could_be_mbox(p):
                continue
            if not looks_like_mbox(p) and not low.endswith((".mbox", ".mbx")):
                continue
            if os.path.getsize(p) == 0 and not low.endswith((".mbox", ".mbx")):
                # Thunderbird keeps an empty file for a folder that only has sub-folders
                name = _mbox_display_name(p)
                made[name.lower()] = folder.add_subfolder(name)
                continue
            name = _mbox_display_name(p)
            sf = folder.add_subfolder(name)
            self._attach_mbox(sf, p)
            made[name.lower()] = sf
        for e in entries:
            p = os.path.join(d, e)
            if not os.path.isdir(p):
                continue
            low = e.lower()
            if low.endswith(".sbd"):
                name = e[:-4]
                parent = made.get(name.lower()) or folder.add_subfolder(name)
                self._build(p, parent)
            elif low.endswith(".mbox"):
                attach_apple_package(self, folder.add_subfolder(e[:-5]), p)
            elif low in ("cur", "new", "tmp") or is_maildir(p):
                continue
            else:
                sf = folder.add_subfolder(e)
                self._build(p, sf)
                if not sf.subfolders() and sf._loader is None:
                    folder._subfolders.remove(sf)

    def close(self):
        for b in self._boxes:
            b.close()


# ---------------------------------------------------------------------------
# Maildir
# ---------------------------------------------------------------------------
def is_maildir(d: str) -> bool:
    return os.path.isdir(os.path.join(d, "cur")) and (os.path.isdir(os.path.join(d, "new"))
                                                        or os.path.isdir(os.path.join(d, "tmp")))


_MAILDIR_FLAGS = re.compile(r"[:!;]2,([A-Za-z]*)")


class MaildirSource(MailSource):
    kind = "maildir"
    kind_label = "Maildir"

    def __init__(self, path: str):
        super().__init__(path)
        self.root.name = os.path.basename(path.rstrip("/\\")) or "Maildir"
        self._build(path, self.root)

    @property
    def description(self) -> str:
        return "Maildir"

    def _build(self, d: str, folder: GenericFolder):
        folder._loader = lambda d=d, folder=folder: self._rows(d, folder)
        try:
            entries = sorted(os.listdir(d), key=str.lower)
        except OSError:
            return
        for e in entries:
            p = os.path.join(d, e)
            if not os.path.isdir(p) or e in ("cur", "new", "tmp"):
                continue
            name = e[1:] if e.startswith(".") else e
            # Courier style ".Inbox.Sub" -> nested names
            target = folder
            for part in [x for x in name.split(".") if x]:
                existing = next((s_ for s_ in target.subfolders() if s_.name == part), None)
                target = existing or target.add_subfolder(part)
            if is_maildir(p):
                self._build(p, target)
            else:
                # a plain directory in between (Dovecot \Noselect parents): look beneath it
                self._build_plain(p, target)
                if not target.subfolders() and target._loader is None and target in folder._subfolders:
                    folder._subfolders.remove(target)

    def _build_plain(self, d: str, folder: GenericFolder):
        try:
            entries = sorted(os.listdir(d), key=str.lower)
        except OSError:
            return
        for e in entries:
            p = os.path.join(d, e)
            if not os.path.isdir(p):
                continue
            sf = folder.add_subfolder(e[1:] if e.startswith(".") else e)
            if is_maildir(p):
                self._build(p, sf)
            else:
                self._build_plain(p, sf)
                if not sf.subfolders() and sf._loader is None:
                    folder._subfolders.remove(sf)

    def _rows(self, d: str, folder: GenericFolder) -> Iterator[GenericRow]:
        for sub in ("cur", "new"):
            sd = os.path.join(d, sub)
            try:
                names = sorted(os.listdir(sd))
            except OSError:
                continue
            for fn in names:
                p = os.path.join(sd, fn)
                if not os.path.isfile(p):
                    continue
                fm = _MAILDIR_FLAGS.search(fn)
                flags = fm.group(1) if fm else ""
                read = "S" in flags if sub == "cur" else False
                if "T" in flags:
                    continue            # trashed: flagged for deletion
                try:
                    raw = _head_bytes(p)
                    size = os.path.getsize(p)
                    mtime = _dt.datetime.fromtimestamp(os.path.getmtime(p), _dt.timezone.utc)
                except OSError:
                    continue
                nid = self.new_id()

                def opener(row, p=p, read=read, mtime=mtime):
                    with open(p, "rb") as fh:
                        return MimeMessage(self, fh.read(), folder=row.folder, row=row, read=read, mtime=mtime)

                row = row_from_bytes(folder, nid, raw, opener, read=read, mtime=mtime)
                row.size = size
                yield row


# ---------------------------------------------------------------------------
# loose files: .eml, .emlx, .msg
# ---------------------------------------------------------------------------
def read_emlx(path: str) -> bytes:
    """Apple Mail .emlx: a byte count line, the message, then a property list."""
    with open(path, "rb") as fh:
        data = fh.read()
    nl = data.find(b"\n")
    if nl > 0:
        try:
            n = int(data[:nl].strip())
            return data[nl + 1:nl + 1 + n]
        except ValueError:
            pass
    return data


def open_message_file(source: MailSource, path: str, folder=None, row=None):
    low = path.lower()
    mtime = None
    try:
        mtime = _dt.datetime.fromtimestamp(os.path.getmtime(path), _dt.timezone.utc)
    except OSError:
        pass
    if low.endswith(".msg") or (not low.endswith((".eml", ".emlx")) and is_compound_file(path)):
        from .msgfile import MsgMessage
        return MsgMessage(source, path, folder=folder, row=row)
    if low.endswith(".emlx"):
        return MimeMessage(source, read_emlx(path), folder=folder, row=row, mtime=mtime)
    with open(path, "rb") as fh:
        return MimeMessage(source, fh.read(), folder=folder, row=row, mtime=mtime)


def _attach_emlx_dir(source: MailSource, folder: GenericFolder, d: str):
    folder._loader = lambda: _file_rows(source, folder, d, (".emlx",), recursive=True)


def attach_apple_package(source: MailSource, folder: GenericFolder, package: str):
    """An Apple Mail mailbox directory: either the export form (an `mbox` file
    inside) or the live form (V2-V10: <uuid>/Data/.../Messages/*.emlx, with
    child mailboxes as nested *.mbox directories)."""
    inner = os.path.join(package, "mbox")
    if os.path.isfile(inner):
        box = _MboxFolder(source, folder, inner)
        folder._loader = box.rows
    else:
        def rows(folder=folder, package=package):
            paths = []
            for base, dirs, files in os.walk(package):
                dirs[:] = [d for d in dirs if not d.lower().endswith(".mbox")]
                for fn in files:
                    if fn.lower().endswith((".emlx", ".partial.emlx")):
                        paths.append(os.path.join(base, fn))
            for p in sorted(paths, key=str.lower):
                yield file_row(source, folder, p)
        folder._loader = rows
    try:
        entries = sorted(os.listdir(package), key=str.lower)
    except OSError:
        entries = []
    for e in entries:
        p = os.path.join(package, e)
        if os.path.isdir(p) and e.lower().endswith(".mbox"):
            attach_apple_package(source, folder.add_subfolder(e[:-5]), p)


def is_apple_package(d: str) -> bool:
    return os.path.isdir(d) and d.rstrip("/\\").lower().endswith(".mbox")


def _file_rows(source: MailSource, folder: GenericFolder, d: str, exts, recursive: bool = False) -> Iterator[GenericRow]:
    paths = []
    if recursive:
        for base, _dirs, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(exts):
                    paths.append(os.path.join(base, fn))
    else:
        try:
            paths = [os.path.join(d, fn) for fn in os.listdir(d) if fn.lower().endswith(exts)
                     and os.path.isfile(os.path.join(d, fn))]
        except OSError:
            paths = []
    for p in sorted(paths, key=str.lower):
        yield file_row(source, folder, p)


def file_row(source: MailSource, folder: GenericFolder, p: str) -> GenericRow:
    nid = source.new_id()
    low = p.lower()

    def opener(row, p=p):
        return open_message_file(source, p, folder=row.folder, row=row)

    try:
        mtime = _dt.datetime.fromtimestamp(os.path.getmtime(p), _dt.timezone.utc)
    except OSError:
        mtime = None
    if low.endswith(".msg") or (not low.endswith((".eml", ".emlx")) and is_compound_file(p)):
        from .msgfile import msg_row
        try:
            return msg_row(source, folder, nid, p, opener)
        except Exception as exc:  # noqa: BLE001
            return GenericRow(folder, nid, opener, subject=f"{os.path.basename(p)} (unreadable: {exc})", date=mtime)
    try:
        raw = read_emlx(p) if low.endswith(".emlx") else _head_bytes(p)
    except OSError as exc:
        return GenericRow(folder, nid, opener, subject=f"{os.path.basename(p)} (unreadable: {exc})", date=mtime)
    row = row_from_bytes(folder, nid, raw, opener, mtime=mtime)
    try:
        row.size = os.path.getsize(p)
    except OSError:
        pass
    return row


def _head_bytes(p: str, limit: int = 262144) -> bytes:
    with open(p, "rb") as fh:
        return fh.read(limit)


class FilesSource(MailSource):
    """A directory tree of .eml / .emlx / .msg files. Directories are the folders."""

    kind = "files"
    kind_label = "Message files"
    EXTS = (".eml", ".emlx", ".msg")

    def __init__(self, path: str):
        super().__init__(path)
        self.root.name = os.path.basename(path.rstrip("/\\")) or path
        self.count = 0
        self._build(path, self.root)

    @property
    def description(self) -> str:
        return f"folder of message files ({self.count} files)"

    def _build(self, d: str, folder: GenericFolder):
        try:
            entries = sorted(os.listdir(d), key=str.lower)
        except OSError as exc:
            folder.error = str(exc)
            return
        files = [os.path.join(d, e) for e in entries if e.lower().endswith(self.EXTS) and os.path.isfile(os.path.join(d, e))]
        self.count += len(files)
        if files:
            folder._loader = lambda files=files, folder=folder: (file_row(self, folder, p) for p in files)
        for e in entries:
            p = os.path.join(d, e)
            if not os.path.isdir(p) or e.lower().endswith((".sbd", ".mbox")) or is_maildir(p) or e in ("cur", "new", "tmp"):
                continue
            sf = folder.add_subfolder(e)
            before = self.count
            self._build(p, sf)
            if self.count == before and not sf.subfolders():
                folder._subfolders.remove(sf)


class SingleFileSource(MailSource):
    """One .eml / .emlx / .msg opened on its own."""

    kind = "file"
    kind_label = "Message"

    def __init__(self, path: str):
        super().__init__(path)
        self.root.name = os.path.basename(path)
        self.root._loader = lambda: [file_row(self, self.root, path)]

    @property
    def description(self) -> str:
        ext = os.path.splitext(self.path)[1].lower().lstrip(".").upper()
        return f"single {ext} message"


# ---------------------------------------------------------------------------
# opening things
# ---------------------------------------------------------------------------
def open_source(path: str) -> MailSource:
    """The right source object for a path, or raise."""
    low = path.lower().rstrip("/\\")
    if os.path.isdir(path):
        if low.endswith(".mbox"):
            return ApplePackageSource(path)
        if is_maildir(path):
            return MaildirSource(path)
        found = find_sources(path)
        if len(found) == 1:
            return open_found(*found[0])
        if not found:
            raise ValueError("nothing openable in that folder")
        raise ValueError("several mailboxes in that folder - open it with find_sources()")
    if low.endswith((".pst", ".ost")):
        from .message import PSTFile
        return PSTFile(path)
    if low.endswith((".eml", ".emlx", ".msg")):
        return SingleFileSource(path)
    if low.endswith((".mbox", ".mbx")) or looks_like_mbox(path):
        return MboxSource(path)
    # last resort: sniff
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    if head[:4] == b"!BDN":
        from .message import PSTFile
        return PSTFile(path)
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return SingleFileSource(path)
    if head[:5] == b"From ":
        return MboxSource(path)
    first = head.split(b"\n", 1)[0]
    if re.match(rb"^[!-9;-~]+:", first):
        return SingleFileSource(path)
    raise ValueError("not a mailbox format this program recognises")


def find_sources(root: str) -> List[Tuple[str, str]]:
    """Everything openable under a directory: (kind, path) pairs, one per source.

    PST/OST files each become a source. A directory holding mbox files (or
    Thunderbird's .sbd / .msf leftovers) becomes one 'mail folder' source that
    also covers every Apple Mail package and mbox beneath it. Apple Mail
    packages and Maildirs elsewhere are sources of their own. Loose
    .eml/.msg/.emlx files anywhere in the tree become one 'message files'
    source rooted at `root`. Nothing is listed twice.
    """
    root = os.path.abspath(root)
    if is_maildir(root):
        return [("maildir", root)]
    if is_apple_package(root):
        return [("mbox", root)]
    out: List[Tuple[str, str]] = []
    loose = False
    mbox_dirs: List[str] = []
    packages: List[str] = []
    maildirs: List[str] = []
    for base, dirs, files in os.walk(root):
        keep = []
        has_mbox = False
        for d in dirs:
            p = os.path.join(base, d)
            low = d.lower()
            if is_maildir(p):
                maildirs.append(p)
            elif low.endswith(".mbox"):
                packages.append(p)
            elif low.endswith(".sbd"):
                has_mbox = True          # Thunderbird: the parent is a mail folder even if its mbox is empty
            else:
                keep.append(d)
        dirs[:] = keep
        for fn in files:
            p = os.path.join(base, fn)
            low = fn.lower()
            if low.endswith((".pst", ".ost")):
                out.append(("pst", p))
            elif low.endswith((".eml", ".emlx", ".msg")):
                loose = True
            elif low.endswith(".msf"):
                has_mbox = True
            elif could_be_mbox(p) and (low.endswith((".mbox", ".mbx")) or looks_like_mbox(p)
                                       or os.path.isdir(p + ".sbd")):
                has_mbox = True
        if has_mbox:
            mbox_dirs.append(base)
    mbox_dirs.sort()
    covered: List[str] = []
    for d in mbox_dirs:
        if any(d == c or d.startswith(c + os.sep) for c in covered):
            continue
        covered.append(d)
        out.append(("mboxdir", d))

    def under_mbox_dir(p: str) -> bool:
        return any(p.startswith(c + os.sep) for c in covered)

    out.extend(("mbox", p) for p in packages if not under_mbox_dir(p))
    out.extend(("maildir", p) for p in maildirs)
    if loose:
        out.append(("files", root))
    return out


def open_found(kind: str, path: str) -> MailSource:
    if kind == "pst":
        from .message import PSTFile
        return PSTFile(path)
    if kind == "maildir":
        return MaildirSource(path)
    if kind == "mbox":
        return ApplePackageSource(path) if os.path.isdir(path) else MboxSource(path)
    if kind == "mboxdir":
        return MailDirSource(path)
    if kind == "files":
        return FilesSource(path)
    return open_source(path)
