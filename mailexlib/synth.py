"""A small PST *writer*, used only by the self-test.

It builds a real Unicode-format PST from scratch - header, node and block
B-trees, heaps, property and table contexts, sub-node trees, multi-block data
trees - so the reader can be exercised end to end on any machine without
shipping somebody's mailbox in the repository. Optional "compressible" or
"high" encryption exercises both cipher paths.

It is deliberately minimal (one B-tree branch level, no allocation maps) and
is not meant for producing files for Outlook.
"""
from __future__ import annotations

import datetime as _dt
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from . import mapi
from ._crypt_tables import CYCLIC_R, CYCLIC_S, PERMUTE
from .ndb import CRYPT_CYCLIC, CRYPT_NONE, CRYPT_PERMUTE

MAX_BLOCK_DATA = 8176      # 8192 - 16 byte trailer
HEAP_ITEM_LIMIT = 3400     # bigger values go to a sub-node, like Outlook does

_INV_PERMUTE = bytes(PERMUTE.index(i) for i in range(256))
_INV_R = bytes(CYCLIC_R.index(i) for i in range(256))
_INV_S = bytes(CYCLIC_S.index(i) for i in range(256))


def encrypt_permute(data: bytes) -> bytes:
    return data.translate(_INV_PERMUTE)


def encrypt_cyclic(data: bytes, key: int) -> bytes:
    salt = ((key >> 16) ^ key) & 0xFFFF
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        lo = salt & 0xFF
        hi = (salt >> 8) & 0xFF
        y = (byte + lo) & 0xFF
        y = _INV_PERMUTE[y]
        y = (y + hi) & 0xFF
        y = _INV_S[y]
        y = (y - hi) & 0xFF
        y = _INV_R[y]
        y = (y - lo) & 0xFF
        out[i] = y
        salt = (salt + 1) & 0xFFFF
    return bytes(out)


