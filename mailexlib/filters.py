"""Listing filters shared by the window and the command line.

Everything here works on the cheap listing rows (MessageRow / GenericRow) and
never touches Tk, so the self-test can exercise it directly. The two things
that *do* need the message opened - its body text and its attachment names -
are handed in through callbacks, so the window can serve them from a cache it
fills in the background while the command line simply opens the message.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple

# -- date bounds ---------------------------------------------------------------
_DATE_RE = re.compile(r"^\s*(\d{4})(?:[-/. ](\d{1,2})(?:[-/. ](\d{1,2}))?)?\s*$")


def parse_date_bound(text: Optional[str], end: bool = False) -> Optional[_dt.datetime]:
    """'2019', '2019-03' or '2019-03-15' -> the start of that period (or, with
    end=True, the last instant of it), as an aware datetime in local time -
    the same clock the list shows. Empty or unparsable text gives None, so a
    half-typed date never filters anything out."""
    if not text or not text.strip():
        return None
    m = _DATE_RE.match(text)
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else None
    day = int(m.group(3)) if m.group(3) else None
    if not (1 <= year <= 9999) or (month is not None and not 1 <= month <= 12):
        return None
    try:
        if day is not None:
            start = _dt.datetime(year, month, day)
            nxt = start + _dt.timedelta(days=1)
        elif month is not None:
            start = _dt.datetime(year, month, 1)
            nxt = _dt.datetime(year + (month == 12), (month % 12) + 1, 1)
        else:
            start = _dt.datetime(year, 1, 1)
            nxt = _dt.datetime(year + 1, 1, 1) if year < 9999 else start + _dt.timedelta(days=365)
        bound = start if not end else nxt - _dt.timedelta(microseconds=1)
    except (ValueError, OverflowError):
        return None
    try:
        return bound.astimezone()          # naive local -> aware local
    except (ValueError, OverflowError, OSError):
        # Windows cannot localise dates before 1970 (or far in the future); use
        # today's offset rather than refusing the date
        return bound.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)


def _aware(d: _dt.datetime) -> _dt.datetime:
    return d if d.tzinfo is not None else d.replace(tzinfo=_dt.timezone.utc)


def date_in_range(d: Optional[_dt.datetime], since: Optional[_dt.datetime], until: Optional[_dt.datetime]) -> bool:
    """A row with no date is kept only when no bound is set."""
    if since is None and until is None:
        return True
    if d is None:
        return False
    d = _aware(d)
    if since is not None and d < _aware(since):
        return False
    if until is not None and d > _aware(until):
        return False
    return True


# -- attachment kinds ----------------------------------------------------------
ATT_TYPES = ("all", "documents", "images", "archives", "other")
ATT_TYPE_LABELS = {"all": "All attachments", "documents": "Documents", "images": "Images",
                   "archives": "Archives", "other": "Other files"}
_DOC_EXT = {"pdf", "doc", "docx", "docm", "dot", "dotx", "rtf", "odt", "ott", "pages",
            "xls", "xlsx", "xlsm", "xlsb", "ods", "csv", "numbers",
            "ppt", "pptx", "pptm", "pps", "ppsx", "odp", "key",
            "txt", "md", "epub", "xps", "oxps", "one", "vsd", "vsdx", "pub", "mpp"}
_IMG_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif", "svg", "ico",
            "psd", "ai", "raw", "cr2", "nef", "emf", "wmf"}
_ARC_EXT = {"zip", "7z", "rar", "gz", "tgz", "tar", "bz2", "xz", "z", "cab", "iso", "dmg", "jar"}


def attachment_kind(filename: str) -> str:
    """documents / images / archives / other, from the extension alone."""
    ext = os.path.splitext(filename or "")[1].lstrip(".").lower()
    if ext in _DOC_EXT:
        return "documents"
    if ext in _IMG_EXT:
        return "images"
    if ext in _ARC_EXT:
        return "archives"
    return "other"


def parse_extensions(text: str) -> Set[str]:
    """'pdf, .docx xlsx' -> {'pdf', 'docx', 'xlsx'}."""
    return {p.strip().lstrip(".").lower() for p in re.split(r"[,;\s]+", text or "") if p.strip().lstrip(".")}


def is_contact_class(mc: Optional[str]) -> bool:
    return (mc or "").upper().startswith("IPM.CONTACT")


def is_mail_class(mc: Optional[str]) -> bool:
    mc = (mc or "IPM.Note").upper()
    return mc.startswith("IPM.NOTE") or mc.startswith("REPORT.") or mc == "IPM" or mc.startswith("IPM.POST")


# -- the row filter ------------------------------------------------------------
# what the callbacks hand back for one row: plain-text body (or None when not
# known yet) and the attachment names with an "is inline / hidden" flag
BodyFn = Callable[[object], Optional[str]]
AttFn = Callable[[object], Optional[Sequence[Tuple[str, bool]]]]


@dataclass
class RowFilter:
    text: str = ""                     # words that must all appear (subject, sender, recipients, body if asked)
    search_body: bool = False
    since: Optional[_dt.datetime] = None
    until: Optional[_dt.datetime] = None
    has_attachments: bool = False
    attachment_type: str = "all"       # one of ATT_TYPES; anything but "all" needs the attachment names
    contacts_only: bool = False
    only_mail: bool = False
    _terms: List[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self):
        self._terms = self.text.lower().split()

    @property
    def terms(self) -> List[str]:
        return self._terms

    @property
    def active(self) -> bool:
        return bool(self._terms or self.since or self.until or self.has_attachments
                    or self.attachment_type not in ("", "all") or self.contacts_only or self.only_mail)

    @property
    def needs_body(self) -> bool:
        return self.search_body and bool(self._terms)

    @property
    def needs_attachment_names(self) -> bool:
        return self.attachment_type not in ("", "all")

    def matches(self, row, body: BodyFn = None, atts: AttFn = None) -> bool:
        """True when the row passes every part of the filter.

        `body(row)` / `atts(row)` may return None to say "not known yet": the
        row is then treated as not matching that part, and the window re-runs
        the filter once its background reader has caught up."""
        mc = getattr(row, "message_class", "")
        if self.only_mail and not is_mail_class(mc):
            return False
        if self.contacts_only and not is_contact_class(mc):
            return False
        if not date_in_range(getattr(row, "date", None), self.since, self.until):
            return False
        if self.has_attachments and not getattr(row, "has_attachments", False):
            return False
        if self.needs_attachment_names:
            names = atts(row) if atts else None
            if names is None:
                return False
            if not any(attachment_kind(n) == self.attachment_type for n, inline in names if not inline):
                return False
        if self._terms:
            hay = f"{row.subject} {row.sender} {row.to}".lower()
            missing = [t for t in self._terms if t not in hay]
            if missing:
                if not self.search_body:
                    return False
                text = body(row) if body else None
                if text is None:
                    return False
                low = text.lower()
                if not all(t in low for t in missing):
                    return False
        return True

    def apply(self, rows: Iterable, body: BodyFn = None, atts: AttFn = None) -> list:
        return [r for r in rows if self.matches(r, body, atts)]

    def describe(self) -> str:
        """A short human summary, for the status line."""
        parts = []
        if self._terms:
            parts.append(f"'{self.text.strip()}'" + (" in bodies too" if self.search_body else ""))
        if self.since or self.until:
            a = self.since.strftime("%Y-%m-%d") if self.since else "…"
            b = self.until.strftime("%Y-%m-%d") if self.until else "…"
            parts.append(f"dated {a} to {b}")
        if self.has_attachments:
            parts.append("with attachments")
        if self.needs_attachment_names:
            parts.append(ATT_TYPE_LABELS.get(self.attachment_type, self.attachment_type).lower())
        if self.contacts_only:
            parts.append("contacts")
        if self.only_mail:
            parts.append("e-mail only")
        return ", ".join(parts)


# -- helpers the command line uses in place of the window's cache --------------
def body_of(row, limit: int = 65536) -> Optional[str]:
    """Plain text of the message behind a row, capped; None if it will not open."""
    try:
        return row.open().best_text()[:limit]
    except Exception:  # noqa: BLE001
        return None


def attachments_of(row) -> Optional[List[Tuple[str, bool]]]:
    """(name, inline-or-hidden) for each attachment; None if the message will not open."""
    try:
        m = row.open()
        return [(a.filename, bool(a.hidden or a.is_inline)) for a in m.attachments()]
    except Exception:  # noqa: BLE001
        return None
