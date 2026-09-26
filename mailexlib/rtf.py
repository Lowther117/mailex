"""Compressed RTF (LZFu) decoding, RTF to plain text, and HTML de-encapsulation."""
from __future__ import annotations

import re
import struct
from typing import Optional, Tuple

from . import mapi

_LZFU_DICT = (
    b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman \\fswiss \\fmodern "
    b"\\fscript \\fdecor MS Sans SerifSymbolArialTimes New RomanCourier{\\colortbl\\red0\\green0"
    b"\\blue0\r\n\\par \\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx"
)
assert len(_LZFU_DICT) == 207


def decompress_rtf(data: bytes) -> bytes:
    """Undo PidTagRtfCompressed's LZFu compression. Returns raw RTF bytes."""
    if len(data) < 16:
        return b""
    comp_size, raw_size, magic, _crc = struct.unpack_from("<IIII", data, 0)
    if magic == 0x414C454D:  # "MELA": stored uncompressed
        return data[16:16 + raw_size]
    if magic != 0x75465A4C:  # "LZFu"
        return b""
    buf = bytearray(4096)
    buf[:len(_LZFU_DICT)] = _LZFU_DICT
    wpos = len(_LZFU_DICT)
    out = bytearray()
    pos = 16
    end = min(len(data), comp_size + 4)
    while pos < end and len(out) < raw_size:
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if pos >= end or len(out) >= raw_size:
                break
            if flags & (1 << bit):
                if pos + 2 > end:
                    break
                ref = (data[pos] << 8) | data[pos + 1]
                pos += 2
                offset = ref >> 4
                length = (ref & 0x0F) + 2
                if offset == wpos:  # end marker
                    return bytes(out[:raw_size])
                for i in range(length):
                    c = buf[(offset + i) & 0xFFF]
                    out.append(c)
                    buf[wpos] = c
                    wpos = (wpos + 1) & 0xFFF
            else:
                c = data[pos]
                pos += 1
                out.append(c)
                buf[wpos] = c
                wpos = (wpos + 1) & 0xFFF
    return bytes(out[:raw_size])


# ---------------------------------------------------------------------------
# RTF tokeniser
# ---------------------------------------------------------------------------
_TOKEN = re.compile(rb"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)", re.S)
_SKIP_DESTINATIONS = {
    b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"object", b"header", b"footer",
    b"headerl", b"headerr", b"headerf", b"footerl", b"footerr", b"footerf", b"footnote",
    b"generator", b"listtable", b"listoverridetable", b"revtbl", b"rsidtbl", b"xmlnstbl",
    b"themedata", b"colorschememapping", b"latentstyles", b"datastore", b"mmathPr",
    b"fldinst", b"pntext", b"ftnsep", b"aftnsep", b"panose", b"falt", b"blipuid",
    b"protusertbl", b"template", b"bkmkstart", b"bkmkend", b"operator", b"title", b"subject",
    b"author", b"company", b"doccomm", b"htmltag",
}


def _codec_from_rtf(rtf: bytes) -> str:
    m = re.search(rb"\\ansicpg(\d+)", rtf)
    if m:
        return mapi.codec_for(int(m.group(1)))
    return "cp1252"


