"""Lists, Tables and Properties layer: heap-on-node, BTH, property and table contexts."""
from __future__ import annotations

import struct
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import mapi
from .ndb import FMT_UNICODE4K, Node, PSTError

HN_SIG = 0xEC
CLIENT_PC = 0xBC   # property context
CLIENT_TC = 0x7C   # table context
CLIENT_BTH = 0xB5


class HeapOnNode:
    """The heap that a node's data blocks make up (HN in [MS-PST])."""

    __slots__ = ("blocks", "fmt4k", "client_sig", "user_root", "_maps")

    def __init__(self, blocks: List[bytes], fmt4k: bool):
        if not blocks or len(blocks[0]) < 12:
            raise PSTError("node has no heap")
        self.blocks = blocks
        self.fmt4k = fmt4k
        first = blocks[0]
        if first[2] != HN_SIG:
            raise PSTError(f"heap signature missing (0x{first[2]:02x})")
        self.client_sig = first[3]
        self.user_root = struct.unpack_from("<I", first, 4)[0]
        self._maps: Dict[int, List[int]] = {}

    def _page_map(self, block_index: int) -> List[int]:
        pm = self._maps.get(block_index)
        if pm is not None:
            return pm
        if block_index >= len(self.blocks):
            raise PSTError(f"heap block {block_index} out of range")
        block = self.blocks[block_index]
        if len(block) < 4:
            raise PSTError("heap block too small")
        ibhnpm = struct.unpack_from("<H", block, 0)[0]
        if ibhnpm + 4 > len(block):
            raise PSTError("heap page map out of range")
        calloc, _cfree = struct.unpack_from("<HH", block, ibhnpm)
        need = ibhnpm + 4 + (calloc + 1) * 2
        if need > len(block):
            raise PSTError("heap page map truncated")
        pm = list(struct.unpack_from(f"<{calloc + 1}H", block, ibhnpm + 4))
        self._maps[block_index] = pm
        return pm

    def split_hid(self, hid: int) -> Tuple[int, int]:
        if self.fmt4k:
            return (hid >> 5) & 0x3FFF, hid >> 19
        return (hid >> 5) & 0x7FF, hid >> 16

    def get(self, hid: int) -> bytes:
        if hid == 0:
            return b""
        if hid & 0x1F:
            raise PSTError(f"0x{hid:x} is a node id, not a heap id")
        index, block_index = self.split_hid(hid)
        if index == 0:
            return b""
        pm = self._page_map(block_index)
        if index >= len(pm):
            raise PSTError(f"heap id 0x{hid:x} out of range")
        start, end = pm[index - 1], pm[index]
        block = self.blocks[block_index]
        if start > end or end > len(block):
            raise PSTError(f"heap id 0x{hid:x} has a bad allocation")
        return block[start:end]


def bth_entries(hn: HeapOnNode, hid_header: int) -> Tuple[int, int, List[Tuple[bytes, bytes]]]:
    """Return (cbKey, cbEnt, [(key_bytes, data_bytes), ...]) for a BTH."""
    hdr = hn.get(hid_header)
    if len(hdr) < 8 or hdr[0] != CLIENT_BTH:
        raise PSTError("BTH header missing")
    cbkey, cbent, levels = hdr[1], hdr[2], hdr[3]
    hid_root = struct.unpack_from("<I", hdr, 4)[0]
    out: List[Tuple[bytes, bytes]] = []
    if hid_root == 0:
        return cbkey, cbent, out
    if cbkey not in (2, 4, 8, 16) or cbent == 0 or cbent > 32:
        raise PSTError("BTH header has impossible sizes")

    def walk(hid: int, level: int, depth: int):
        if depth > 8:
            raise PSTError("BTH too deep")
        data = hn.get(hid)
        if level > 0:
            rec = cbkey + 4
            for off in range(0, len(data) - rec + 1, rec):
                child = struct.unpack_from("<I", data, off + cbkey)[0]
                if child:
                    walk(child, level - 1, depth + 1)
        else:
            rec = cbkey + cbent
            for off in range(0, len(data) - rec + 1, rec):
                out.append((data[off:off + cbkey], data[off + cbkey:off + rec]))

    walk(hid_root, levels, 0)
    return cbkey, cbent, out


