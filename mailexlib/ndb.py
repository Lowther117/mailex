"""Node Database layer of a PST / OST file, written from the [MS-PST] spec.

This is the storage layer: the file header, the two B-trees (node and block),
raw blocks with their two "encryption" ciphers, the data trees that stitch big
values together from many blocks, and the sub-node trees that hang recipients,
attachments and large property values off a node.

Three on-disk variants are handled:

  ANSI      32-bit identifiers, 512-byte pages, Outlook 97-2002 PST files
  UNICODE   64-bit identifiers, 512-byte pages, Outlook 2003+ PST and older OST
  UNICODE4K 64-bit identifiers, 4096-byte pages and zlib-compressed blocks,
            the OST format used by Outlook 2013 and later

Nothing here needs Outlook, MAPI or any compiled library.
"""
from __future__ import annotations

import os
import struct
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from ._crypt_tables import CYCLIC_R, CYCLIC_S, PERMUTE

MAGIC = b"!BDN"

FMT_ANSI = "ansi"
FMT_UNICODE = "unicode"
FMT_UNICODE4K = "unicode4k"

CRYPT_NONE = 0
CRYPT_PERMUTE = 1
CRYPT_CYCLIC = 2
CRYPT_WINDOWS = 0x10  # EFS style; cannot be read without the key

# node identifier types (low 5 bits of a NID)
NID_TYPE_HID = 0x00
NID_TYPE_INTERNAL = 0x01
NID_TYPE_NORMAL_FOLDER = 0x02
NID_TYPE_SEARCH_FOLDER = 0x03
NID_TYPE_NORMAL_MESSAGE = 0x04
NID_TYPE_ATTACHMENT = 0x05
NID_TYPE_ASSOC_MESSAGE = 0x08
NID_TYPE_HIERARCHY_TABLE = 0x0D
NID_TYPE_CONTENTS_TABLE = 0x0E
NID_TYPE_ASSOC_CONTENTS_TABLE = 0x0F
NID_TYPE_ATTACHMENT_TABLE = 0x11
NID_TYPE_RECIPIENT_TABLE = 0x12
NID_TYPE_LTP = 0x1F

NID_MESSAGE_STORE = 0x21
NID_NAME_TO_ID_MAP = 0x61
NID_ROOT_FOLDER = 0x122
NID_ATTACHMENT_TABLE = 0x671
NID_RECIPIENT_TABLE = 0x692


def nid_type(nid: int) -> int:
    return nid & 0x1F


class PSTError(Exception):
    """Anything structurally wrong with the file."""


class PSTUnsupported(PSTError):
    """The file is a PST but uses something this reader cannot handle."""


# --------------------------------------------------------------------------
# ciphers
# --------------------------------------------------------------------------
# The tables are stored in their DECRYPT direction (as libpff keeps them), so
# "compressible encryption" is undone with a single bytes.translate().  The
# cyclic cipher runs each byte through R, then S, then the permutation table,
# with offsets derived from a rolling 16-bit salt seeded from the block id.
_CYC_A = CYCLIC_R
_CYC_B = CYCLIC_S
_CYC_C = PERMUTE


def decrypt_permute(data: bytes) -> bytes:
    return data.translate(_CYC_C)


def decrypt_cyclic(data: bytes, key: int) -> bytes:
    salt = ((key >> 16) ^ key) & 0xFFFF
    out = bytearray(len(data))
    a, b, c = _CYC_A, _CYC_B, _CYC_C
    for i, byte in enumerate(data):
        lo = salt & 0xFF
        hi = (salt >> 8) & 0xFF
        x = (byte + lo) & 0xFF
        x = a[x]
        x = (x + hi) & 0xFF
        x = b[x]
        x = (x - hi) & 0xFF
        x = c[x]
        x = (x - lo) & 0xFF
        out[i] = x
        salt = (salt + 1) & 0xFFFF
    return bytes(out)


# --------------------------------------------------------------------------
# small structures
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BBTEntry:
    bid: int
    ib: int
    cb: int
    cref: int

    @property
    def internal(self) -> bool:
        return bool(self.bid & 2)