def rtf_to_text(rtf: bytes) -> str:
    """Best-effort plain text from RTF (good enough for e-mail bodies)."""
    if not rtf:
        return ""
    codec = _codec_from_rtf(rtf)
    out = []
    pending = bytearray()
    stack = []
    skip = 0          # depth at which we started skipping a destination (0 = not skipping)
    uc_skip = 1       # \ucN: bytes to skip after a \uN
    skip_bytes = 0    # bytes still to skip after \uN
    depth = 0

    def flush():
        if pending:
            try:
                out.append(pending.decode(codec, errors="replace"))
            except LookupError:
                out.append(pending.decode("cp1252", errors="replace"))
            pending.clear()

    for m in _TOKEN.finditer(rtf):
        word, num, hexch, sym, brace, text = m.groups()
        if brace == b"{":
            depth += 1
            stack.append(uc_skip)
            continue
        if brace == b"}":
            if skip and depth <= skip:
                skip = 0
            depth -= 1
            if stack:
                uc_skip = stack.pop()
            continue
        if skip:
            continue
        if word is not None:
            if word in _SKIP_DESTINATIONS:
                skip = depth
                continue
            if word == b"par" or word == b"line" or word == b"sect" or word == b"page":
                flush()
                out.append("\n")
            elif word == b"tab":
                flush()
                out.append("\t")
            elif word == b"uc":
                uc_skip = int(num or 1)
            elif word == b"u":
                flush()
                cp = int(num or 0)
                if cp < 0:
                    cp += 65536
                out.append(chr(cp) if 0 <= cp < 0x110000 else "?")
                skip_bytes = uc_skip
            elif word in (b"emdash",):
                flush(); out.append("\u2014")
            elif word in (b"endash",):
                flush(); out.append("\u2013")
            elif word in (b"lquote",):
                flush(); out.append("\u2018")
            elif word in (b"rquote",):
                flush(); out.append("\u2019")
            elif word in (b"ldblquote",):
                flush(); out.append("\u201c")
            elif word in (b"rdblquote",):
                flush(); out.append("\u201d")
            elif word == b"bullet":
                flush(); out.append("\u2022")
            elif word in (b"emspace", b"enspace", b"qmspace"):
                flush(); out.append(" ")
            elif word == b"ansicpg" and num:
                codec = mapi.codec_for(int(num))
            continue
        if hexch is not None:
            if skip_bytes:
                skip_bytes -= 1
                continue
            pending.append(int(hexch, 16))
            continue
        if sym is not None:
            if sym == b"*":
                # \* introduces an ignorable destination unless we know the word
                nxt = rtf[m.end():m.end() + 24]
                mm = re.match(rb"\\([a-zA-Z]+)", nxt)
                if not mm or mm.group(1) in _SKIP_DESTINATIONS or mm.group(1) not in (b"htmltag",):
                    skip = depth
                continue
            if sym in (b"\\", b"{", b"}"):
                pending.extend(sym)
            elif sym == b"~":
                pending.extend(b"\xa0" if codec == "cp1252" else b" ")
            elif sym == b"-":
                pass
            elif sym in (b"\r", b"\n"):
                pass
            continue
        if text is not None:
            t = text.replace(b"\r", b"").replace(b"\n", b"")
            if skip_bytes:
                cut = min(skip_bytes, len(t))
                t = t[cut:]
                skip_bytes -= cut
            pending.extend(t)
    flush()
    s = "".join(out).replace("\x00", "")
    return s.strip("\n")


def is_encapsulated_html(rtf: bytes) -> bool:
    return b"\\fromhtml" in rtf[:512]


