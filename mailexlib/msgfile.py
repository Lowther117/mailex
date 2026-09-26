"""Outlook .msg files ([MS-OXMSG]): MAPI properties stored in a compound file.

A .msg is a storage tree: `__substg1.0_XXXXYYYY` streams hold variable-length
property values (XXXX = property id, YYYY = type), `__properties_version1.0`
holds the fixed-size ones, `__recip_version1.0_#0000000N` storages are the
recipients and `__attach_version1.0_#0000000N` the attachments - an embedded
message being an attachment whose data stream is itself a storage.
"""
from __future__ import annotations

import os
import re
import struct
from typing import Any, Dict, Iterator, List, Optional

from . import mapi
from .cfb import CFBError, CompoundFile, Entry
from .message import PropertyMessage, Recipient

_SUBSTG = re.compile(r"^__substg1\.0_([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})(?:-([0-9A-Fa-f]+))?$")
PROPS_STREAM = "__properties_version1.0"
# types stored inline in the properties stream ([MS-OXMSG] 2.4.2.1: 8 bytes or fewer)
INLINE_TYPES = {mapi.PT_I2, mapi.PT_LONG, mapi.PT_R4, mapi.PT_DOUBLE, mapi.PT_CURRENCY, mapi.PT_APPTIME,
                mapi.PT_ERROR, mapi.PT_BOOLEAN, mapi.PT_I8, mapi.PT_SYSTIME}


class MsgPropertyBag:
    """Same interface as the PST PropertyContext, over one .msg storage."""

    def __init__(self, storage: Entry, cpid: Optional[int], header_size: int):
        self.storage = storage
        self.kids = storage.children()
        self.raw: Dict[int, tuple] = {}     # pid -> (ptype, fixed_bytes or None, stream entry or None)
        self._cache: Dict[int, Any] = {}
        self._mv: Dict[int, List[Entry]] = {}
        for name, e in self.kids.items():
            m = _SUBSTG.match(name)
            if not m or not e.is_stream:
                continue
            pid, ptype = int(m.group(1), 16), int(m.group(2), 16)
            if m.group(3) is not None:
                self._mv.setdefault(pid, []).append(e)
                continue
            self.raw[pid] = (ptype, None, e)
        props = self.kids.get(PROPS_STREAM)
        self.header: bytes = b""
        if props is not None and props.is_stream:
            data = props.read()
            self.header = data[:header_size]
            for off in range(header_size, len(data) - 15, 16):
                tag, _flags = struct.unpack_from("<II", data, off)
                pid, ptype = tag >> 16, tag & 0xFFFF
                if ptype in INLINE_TYPES:
                    self.raw[pid] = (ptype, data[off + 8:off + 16], None)
                elif pid not in self.raw:
                    # variable-length property whose stream is missing: keep the type
                    self.raw[pid] = (ptype, None, None)
        self.cpid = cpid
        own = self.get(mapi.PR_MESSAGE_CODEPAGE)
        if not isinstance(own, int):
            own = self.get(mapi.PR_INTERNET_CPID)
        if isinstance(own, int) and own:
            self.cpid = own

    def __contains__(self, pid: int) -> bool:
        return pid in self.raw

    def keys(self):
        return self.raw.keys()

    def ptype(self, pid: int) -> Optional[int]:
        ent = self.raw.get(pid)
        return ent[0] if ent else None

    def raw_value(self, pid: int) -> Optional[bytes]:
        ent = self.raw.get(pid)
        if ent is None:
            return None
        ptype, fixed, stream = ent
        if fixed is not None:
            return fixed
        if stream is not None:
            return stream.read()
        return None

    def get(self, pid: int, default: Any = None) -> Any:
        if pid in self._cache:
            return self._cache[pid]
        ent = self.raw.get(pid)
        if ent is None:
            return default
        ptype, fixed, stream = ent
        val: Any = default
        try:
            if ptype & mapi.PT_MV_FLAG:
                base = ptype & ~mapi.PT_MV_FLAG
                parts = self._mv.get(pid)
                if parts:
                    def idx(e):
                        m = _SUBSTG.match(e.name)
                        return int(m.group(3), 16) if m and m.group(3) else 0
                    val = [mapi.decode_scalar(base, p.read(), self.cpid) for p in sorted(parts, key=idx)]
                elif stream is not None:
                    data = stream.read()
                    size = mapi.FIXED_SIZES.get(base)
                    if size:
                        val = [mapi.decode_scalar(base, data[i:i + size], self.cpid) for i in range(0, len(data) - size + 1, size)]
                    else:
                        val = []
                else:
                    val = []
            elif fixed is not None:
                val = mapi.decode_scalar(ptype, fixed, self.cpid)
            elif stream is not None:
                if ptype == mapi.PT_OBJECT:
                    val = mapi.ObjectRef(0, stream.size)
                else:
                    val = mapi.decode_scalar(ptype, stream.read(), self.cpid)
        except (CFBError, struct.error, ValueError):
            val = default
        self._cache[pid] = val
        return val

    def get_string(self, pid: int, default: str = "") -> str:
        v = self.get(pid)
        if v is None:
            return default
        if isinstance(v, bytes):
            return mapi.decode_string8(v, self.cpid)
        return v if isinstance(v, str) else str(v)

    def items(self) -> Iterator[tuple]:
        for pid in sorted(self.raw):
            yield pid, self.raw[pid][0], self.get(pid)


