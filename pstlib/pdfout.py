"""PDF rendering of messages with ReportLab.

A Unicode-capable system font is used when one can be found (Segoe UI or Arial
on Windows, Arial Unicode / Helvetica on macOS, DejaVu Sans on Linux), so
accented and non-Latin text comes out as text rather than boxes. Failing all
of that, ReportLab's built-in Helvetica is used and characters outside
Latin-1 are replaced.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, HRFlowable, PageBreak, PageTemplate, Paragraph, Spacer

from .export import header_lines
from .message import Message

_FONT_CANDIDATES = [
    # (regular, bold) pairs; the first pair whose regular file exists wins
    (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    (r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\calibrib.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Verdana.ttf", "/System/Library/Fonts/Supplemental/Verdana Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
]
_MONO_CANDIDATES = [
    r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\cour.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf", "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

_fonts: Optional[dict] = None


def _windows_font_dir() -> str:
    return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def fonts() -> dict:
    """Register fonts once; returns {'regular': name, 'bold': name, 'mono': name, 'unicode': bool}."""
    global _fonts
    if _fonts is not None:
        return _fonts
    reg = bold = None
    cands = list(_FONT_CANDIDATES)
    if sys.platform.startswith("win"):
        fd = _windows_font_dir()
        cands = [(os.path.join(fd, "segoeui.ttf"), os.path.join(fd, "segoeuib.ttf")),
                 (os.path.join(fd, "arial.ttf"), os.path.join(fd, "arialbd.ttf"))] + cands
    for r, b in cands:
        if os.path.isfile(r):
            try:
                pdfmetrics.registerFont(TTFont("PSTBody", r))
                reg = "PSTBody"
                if os.path.isfile(b):
                    try:
                        pdfmetrics.registerFont(TTFont("PSTBold", b))
                        bold = "PSTBold"
                    except Exception:  # noqa: BLE001
                        bold = None
                break
            except Exception:  # noqa: BLE001
                reg = None
                continue
    if reg is None:
        # ReportLab ships Bitstream Vera (Latin only) - better than nothing
        try:
            import reportlab
            vera = os.path.join(os.path.dirname(reportlab.__file__), "fonts", "Vera.ttf")
            verab = os.path.join(os.path.dirname(reportlab.__file__), "fonts", "VeraBd.ttf")
            pdfmetrics.registerFont(TTFont("PSTBody", vera))
            pdfmetrics.registerFont(TTFont("PSTBold", verab))
            reg, bold = "PSTBody", "PSTBold"
        except Exception:  # noqa: BLE001
            reg, bold = "Helvetica", "Helvetica-Bold"
    if bold is None:
        bold = reg
    mono = "Courier"
    for m in _MONO_CANDIDATES:
        if os.path.isfile(m):
            try:
                pdfmetrics.registerFont(TTFont("PSTMono", m))
                mono = "PSTMono"
                break
            except Exception:  # noqa: BLE001
                continue
    _fonts = {"regular": reg, "bold": bold, "mono": mono, "unicode": reg != "Helvetica"}
    return _fonts


def _styles():
    f = fonts()
    body = ParagraphStyle("body", fontName=f["regular"], fontSize=9.5, leading=13, alignment=TA_LEFT)
    hdr_k = ParagraphStyle("k", fontName=f["bold"], fontSize=9, leading=12, textColor=colors.HexColor("#444444"))
    hdr_v = ParagraphStyle("v", fontName=f["regular"], fontSize=9, leading=12)
    title = ParagraphStyle("t", fontName=f["bold"], fontSize=13, leading=17, spaceAfter=4)
    small = ParagraphStyle("s", fontName=f["regular"], fontSize=7.5, leading=10, textColor=colors.HexColor("#777777"))
    return body, hdr_k, hdr_v, title, small


def _safe(text: str) -> str:
    """Escape for Paragraph and drop characters the font cannot show when we only have Helvetica."""
    text = (text or "").replace("\x00", "")
    if not fonts()["unicode"]:
        text = text.encode("latin-1", errors="replace").decode("latin-1")
    return escape(text).replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;")


def _cap(text: str, limit: int = 1500) -> str:
    """Header values with hundreds of recipients would make a block taller than a page."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    more = text[limit:].count(",") + 1
    return f"{cut.rstrip(', ')} ... and about {more} more"


