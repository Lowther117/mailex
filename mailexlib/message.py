"""Messaging layer: the store, folders, messages, recipients and attachments."""
from __future__ import annotations

import datetime as _dt
import os
import struct
from typing import Any, Dict, Iterator, List, Optional

from . import mapi, rtf
from .ltp import PropertyContext, TableContext
from .ndb import (NDB, NID_ATTACHMENT_TABLE, NID_MESSAGE_STORE, NID_RECIPIENT_TABLE,
                  NID_ROOT_FOLDER, NID_TYPE_ASSOC_MESSAGE, NID_TYPE_NORMAL_FOLDER,
                  NID_TYPE_NORMAL_MESSAGE, NID_TYPE_SEARCH_FOLDER, Node, PSTError, nid_type)


def _clean_subject(s: Optional[str]) -> str:
    if not s:
        return ""
    if s[0] == "\x01" and len(s) >= 2:
        s = s[2:]
    return s.replace("\r", " ").replace("\n", " ").strip()


def _nl(s: str) -> str:
    """Normalise line endings to \n and drop stray NULs."""
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def _clean_text(v) -> str:
    """A table-cell string, or '' if the cell held junk (OST contents tables do that)."""
    if not isinstance(v, str):
        return ""
    if any(ord(c) < 0x20 and c not in "\t\r\n" for c in v) or any(0xD800 <= ord(c) < 0xE000 for c in v):
        return ""
    return v.strip()


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


class Recipient:
    __slots__ = ("kind", "name", "email", "addrtype", "smtp")
    KINDS = {1: "To", 2: "Cc", 3: "Bcc"}

    def __init__(self, row: Dict[int, Any]):
        kind = row.get(mapi.PR_RECIPIENT_TYPE)
        kind = (int(kind) & 0xF) if isinstance(kind, int) else 1   # strip MAPI_SUBMITTED / MAPI_P1 flags
        self.kind = self.KINDS.get(kind, "To")
        self.name = str(_first(row.get(mapi.PR_DISPLAY_NAME), row.get(mapi.PR_RECIPIENT_DISPLAY_NAME)) or "").strip()
        self.addrtype = str(row.get(mapi.PR_ADDRTYPE) or "").upper().strip()
        self.email = str(row.get(mapi.PR_EMAIL_ADDRESS) or "").strip()
        self.smtp = str(row.get(mapi.PR_SMTP_ADDRESS) or "").strip()
        if not self.smtp and "@" in self.email and self.addrtype in ("SMTP", ""):
            self.smtp = self.email

    @classmethod
    def simple(cls, kind: str, name: str, addr: str) -> "Recipient":
        r = cls.__new__(cls)
        r.kind = kind if kind in ("To", "Cc", "Bcc") else "To"
        r.name = (name or "").strip()
        r.addrtype = "SMTP"
        r.email = (addr or "").strip()
        r.smtp = r.email if "@" in r.email else ""
        return r

    @property
    def address(self) -> str:
        """The most useful routable address we have."""
        return self.smtp or (self.email if "@" in self.email else "")

    def formatted(self) -> str:
        addr = self.address
        if addr and self.name and self.name.lower() != addr.lower():
            return f"{self.name} <{addr}>"
        return addr or self.name or self.email

    def __repr__(self):
        return f"Recipient({self.kind}, {self.formatted()!r})"