class MsgAttachment:
    def __init__(self, message: "MsgMessage", index: int, storage: Entry):
        self.message = message
        self.nid = index
        self.storage = storage
        self.error: Optional[str] = None
        self.pc = MsgPropertyBag(storage, message.cpid, 8)
        self.row: Dict[int, Any] = {}
        self._embedded: Optional["MsgMessage"] = None
        self._embedded_tried = False

    def _get(self, pid, default=None):
        v = self.pc.get(pid)
        return default if v is None else v

    @property
    def method(self) -> int:
        v = self._get(mapi.PR_ATTACH_METHOD)
        return int(v) if isinstance(v, int) else mapi.ATTACH_BY_VALUE

    @property
    def _data_storage(self) -> Optional[Entry]:
        e = self.storage.get("__substg1.0_3701000D")
        return e if e is not None and e.is_storage else None

    @property
    def is_embedded_message(self) -> bool:
        if self.method == mapi.ATTACH_EMBEDDED_MSG:
            return True
        st = self._data_storage
        return st is not None and self.method != mapi.ATTACH_OLE and st.get(PROPS_STREAM) is not None

    @property
    def filename(self) -> str:
        name = self.pc.get_string(mapi.PR_ATTACH_LONG_FILENAME) or self.pc.get_string(mapi.PR_ATTACH_FILENAME) \
            or self.pc.get_string(mapi.PR_DISPLAY_NAME)
        if not name and self.is_embedded_message:
            emb = self.embedded_message
            if emb is not None and emb.subject:
                name = emb.subject + ".eml"
        if not name:
            name = f"attachment-{self.nid}{self.pc.get_string(mapi.PR_ATTACH_EXTENSION)}"
        return name.replace("\x00", "")

    @property
    def mime_type(self) -> str:
        return self.pc.get_string(mapi.PR_ATTACH_MIME_TAG)

    @property
    def content_id(self) -> str:
        return self.pc.get_string(mapi.PR_ATTACH_CONTENT_ID).strip("<>")

    @property
    def hidden(self) -> bool:
        return bool(self._get(mapi.PR_ATTACHMENT_HIDDEN, False))

    @property
    def is_inline(self) -> bool:
        return bool(self.content_id)

    @property
    def size(self) -> int:
        v = self._get(mapi.PR_ATTACH_SIZE)
        if isinstance(v, int):
            return v
        d = self.data
        return len(d) if d else 0

    @property
    def data(self) -> Optional[bytes]:
        e = self.storage.get("__substg1.0_37010102")
        if e is not None and e.is_stream:
            try:
                return e.read()
            except CFBError as exc:
                self.error = str(exc)
                return None
        if self.method == mapi.ATTACH_OLE:
            st = self._data_storage
            if st is not None:
                # an OLE "package": the embedded file's bytes live in one of a few known streams
                try:
                    native = st.get("\x01Ole10Native")
                    if native is not None and native.is_stream:
                        blob = native.read()
                        return blob[4:] if len(blob) > 4 else blob
                    for name in ("CONTENTS", "Package", "\x01Ole10ItemName"):
                        e2 = st.get(name)
                        if e2 is not None and e2.is_stream:
                            return e2.read()
                except CFBError as exc:
                    self.error = str(exc)
                    return None
                self.error = "OLE object of a kind that has no extractable file inside"
        return None

    @property
    def embedded_message(self) -> Optional["MsgMessage"]:
        if self._embedded_tried:
            return self._embedded
        self._embedded_tried = True
        st = self._data_storage
        if st is None or not self.is_embedded_message:
            return None
        try:
            self._embedded = MsgMessage(self.message.source, None, folder=self.message.folder, storage=st,
                                        cpid=self.message.cpid, embedded=True, cf=self.message.cf)
        except (CFBError, ValueError) as exc:
            self.error = str(exc)
        return self._embedded

    def __repr__(self):
        return f"MsgAttachment({self.filename!r})"