class PropertyContext:
    """The property bag of a node (a "bc table" / PC)."""

    def __init__(self, node: Node, cpid: Optional[int] = None):
        self.node = node
        blocks = node.data_blocks()
        self.hn = HeapOnNode(blocks, node.ndb.fmt == FMT_UNICODE4K)
        if self.hn.client_sig != CLIENT_PC:
            raise PSTError(f"node 0x{node.nid:x} is not a property context (0x{self.hn.client_sig:02x})")
        cbkey, cbent, entries = bth_entries(self.hn, self.hn.user_root)
        if cbkey != 2 or cbent != 6:
            raise PSTError("property context BTH has the wrong shape")
        self.raw: Dict[int, Tuple[int, bytes]] = {}
        for key, data in entries:
            pid = struct.unpack_from("<H", key)[0]
            ptype = struct.unpack_from("<H", data)[0]
            self.raw[pid] = (ptype, bytes(data[2:6]))
        self._cache: Dict[int, Any] = {}
        # code page for 8-bit strings: the message's own, then the internet one
        self.cpid = cpid
        own = self._inline_long(mapi.PR_MESSAGE_CODEPAGE)
        if own is None:
            own = self._inline_long(mapi.PR_INTERNET_CPID)
        if own:
            self.cpid = own

    def _inline_long(self, pid: int) -> Optional[int]:
        ent = self.raw.get(pid)
        if ent and ent[0] == mapi.PT_LONG:
            return struct.unpack_from("<i", ent[1])[0]
        return None

    def __contains__(self, pid: int) -> bool:
        return pid in self.raw

    def keys(self):
        return self.raw.keys()

    def ptype(self, pid: int) -> Optional[int]:
        ent = self.raw.get(pid)
        return ent[0] if ent else None

    def raw_value(self, pid: int) -> Optional[bytes]:
        """The undecoded bytes of a property (inline slot or referenced data)."""
        ent = self.raw.get(pid)
        if ent is None:
            return None
        ptype, slot = ent
        if ptype in mapi.PC_INLINE_TYPES:
            return slot
        hnid = struct.unpack_from("<I", slot)[0]
        return self._fetch(hnid)

    def _fetch(self, hnid: int) -> Optional[bytes]:
        if hnid == 0:
            return b""
        if (hnid & 0x1F) == 0:
            return self.hn.get(hnid)
        sub = self.node.subnode(hnid)
        if sub is None:
            return None
        return sub.data()

    def get(self, pid: int, default: Any = None) -> Any:
        if pid in self._cache:
            return self._cache[pid]
        ent = self.raw.get(pid)
        if ent is None:
            return default
        ptype, slot = ent
        try:
            if ptype in mapi.PC_INLINE_TYPES:
                val = mapi.decode_scalar(ptype, slot, self.cpid)
            else:
                data = self._fetch(struct.unpack_from("<I", slot)[0])
                if data is None:
                    val = default
                elif ptype & mapi.PT_MV_FLAG:
                    val = mapi.decode_multi(ptype, data, self.cpid)
                else:
                    val = mapi.decode_scalar(ptype, data, self.cpid)
        except PSTError:
            val = default
        self._cache[pid] = val
        return val

    def get_string(self, pid: int, default: str = "") -> str:
        v = self.get(pid)
        if v is None:
            return default
        if isinstance(v, bytes):
            return mapi.decode_string8(v, self.cpid)
        if isinstance(v, str):
            return v
        return str(v)

    def items(self) -> Iterator[Tuple[int, int, Any]]:
        for pid in sorted(self.raw):
            yield pid, self.raw[pid][0], self.get(pid)