class Attachment:
    def __init__(self, message: "Message", nid: int, row: Optional[Dict[int, Any]]):
        self.message = message
        self.nid = nid
        self.row = row or {}
        self._pc: Optional[PropertyContext] = None
        self._node: Optional[Node] = None
        self.error: Optional[str] = None
        try:
            self._node = message.node.subnode(nid)
            if self._node is None:
                self.error = "attachment sub-node missing"
            else:
                self._pc = PropertyContext(self._node, message.cpid)
        except PSTError as exc:
            self.error = str(exc)

    def _get(self, pid, default=None):
        if self._pc is not None and pid in self._pc:
            v = self._pc.get(pid)
            if v is not None:
                return v
        return self.row.get(pid, default)

    @property
    def method(self) -> int:
        v = self._get(mapi.PR_ATTACH_METHOD)
        return int(v) if isinstance(v, int) else mapi.ATTACH_BY_VALUE

    @property
    def is_embedded_message(self) -> bool:
        if self.method == mapi.ATTACH_EMBEDDED_MSG:
            return True
        return self._pc is not None and self._pc.ptype(mapi.PR_ATTACH_DATA) == mapi.PT_OBJECT and self.method != mapi.ATTACH_OLE

    @property
    def filename(self) -> str:
        name = _first(self._get(mapi.PR_ATTACH_LONG_FILENAME), self._get(mapi.PR_ATTACH_FILENAME),
                      self._get(mapi.PR_DISPLAY_NAME))
        if not name and self.is_embedded_message:
            emb = self.embedded_message
            if emb is not None and emb.subject:
                name = emb.subject + ".eml"
        if not name:
            ext = self._get(mapi.PR_ATTACH_EXTENSION) or ""
            name = f"attachment-{self.nid:x}{ext}"
        return str(name).replace("\x00", "")

    @property
    def mime_type(self) -> str:
        v = self._get(mapi.PR_ATTACH_MIME_TAG)
        return str(v) if v else ""

    @property
    def content_id(self) -> str:
        v = self._get(mapi.PR_ATTACH_CONTENT_ID)
        return str(v).strip("<>") if v else ""

    @property
    def hidden(self) -> bool:
        return bool(self._get(mapi.PR_ATTACHMENT_HIDDEN, False))

    @property
    def is_inline(self) -> bool:
        flags = self._get(mapi.PR_ATTACH_FLAGS) or 0
        return bool(self.content_id) or bool(int(flags) & 4) if isinstance(flags, int) else bool(self.content_id)

    @property
    def size(self) -> int:
        v = self._get(mapi.PR_ATTACH_SIZE)
        if isinstance(v, int):
            return v
        return 0

    @property
    def data(self) -> Optional[bytes]:
        """Attachment bytes (None for embedded messages, references and errors)."""
        if self._pc is None:
            return None
        if self._pc.ptype(mapi.PR_ATTACH_DATA) == mapi.PT_OBJECT:
            if self.method == mapi.ATTACH_OLE:
                # an OLE object: the sub-node holds the raw storage
                ref = self._pc.get(mapi.PR_ATTACH_DATA)
                if isinstance(ref, mapi.ObjectRef) and self._node is not None:
                    sub = self._node.subnode(ref.nid)
                    if sub is not None:
                        try:
                            return sub.data()
                        except PSTError:
                            return None
            return None
        try:
            v = self._pc.get(mapi.PR_ATTACH_DATA)
        except PSTError as exc:
            self.error = str(exc)
            return None
        if isinstance(v, bytes):
            return v
        if isinstance(v, str):
            return v.encode("utf-8")
        return None

    @property
    def embedded_message(self) -> Optional["Message"]:
        if self._pc is None or self._node is None:
            return None
        ref = self._pc.get(mapi.PR_ATTACH_DATA)
        if not isinstance(ref, mapi.ObjectRef):
            return None
        sub = self._node.subnode(ref.nid)
        if sub is None:
            return None
        try:
            return Message(self.message.pst, sub, folder=self.message.folder, cpid=self.message.cpid,
                           embedded=True)
        except PSTError as exc:
            self.error = str(exc)
            return None

    def __repr__(self):
        return f"Attachment({self.filename!r}, method={self.method}, size={self.size})"