@dataclass(frozen=True)
class NBTEntry:
    nid: int
    bid_data: int
    bid_sub: int
    nid_parent: int


class _LRU:
    """A small LRU cache shared by the worker threads that read one file."""

    def __init__(self, cap: int):
        self.cap = cap
        self.d: "OrderedDict[int, object]" = OrderedDict()
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            v = self.d.get(k)
            if v is not None:
                self.d.move_to_end(k)
            return v

    def put(self, k, v):
        with self.lock:
            self.d[k] = v
            self.d.move_to_end(k)
            if len(self.d) > self.cap:
                self.d.popitem(last=False)


# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------
class NDB:
    """Open PST/OST file with lazy, cached access to nodes and blocks."""

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "rb")
        self._lock = threading.RLock()
        self.size = os.path.getsize(path)
        self._page_cache = _LRU(4096)
        self._block_cache = _LRU(512)
        self._sub_cache = _LRU(512)
        self._read_header()

    # -- lifecycle --------------------------------------------------------
    def close(self):
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.size:
            raise PSTError(f"read past end of file (offset {offset}, size {size})")
        with self._lock:
            self._fh.seek(offset)
            data = self._fh.read(size)
        if len(data) != size:
            raise PSTError("short read")
        return data

    # -- header -----------------------------------------------------------
    def _read_header(self):
        hdr = self._read_at(0, 564 if self.size >= 564 else self.size)
        if hdr[:4] != MAGIC:
            raise PSTError("not a PST/OST file (missing !BDN signature)")
        self.client_sig = hdr[8:10]
        (self.ver, self.ver_client) = struct.unpack_from("<HH", hdr, 10)
        if self.ver in (14, 15):
            self.fmt = FMT_ANSI
        elif self.ver in (21, 23):
            self.fmt = FMT_UNICODE
        elif self.ver >= 36:
            self.fmt = FMT_UNICODE4K
        else:
            raise PSTUnsupported(f"unknown NDB version {self.ver}")

        if self.fmt == FMT_ANSI:
            self.idsize = 4
            self.page_size = 512
            self.block_align = 64
            self.block_trailer = 12
            self.max_block = 8192
            self.file_eof = struct.unpack_from("<I", hdr, 168)[0]
            nbt_bid, nbt_ib, bbt_bid, bbt_ib = struct.unpack_from("<IIII", hdr, 184)
            self.crypt = hdr[461]
        else:
            self.idsize = 8
            self.file_eof = struct.unpack_from("<Q", hdr, 184)[0]
            nbt_bid, nbt_ib, bbt_bid, bbt_ib = struct.unpack_from("<QQQQ", hdr, 216)
            self.crypt = hdr[513]
            if self.fmt == FMT_UNICODE:
                self.page_size = 512
                self.block_align = 64
                self.block_trailer = 16
                self.max_block = 8192
            else:
                self.page_size = 4096
                self.block_align = 512
                self.block_trailer = 24
                self.max_block = 65536
        self.nbt_root = nbt_ib
        self.bbt_root = bbt_ib
        if self.crypt not in (CRYPT_NONE, CRYPT_PERMUTE, CRYPT_CYCLIC):
            if self.crypt == CRYPT_WINDOWS:
                raise PSTUnsupported("this file uses Windows (EFS-style) encryption, which needs the "
                                     "original user's key; it cannot be read without Outlook")
            raise PSTUnsupported(f"unknown encryption method 0x{self.crypt:02x}")
        # OST files ("SO") from Outlook 2013+ are normally the 4K variant. Nothing
        # else about them differs for reading purposes.
        self.is_ost = self.client_sig == b"SO"

    @property
    def description(self) -> str:
        kind = {b"SM": "PST", b"SO": "OST", b"AB": "PAB"}.get(self.client_sig, "PFF")
        fmt = {FMT_ANSI: "ANSI (32-bit)", FMT_UNICODE: "Unicode (64-bit)",
               FMT_UNICODE4K: "Unicode 4K-page (64-bit)"}[self.fmt]
        crypt = {CRYPT_NONE: "no encryption", CRYPT_PERMUTE: "compressible encryption",
                 CRYPT_CYCLIC: "high encryption"}.get(self.crypt, "unknown encryption")
        return f"{kind}, {fmt}, {crypt}"

    # -- pages ------------------------------------------------------------
    def _page(self, ib: int) -> Tuple[int, int, int, int, bytes]:
        """Return (cent, cbent, clevel, ptype, entries_bytes) for a B-tree page."""
        cached = self._page_cache.get(ib)
        if cached is not None:
            return cached
        page = self._read_at(ib, self.page_size)
        if self.fmt == FMT_ANSI:
            cent, cmax, cbent, clevel = struct.unpack_from("<BBBB", page, 496)
            ptype = page[500]
            entries = page[:496]
        elif self.fmt == FMT_UNICODE:
            cent, cmax, cbent, clevel = struct.unpack_from("<BBBB", page, 488)
            ptype = page[496]
            entries = page[:488]
        else:
            cent, cmax, cbent, clevel = struct.unpack_from("<HHBB", page, 4056)
            ptype = page[4072]
            entries = page[:4056]
        if ptype not in (0x80, 0x81):
            raise PSTError(f"expected a B-tree page at offset {ib}, found page type 0x{ptype:02x}")
        if cbent == 0 or cent * cbent > len(entries):
            raise PSTError(f"corrupt B-tree page at offset {ib}")
        result = (cent, cbent, clevel, ptype, entries)
        self._page_cache.put(ib, result)
        return result

    def _branch_entries(self, entries: bytes, cent: int, cbent: int) -> List[Tuple[int, int]]:
        out = []
        if self.idsize == 8:
            for i in range(cent):
                key, _bid, ib = struct.unpack_from("<QQQ", entries, i * cbent)
                out.append((key, ib))
        else:
            for i in range(cent):
                key, _bid, ib = struct.unpack_from("<III", entries, i * cbent)
                out.append((key, ib))
        return out

    def _descend(self, root_ib: int, key: int, want_ptype: int):
        """Walk from a root page to the leaf page that would hold `key`."""
        ib = root_ib
        for _ in range(64):  # depth guard
            cent, cbent, clevel, ptype, entries = self._page(ib)
            if ptype != want_ptype:
                raise PSTError("B-tree page type mismatch")
            if clevel == 0:
                return cent, cbent, entries
            branches = self._branch_entries(entries, cent, cbent)
            if not branches:
                return 0, cbent, entries
            # last entry whose key <= search key
            lo, hi = 0, len(branches) - 1
            pick = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if branches[mid][0] <= key:
                    pick = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            ib = branches[pick][1]
        raise PSTError("B-tree too deep (corrupt)")

    # -- block B-tree -----------------------------------------------------
    def _parse_bbt_leaf(self, entries: bytes, i: int, cbent: int) -> BBTEntry:
        if self.idsize == 8:
            bid, ib, cb, cref = struct.unpack_from("<QQHH", entries, i * cbent)
        else:
            bid, ib, cb, cref = struct.unpack_from("<IIHH", entries, i * cbent)
        return BBTEntry(bid, ib, cb, cref)

    def lookup_bbt(self, bid: int) -> Optional[BBTEntry]:
        bid &= ~1
        if bid == 0:
            return None
        cent, cbent, entries = self._descend(self.bbt_root, bid, 0x80)
        lo, hi = 0, cent - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            e = self._parse_bbt_leaf(entries, mid, cbent)
            k = e.bid & ~1
            if k == bid:
                return e
            if k < bid:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    # -- node B-tree ------------------------------------------------------
    def _parse_nbt_leaf(self, entries: bytes, i: int, cbent: int) -> NBTEntry:
        if self.idsize == 8:
            nid, bdata, bsub, parent = struct.unpack_from("<QQQI", entries, i * cbent)
            nid &= 0xFFFFFFFF
        else:
            nid, bdata, bsub, parent = struct.unpack_from("<IIII", entries, i * cbent)
        return NBTEntry(nid, bdata, bsub, parent)

    def lookup_nbt(self, nid: int) -> Optional[NBTEntry]:
        cent, cbent, entries = self._descend(self.nbt_root, nid, 0x81)
        lo, hi = 0, cent - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            e = self._parse_nbt_leaf(entries, mid, cbent)
            if e.nid == nid:
                return e
            if e.nid < nid:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    def iter_nbt(self) -> Iterator[NBTEntry]:
        """Every node in the file, in NID order. Used for recovery / orphan scans."""
        stack = [self.nbt_root]
        seen = set()
        while stack:
            ib = stack.pop()
            if ib in seen:
                continue
            seen.add(ib)
            try:
                cent, cbent, clevel, ptype, entries = self._page(ib)
            except PSTError:
                continue
            if clevel == 0:
                for i in range(cent):
                    yield self._parse_nbt_leaf(entries, i, cbent)
            else:
                for _key, child in reversed(self._branch_entries(entries, cent, cbent)):
                    stack.append(child)

    # -- blocks -----------------------------------------------------------
    def _block_span(self, cb: int) -> int:
        """Size on disk of a block holding cb bytes of data (data + padding + trailer)."""
        span = -(-cb // self.block_align) * self.block_align
        if span - cb < self.block_trailer:
            span += self.block_align
        return span

    def read_block(self, bid: int) -> bytes:
        """Raw bytes of one block, decompressed and decrypted as appropriate."""
        key = bid & ~1
        cached = self._block_cache.get(key)
        if cached is not None:
            return cached
        entry = self.lookup_bbt(bid)
        if entry is None:
            raise PSTError(f"block 0x{bid:x} is not in the block B-tree")
        data = self._read_block_entry(entry)
        self._block_cache.put(key, data)
        return data

    def _read_block_entry(self, entry: BBTEntry) -> bytes:
        cb = entry.cb
        if cb == 0:
            return b""
        span = self._block_span(cb)
        if span > self.max_block:
            # Some writers exceed the documented maximum; trust the index.
            span = self._block_span(cb)
        raw = self._read_at(entry.ib, span)
        data = raw[:cb]
        if self.fmt == FMT_UNICODE4K:
            trailer = raw[span - self.block_trailer:]
            uncompressed = struct.unpack_from("<H", trailer, 18)[0]
            if uncompressed and uncompressed != cb:
                try:
                    data = zlib.decompress(data)
                except zlib.error as exc:
                    raise PSTError(f"block 0x{entry.bid:x} would not decompress: {exc}") from exc
        if not entry.internal and self.crypt != CRYPT_NONE:
            if self.crypt == CRYPT_PERMUTE:
                data = decrypt_permute(data)
            else:
                data = decrypt_cyclic(data, entry.bid & 0xFFFFFFFF)
        return data

    # -- data trees -------------------------------------------------------
    def read_data_blocks(self, bid: int) -> List[bytes]:
        """All leaf data blocks referenced by bid, in order (XBLOCK/XXBLOCK aware)."""
        if bid == 0:
            return []
        entry = self.lookup_bbt(bid)
        if entry is None:
            raise PSTError(f"data block 0x{bid:x} missing from the block B-tree")
        data = self._read_block_entry(entry) if entry.internal else self.read_block(bid)
        if not entry.internal:
            return [data]
        if len(data) < 8 or data[0] != 1:
            # Internal flag set but not an array block: treat it as plain data.
            return [data]
        level = data[1]
        cent = struct.unpack_from("<H", data, 2)[0]
        ids = []
        fmt = "<Q" if self.idsize == 8 else "<I"
        for i in range(cent):
            off = 8 + i * self.idsize
            if off + self.idsize > len(data):
                break
            ids.append(struct.unpack_from(fmt, data, off)[0])
        out: List[bytes] = []
        if level == 1:
            for child in ids:
                out.append(self.read_block(child))
        else:
            for child in ids:
                out.extend(self.read_data_blocks(child))
        return out

    def read_data(self, bid: int) -> bytes:
        blocks = self.read_data_blocks(bid)
        if len(blocks) == 1:
            return blocks[0]
        return b"".join(blocks)

    # -- sub-node trees ---------------------------------------------------
    def read_subnodes(self, bid_sub: int) -> Dict[int, Tuple[int, int]]:
        """nid -> (bid_data, bid_sub) for every entry in a sub-node tree."""
        if bid_sub == 0:
            return {}
        cached = self._sub_cache.get(bid_sub)
        if cached is not None:
            return cached
        out: Dict[int, Tuple[int, int]] = {}
        self._walk_subnodes(bid_sub, out, 0)
        self._sub_cache.put(bid_sub, out)
        return out

    def _walk_subnodes(self, bid: int, out: Dict[int, Tuple[int, int]], depth: int):
        if depth > 8:
            raise PSTError("sub-node tree too deep (corrupt)")
        entry = self.lookup_bbt(bid)
        if entry is None:
            raise PSTError(f"sub-node block 0x{bid:x} missing from the block B-tree")
        data = self._read_block_entry(entry)
        if len(data) < 4 or data[0] != 2:
            raise PSTError(f"block 0x{bid:x} is not a sub-node block")
        level = data[1]
        cent = struct.unpack_from("<H", data, 2)[0]
        if self.idsize == 8:
            base = 8
            if level == 0:
                for i in range(cent):
                    off = base + i * 24
                    if off + 24 > len(data):
                        break
                    nid, bdata, bsub = struct.unpack_from("<QQQ", data, off)
                    out[nid & 0xFFFFFFFF] = (bdata, bsub)
            else:
                for i in range(cent):
                    off = base + i * 16
                    if off + 16 > len(data):
                        break
                    _nid, child = struct.unpack_from("<QQ", data, off)
                    self._walk_subnodes(child, out, depth + 1)
        else:
            base = 4
            if level == 0:
                for i in range(cent):
                    off = base + i * 12
                    if off + 12 > len(data):
                        break
                    nid, bdata, bsub = struct.unpack_from("<III", data, off)
                    out[nid] = (bdata, bsub)
            else:
                for i in range(cent):
                    off = base + i * 8
                    if off + 8 > len(data):
                        break
                    _nid, child = struct.unpack_from("<II", data, off)
                    self._walk_subnodes(child, out, depth + 1)

    # -- nodes ------------------------------------------------------------
    def node(self, nid: int) -> Optional["Node"]:
        e = self.lookup_nbt(nid)
        if e is None:
            return None
        return Node(self, e.nid, e.bid_data, e.bid_sub, e.nid_parent)


class Node:
    """A node: a data tree plus an optional sub-node tree.

    Sub-nodes are also Nodes, so an attachment inside a message inside a
    message is just a chain of .subnode() calls.
    """

    __slots__ = ("ndb", "nid", "bid_data", "bid_sub", "parent", "_subs")

    def __init__(self, ndb: NDB, nid: int, bid_data: int, bid_sub: int, parent: int = 0):
        self.ndb = ndb
        self.nid = nid
        self.bid_data = bid_data
        self.bid_sub = bid_sub
        self.parent = parent
        self._subs = None

    def data_blocks(self) -> List[bytes]:
        return self.ndb.read_data_blocks(self.bid_data)

    def data(self) -> bytes:
        return self.ndb.read_data(self.bid_data)

    def subnodes(self) -> Dict[int, Tuple[int, int]]:
        if self._subs is None:
            self._subs = self.ndb.read_subnodes(self.bid_sub) if self.bid_sub else {}
        return self._subs

    def subnode(self, nid: int) -> Optional["Node"]:
        ent = self.subnodes().get(nid)
        if ent is None:
            return None
        return Node(self.ndb, nid, ent[0], ent[1], self.nid)

    def __repr__(self):
        return f"Node(nid=0x{self.nid:x}, data=0x{self.bid_data:x}, sub=0x{self.bid_sub:x})"
