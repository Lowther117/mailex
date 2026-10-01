"""Text out of attachments, for the index.

Pure Python for everything but PDF: Office documents are zip files of XML,
OpenDocument likewise, RTF goes through the RTF reader, saved messages through
the message readers, archives give up their member names (and the text of
text-like members one level down). PDF uses PyMuPDF when it is installed and
otherwise a small built-in reader that inflates the page streams and pulls the
plain text operators out - enough for ordinary documents, not for every PDF.
Images are recorded by name only.
"""
from __future__ import annotations

import base64
import html as _html
import io
import re
import zipfile
import zlib
from typing import Callable, Dict, Optional

from . import rtf

MAX_TEXT = 400_000          # characters kept per attachment
MAX_BYTES = 60 * 1024 * 1024  # bigger attachments are recorded by name only

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".log", ".ini", ".cfg", ".conf", ".yaml",
            ".yml", ".py", ".js", ".ts", ".css", ".sql", ".sh", ".bat", ".ps1", ".cmd", ".ics", ".vcf", ".vcard",
            ".tex", ".rst", ".nfo", ".srt", ".c", ".h", ".cpp", ".java", ".cs", ".go", ".rb", ".php", ".pl",
            ".toml", ".properties", ".diff", ".patch"}
HTML_EXT = {".html", ".htm", ".xhtml", ".mht", ".mhtml"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif", ".svg", ".ico",
             ".psd", ".raw", ".cr2", ".nef"}
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = _WS.sub(" ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()[:MAX_TEXT]


def _decode(raw: bytes) -> str:
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    for codec in ("utf-8", "cp1252"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


# ---------------------------------------------------------------------------
# Office / OpenDocument
# ---------------------------------------------------------------------------
def _xml_text(xml: bytes, run_tags=("w:t", "a:t", "t", "text:p", "text:span", "text:h"), para_tags=("w:p", "a:p", "text:p", "text:h", "row")) -> str:
    """Text content of an XML part: runs joined (a word split by formatting
    comes back whole), paragraphs separated by newlines."""
    s = _decode(xml)
    # paragraph ends -> newline, run ends -> nothing, everything else stripped
    s = re.sub(r"</(?:%s)>" % "|".join(re.escape(t) for t in para_tags), "\n", s)
    s = re.sub(r"<(?:w:tab|w:br|text:tab|text:line-break)\b[^>]*/?>", " ", s)
    s = _TAG.sub("", s)
    return _html.unescape(s)


def _ooxml(z: zipfile.ZipFile, names) -> str:
    parts = []
    for n in names:
        try:
            parts.append(_xml_text(z.read(n)))
        except (KeyError, zipfile.BadZipFile, zlib.error):
            continue
        if sum(len(p) for p in parts) > MAX_TEXT:
            break
    return "\n".join(parts)


def extract_docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.startswith("word/") and n.endswith(".xml")
                 and (n == "word/document.xml" or "header" in n or "footer" in n or "footnotes" in n
                      or "endnotes" in n or "comments" in n)]
        names.sort(key=lambda n: (n != "word/document.xml", n))
        return _ooxml(z, names)


def extract_pptx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted((n for n in z.namelist() if re.match(r"ppt/(slides|notesSlides)/[^/]+\.xml$", n)),
                       key=lambda n: (int(re.search(r"(\d+)\.xml$", n).group(1)) if re.search(r"(\d+)\.xml$", n) else 0, n))
        return _ooxml(z, names)


def extract_xlsx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        out = []
        try:
            shared = _decode(z.read("xl/sharedStrings.xml"))
            strings = [_html.unescape(_TAG.sub("", m)) for m in re.findall(r"<si>(.*?)</si>", shared, re.S)]
        except KeyError:
            strings = []
        out.extend(strings)
        for n in sorted(x for x in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", x)):
            try:
                xml = _decode(z.read(n))
            except (KeyError, zlib.error):
                continue
            # inline strings and numbers (shared strings are already in)
            for m in re.finditer(r"<c\b([^>]*)>(.*?)</c>", xml, re.S):
                attrs, inner = m.group(1), m.group(2)
                if 't="s"' in attrs:
                    continue
                v = re.search(r"<(?:v|t)>(.*?)</(?:v|t)>", inner, re.S)
                if v:
                    out.append(_html.unescape(_TAG.sub("", v.group(1))))
            if sum(len(x) for x in out) > MAX_TEXT:
                break
        return "\n".join(out)


def extract_odf(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return _ooxml(z, ["content.xml"])


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _pymupdf():
    try:
        import pymupdf  # noqa: F401
        return pymupdf
    except ImportError:
        try:
            import fitz  # noqa: F401
            return fitz
        except ImportError:
            return None


def extract_pdf(data: bytes) -> str:
    mod = _pymupdf()
    if mod is not None:
        try:
            try:
                mod.TOOLS.mupdf_display_errors(False)
            except Exception:  # noqa: BLE001
                pass
            doc = mod.open(stream=data, filetype="pdf")
            out = []
            total = 0
            for page in doc:
                t = page.get_text()
                out.append(t)
                total += len(t)
                if total > MAX_TEXT:
                    break
            doc.close()
            return "\n".join(out)
        except Exception:  # noqa: BLE001 - fall through to the built-in reader
            pass
    return _pdf_builtin(data)


_PDF_STRING = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]+>")


def _inflate(chunk: bytes) -> bytes:
    try:
        return zlib.decompress(chunk)
    except zlib.error:
        try:
            return zlib.decompressobj().decompress(chunk)
        except zlib.error:
            return chunk


def _pdf_decode_stream(dictionary: bytes, chunk: bytes) -> bytes:
    """Apply the stream's /Filter chain (the text-friendly subset: ASCII85,
    ASCIIHex, Flate). Anything else (LZW, DCT...) is left alone and will not
    contain Tj operators, so it is skipped by the caller."""
    filters = re.findall(rb"/(ASCII85Decode|ASCIIHexDecode|FlateDecode|LZWDecode|DCTDecode|"
                         rb"RunLengthDecode|CCITTFaxDecode|JBIG2Decode|JPXDecode)", dictionary)
    if not filters:
        # no filter named: maybe plain, maybe flate without a dictionary match
        return _inflate(chunk) if chunk[:1] in (b"x", b"\x78") else chunk
    for f in filters:
        try:
            if f == b"ASCII85Decode":
                body = chunk.strip()
                if body.startswith(b"<~"):
                    body = body[2:]
                if body.endswith(b"~>"):
                    body = body[:-2]
                chunk = base64.a85decode(re.sub(rb"\s", b"", body))
            elif f == b"ASCIIHexDecode":
                body = re.sub(rb"[\s>]", b"", chunk)
                if len(body) % 2:
                    body += b"0"
                chunk = bytes.fromhex(body.decode("ascii"))
            elif f == b"FlateDecode":
                chunk = _inflate(chunk)
            else:
                return b""
        except Exception:  # noqa: BLE001
            return b""
    return chunk


_PDF_TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|[A-Za-z'\"]{1,2}(?![A-Za-z])|\[|\]")
_PDF_ESCAPES = {b"n": b"\n", b"r": b"", b"t": b" ", b"b": b"", b"f": b"", b"(": b"(", b")": b")", b"\\": b"\\"}


def _pdf_literal(tok: bytes) -> bytes:
    txt = tok[1:-1]
    txt = re.sub(rb"\\([nrtbf()\\])", lambda x: _PDF_ESCAPES[x.group(1)], txt)
    txt = re.sub(rb"\\(\d{1,3})", lambda x: bytes([int(x.group(1), 8) & 0xFF]), txt)
    return txt.replace(b"\\\n", b"")


def _pdf_streams(data: bytes):
    """(dictionary, raw stream bytes) for every stream object, found by plain
    scanning - no backtracking regexes over multi-megabyte files."""
    pos = 0
    n = len(data)
    while True:
        s = data.find(b"stream", pos)
        if s < 0:
            return
        head = s + 6
        if data[head:head + 2] == b"\r\n":
            body = head + 2
        elif data[head:head + 1] == b"\n":
            body = head + 1
        else:
            pos = head          # "endstream" or a word containing stream
            continue
        e = data.find(b"endstream", body)
        if e < 0:
            return
        d = data.rfind(b"<<", max(0, s - 800), s)
        yield data[d:s] if d >= 0 else b"", data[body:e].rstrip(b"\r\n")
        pos = e + 9
        if pos >= n:
            return


def _pdf_builtin(data: bytes) -> str:
    """Plain text from the page content streams: literal strings inside Tj/TJ/'/".
    Works for PDFs written with standard encodings; subset fonts with custom
    maps come out as noise and are filtered by the printable-ratio check."""
    out = []
    total = 0
    for dictionary, raw in _pdf_streams(data):
        chunk = _pdf_decode_stream(dictionary, raw)
        if not chunk or (b"Tj" not in chunk and b"TJ" not in chunk and b"'" not in chunk):
            continue
        pending = []
        for m in _PDF_TOKEN.finditer(chunk):
            tok = m.group(0)
            c0 = tok[:1]
            if c0 == b"(":
                pending.append(_pdf_literal(tok))
            elif c0 == b"<":
                hexs = re.sub(rb"\s", b"", tok[1:-1])
                try:
                    b = bytes.fromhex(hexs.decode("ascii") + ("0" if len(hexs) % 2 else ""))
                except ValueError:
                    continue
                if b and all(32 <= c < 127 or c in (9, 10, 13) for c in b):
                    pending.append(b)
            elif tok in (b"Tj", b"TJ", b"'", b'"'):
                if pending:
                    out.append(b"".join(pending))
                    total += sum(len(p) for p in pending)
                pending = []
            elif tok in (b"[", b"]"):
                continue
            else:
                # another operator: strings before it were not text
                pending = []
            if total > MAX_TEXT * 2:
                break
        if total > MAX_TEXT * 2:
            break
    raw = b" ".join(out)
    text = raw.decode("latin-1", "replace")
    if not text:
        return ""
    printable = sum(1 for c in text if c.isprintable() or c in "\n\t")
    if printable < 0.9 * len(text):
        return ""          # custom-encoded glyphs, not words
    return text


# ---------------------------------------------------------------------------
# saved messages, archives
# ---------------------------------------------------------------------------
def extract_eml(data: bytes) -> str:
    from .sources import MimeMessage
    m = MimeMessage(None, data)
    head = "\n".join(f"{k}: {v}" for k, v in (("Subject", m.subject), ("From", m.sender), ("To", m.display_to)) if v)
    parts = [head, m.best_text()]
    for a in m.attachments():
        parts.append(a.filename)
        parts.append(extract(a.filename, a.data, a.mime_type, depth=1))
    return "\n".join(p for p in parts if p)


def extract_msg(data: bytes) -> str:
    from .cfb import CompoundFile
    from .msgfile import MsgMessage
    m = MsgMessage(None, None, cf=CompoundFile(data))
    head = "\n".join(f"{k}: {v}" for k, v in (("Subject", m.subject), ("From", m.sender), ("To", m.display_to)) if v)
    parts = [head, m.best_text()]
    for a in m.attachments():
        parts.append(a.filename)
        if a.is_embedded_message:
            emb = a.embedded_message
            if emb is not None:
                parts.append(emb.subject)
                parts.append(emb.best_text())
        else:
            parts.append(extract(a.filename, a.data, a.mime_type, depth=1))
    return "\n".join(p for p in parts if p)


def extract_zip(data: bytes, depth: int) -> str:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        total = 0
        for info in z.infolist():
            if info.is_dir():
                continue
            out.append(info.filename)
            if depth >= 2 or info.file_size > 20 * 1024 * 1024:
                continue
            low = info.filename.lower()
            ext = low[low.rfind("."):] if "." in low else ""
            if ext in TEXT_EXT | HTML_EXT | {".docx", ".xlsx", ".pptx", ".pdf", ".rtf", ".odt", ".ods", ".odp", ".eml", ".msg"}:
                try:
                    t = extract(info.filename, z.read(info), "", depth=depth + 1)
                except Exception:  # noqa: BLE001
                    continue
                if t:
                    out.append(t)
                    total += len(t)
            if total > MAX_TEXT:
                break
    return "\n".join(out)


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------
def kind_of(filename: str, data: Optional[bytes], mime: str = "") -> str:
    low = (filename or "").lower()
    ext = low[low.rfind("."):] if "." in low else ""
    head = (data or b"")[:8]
    if head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "msg" if ext in (".msg", ".oft", "") else "ole"
    if head[:4] == b"%PDF" or ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if head[:2] == b"PK":
        if ext in (".docx", ".docm", ".dotx"):
            return "docx"
        if ext in (".xlsx", ".xlsm", ".xltx"):
            return "xlsx"
        if ext in (".pptx", ".pptm", ".potx"):
            return "pptx"
        if ext in (".odt", ".ods", ".odp"):
            return "odf"
        return "zip"
    if ext in (".rtf",) or head[:5] == b"{\\rtf":
        return "rtf"
    if ext in (".eml", ".emlx") or mime == "message/rfc822":
        return "eml"
    if ext in HTML_EXT or mime == "text/html":
        return "html"
    if ext in TEXT_EXT or mime.startswith("text/"):
        return "text"
    if ext in IMAGE_EXT or mime.startswith("image/"):
        return "image"
    if ext in (".doc", ".xls", ".ppt"):
        return "ole"
    return "other"


def _strings(data: bytes, minlen: int = 6) -> str:
    """Readable runs from a binary (legacy Office): ASCII and UTF-16LE."""
    out = []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, data[:4_000_000]):
        out.append(m.group(0).decode("ascii"))
    for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % minlen, data[:4_000_000]):
        out.append(m.group(0).decode("utf-16-le", "replace"))
    return "\n".join(out)


def extract(filename: str, data: Optional[bytes], mime: str = "", depth: int = 0) -> str:
    """Searchable text of one attachment ('' when there is none to be had)."""
    if not data or len(data) > MAX_BYTES:
        return ""
    kind = kind_of(filename, data, mime or "")
    try:
        if kind == "text":
            return _clean(_decode(data))
        if kind == "html":
            return _clean(rtf.html_to_text(_decode(data)))
        if kind == "rtf":
            return _clean(rtf.rtf_to_text(data))
        if kind == "docx":
            return _clean(extract_docx(data))
        if kind == "xlsx":
            return _clean(extract_xlsx(data))
        if kind == "pptx":
            return _clean(extract_pptx(data))
        if kind == "odf":
            return _clean(extract_odf(data))
        if kind == "pdf":
            return _clean(extract_pdf(data))
        if kind == "eml" and depth < 3:
            return _clean(extract_eml(data))
        if kind == "msg" and depth < 3:
            return _clean(extract_msg(data))
        if kind == "zip" and depth < 2:
            return _clean(extract_zip(data, depth))
        if kind == "ole":
            return _clean(_strings(data))
    except Exception:  # noqa: BLE001 - a broken attachment is recorded by name only
        return ""
    return ""


def pdf_engine() -> str:
    return "PyMuPDF" if _pymupdf() is not None else "built-in"