class MessageRow:
    """What the folder's contents table knows about a message, without opening it."""

    __slots__ = ("folder", "nid", "subject", "sender", "date", "size", "has_attachments",
                 "message_class", "to", "read", "importance", "row", "_filled")

    def __init__(self, folder: "Folder", row: Dict[int, Any]):
        self.folder = folder
        self.row = row
        self._filled = False
        self.nid = row.get(mapi.PR_LTP_ROW_ID, 0)
        self.subject = _clean_subject(_first(row.get(mapi.PR_SUBJECT), row.get(mapi.PR_NORMALIZED_SUBJECT),
                                             row.get(mapi.PR_CONVERSATION_TOPIC)))
        self.sender = _first(_clean_text(row.get(mapi.PR_SENDER_NAME)), _clean_text(row.get(mapi.PR_SENT_REPRESENTING_NAME)),
                             _clean_text(row.get(mapi.PR_SENDER_SMTP)), _clean_text(row.get(mapi.PR_SENDER_EMAIL))) or ""
        d = _first(row.get(mapi.PR_MESSAGE_DELIVERY_TIME), row.get(mapi.PR_CLIENT_SUBMIT_TIME),
                   row.get(mapi.PR_LAST_MODIFICATION_TIME), row.get(mapi.PR_CREATION_TIME))
        self.date = d if isinstance(d, _dt.datetime) else None
        s = row.get(mapi.PR_MESSAGE_SIZE)
        self.size = int(s) if isinstance(s, int) else 0
        flags = row.get(mapi.PR_MESSAGE_FLAGS)
        flags = int(flags) if isinstance(flags, int) else 0
        ha = row.get(mapi.PR_HASATTACH)
        self.has_attachments = bool(ha) if ha is not None else bool(flags & mapi.MSGFLAG_HASATTACH)
        self.read = bool(flags & mapi.MSGFLAG_READ)
        self.message_class = row.get(mapi.PR_MESSAGE_CLASS) or ""
        self.to = _clean_text(row.get(mapi.PR_DISPLAY_TO))
        imp = row.get(mapi.PR_IMPORTANCE)
        self.importance = int(imp) if isinstance(imp, int) else 1

    @property
    def pst(self) -> "PSTFile":
        return self.folder.pst

    @property
    def source(self) -> "PSTFile":
        return self.folder.pst

    def open(self) -> "Message":
        return self.pst.message(self.nid, folder=self.folder, row=self)

    def fill_from_message(self, force_sender: bool = False) -> None:
        """Fill in fields the contents table did not have by opening the message."""
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

    def __repr__(self):
        return f"MessageRow(0x{self.nid:x}, {self.subject!r})"