class MsgMessage(PropertyMessage):
    """An Outlook .msg file (or a message embedded in one)."""

    def __init__(self, source, path: Optional[str], folder=None, row=None, storage: Optional[Entry] = None,
                 cpid: Optional[int] = None, embedded: bool = False, cf: Optional[CompoundFile] = None):
        self.pst = source
        self.source = source
        self.folder = folder
        self.row = row
        self.nid = row.nid if row is not None else 0
        self.embedded = embedded
        self.path = path
        if cf is None:
            if path is None:
                raise ValueError("a path or a storage is needed")
            cf = CompoundFile.open(path)
        self.cf = cf
        self.storage = storage or cf.root
        self.pc = MsgPropertyBag(self.storage, cpid, 24 if embedded else 32)
        self.cpid = self.pc.cpid
        self._init_common()

    @property
    def message_class(self) -> str:
        return self.text(mapi.PR_MESSAGE_CLASS) or "IPM.Note"

    def name_map(self):
        """The file's name-to-id table lives at the top level of the compound file
        (embedded messages share it), so it is cached on the CompoundFile."""
        nm = getattr(self.cf, "_name_map", None)
        if nm is None:
            from .namedprops import NameMap
            nm = NameMap.from_msg_root(self.cf.root)
            self.cf._name_map = nm
        return nm

    @property
    def size(self) -> int:
        v = self.get(mapi.PR_MESSAGE_SIZE)
        if isinstance(v, int) and v:
            return v
        if self.path:
            try:
                return os.path.getsize(self.path)
            except OSError:
                return 0
        return 0

    def recipients(self) -> List[Recipient]:
        if self._recipients is None:
            out = []
            for name in sorted(self.storage.children()):
                if name.startswith("__recip_version1.0_#"):
                    st = self.storage.children()[name]
                    if not st.is_storage:
                        continue
                    bag = MsgPropertyBag(st, self.cpid, 8)
                    row = {pid: bag.get(pid) for pid in (mapi.PR_RECIPIENT_TYPE, mapi.PR_DISPLAY_NAME,
                                                          mapi.PR_RECIPIENT_DISPLAY_NAME, mapi.PR_ADDRTYPE,
                                                          mapi.PR_EMAIL_ADDRESS, mapi.PR_SMTP_ADDRESS)}
                    out.append(Recipient(row))
            self._recipients = out
        return self._recipients

    def attachments(self) -> List[MsgAttachment]:
        if self._attachments is None:
            out = []
            i = 0
            for name in sorted(self.storage.children()):
                if name.startswith("__attach_version1.0_#"):
                    st = self.storage.children()[name]
                    if st.is_storage:
                        i += 1
                        out.append(MsgAttachment(self, i, st))
            self._attachments = out
        return self._attachments


def msg_row(source, folder, nid: int, path: str, opener):
    """A listing row for a .msg file: reads just the properties it needs."""
    from .sources import GenericRow
    m = MsgMessage(source, path, folder=folder)
    row = GenericRow(folder, nid, opener, subject=m.subject, sender=m.sender_name or m.sender_email, date=m.date,
                     size=m.size, has_attachments=m.has_attachments, to=m.display_to, read=m.read,
                     importance=m.importance, message_class=m.message_class)
    return row