def message_flowables(msg: Message, width: float) -> List:
    body, hdr_k, hdr_v, title, small = _styles()
    items: List = [Paragraph(_safe(msg.subject or "(no subject)"), title)]
    # Header block as paragraphs (a table cell cannot split across pages, so a
    # long recipient list inside one would fail to lay out at all).
    f = fonts()
    hdr = ParagraphStyle("hdr", fontName=f["regular"], fontSize=9, leading=12, leftIndent=27 * mm,
                         firstLineIndent=-27 * mm)
    for k, v in header_lines(msg):
        items.append(Paragraph(f'<font name="{f["bold"]}" color="#444444">{_safe(k)}</font>'
                               f'<font name="{f["regular"]}">{"&nbsp;" * max(1, 14 - len(k) * 2)}</font>'
                               f'{_safe(_cap(v))}', hdr))
    items.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#bbbbbb"), spaceBefore=4, spaceAfter=6))
    text = msg.best_text().replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = text.split("\n")
    # collapse runs of blank lines, keep single blanks as spacing
    blank = False
    for para in paragraphs:
        if not para.strip():
            if not blank:
                items.append(Spacer(1, 5))
            blank = True
            continue
        blank = False
        # very long lines (base64 blobs etc.) need a break opportunity
        chunks = [para[i:i + 3000] for i in range(0, len(para), 3000)] or [para]
        for ch in chunks:
            items.append(Paragraph(_safe(ch), body))
    if not text.strip():
        items.append(Paragraph("(no body)", small))
    return items


def _page_footer(canvas, doc):
    f = fonts()
    canvas.saveState()
    canvas.setFont(f["regular"], 7.5)
    canvas.setFillColor(colors.HexColor("#888888"))
    canvas.drawRightString(doc.pagesize[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _doc(path: str) -> BaseDocTemplate:
    doc = BaseDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                          topMargin=15 * mm, bottomMargin=15 * mm, title="Exported message",
                          author="PST Exporter")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f", leftPadding=0,
                  rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=_page_footer)])
    return doc


def write_message_pdf(msg: Message, path: str):
    doc = _doc(path)
    doc.title = msg.subject or "Exported message"
    doc.build(message_flowables(msg, doc.width))


class PdfBook:
    """Many messages in one PDF, each starting on a new page."""

    def __init__(self, path: str):
        self.path = path
        self.doc = _doc(path)
        self.doc.title = "Exported messages"
        self.messages: List[List] = []
        self.skipped: List[str] = []

    def add_message(self, msg: Message):
        self.messages.append(message_flowables(msg, self.doc.width))

    def _items(self, groups: List[List]) -> List:
        out: List = []
        for g in groups:
            if out:
                out.append(PageBreak())
            out.extend(g)
        return out

    def finish(self):
        if not self.messages:
            self.doc.build([Paragraph("No messages.", _styles()[0])])
            return
        try:
            self.doc.build(self._items(self.messages))
            return
        except Exception:  # noqa: BLE001 - one unlayoutable message must not lose the whole book
            pass
        # find the offenders by trial-building each message on its own
        import copy
        import io
        good: List[List] = []
        for i, g in enumerate(self.messages):
            try:
                trial = _doc(io.BytesIO())
                trial.build(copy.deepcopy(g))
                good.append(g)
            except Exception as exc:  # noqa: BLE001
                self.skipped.append(f"message {i + 1}: {type(exc).__name__}: {exc}")
        self.doc = _doc(self.path)
        self.doc.title = "Exported messages"
        self.doc.build(self._items(good) if good else [Paragraph("No messages.", _styles()[0])])
        if self.skipped:
            raise RuntimeError(f"{len(self.skipped)} message(s) could not be laid out and were left out of the "
                               f"combined PDF: " + "; ".join(self.skipped[:5]))