class PropertyMessage:
    """Everything a message can say about itself from its MAPI property bag.

    Subclasses supply `self.pc` (get / get_string / ptype / items / __contains__),
    `self.cpid`, and the recipients() / attachments() lookups; the PST reader
    and the .msg reader both build on this so exporters see one shape.
    """

    pst = None
    source = None
    folder = None
    row = None
    nid = 0
    embedded = False

    def _init_common(self):
        self._recipients: Optional[List[Recipient]] = None
        self._attachments: Optional[list] = None
        self.warnings: List[str] = []

    def recipients(self) -> List[Recipient]:  # pragma: no cover - overridden
        return []

    def attachments(self) -> list:  # pragma: no cover - overridden
        return []

    def raw_eml(self) -> Optional[bytes]:
        """Original RFC 822 bytes when the source had them; PST/MSG never do."""
        return None

    # -- simple properties --------------------------------------------------
    def get(self, pid, default=None):
        return self.pc.get(pid, default)

    def text(self, pid) -> str:
        return self.pc.get_string(pid, "")

    @property
    def subject(self) -> str:
        return _clean_subject(_first(self.text(mapi.PR_SUBJECT), self.text(mapi.PR_NORMALIZED_SUBJECT),
                                     self.text(mapi.PR_CONVERSATION_TOPIC)))

    @property
    def message_class(self) -> str:
        return self.text(mapi.PR_MESSAGE_CLASS) or "IPM.Note"

    @property
    def is_email(self) -> bool:
        mc = self.message_class.upper()
        return mc.startswith("IPM.NOTE") or mc.startswith("REPORT.") or mc.startswith("IPM.SCHEDULE")

    @property
    def sender_name(self) -> str:
        return _first(self.text(mapi.PR_SENDER_NAME), self.text(mapi.PR_SENT_REPRESENTING_NAME)) or ""

    @property
    def sender_email(self) -> str:
        for pid, tpid in ((mapi.PR_SENDER_SMTP, None), (mapi.PR_SENT_REPRESENTING_SMTP, None),
                          (mapi.PR_SENDER_EMAIL, mapi.PR_SENDER_ADDRTYPE),
                          (mapi.PR_SENT_REPRESENTING_EMAIL, mapi.PR_SENT_REPRESENTING_ADDRTYPE)):
            v = self.text(pid)
            if v and "@" in v:
                return v
        # fall back to whatever address-like thing we have
        return _first(self.text(mapi.PR_SENDER_EMAIL), self.text(mapi.PR_SENT_REPRESENTING_EMAIL)) or ""

    @property
    def sender(self) -> str:
        name, addr = self.sender_name, self.sender_email
        if addr and name and name.lower() != addr.lower():
            return f"{name} <{addr}>"
        return addr or name

    @property
    def date(self) -> Optional[_dt.datetime]:
        for pid in (mapi.PR_MESSAGE_DELIVERY_TIME, mapi.PR_CLIENT_SUBMIT_TIME,
                    mapi.PR_LAST_MODIFICATION_TIME, mapi.PR_CREATION_TIME):
            v = self.get(pid)
            if isinstance(v, _dt.datetime):
                return v
        return None

    @property
    def sent_date(self) -> Optional[_dt.datetime]:
        v = self.get(mapi.PR_CLIENT_SUBMIT_TIME)
        return v if isinstance(v, _dt.datetime) else self.date

    @property
    def display_to(self) -> str:
        return self.text(mapi.PR_DISPLAY_TO)

    @property
    def display_cc(self) -> str:
        return self.text(mapi.PR_DISPLAY_CC)

    @property
    def display_bcc(self) -> str:
        return self.text(mapi.PR_DISPLAY_BCC)

    @property
    def internet_message_id(self) -> str:
        return self.text(mapi.PR_INTERNET_MESSAGE_ID).strip()

    @property
    def in_reply_to(self) -> str:
        return self.text(mapi.PR_IN_REPLY_TO_ID).strip()

    @property
    def references(self) -> str:
        return self.text(mapi.PR_INTERNET_REFERENCES).strip()

    @property
    def transport_headers(self) -> str:
        return self.text(mapi.PR_TRANSPORT_HEADERS)

    @property
    def flags(self) -> int:
        v = self.get(mapi.PR_MESSAGE_FLAGS)
        return int(v) if isinstance(v, int) else 0

    @property
    def read(self) -> bool:
        return bool(self.flags & mapi.MSGFLAG_READ)

    @property
    def importance(self) -> int:
        v = self.get(mapi.PR_IMPORTANCE)
        return int(v) if isinstance(v, int) else 1

    @property
    def size(self) -> int:
        v = self.get(mapi.PR_MESSAGE_SIZE)
        return int(v) if isinstance(v, int) else 0

    # -- bodies -------------------------------------------------------------
    @property
    def body_text(self) -> str:
        v = self.get(mapi.PR_BODY)
        if isinstance(v, bytes):
            v = mapi.decode_string8(v, self.cpid)
        return _nl(v or "")

    @property
    def body_html(self) -> str:
        v = self.get(mapi.PR_HTML)
        if isinstance(v, bytes):
            cp = self.get(mapi.PR_INTERNET_CPID) or self.cpid
            return rtf.decode_html(v, cp)
        return v or ""

    @property
    def body_rtf(self) -> bytes:
        v = self.get(mapi.PR_RTF_COMPRESSED)
        if isinstance(v, bytes) and v:
            try:
                return rtf.decompress_rtf(v)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"RTF body would not decompress: {exc}")
        return b""

    def best_html(self) -> str:
        """HTML body if there is one (directly, or encapsulated in the RTF)."""
        h = self.body_html
        if h.strip():
            return h
        r = self.body_rtf
        if r and rtf.is_encapsulated_html(r):
            de = rtf.rtf_to_html(r)
            if de and de.strip():
                return de
        return ""

    def best_text(self) -> str:
        """Plain text body: the stored one, else derived from HTML, else from RTF."""
        t = self.body_text
        if t.strip():
            return t
        h = self.best_html()
        if h.strip():
            return _nl(rtf.html_to_text(h))
        r = self.body_rtf
        if r:
            return _nl(rtf.rtf_to_text(r))
        return ""

    # -- sub-objects --------------------------------------------------------
    def recipients_of(self, kind: str) -> List[Recipient]:
        return [r for r in self.recipients() if r.kind == kind]

    @property
    def has_attachments(self) -> bool:
        v = self.get(mapi.PR_HASATTACH)
        if v is None:
            return bool(self.flags & mapi.MSGFLAG_HASATTACH) or bool(self.attachments())
        return bool(v)

    def all_properties(self) -> Iterator[tuple]:
        """(id, name, type, value) for every property - for the property inspector."""
        for pid, ptype, val in self.pc.items():
            yield pid, mapi.tag_name(pid), ptype, val

    def __repr__(self):
        return f"{type(self).__name__}(0x{self.nid:x}, {self.subject!r})"