class TableContext:
    """A table (TC, "7c table"): rows of the same set of columns."""

    def __init__(self, node: Node, cpid: Optional[int] = None):
        self.node = node
        self.cpid = cpid
        blocks = node.data_blocks()
        self.hn = HeapOnNode(blocks, node.ndb.fmt == FMT_UNICODE4K)
        if self.hn.client_sig != CLIENT_TC:
            raise PSTError(f"node 0x{node.nid:x} is not a table context (0x{self.hn.client_sig:02x})")
        info = self.hn.get(self.hn.user_root)
        if len(info) < 22 or info[0] != CLIENT_TC:
            raise PSTError("table info header missing")
        ccols = info[1]
        self.ib_4b, self.ib_2b, self.ib_1b, self.row_size = struct.unpack_from("<HHHH", info, 2)
        self.hid_row_index, self.hnid_rows = struct.unpack_from("<II", info, 10)
        self.columns: List[Tuple[int, int, int, int, int]] = []  # (pid, ptype, ib, cb, ibit)
        for i in range(ccols):
            off = 22 + i * 8
            if off + 8 > len(info):
                break
            tag, ib, cb, ibit = struct.unpack_from("<IHBB", info, off)
            self.columns.append((tag >> 16, tag & 0xFFFF, ib, cb, ibit))
        self._rows_cache: Optional[List[Dict[int, Any]]] = None
        self._row_ids: Optional[List[int]] = None

    @property
    def column_ids(self) -> List[int]:
        return [c[0] for c in self.columns]

    def _row_index(self) -> List[Tuple[int, int]]:
        """(row_id, row_index) pairs, in row-index order."""
        if self.hid_row_index == 0:
            return []
        cbkey, cbent, entries = bth_entries(self.hn, self.hid_row_index)
        out = []
        for key, data in entries:
            rid = struct.unpack_from("<I", key)[0] if cbkey == 4 else int.from_bytes(key, "little")
            idx = struct.unpack_from("<I", data)[0] if cbent >= 4 else struct.unpack_from("<H", data)[0]
            out.append((rid, idx))
        out.sort(key=lambda t: t[1])
        return out

    def _row_chunks(self) -> List[bytes]:
        if self.hnid_rows == 0:
            return []
        if (self.hnid_rows & 0x1F) == 0:
            return [self.hn.get(self.hnid_rows)]
        sub = self.node.subnode(self.hnid_rows)
        if sub is None:
            return []
        return sub.data_blocks()

    def row_ids(self) -> List[int]:
        if self._row_ids is None:
            self._row_ids = [rid for rid, _ in self._row_index()]
        return self._row_ids

    def __len__(self) -> int:
        return len(self.row_ids())

    def _fetch(self, hnid: int) -> Optional[bytes]:
        if hnid == 0:
            return b""
        if (hnid & 0x1F) == 0:
            return self.hn.get(hnid)
        sub = self.node.subnode(hnid)
        if sub is None:
            return None
        return sub.data()

    def rows(self) -> List[Dict[int, Any]]:
        if self._rows_cache is not None:
            return self._rows_cache
        index = self._row_index()
        chunks = self._row_chunks()
        rs = self.row_size
        out: List[Dict[int, Any]] = []
        if rs == 0 or not chunks:
            self._rows_cache = out
            return out
        # rows never straddle a block; each block holds floor(len / row_size) rows
        per_chunk = [len(c) // rs for c in chunks]
        starts = []
        acc = 0
        for n in per_chunk:
            starts.append(acc)
            acc += n
        total = acc
        ceb_off = self.ib_1b
        ceb_len = self.row_size - self.ib_1b
        for rid, idx in index:
            if idx >= total:
                continue
            # locate chunk
            ci = 0
            for ci in range(len(chunks) - 1, -1, -1):
                if starts[ci] <= idx:
                    break
            local = idx - starts[ci]
            chunk = chunks[ci]
            row = chunk[local * rs:(local + 1) * rs]
            if len(row) < rs:
                continue
            ceb = row[ceb_off:ceb_off + ceb_len]
            vals: Dict[int, Any] = {mapi.PR_LTP_ROW_ID: rid}
            for pid, ptype, ib, cb, ibit in self.columns:
                byte_i, bit_i = ibit >> 3, 7 - (ibit & 7)
                if byte_i >= len(ceb) or not (ceb[byte_i] >> bit_i) & 1:
                    continue
                if ib + cb > rs:
                    continue
                cell = row[ib:ib + cb]
                try:
                    vals[pid] = self._decode_cell(ptype, cell, cb)
                except PSTError:
                    continue
            out.append(vals)
        self._rows_cache = out
        return out

    def _decode_cell(self, ptype: int, cell: bytes, cb: int) -> Any:
        fixed = mapi.FIXED_SIZES.get(ptype)
        if fixed is not None and cb == fixed:
            return mapi.decode_scalar(ptype, cell, self.cpid)
        if ptype in (mapi.PT_I2, mapi.PT_LONG, mapi.PT_BOOLEAN, mapi.PT_R4, mapi.PT_ERROR) and cb <= 4:
            return mapi.decode_scalar(ptype, cell, self.cpid)
        if cb != 4:
            return bytes(cell)
        hnid = struct.unpack_from("<I", cell)[0]
        data = self._fetch(hnid)
        if data is None:
            return None
        if ptype & mapi.PT_MV_FLAG:
            return mapi.decode_multi(ptype, data, self.cpid)
        return mapi.decode_scalar(ptype, data, self.cpid)