def rtf_to_html(rtf: bytes) -> Optional[str]:
    """De-encapsulate HTML stored in RTF (\\fromhtml1, [MS-OXRTFEX]). None if not encapsulated."""
    if not is_encapsulated_html(rtf):
        return None
    codec = _codec_from_rtf(rtf)
    out = []
    pending = bytearray()
    depth = 0
    htmlrtf = 0             # inside \htmlrtf ... \htmlrtf0 : RTF-only content, drop it
    in_htmltag = []          # stack of depth values where an \*\htmltag group opened
    skip = 0
    stack = []
    uc_skip = 1
    skip_bytes = 0

    def flush():
        if pending:
            out.append(pending.decode(codec, errors="replace"))
            pending.clear()

    for m in _TOKEN.finditer(rtf):
        word, num, hexch, sym, brace, text = m.groups()
        if brace == b"{":
            depth += 1
            stack.append(uc_skip)
            continue
        if brace == b"}":
            if skip and depth <= skip:
                skip = 0
            if in_htmltag and in_htmltag[-1] == depth:
                in_htmltag.pop()
            depth -= 1
            if stack:
                uc_skip = stack.pop()
            continue
        if skip:
            continue
        if word is not None:
            if word == b"htmltag":
                in_htmltag.append(depth)
                continue
            if word == b"htmlrtf":
                htmlrtf = 0 if (num is not None and int(num) == 0) else 1
                continue
            if word in _SKIP_DESTINATIONS and word != b"htmltag":
                skip = depth
                continue
            if htmlrtf and not in_htmltag:
                continue
            if word == b"par" or word == b"line":
                flush(); out.append("\r\n")
            elif word == b"tab":
                flush(); out.append("\t")
            elif word == b"uc":
                uc_skip = int(num or 1)
            elif word == b"u":
                flush()
                cp = int(num or 0)
                if cp < 0:
                    cp += 65536
                out.append(chr(cp) if 0 <= cp < 0x110000 else "?")
                skip_bytes = uc_skip
            elif word == b"ansicpg" and num:
                codec = mapi.codec_for(int(num))
            elif word in (b"lquote", b"rquote", b"ldblquote", b"rdblquote", b"emdash", b"endash", b"bullet"):
                flush()
                out.append({b"lquote": "\u2018", b"rquote": "\u2019", b"ldblquote": "\u201c",
                            b"rdblquote": "\u201d", b"emdash": "\u2014", b"endash": "\u2013",
                            b"bullet": "\u2022"}[word])
            continue
        if hexch is not None:
            if skip_bytes:
                skip_bytes -= 1
                continue
            if htmlrtf and not in_htmltag:
                continue
            pending.append(int(hexch, 16))
            continue
        if sym is not None:
            if sym == b"*":
                nxt = rtf[m.end():m.end() + 24]
                mm = re.match(rb"\\([a-zA-Z]+)", nxt)
                if not mm or mm.group(1) != b"htmltag":
                    skip = depth
                continue
            if htmlrtf and not in_htmltag:
                continue
            if sym in (b"\\", b"{", b"}"):
                pending.extend(sym)
            elif sym == b"~":
                pending.extend(b"&nbsp;")
            continue
        if text is not None:
            t = text.replace(b"\r", b"").replace(b"\n", b"")
            if skip_bytes:
                cut = min(skip_bytes, len(t))
                t = t[cut:]
                skip_bytes -= cut
            if htmlrtf and not in_htmltag:
                continue
            pending.extend(t)
    flush()
    return "".join(out).replace("\x00", "")


# ---------------------------------------------------------------------------
# HTML to text (for previews, PDF and TXT when a message has no plain body)
# ---------------------------------------------------------------------------
_HTML_BLOCK = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h[1-6]|/table|/blockquote|p|div|tr|li|h[1-6])\b[^>]*>", re.I)
_HTML_STRIP = re.compile(r"<\s*(script|style|head)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HTML_WS = re.compile(r"[ \t\r\f\v]+")
_HTML_ENT = re.compile(r"&(#x[0-9a-fA-F]+|#\d+|[a-zA-Z][a-zA-Z0-9]*);")


def html_to_text(html: str) -> str:
    import html as _html
    s = _HTML_COMMENT.sub("", html)
    s = _HTML_STRIP.sub("", s)
    s = re.sub(r"<\s*td\b[^>]*>", "\t", s, flags=re.I)
    s = _HTML_BLOCK.sub("\n", s)
    s = _HTML_TAG.sub("", s)
    s = _html.unescape(s)
    s = s.replace("\xa0", " ")
    lines = [_HTML_WS.sub(" ", ln).strip() for ln in s.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_charset(raw: bytes) -> Optional[str]:
    head = raw[:4096]
    m = re.search(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", head, re.I)
    if m:
        return m.group(1).decode("ascii", "replace")
    return None


def decode_html(raw: bytes, cpid: Optional[int]) -> str:
    """PidTagHtml is stored as bytes; work out how to decode it."""
    if raw[:2] in (b"\xff\xfe",):
        return raw.decode("utf-16-le", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace")
    cs = html_charset(raw)
    candidates = []
    if cs:
        candidates.append(cs)
    if cpid:
        candidates.append(mapi.codec_for(cpid))
    candidates += ["utf-8", "cp1252"]
    for c in candidates:
        try:
            return raw.decode(c)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("cp1252", errors="replace")