class Message(PropertyMessage):
    """A fully opened PST message (or an embedded one)."""

    def __init__(self, pst: "PSTFile", node: Node, folder: Optional["Folder"] = None,
                 row: Optional[MessageRow] = None, cpid: Optional[int] = None, embedded: bool = False):
        self.pst = pst
        self.source = pst
        self.node = node
        self.nid = node.nid
        self.folder = folder
        self.row = row
        self.embedded = embedded
        self.pc = PropertyContext(node, cpid if cpid is not None else pst.cpid)
        self.cpid = self.pc.cpid
        self._init_common()

    def recipients(self) -> List[Recipient]:
        if self._recipients is None:
            out: List[Recipient] = []
            sub = self.node.subnode(NID_RECIPIENT_TABLE)
            if sub is not None:
                try:
                    tc = TableContext(sub, self.cpid)
                    for row in tc.rows():
                        out.append(Recipient(row))
                except PSTError as exc:
                    self.warnings.append(f"recipient table unreadable: {exc}")
            self._recipients = out
        return self._recipients

    def attachments(self) -> List[Attachment]:
        if self._attachments is None:
            out: List[Attachment] = []
            sub = self.node.subnode(NID_ATTACHMENT_TABLE)
            if sub is not None:
                try:
                    tc = TableContext(sub, self.cpid)
                    for row in tc.rows():
                        nid = row.get(mapi.PR_LTP_ROW_ID)
                        if nid:
                            out.append(Attachment(self, nid, row))
                except PSTError as exc:
                    self.warnings.append(f"attachment table unreadable: {exc}")
            elif self.get(mapi.PR_HASATTACH):
                # no attachment table, but sub-nodes of attachment type exist
                for nid in self.node.subnodes():
                    if nid_type(nid) == 0x05:
                        out.append(Attachment(self, nid, None))
            self._attachments = out
        return self._attachments

    @property
    def has_attachments(self) -> bool:
        v = self.get(mapi.PR_HASATTACH)
        if v is None:
            return bool(self.flags & mapi.MSGFLAG_HASATTACH) or self.node.subnode(NID_ATTACHMENT_TABLE) is not None
        return bool(v)


