"""Compound File Binary (OLE2 structured storage) reader, from [MS-CFB].

Outlook .msg files are compound files: a tree of storages (directories) and
streams (files). This reads versions 3 (512-byte sectors) and 4 (4096-byte
sectors), the FAT, DIFAT and mini-FAT, and hands back streams as bytes.
"""
from __future__ import annotations

import mmap
import struct
from typing import Dict, List, Optional

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF

TYPE_STORAGE = 1
TYPE_STREAM = 2
TYPE_ROOT = 5


class CFBError(Exception):
    pass


class Entry:
    __slots__ = ("cf", "index", "name", "type", "left", "right", "child", "start", "size", "clsid", "_kids")

    def __init__(self, cf: "CompoundFile", index: int, data: bytes):
        self.cf = cf
        self.index = index
        name_len = struct.unpack_from("<H", data, 64)[0]
        name_len = max(0, min(name_len, 64))
        raw = data[:name_len]
        if raw.endswith(b"\x00\x00"):
            raw = raw[:-2]
        try:
            self.name = raw.decode("utf-16-le", errors="replace")
        except Exception:  # noqa: BLE001
            self.name = ""
        self.type = data[66]
        self.left, self.right, self.child = struct.unpack_from("<III", data, 68)
        self.clsid = data[80:96]
        self.start = struct.unpack_from("<I", data, 116)[0]
        size = struct.unpack_from("<Q", data, 120)[0]
        if cf.major == 3:
            size &= 0xFFFFFFFF
        self.size = size
        self._kids: Optional[Dict[str, "Entry"]] = None

    @property
    def is_storage(self) -> bool:
        return self.type in (TYPE_STORAGE, TYPE_ROOT)

    @property
    def is_stream(self) -> bool:
        return self.type == TYPE_STREAM

    def children(self) -> Dict[str, "Entry"]:
        """Name -> entry for everything directly inside this storage."""
        if self._kids is None:
            kids: Dict[str, Entry] = {}
            if self.is_storage and self.child != NOSTREAM:
                self.cf._collect(self.child, kids, 0)
            self._kids = kids
        return self._kids

    def get(self, name: str) -> Optional["Entry"]:
        kids = self.children()
        e = kids.get(name)
        if e is None:
            low = name.lower()
            for k, v in kids.items():
                if k.lower() == low:
                    return v
        return e

    def read(self) -> bytes:
        return self.cf.read(self)

    def __repr__(self):
        return f"Entry({self.name!r}, {'storage' if self.is_storage else 'stream'}, {self.size})"