def datetime_to_filetime(d: _dt.datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    delta = d - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    return int(delta.total_seconds() * 10_000_000) + 116444736000000000


def encode_value(ptype: int, value) -> bytes:
    """Bytes of a property value as stored in a PST."""
    if ptype == mapi.PT_UNICODE:
        return str(value).encode("utf-16-le")
    if ptype == mapi.PT_STRING8:
        return value if isinstance(value, bytes) else str(value).encode("cp1252", "replace")
    if ptype == mapi.PT_BINARY:
        return bytes(value)
    if ptype == mapi.PT_LONG:
        return struct.pack("<i", int(value))
    if ptype == mapi.PT_I2:
        return struct.pack("<h", int(value))
    if ptype == mapi.PT_BOOLEAN:
        return b"\x01" if value else b"\x00"
    if ptype == mapi.PT_SYSTIME:
        return struct.pack("<Q", datetime_to_filetime(value))
    if ptype == mapi.PT_I8:
        return struct.pack("<q", int(value))
    if ptype == mapi.PT_DOUBLE:
        return struct.pack("<d", float(value))
    if ptype == mapi.PT_OBJECT:
        nid, size = value
        return struct.pack("<II", nid, size)
    if ptype == mapi.PT_MV_FLAG | mapi.PT_UNICODE:
        items = [str(v).encode("utf-16-le") for v in value]
        offs = []
        pos = 4 + 4 * len(items)
        for it in items:
            offs.append(pos)
            pos += len(it)
        return struct.pack("<I", len(items)) + b"".join(struct.pack("<I", o) for o in offs) + b"".join(items)
    if ptype == mapi.PT_MV_FLAG | mapi.PT_LONG:
        return b"".join(struct.pack("<i", int(v)) for v in value)
    raise ValueError(f"unsupported type 0x{ptype:04x}")


class PSTWriter:
    def __init__(self, crypt: int = CRYPT_NONE):
        self.crypt = crypt
        self.chunks: List[Tuple[int, bytes]] = []  # (offset, bytes)
        self.pos = 0x4600                           # leave room for header + a fake AMap page
        self.bbt: Dict[int, Tuple[int, int]] = {}    # bid -> (ib, cb)
        self.nbt: Dict[int, Tuple[int, int, int]] = {}  # nid -> (bid_data, bid_sub, parent)
        self.next_bid = 4
        self.next_ltp = 1

    # -- raw blocks -----------------------------------------------------------
    def _alloc(self, size: int, align: int) -> int:
        self.pos = -(-self.pos // align) * align
        ib = self.pos
        self.pos += size
        return ib

    def add_block(self, data: bytes, internal: bool = False) -> int:
        if len(data) > MAX_BLOCK_DATA:
            raise ValueError("block too big")
        bid = self.next_bid | (2 if internal else 0)
        self.next_bid += 4
        payload = data
        if not internal and self.crypt == CRYPT_PERMUTE:
            payload = encrypt_permute(data)
        elif not internal and self.crypt == CRYPT_CYCLIC:
            payload = encrypt_cyclic(data, bid & 0xFFFFFFFF)
        cb = len(payload)
        span = -(-cb // 64) * 64
        if span - cb < 16:
            span += 64
        pad = span - cb - 16
        trailer = struct.pack("<HHIQ", cb, 0x1234, 0, bid)
        block = payload + b"\x00" * pad + trailer
        ib = self._alloc(span, 64)
        self.chunks.append((ib, block))
        self.bbt[bid] = (ib, cb)
        return bid

    def add_data(self, data: bytes) -> int:
        """Data of any size: a single block, or an XBLOCK (or XXBLOCK) tree."""
        if len(data) <= MAX_BLOCK_DATA:
            return self.add_block(data)
        leaves = [self.add_block(data[i:i + MAX_BLOCK_DATA]) for i in range(0, len(data), MAX_BLOCK_DATA)]
        xblocks = []
        per = (MAX_BLOCK_DATA - 8) // 8
        for i in range(0, len(leaves), per):
            group = leaves[i:i + per]
            total = sum(self.bbt[b][1] for b in group) if self.crypt == CRYPT_NONE else 0
            body = struct.pack("<BBHI", 1, 1, len(group), total) + b"".join(struct.pack("<Q", b) for b in group)
            xblocks.append(self.add_block(body, internal=True))
        if len(xblocks) == 1:
            return xblocks[0]
        body = struct.pack("<BBHI", 1, 2, len(xblocks), len(data)) + b"".join(struct.pack("<Q", b) for b in xblocks)
        return self.add_block(body, internal=True)

    def add_blocks(self, blocks: List[bytes]) -> int:
        """A data tree made of exactly these blocks (each one a heap block)."""
        if len(blocks) == 1:
            return self.add_block(blocks[0])
        leaves = [self.add_block(b) for b in blocks]
        total = sum(len(b) for b in blocks)
        body = struct.pack("<BBHI", 1, 1, len(leaves), total) + b"".join(struct.pack("<Q", b) for b in leaves)
        return self.add_block(body, internal=True)

    def add_subnodes(self, entries: Sequence[Tuple[int, int, int]]) -> int:
        """SLBLOCK from (nid, bid_data, bid_sub) entries."""
        if not entries:
            return 0
        entries = sorted(entries)
        body = struct.pack("<BBHI", 2, 0, len(entries), 0) + b"".join(
            struct.pack("<QQQ", nid, bd, bs) for nid, bd, bs in entries)
        return self.add_block(body, internal=True)

    def add_node(self, nid: int, bid_data: int, bid_sub: int = 0, parent: int = 0):
        self.nbt[nid] = (bid_data, bid_sub, parent)

    def ltp_nid(self) -> int:
        nid = (self.next_ltp << 5) | 0x1F
        self.next_ltp += 1
        return nid

    # -- B-tree pages ---------------------------------------------------------
    def _page(self, ptype: int, level: int, entries: List[bytes], cbent: int) -> int:
        body = b"".join(entries)
        assert len(body) <= 488
        page = body.ljust(488, b"\x00")
        bid = self.next_bid
        self.next_bid += 4
        page += struct.pack("<BBBB", len(entries), 488 // cbent, cbent, level) + b"\x00" * 4
        page += struct.pack("<BBHIQ", ptype, ptype, 0x1234, 0, bid)
        assert len(page) == 512
        ib = self._alloc(512, 512)
        self.chunks.append((ib, page))
        return ib

    def _build_tree(self, ptype: int, leaf_entries: List[Tuple[int, bytes]], cbent: int) -> int:
        leaf_entries.sort(key=lambda t: t[0])
        per = 488 // cbent
        level_nodes: List[Tuple[int, int]] = []  # (first key, ib)
        for i in range(0, len(leaf_entries), per):
            group = leaf_entries[i:i + per]
            ib = self._page(ptype, 0, [e for _k, e in group], cbent)
            level_nodes.append((group[0][0], ib))
        level = 1
        while len(level_nodes) > 1:
            nxt = []
            for i in range(0, len(level_nodes), 20):
                group = level_nodes[i:i + 20]
                ib = self._page(ptype, level, [struct.pack("<QQQ", key, 0, cib) for key, cib in group], 24)
                nxt.append((group[0][0], ib))
            level_nodes = nxt
            level += 1
        return level_nodes[0][1]

    def write(self, path: str):
        bbt_entries = [(bid, struct.pack("<QQHHI", bid, ib, cb, 2, 0)) for bid, (ib, cb) in self.bbt.items()]
        bbt_root = self._build_tree(0x80, bbt_entries, 24)
        nbt_entries = [(nid, struct.pack("<QQQII", nid, bd, bs, parent, 0)) for nid, (bd, bs, parent) in self.nbt.items()]
        nbt_root = self._build_tree(0x81, nbt_entries, 32)
        size = self.pos
        hdr = bytearray(564)
        hdr[0:4] = b"!BDN"
        hdr[8:10] = b"SM"
        struct.pack_into("<HH", hdr, 10, 23, 19)
        hdr[14] = hdr[15] = 1
        struct.pack_into("<Q", hdr, 184, size)          # ibFileEof
        struct.pack_into("<QQQQ", hdr, 216, 0, nbt_root, 0, bbt_root)
        hdr[248] = 2                                     # fAMapValid
        hdr[512] = 0x80
        hdr[513] = self.crypt
        struct.pack_into("<Q", hdr, 516, self.next_bid)
        out = bytearray(size)
        out[:564] = hdr
        for ib, data in self.chunks:
            out[ib:ib + len(data)] = data
        with open(path, "wb") as fh:
            fh.write(out)


# ---------------------------------------------------------------------------
# heap / BTH / PC / TC builders
# ---------------------------------------------------------------------------
class HeapBuilder:
    """A heap-on-node spread over as many data blocks as it needs."""

    def __init__(self, client_sig: int):
        self.client_sig = client_sig
        self.blocks: List[List[bytes]] = [[]]

    @staticmethod
    def _header_size(block_index: int) -> int:
        if block_index == 0:
            return 12
        if block_index % 128 == 8:
            return 66            # HNBITMAPHDR
        return 2                 # HNPAGEHDR

    def _used(self, block_index: int) -> int:
        items = self.blocks[block_index]
        return self._header_size(block_index) + sum(len(i) for i in items) + 4 + 2 * (len(items) + 1)

    def add(self, data: bytes) -> int:
        bi = len(self.blocks) - 1
        if self._used(bi) + len(data) + 2 > MAX_BLOCK_DATA or len(self.blocks[bi]) >= 2000:
            self.blocks.append([])
            bi += 1
        if len(data) > MAX_BLOCK_DATA - 100:
            raise ValueError("heap item too big - should have gone to a sub-node")
        self.blocks[bi].append(data)
        return (len(self.blocks[bi]) << 5) | (bi << 16)

    def build(self, user_root: int) -> List[bytes]:
        out = []
        for bi, items in enumerate(self.blocks):
            hs = self._header_size(bi)
            body = bytearray(hs)
            for it in items:
                body += it
            ibhnpm = len(body)
            offs = [hs]
            for it in items:
                offs.append(offs[-1] + len(it))
            body += struct.pack("<HH", len(items), 0) + b"".join(struct.pack("<H", o) for o in offs)
            if len(body) > MAX_BLOCK_DATA:
                raise ValueError("heap block overflow")
            struct.pack_into("<H", body, 0, ibhnpm)
            if bi == 0:
                struct.pack_into("<BBI", body, 2, 0xEC, self.client_sig, user_root)
            out.append(bytes(body))
        return out


def _bth(heap: HeapBuilder, cbkey: int, cbent: int, records: List[Tuple[bytes, bytes]]) -> int:
    records.sort(key=lambda r: int.from_bytes(r[0], "little"))
    data = b"".join(k + v for k, v in records)
    hid_data = heap.add(data) if data else 0
    return heap.add(struct.pack("<BBBBI", 0xB5, cbkey, cbent, 0, hid_data))


class NodeBuilder:
    """Collects the sub-nodes (large values, tables, attachments) of one node."""

    def __init__(self, w: PSTWriter):
        self.w = w
        self.subs: List[Tuple[int, int, int]] = []

    def add_sub(self, nid: int, bid_data: int, bid_sub: int = 0):
        self.subs.append((nid, bid_data, bid_sub))

    def big_value(self, data: bytes) -> int:
        nid = self.w.ltp_nid()
        self.add_sub(nid, self.w.add_data(data), 0)
        return nid

    def bid_sub(self) -> int:
        return self.w.add_subnodes(self.subs)


def build_pc(w: PSTWriter, nb: NodeBuilder, props: Dict[int, Tuple[int, object]]) -> int:
    """props: pid -> (ptype, value). Returns the data block bid."""
    heap = HeapBuilder(0xBC)
    records = []
    for pid, (ptype, value) in props.items():
        if ptype in mapi.PC_INLINE_TYPES:
            slot = encode_value(ptype, value).ljust(4, b"\x00")[:4]
        else:
            data = encode_value(ptype, value)
            if len(data) == 0:
                hnid = 0
            elif len(data) > HEAP_ITEM_LIMIT:
                hnid = nb.big_value(data)
            else:
                hnid = heap.add(data)
            slot = struct.pack("<I", hnid)
        records.append((struct.pack("<H", pid), struct.pack("<H", ptype) + slot))
    root = _bth(heap, 2, 6, records)
    return w.add_blocks(heap.build(root))


def build_tc(w: PSTWriter, nb: NodeBuilder, columns: List[Tuple[int, int]], rows: List[Dict[int, object]]) -> int:
    """columns: [(pid, ptype)] - PR_LTP_ROW_ID is added automatically. rows: pid -> value."""
    cols = [(mapi.PR_LTP_ROW_ID, mapi.PT_LONG)] + [c for c in columns if c[0] != mapi.PR_LTP_ROW_ID]
    # assign offsets: 8/4-byte, then 2-byte, then 1-byte, then the bitmap
    def size_of(ptype):
        return mapi.FIXED_SIZES.get(ptype, 4)
    order = range(len(cols))
    ib = 0
    layout: Dict[int, Tuple[int, int]] = {}
    for grp in ((8, 4), (2,), (1,)):
        for i in order:
            pid, ptype = cols[i]
            if size_of(ptype) in grp:
                layout[i] = (ib, size_of(ptype))
                ib += size_of(ptype)
        if grp == (8, 4):
            ib4 = ib
        elif grp == (2,):
            ib2 = ib
    ib1 = ib
    ceb = -(-len(cols) // 8)
    rowsize = ib1 + ceb
    heap = HeapBuilder(0x7C)
    # rows
    row_bytes = []
    index_records = []
    for ri, row in enumerate(rows):
        buf = bytearray(rowsize)
        bitmap = bytearray(ceb)
        for i, (pid, ptype) in enumerate(cols):
            if pid not in row:
                continue
            off, cb = layout[i]
            value = row[pid]
            if ptype in mapi.FIXED_SIZES:
                buf[off:off + cb] = encode_value(ptype, value)[:cb].ljust(cb, b"\x00")
            else:
                data = encode_value(ptype, value)
                if len(data) == 0:
                    hnid = 0
                elif len(data) > HEAP_ITEM_LIMIT:
                    hnid = nb.big_value(data)
                else:
                    hnid = heap.add(data)
                buf[off:off + 4] = struct.pack("<I", hnid)
            bitmap[i >> 3] |= 1 << (7 - (i & 7))
        buf[ib1:ib1 + ceb] = bitmap
        row_bytes.append(bytes(buf))
        index_records.append((struct.pack("<I", int(row[mapi.PR_LTP_ROW_ID])), struct.pack("<I", ri)))
    all_rows = b"".join(row_bytes)
    if all_rows and len(all_rows) <= HEAP_ITEM_LIMIT:
        hnid_rows = heap.add(all_rows)
    elif all_rows:
        # rows must not straddle blocks: chunk by whole rows
        per = MAX_BLOCK_DATA // rowsize
        nid = w.ltp_nid()
        chunks = [b"".join(row_bytes[i:i + per]) for i in range(0, len(row_bytes), per)]
        if len(chunks) == 1:
            bid = w.add_block(chunks[0])
        else:
            leaves = [w.add_block(c) for c in chunks]
            body = struct.pack("<BBHI", 1, 1, len(leaves), len(all_rows)) + b"".join(struct.pack("<Q", b) for b in leaves)
            bid = w.add_block(body, internal=True)
        nb.add_sub(nid, bid, 0)
        hnid_rows = nid
    else:
        hnid_rows = 0
    hid_row_index = _bth(heap, 4, 4, index_records)
    info = struct.pack("<BBHHHHIII", 0x7C, len(cols), ib4, ib2, ib1, rowsize, hid_row_index, hnid_rows, 0)
    for i, (pid, ptype) in enumerate(cols):
        off, cb = layout[i]
        info += struct.pack("<IHBB", (pid << 16) | ptype, off, cb, i)
    root = heap.add(info)
    return w.add_blocks(heap.build(root))


# ---------------------------------------------------------------------------
# a whole mailbox
# ---------------------------------------------------------------------------
FOLDER_COLS = [(mapi.PR_DISPLAY_NAME, mapi.PT_UNICODE), (mapi.PR_CONTENT_COUNT, mapi.PT_LONG),
               (mapi.PR_CONTENT_UNREAD, mapi.PT_LONG), (mapi.PR_SUBFOLDERS, mapi.PT_BOOLEAN),
               (mapi.PR_CONTAINER_CLASS, mapi.PT_UNICODE)]
MSG_COLS = [(mapi.PR_SUBJECT, mapi.PT_UNICODE), (mapi.PR_SENDER_NAME, mapi.PT_UNICODE),
            (mapi.PR_MESSAGE_DELIVERY_TIME, mapi.PT_SYSTIME), (mapi.PR_MESSAGE_SIZE, mapi.PT_LONG),
            (mapi.PR_HASATTACH, mapi.PT_BOOLEAN), (mapi.PR_MESSAGE_CLASS, mapi.PT_UNICODE),
            (mapi.PR_DISPLAY_TO, mapi.PT_UNICODE), (mapi.PR_MESSAGE_FLAGS, mapi.PT_LONG),
            (mapi.PR_IMPORTANCE, mapi.PT_LONG)]
RECIP_COLS = [(mapi.PR_RECIPIENT_TYPE, mapi.PT_LONG), (mapi.PR_DISPLAY_NAME, mapi.PT_UNICODE),
              (mapi.PR_ADDRTYPE, mapi.PT_UNICODE), (mapi.PR_EMAIL_ADDRESS, mapi.PT_UNICODE),
              (mapi.PR_SMTP_ADDRESS, mapi.PT_UNICODE)]
ATT_COLS = [(mapi.PR_ATTACH_SIZE, mapi.PT_LONG), (mapi.PR_ATTACH_FILENAME, mapi.PT_UNICODE),
            (mapi.PR_ATTACH_METHOD, mapi.PT_LONG), (mapi.PR_RENDERING_POSITION, mapi.PT_LONG)]


class SynthMessage:
    def __init__(self, subject: str, sender: str, sender_email: str, to: List[Tuple[str, str]],
                 date: _dt.datetime, body: str = "", html: str = "", rtf: bytes = b"",
                 attachments: Optional[List[tuple]] = None,
                 embedded: Optional["SynthMessage"] = None, message_class: str = "IPM.Note",
                 read: bool = True, extra: Optional[Dict[int, Tuple[int, object]]] = None,
                 cc: Optional[List[Tuple[str, str]]] = None, headers: str = ""):
        self.subject = subject
        self.sender = sender
        self.sender_email = sender_email
        self.to = to
        self.cc = cc or []
        self.date = date
        self.body = body
        self.html = html
        self.rtf = rtf
        self.attachments = attachments or []   # (filename, data, mime[, content-id])
        self.embedded = embedded
        self.message_class = message_class
        self.read = read
        self.extra = extra or {}
        self.headers = headers


def _write_message(w: PSTWriter, m: SynthMessage, nid: int, parent_nid: int, embedded: bool = False) -> Tuple[int, int]:
    """Returns (bid_data, bid_sub) for the message node."""
    nb = NodeBuilder(w)
    flags = (1 if m.read else 0) | (0x10 if (m.attachments or m.embedded) else 0)
    props: Dict[int, Tuple[int, object]] = {
        mapi.PR_MESSAGE_CLASS: (mapi.PT_UNICODE, m.message_class),
        mapi.PR_SUBJECT: (mapi.PT_UNICODE, m.subject),
        mapi.PR_SENDER_NAME: (mapi.PT_UNICODE, m.sender),
        mapi.PR_SENDER_EMAIL: (mapi.PT_UNICODE, m.sender_email),
        mapi.PR_SENDER_ADDRTYPE: (mapi.PT_UNICODE, "SMTP"),
        mapi.PR_SENT_REPRESENTING_NAME: (mapi.PT_UNICODE, m.sender),
        mapi.PR_MESSAGE_DELIVERY_TIME: (mapi.PT_SYSTIME, m.date),
        mapi.PR_CLIENT_SUBMIT_TIME: (mapi.PT_SYSTIME, m.date),
        mapi.PR_MESSAGE_FLAGS: (mapi.PT_LONG, flags),
        mapi.PR_HASATTACH: (mapi.PT_BOOLEAN, bool(m.attachments or m.embedded)),
        mapi.PR_DISPLAY_TO: (mapi.PT_UNICODE, "; ".join(n for n, _a in m.to)),
        mapi.PR_DISPLAY_CC: (mapi.PT_UNICODE, "; ".join(n for n, _a in m.cc)),
        mapi.PR_IMPORTANCE: (mapi.PT_LONG, 1),
        mapi.PR_MESSAGE_SIZE: (mapi.PT_LONG, len(m.body) + len(m.html) + sum(len(a[1]) for a in m.attachments) + 500),
        mapi.PR_INTERNET_MESSAGE_ID: (mapi.PT_UNICODE, f"<synth-{nid:x}@mailex.test>"),
    }
    if m.body:
        props[mapi.PR_BODY] = (mapi.PT_UNICODE, m.body)
    if m.html:
        props[mapi.PR_HTML] = (mapi.PT_BINARY, m.html.encode("utf-8"))
        props[mapi.PR_INTERNET_CPID] = (mapi.PT_LONG, 65001)
    if m.rtf:
        props[mapi.PR_RTF_COMPRESSED] = (mapi.PT_BINARY, m.rtf)
    if m.headers:
        props[mapi.PR_TRANSPORT_HEADERS] = (mapi.PT_UNICODE, m.headers)
    props.update(m.extra)
    # recipients
    rrows = []
    for i, (name, addr) in enumerate([(n, a) for n, a in m.to] + [(n, a) for n, a in m.cc]):
        kind = 1 if i < len(m.to) else 2
        rrows.append({mapi.PR_LTP_ROW_ID: 0x100 + i, mapi.PR_RECIPIENT_TYPE: kind, mapi.PR_DISPLAY_NAME: name,
                      mapi.PR_ADDRTYPE: "SMTP", mapi.PR_EMAIL_ADDRESS: addr, mapi.PR_SMTP_ADDRESS: addr})
    if rrows:
        rnb = NodeBuilder(w)
        rbid = build_tc(w, rnb, RECIP_COLS, rrows)
        nb.add_sub(0x692, rbid, rnb.bid_sub())
    # attachments
    arows = []
    att_index = 0
    for att in m.attachments:
        fname, data, mime = att[0], att[1], att[2]
        cid = att[3] if len(att) > 3 else ""
        anid = ((att_index + 1) << 5) | 0x05
        att_index += 1
        anb = NodeBuilder(w)
        aprops = {
            mapi.PR_ATTACH_METHOD: (mapi.PT_LONG, mapi.ATTACH_BY_VALUE),
            mapi.PR_ATTACH_FILENAME: (mapi.PT_UNICODE, fname[:8] + fname[fname.rfind("."):] if "." in fname else fname),
            mapi.PR_ATTACH_LONG_FILENAME: (mapi.PT_UNICODE, fname),
            mapi.PR_ATTACH_MIME_TAG: (mapi.PT_UNICODE, mime),
            mapi.PR_ATTACH_SIZE: (mapi.PT_LONG, len(data) + 200),
            mapi.PR_ATTACH_DATA: (mapi.PT_BINARY, data),
            mapi.PR_RENDERING_POSITION: (mapi.PT_LONG, -1),
        }
        if cid:
            aprops[mapi.PR_ATTACH_CONTENT_ID] = (mapi.PT_UNICODE, cid)
            aprops[mapi.PR_ATTACHMENT_HIDDEN] = (mapi.PT_BOOLEAN, True)
        abid = build_pc(w, anb, aprops)
        nb.add_sub(anid, abid, anb.bid_sub())
        arows.append({mapi.PR_LTP_ROW_ID: anid, mapi.PR_ATTACH_SIZE: len(data) + 200, mapi.PR_ATTACH_FILENAME: fname,
                      mapi.PR_ATTACH_METHOD: mapi.ATTACH_BY_VALUE, mapi.PR_RENDERING_POSITION: -1})
    if m.embedded is not None:
        anid = ((att_index + 1) << 5) | 0x05
        att_index += 1
        anb = NodeBuilder(w)
        enid = 0x8000004 + att_index  # any message-typed nid inside the attachment's sub-tree
        enid = (enid & ~0x1F) | 0x04
        ebid, esub = _write_message(w, m.embedded, enid, 0, embedded=True)
        anb.add_sub(enid, ebid, esub)
        aprops = {
            mapi.PR_ATTACH_METHOD: (mapi.PT_LONG, mapi.ATTACH_EMBEDDED_MSG),
            mapi.PR_ATTACH_LONG_FILENAME: (mapi.PT_UNICODE, m.embedded.subject + ".eml"),
            mapi.PR_DISPLAY_NAME: (mapi.PT_UNICODE, m.embedded.subject),
            mapi.PR_ATTACH_SIZE: (mapi.PT_LONG, 4000),
            mapi.PR_ATTACH_DATA: (mapi.PT_OBJECT, (enid, 4000)),
            mapi.PR_RENDERING_POSITION: (mapi.PT_LONG, -1),
        }
        abid = build_pc(w, anb, aprops)
        nb.add_sub(anid, abid, anb.bid_sub())
        arows.append({mapi.PR_LTP_ROW_ID: anid, mapi.PR_ATTACH_SIZE: 4000, mapi.PR_ATTACH_FILENAME: m.embedded.subject + ".eml",
                      mapi.PR_ATTACH_METHOD: mapi.ATTACH_EMBEDDED_MSG, mapi.PR_RENDERING_POSITION: -1})
    if arows:
        anb2 = NodeBuilder(w)
        abid = build_tc(w, anb2, ATT_COLS, arows)
        nb.add_sub(0x671, abid, anb2.bid_sub())
    bid_data = build_pc(w, nb, props)
    bid_sub = nb.bid_sub()
    if not embedded:
        w.add_node(nid, bid_data, bid_sub, parent_nid)
    return bid_data, bid_sub


class SynthFolder:
    def __init__(self, name: str, messages: Optional[List[SynthMessage]] = None,
                 subfolders: Optional[List["SynthFolder"]] = None, container_class: str = "IPM.Note"):
        self.name = name
        self.messages = messages or []
        self.subfolders = subfolders or []
        self.container_class = container_class


def write_pst(path: str, root_folders: List[SynthFolder], store_name: str = "Synthetic test store",
              crypt: int = CRYPT_NONE):
    w = PSTWriter(crypt)
    next_folder = [0x8022]
    next_msg = [0x200004]

    def folder_row(nid: int, f: SynthFolder) -> Dict[int, object]:
        return {mapi.PR_LTP_ROW_ID: nid, mapi.PR_DISPLAY_NAME: f.name, mapi.PR_CONTENT_COUNT: len(f.messages),
                mapi.PR_CONTENT_UNREAD: sum(1 for m in f.messages if not m.read),
                mapi.PR_SUBFOLDERS: bool(f.subfolders), mapi.PR_CONTAINER_CLASS: f.container_class}

    def write_folder(nid: int, f: SynthFolder, parent: int):
        nb = NodeBuilder(w)
        props = {mapi.PR_DISPLAY_NAME: (mapi.PT_UNICODE, f.name),
                 mapi.PR_CONTENT_COUNT: (mapi.PT_LONG, len(f.messages)),
                 mapi.PR_CONTENT_UNREAD: (mapi.PT_LONG, sum(1 for m in f.messages if not m.read)),
                 mapi.PR_SUBFOLDERS: (mapi.PT_BOOLEAN, bool(f.subfolders)),
                 mapi.PR_CONTAINER_CLASS: (mapi.PT_UNICODE, f.container_class)}
        w.add_node(nid, build_pc(w, nb, props), nb.bid_sub(), parent)
        # children
        child_rows = []
        for sf in f.subfolders:
            cnid = next_folder[0]
            next_folder[0] += 0x20
            child_rows.append(folder_row(cnid, sf))
            write_folder(cnid, sf, nid)
        hnb = NodeBuilder(w)
        w.add_node((nid & ~0x1F) | 0x0D, build_tc(w, hnb, FOLDER_COLS, child_rows), hnb.bid_sub(), nid)
        msg_rows = []
        for m in f.messages:
            mnid = next_msg[0]
            next_msg[0] += 0x20
            _write_message(w, m, mnid, nid)
            msg_rows.append({mapi.PR_LTP_ROW_ID: mnid, mapi.PR_SUBJECT: m.subject, mapi.PR_SENDER_NAME: m.sender,
                             mapi.PR_MESSAGE_DELIVERY_TIME: m.date, mapi.PR_MESSAGE_SIZE: len(m.body) + 500,
                             mapi.PR_HASATTACH: bool(m.attachments or m.embedded), mapi.PR_MESSAGE_CLASS: m.message_class,
                             mapi.PR_DISPLAY_TO: "; ".join(n for n, _a in m.to),
                             mapi.PR_MESSAGE_FLAGS: (1 if m.read else 0) | (0x10 if (m.attachments or m.embedded) else 0),
                             mapi.PR_IMPORTANCE: 1})
        cnb = NodeBuilder(w)
        w.add_node((nid & ~0x1F) | 0x0E, build_tc(w, cnb, MSG_COLS, msg_rows), cnb.bid_sub(), nid)

    # store
    snb = NodeBuilder(w)
    w.add_node(0x21, build_pc(w, snb, {mapi.PR_DISPLAY_NAME: (mapi.PT_UNICODE, store_name),
                                       mapi.PR_MESSAGE_CODEPAGE: (mapi.PT_LONG, 1252)}), snb.bid_sub(), 0)
    # root folder (nid 0x122) and its children
    root = SynthFolder("", subfolders=root_folders)
    write_folder(0x122, root, 0x122)
    w.write(path)
    return path


# ---------------------------------------------------------------------------
# a compound file (OLE2) and .msg writer - again test-only
# ---------------------------------------------------------------------------
class _Node:
    def __init__(self, name: str, data: Optional[bytes] = None):
        self.name = name
        self.data = data          # None = storage
        self.kids: List["_Node"] = []
        self.index = -1
        self.start = 0xFFFFFFFE

    def storage(self, name: str) -> "_Node":
        n = _Node(name)
        self.kids.append(n)
        return n

    def stream(self, name: str, data: bytes) -> "_Node":
        n = _Node(name, data)
        self.kids.append(n)
        return n


def write_compound_file(path: str, root: _Node, sector_size: int = 512):
    """A version-3 compound file. Directory trees are written as right-hand
    chains (valid to read, if not balanced), small streams go in the mini stream."""
    MINI = 64
    CUTOFF = 4096
    ENDOFCHAIN, FREESECT, FATSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
    per = sector_size // 4
    # number the entries: root first, then depth-first
    entries: List[_Node] = []

    def number(n: _Node):
        n.index = len(entries)
        entries.append(n)
        for k in n.kids:
            number(k)
    number(root)
    # mini stream
    mini = bytearray()
    minifat: List[int] = []
    for e in entries:
        if e is root or e.data is None or len(e.data) == 0 or len(e.data) >= CUTOFF:
            continue
        first = len(minifat)
        nsec = -(-len(e.data) // MINI)
        for i in range(nsec):
            minifat.append(first + i + 1 if i < nsec - 1 else ENDOFCHAIN)
        mini += e.data.ljust(nsec * MINI, b"\x00")
        e.start = first
    # big streams (including the mini stream itself as the root's data)
    fat: List[int] = []
    sectors: List[bytes] = []

    def add_big(data: bytes) -> int:
        if not data:
            return ENDOFCHAIN
        nsec = -(-len(data) // sector_size)
        first = len(sectors)
        for i in range(nsec):
            sectors.append(data[i * sector_size:(i + 1) * sector_size].ljust(sector_size, b"\x00"))
            fat.append(first + i + 1 if i < nsec - 1 else ENDOFCHAIN)
        return first
    for e in entries:
        if e is root or e.data is None or len(e.data) < CUTOFF:
            continue
        e.start = add_big(e.data)
    root.data = bytes(mini)
    root.start = add_big(root.data) if mini else ENDOFCHAIN
    mf = b"".join(struct.pack("<I", v) for v in minifat)
    first_minifat = add_big(mf)
    num_minifat = -(-len(mf) // sector_size) if mf else 0
    # directory
    dir_entries = []
    for e in entries:
        name = (e.name + "\x00").encode("utf-16-le")
        left = FREESECT
        right = FREESECT
        child = FREESECT
        parent = next((p for p in entries if e in p.kids), None)
        if parent is not None:
            sib = parent.kids
            i = sib.index(e)
            if i + 1 < len(sib):
                right = sib[i + 1].index
        if e.kids:
            child = e.kids[0].index
        etype = 5 if e is root else (1 if e.data is None else 2)
        size = len(e.data) if e.data is not None else 0
        rec = name.ljust(64, b"\x00") + struct.pack("<HBBIII", len(name), etype, 1, left, right, child)
        rec += b"\x00" * 16 + struct.pack("<I", 0) + b"\x00" * 16 + struct.pack("<IQ", e.start if size or e is root else ENDOFCHAIN, size)
        assert len(rec) == 128
        dir_entries.append(rec)
    dirdata = b"".join(dir_entries)
    first_dir = add_big(dirdata)
    # FAT sectors: mark them, then write them
    nfat = -(-(len(fat) + 1) // per)
    while -(-(len(fat) + nfat) // per) > nfat:
        nfat += 1
    fat_first = len(sectors)
    for i in range(nfat):
        fat.append(FATSECT)
        sectors.append(b"")
    padded = fat + [FREESECT] * (nfat * per - len(fat))
    fatbytes = b"".join(struct.pack("<I", v) for v in padded)
    for i in range(nfat):
        sectors[fat_first + i] = fatbytes[i * sector_size:(i + 1) * sector_size]
    hdr = bytearray(512)
    hdr[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", hdr, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIII", hdr, 0x2C, nfat, first_dir, 0, CUTOFF, first_minifat, num_minifat, ENDOFCHAIN, 0)
    difat = [fat_first + i for i in range(nfat)] + [FREESECT] * (109 - nfat)
    struct.pack_into("<109I", hdr, 0x4C, *difat)
    with open(path, "wb") as fh:
        fh.write(bytes(hdr))
        for s in sectors:
            fh.write(s)


def _msg_props(st: _Node, props: Dict[int, Tuple[int, object]], header: bytes):
    """Write a property set into a storage: substg streams plus the properties stream."""
    body = bytearray()
    for pid, (ptype, value) in sorted(props.items()):
        if ptype in mapi.FIXED_SIZES and mapi.FIXED_SIZES[ptype] <= 8 or ptype == mapi.PT_BOOLEAN:
            data = encode_value(ptype, value)
            body += struct.pack("<II", (pid << 16) | ptype, 6) + data.ljust(8, b"\x00")[:8]
        else:
            data = encode_value(ptype, value)
            if ptype in (mapi.PT_UNICODE,):
                data += b"\x00\x00"
            elif ptype == mapi.PT_STRING8:
                data += b"\x00"
            st.stream(f"__substg1.0_{pid:04X}{ptype:04X}", data)
            body += struct.pack("<II", (pid << 16) | ptype, 6) + struct.pack("<II", len(data), 0)
    st.stream("__properties_version1.0", header + bytes(body))


def build_msg_tree(m: SynthMessage, st: _Node, embedded: bool = False):
    props: Dict[int, Tuple[int, object]] = {
        mapi.PR_MESSAGE_CLASS: (mapi.PT_UNICODE, m.message_class),
        mapi.PR_SUBJECT: (mapi.PT_UNICODE, m.subject),
        mapi.PR_SENDER_NAME: (mapi.PT_UNICODE, m.sender),
        mapi.PR_SENDER_EMAIL: (mapi.PT_UNICODE, m.sender_email),
        mapi.PR_SENDER_ADDRTYPE: (mapi.PT_UNICODE, "SMTP"),
        mapi.PR_MESSAGE_DELIVERY_TIME: (mapi.PT_SYSTIME, m.date),
        mapi.PR_CLIENT_SUBMIT_TIME: (mapi.PT_SYSTIME, m.date),
        mapi.PR_MESSAGE_FLAGS: (mapi.PT_LONG, (1 if m.read else 0) | (0x10 if (m.attachments or m.embedded) else 0)),
        mapi.PR_HASATTACH: (mapi.PT_BOOLEAN, bool(m.attachments or m.embedded)),
        mapi.PR_DISPLAY_TO: (mapi.PT_UNICODE, "; ".join(n for n, _a in m.to)),
        mapi.PR_IMPORTANCE: (mapi.PT_LONG, 1),
        mapi.PR_MESSAGE_CODEPAGE: (mapi.PT_LONG, 1252),
        0x340D: (mapi.PT_LONG, 0x40000),        # PR_STORE_SUPPORT_MASK: unicode strings
    }
    if m.body:
        props[mapi.PR_BODY] = (mapi.PT_UNICODE, m.body)
    if m.html:
        props[mapi.PR_HTML] = (mapi.PT_BINARY, m.html.encode("utf-8"))
        props[mapi.PR_INTERNET_CPID] = (mapi.PT_LONG, 65001)
    if m.rtf:
        props[mapi.PR_RTF_COMPRESSED] = (mapi.PT_BINARY, m.rtf)
    if m.headers:
        props[mapi.PR_TRANSPORT_HEADERS] = (mapi.PT_UNICODE, m.headers)
    props.update(m.extra)
    recips = [(1, n, a) for n, a in m.to] + [(2, n, a) for n, a in m.cc]
    natt = len(m.attachments) + (1 if m.embedded else 0)
    if embedded:
        header = b"\x00" * 8 + struct.pack("<IIII", len(recips), natt, len(recips), natt)
    else:
        header = b"\x00" * 8 + struct.pack("<IIII", len(recips), natt, len(recips), natt) + b"\x00" * 8
    _msg_props(st, props, header)
    st.storage("__nameid_version1.0")
    for i, (kind, name, addr) in enumerate(recips):
        rs = st.storage(f"__recip_version1.0_#{i:08X}")
        _msg_props(rs, {mapi.PR_RECIPIENT_TYPE: (mapi.PT_LONG, kind), mapi.PR_DISPLAY_NAME: (mapi.PT_UNICODE, name),
                        mapi.PR_ADDRTYPE: (mapi.PT_UNICODE, "SMTP"), mapi.PR_EMAIL_ADDRESS: (mapi.PT_UNICODE, addr),
                        mapi.PR_SMTP_ADDRESS: (mapi.PT_UNICODE, addr), 0x3000: (mapi.PT_LONG, i)}, b"\x00" * 8)
    ai = 0
    for att in m.attachments:
        fname, data, mime = att[0], att[1], att[2]
        cid = att[3] if len(att) > 3 else ""
        a = st.storage(f"__attach_version1.0_#{ai:08X}")
        ai += 1
        ap = {mapi.PR_ATTACH_METHOD: (mapi.PT_LONG, mapi.ATTACH_BY_VALUE),
              mapi.PR_ATTACH_LONG_FILENAME: (mapi.PT_UNICODE, fname),
              mapi.PR_ATTACH_FILENAME: (mapi.PT_UNICODE, fname[:8]),
              mapi.PR_ATTACH_MIME_TAG: (mapi.PT_UNICODE, mime),
              mapi.PR_ATTACH_SIZE: (mapi.PT_LONG, len(data) + 200),
              mapi.PR_ATTACH_DATA: (mapi.PT_BINARY, data),
              mapi.PR_RENDERING_POSITION: (mapi.PT_LONG, -1)}
        if cid:
            ap[mapi.PR_ATTACH_CONTENT_ID] = (mapi.PT_UNICODE, cid)
            ap[mapi.PR_ATTACHMENT_HIDDEN] = (mapi.PT_BOOLEAN, True)
        _msg_props(a, ap, b"\x00" * 8)
    if m.embedded is not None:
        a = st.storage(f"__attach_version1.0_#{ai:08X}")
        ap = {mapi.PR_ATTACH_METHOD: (mapi.PT_LONG, mapi.ATTACH_EMBEDDED_MSG),
              mapi.PR_ATTACH_LONG_FILENAME: (mapi.PT_UNICODE, m.embedded.subject + ".eml"),
              mapi.PR_DISPLAY_NAME: (mapi.PT_UNICODE, m.embedded.subject),
              mapi.PR_RENDERING_POSITION: (mapi.PT_LONG, -1)}
        _msg_props(a, ap, b"\x00" * 8)
        inner = a.storage("__substg1.0_3701000D")
        build_msg_tree(m.embedded, inner, embedded=True)


def write_msg(path: str, m: SynthMessage):
    root = _Node("Root Entry")
    build_msg_tree(m, root)
    write_compound_file(path, root)
    return path