class Folder:
    def __init__(self, pst: "PSTFile", nid: int, name: str = "", parent: Optional["Folder"] = None,
                 row: Optional[Dict[int, Any]] = None):
        self.pst = pst
        self.source = pst
        self.nid = nid
        self.parent = parent
        self.row = row or {}
        self.name = name
        self.content_count = int(self.row.get(mapi.PR_CONTENT_COUNT) or 0)
        self.unread_count = int(self.row.get(mapi.PR_CONTENT_UNREAD) or 0)
        self.has_subfolders = bool(self.row.get(mapi.PR_SUBFOLDERS, True))
        self.container_class = self.row.get(mapi.PR_CONTAINER_CLASS) or ""
        self._subfolders: Optional[List[Folder]] = None
        self._rows: Optional[List[MessageRow]] = None
        self.error: Optional[str] = None

    def load_properties(self):
        """Read the folder's own property context (the hierarchy row usually suffices)."""
        node = self.pst.ndb.node(self.nid)
        if node is None:
            self.error = "folder node missing"
            return
        try:
            pc = PropertyContext(node, self.pst.cpid)
        except PSTError as exc:
            self.error = str(exc)
            return
        self.name = self.name or pc.get_string(mapi.PR_DISPLAY_NAME)
        cc = pc.get(mapi.PR_CONTENT_COUNT)
        if isinstance(cc, int):
            self.content_count = cc
        uc = pc.get(mapi.PR_CONTENT_UNREAD)
        if isinstance(uc, int):
            self.unread_count = uc
        sf = pc.get(mapi.PR_SUBFOLDERS)
        if sf is not None:
            self.has_subfolders = bool(sf)
        self.container_class = pc.get_string(mapi.PR_CONTAINER_CLASS) or self.container_class

    @property
    def path(self) -> List[str]:
        parts = []
        f: Optional[Folder] = self
        while f is not None:
            parts.append(f.name or f"folder-{f.nid:x}")
            f = f.parent
        parts.reverse()
        return parts

    @property
    def path_str(self) -> str:
        return "/".join(self.path)

    def subfolders(self) -> List["Folder"]:
        if self._subfolders is not None:
            return self._subfolders
        out: List[Folder] = []
        node = self.pst.ndb.node((self.nid & ~0x1F) | 0x0D)
        rows = None
        if node is not None:
            try:
                rows = TableContext(node, self.pst.cpid).rows()
            except PSTError as exc:
                self.error = f"hierarchy table unreadable: {exc}"
        if rows is not None:
            for row in rows:
                nid = row.get(mapi.PR_LTP_ROW_ID)
                if not nid or nid_type(nid) not in (NID_TYPE_NORMAL_FOLDER, NID_TYPE_SEARCH_FOLDER):
                    continue
                name = row.get(mapi.PR_DISPLAY_NAME) or ""
                if isinstance(name, bytes):
                    name = mapi.decode_string8(name, self.pst.cpid)
                out.append(Folder(self.pst, nid, name, self, row))
        else:
            # fall back to scanning the node B-tree for children
            for nid in self.pst.children_by_scan(self.nid, (NID_TYPE_NORMAL_FOLDER,)):
                f = Folder(self.pst, nid, "", self)
                f.load_properties()
                out.append(f)
        self._subfolders = out
        return out

    def walk(self, _seen: Optional[set] = None) -> Iterator["Folder"]:
        seen = _seen if _seen is not None else set()
        if self.nid in seen:
            return          # a corrupt hierarchy table can loop back on itself
        seen.add(self.nid)
        yield self
        for sf in self.subfolders():
            yield from sf.walk(seen)

    def messages(self) -> List[MessageRow]:
        """Rows of the contents table (normal messages, not the hidden associated ones)."""
        if self._rows is not None:
            return self._rows
        out: List[MessageRow] = []
        node = self.pst.ndb.node((self.nid & ~0x1F) | 0x0E)
        rows = None
        if node is not None:
            try:
                rows = TableContext(node, self.pst.cpid).rows()
            except PSTError as exc:
                self.error = f"contents table unreadable: {exc}"
        if rows is not None:
            for row in rows:
                nid = row.get(mapi.PR_LTP_ROW_ID)
                if not nid:
                    continue
                out.append(MessageRow(self, row))
        elif self.content_count or node is None:
            for nid in self.pst.children_by_scan(self.nid, (NID_TYPE_NORMAL_MESSAGE,)):
                out.append(MessageRow(self, {mapi.PR_LTP_ROW_ID: nid, mapi.PR_SUBJECT: f"(message 0x{nid:x})"}))
        self._rows = out
        return out

    def total_messages(self) -> int:
        return sum(len(f.messages()) for f in self.walk())

    def __repr__(self):
        return f"Folder({self.path_str!r}, {self.content_count})"