class CompoundFile:
    def __init__(self, data):
        self.data = data
        self._fh = None
        if len(data) < 512 or bytes(data[:8]) != MAGIC:
            raise CFBError("not a compound (OLE2) file")
        self.major = struct.unpack_from("<H", data, 0x1A)[0]
        sector_shift = struct.unpack_from("<H", data, 0x1E)[0]
        mini_shift = struct.unpack_from("<H", data, 0x20)[0]
        if sector_shift not in (9, 12) or mini_shift != 6:
            raise CFBError("unsupported sector size")
        self.sector_size = 1 << sector_shift
        self.mini_size = 1 << mini_shift
        (self.num_fat, self.first_dir, _tx, self.mini_cutoff, self.first_minifat, self.num_minifat,
         self.first_difat, self.num_difat) = struct.unpack_from("<IIIIIIII", data, 0x2C)
        self.per_sector = self.sector_size // 4
        self.num_sectors = max(0, (len(data) - self.sector_size) // self.sector_size + 1)
        self._fat = self._load_fat()
        self._minifat: Optional[List[int]] = None
        self._entries: Dict[int, Entry] = {}
        self._dir_data = self._read_chain(self.first_dir, None)
        if len(self._dir_data) < 128:
            raise CFBError("no directory")
        self.root = self.entry(0)
        if self.root.type != TYPE_ROOT:
            raise CFBError("first directory entry is not the root")
        self._mini_stream: Optional[bytes] = None

    @classmethod
    def open(cls, path: str) -> "CompoundFile":
        """Memory-map the file, so listing a folder of big .msg files only touches
        the pages actually read (headers, properties), not every attachment."""
        fh = open(path, "rb")
        try:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            data = fh.read()
            fh.close()
            return cls(data)
        cf = cls(mm)
        cf._fh = fh
        return cf

    def close(self):
        try:
            if isinstance(self.data, mmap.mmap):
                self.data.close()
        except Exception:  # noqa: BLE001
            pass
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None

    def __del__(self):
        self.close()

    # -- sectors ----------------------------------------------------------
    def _sector(self, n: int) -> bytes:
        if n >= self.num_sectors:
            raise CFBError(f"sector {n} is beyond the end of the file")
        off = (n + 1) * self.sector_size
        chunk = bytes(self.data[off:off + self.sector_size])
        if len(chunk) < self.sector_size:
            chunk = chunk.ljust(self.sector_size, b"\x00")
        return chunk

    def _load_fat(self) -> List[int]:
        difat: List[int] = list(struct.unpack_from("<109I", self.data, 0x4C))
        n = self.first_difat
        seen = set()
        for _ in range(min(self.num_difat, self.num_sectors)):
            if n in (ENDOFCHAIN, FREESECT, NOSTREAM) or n in seen or n >= self.num_sectors:
                break
            seen.add(n)
            vals = struct.unpack_from(f"<{self.per_sector}I", self._sector(n))
            difat.extend(vals[:-1])
            n = vals[-1]
        fat: List[int] = []
        wanted = min(self.num_fat, len(difat)) if self.num_fat else 109
        for s in difat[:wanted]:
            if s in (FREESECT, ENDOFCHAIN, NOSTREAM) or s >= self.num_sectors:
                continue
            fat.extend(struct.unpack_from(f"<{self.per_sector}I", self._sector(s)))
            if len(fat) >= self.num_sectors + self.per_sector:
                break
        return fat[:self.num_sectors + self.per_sector]

    def _read_chain(self, start: int, size: Optional[int]) -> bytes:
        out = bytearray()
        n = start
        seen = set()
        while n not in (ENDOFCHAIN, FREESECT, NOSTREAM):
            if n in seen or n >= len(self._fat):
                break
            seen.add(n)
            out += self._sector(n)
            if size is not None and len(out) >= size:
                break
            n = self._fat[n]
        return bytes(out[:size]) if size is not None else bytes(out)

    def _mini(self) -> bytes:
        if self._mini_stream is None:
            self._mini_stream = self._read_chain(self.root.start, self.root.size)
        return self._mini_stream

    def _load_minifat(self) -> List[int]:
        if self._minifat is None:
            data = self._read_chain(self.first_minifat, None) if self.num_minifat else b""
            self._minifat = list(struct.unpack_from(f"<{len(data) // 4}I", data)) if data else []
        return self._minifat

    def _read_mini_chain(self, start: int, size: int) -> bytes:
        minifat = self._load_minifat()
        mini = self._mini()
        out = bytearray()
        n = start
        seen = set()
        while n not in (ENDOFCHAIN, FREESECT, NOSTREAM):
            if n in seen or n >= len(minifat):
                break
            seen.add(n)
            off = n * self.mini_size
            out += mini[off:off + self.mini_size]
            if len(out) >= size:
                break
            n = minifat[n]
        return bytes(out[:size])

    # -- directory --------------------------------------------------------
    def entry(self, index: int) -> Entry:
        e = self._entries.get(index)
        if e is None:
            off = index * 128
            if off + 128 > len(self._dir_data):
                raise CFBError(f"directory entry {index} out of range")
            e = Entry(self, index, self._dir_data[off:off + 128])
            self._entries[index] = e
        return e

    def _collect(self, index: int, into: Dict[str, Entry], _depth: int = 0):
        """In-order walk of a storage's child tree - iterative, with a visited set,
        so a lopsided tree (some writers chain every child to the right) is
        read completely and a corrupt one that loops cannot hang."""
        limit = len(self._dir_data) // 128
        stack: List[int] = []
        seen = set()
        cur = index
        while True:
            while cur not in (NOSTREAM, FREESECT) and cur < limit and cur not in seen:
                seen.add(cur)
                stack.append(cur)
                cur = self.entry(cur).left
            if not stack:
                return
            i = stack.pop()
            e = self.entry(i)
            if e.type != 0:
                into[e.name] = e
            cur = e.right

    def read(self, e: Entry) -> bytes:
        if e.type == TYPE_ROOT:
            return self._mini()
        if not e.is_stream:
            return b""
        if e.size == 0:
            return b""
        if e.size < self.mini_cutoff:
            return self._read_mini_chain(e.start, e.size)
        return self._read_chain(e.start, e.size)

    def walk(self, entry: Optional[Entry] = None, prefix: str = ""):
        entry = entry or self.root
        for name, kid in entry.children().items():
            path = f"{prefix}/{name}"
            yield path, kid
            if kid.is_storage:
                yield from self.walk(kid, path)