class PSTFile:
    """A PST or OST file opened for reading."""

    kind = "pst"

    def __init__(self, path: str):
        self.path = path
        self.ndb = NDB(path)
        self.cpid: Optional[int] = None
        self.name = os.path.basename(path)
        self.store_name = ""
        self.warnings: List[str] = []
        self._scan_index: Optional[Dict[int, List[int]]] = None
        node = self.ndb.node(NID_MESSAGE_STORE)
        if node is not None:
            try:
                pc = PropertyContext(node)
                self.store_name = pc.get_string(mapi.PR_DISPLAY_NAME)
                cp = pc.get(mapi.PR_MESSAGE_CODEPAGE) or pc.get(0x66C3)
                if isinstance(cp, int) and cp:
                    self.cpid = cp
            except PSTError as exc:
                self.warnings.append(f"message store unreadable: {exc}")
        else:
            self.warnings.append("no message store node (file may be damaged or an ExMerge export)")
        self.root = Folder(self, NID_ROOT_FOLDER, "")
        self.root.load_properties()
        if not self.root.name:
            self.root.name = self.store_name or os.path.splitext(self.name)[0]

    @property
    def description(self) -> str:
        return self.ndb.description

    @property
    def kind_label(self) -> str:
        return "OST" if self.ndb.is_ost else "PST"

    @property
    def is_ost(self) -> bool:
        return self.ndb.is_ost

    @property
    def sender_from_message(self) -> bool:
        # OST contents tables carry a stale sender column; the message itself is right
        return self.ndb.is_ost

    @property
    def size(self) -> int:
        return self.ndb.size

    def close(self):
        self.ndb.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- access ---------------------------------------------------------------
    def folder(self, nid: int) -> Optional[Folder]:
        for f in self.root.walk():
            if f.nid == nid:
                return f
        return None

    def message(self, nid: int, folder: Optional[Folder] = None, row: Optional[MessageRow] = None) -> Message:
        node = self.ndb.node(nid)
        if node is None:
            raise PSTError(f"message node 0x{nid:x} is not in the file")
        return Message(self, node, folder=folder, row=row)

    def all_message_rows(self, root: Optional[Folder] = None) -> Iterator[MessageRow]:
        for f in (root or self.root).walk():
            yield from f.messages()

    def folders_with_mail(self) -> Iterator[Folder]:
        for f in self.root.walk():
            if f.messages():
                yield f

    # -- recovery -------------------------------------------------------------
    def _build_scan_index(self) -> Dict[int, List[int]]:
        if self._scan_index is None:
            idx: Dict[int, List[int]] = {}
            for e in self.ndb.iter_nbt():
                t = nid_type(e.nid)
                if t in (NID_TYPE_NORMAL_FOLDER, NID_TYPE_SEARCH_FOLDER, NID_TYPE_NORMAL_MESSAGE, NID_TYPE_ASSOC_MESSAGE):
                    idx.setdefault(e.nid_parent, []).append(e.nid)
            self._scan_index = idx
        return self._scan_index

    def children_by_scan(self, parent_nid: int, types) -> List[int]:
        idx = self._build_scan_index()
        return sorted(n for n in idx.get(parent_nid, []) if nid_type(n) in types)

    def orphan_messages(self) -> List[int]:
        """Message NIDs whose parent folder is not reachable from the root (deleted/lost)."""
        reachable = {f.nid for f in self.root.walk()}
        listed = {r.nid for r in self.all_message_rows()}
        idx = self._build_scan_index()
        out = []
        for parent, nids in idx.items():
            for n in nids:
                if nid_type(n) == NID_TYPE_NORMAL_MESSAGE and n not in listed and parent not in reachable:
                    out.append(n)
        return sorted(out)
