"""The content index: every message and every attachment, searchable in milliseconds.

Design (mirrors findex):
  * one SQLite file, WAL mode, FTS5 full-text table over subject / sender /
    recipients / body / attachment text / attachment names;
  * incremental - a mailbox whose size and mtime have not changed is skipped;
    otherwise only messages that are new to the index are extracted, and
    messages that have vanished are removed;
  * one query syntax everywhere (GUI and CLI):
        invoice 2023                 all words, any field
        "exact phrase"               phrase
        from:bob  to:alice           sender / recipients
        subject:renewal  body:total  one field
        att:pdf  att:"q4 report"     attachment names and contents
        in:inbox  source:work.pst    folder path / mailbox contains
        has:att                      only messages with attachments
        after:2023-06  before:2024   date window (YYYY, YYYY-MM or YYYY-MM-DD)
        !spam  -newsletter           must not contain
    The last bare word is treated as a prefix while typing (live=True).

Nothing here touches Tk; the GUI and CLI call Indexer from a worker thread.
"""
from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import extract
from .paths import APP, app_dir

SCHEMA_VERSION = 1
DB_NAME = "mailex-index.db"
MAX_BODY = 400_000          # characters of body text kept per message
MAX_ATT_TEXT = 600_000      # characters of attachment text kept per message
MAX_EMBED_DEPTH = 3
COMMIT_EVERY = 200

ProgressFn = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# where the database lives
# ---------------------------------------------------------------------------
def user_data_dir() -> str:
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP)
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, APP)
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"), APP.lower())


def default_db_path(settings: Optional[dict] = None) -> str:
    """The configured location, else beside the app (like the settings file),
    else the per-user data folder when the app folder is read-only."""
    if settings:
        p = settings.get("index_path")
        if p:
            return p
    beside = os.path.join(app_dir(), DB_NAME)
    if os.path.exists(beside):
        return beside
    try:
        probe = os.path.join(app_dir(), ".mailex-write-test")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return beside
    except OSError:
        return os.path.join(user_data_dir(), DB_NAME)


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass
class Hit:
    id: int
    source_id: int
    source_path: str
    source_kind: str
    source_name: str
    folder: str
    key: str
    subject: str
    sender: str
    sender_email: str
    recipients: str
    ts: Optional[float]
    size: int
    att_count: int
    att_names: str
    message_class: str
    where: str = ""          # "subject" | "sender" | "recipients" | "body" | "attachment" | "attachment name" | ""
    snippet: str = ""
    score: float = 0.0

    @property
    def date(self) -> Optional[_dt.datetime]:
        if self.ts is None:
            return None
        try:
            return _dt.datetime.fromtimestamp(self.ts, _dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    @property
    def has_attachments(self) -> bool:
        return self.att_count > 0


@dataclass
class IndexResult:
    source: str
    added: int = 0
    removed: int = 0
    skipped: int = 0         # already indexed, unchanged
    errors: int = 0
    attachments: int = 0
    seconds: float = 0.0
    cancelled: bool = False
    unchanged: bool = False  # whole mailbox skipped (size + mtime identical)
    notes: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.unchanged:
            return f"{self.source}: unchanged, skipped"
        bits = [f"{self.added} added"]
        if self.removed:
            bits.append(f"{self.removed} removed")
        if self.skipped:
            bits.append(f"{self.skipped} already indexed")
        if self.errors:
            bits.append(f"{self.errors} with errors")
        bits.append(f"{self.attachments} attachments")
        s = f"{self.source}: " + ", ".join(bits) + f" in {self.seconds:.1f}s"
        return s + " (cancelled)" if self.cancelled else s


@dataclass
class Query:
    """A parsed search: an FTS expression plus the SQL-side filters."""
    positive: List[str] = field(default_factory=list)   # FTS5 fragments, ANDed
    negative: List[str] = field(default_factory=list)   # FTS5 fragments, any excludes
    folder: List[str] = field(default_factory=list)
    source: List[str] = field(default_factory=list)
    after: Optional[float] = None
    before: Optional[float] = None
    has_att: Optional[bool] = None
    text: str = ""

    @property
    def empty(self) -> bool:
        return not (self.positive or self.negative or self.folder or self.source
                    or self.after is not None or self.before is not None or self.has_att is not None)


# ---------------------------------------------------------------------------
# message identity
# ---------------------------------------------------------------------------
def message_key(row) -> str:
    """Stable id of a message inside its mailbox. PST/OST rows have a node id;
    everything else is hashed from the listing fields (duplicates get #n)."""
    src = row.folder.source
    d = row.date.isoformat() if row.date is not None else ""
    if getattr(src, "kind", "") == "pst":
        # node ids are stable for a message's life; date+size guard against a
        # rebuilt store handing an old id to a different message
        return f"nid:{row.nid}|{d}|{row.size or 0}"
    raw = "|".join((row.folder.path_str, row.subject or "", row.sender or "", d, str(row.size or 0),
                    row.to or ""))
    return "h:" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def _ts(d: Optional[_dt.datetime]) -> Optional[float]:
    if d is None:
        return None
    try:
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _source_stat(path: str) -> Tuple[int, float]:
    try:
        if os.path.isdir(path):
            size, mtime = 0, 0.0
            for base, _dirs, files in os.walk(path):
                for fn in files:
                    try:
                        st = os.stat(os.path.join(base, fn))
                    except OSError:
                        continue
                    size += st.st_size
                    mtime = max(mtime, st.st_mtime)
            return size, mtime
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return 0, 0.0


# ---------------------------------------------------------------------------
# text of one message
# ---------------------------------------------------------------------------
def _recipients_text(m) -> str:
    parts = []
    try:
        for r in m.recipients():
            name = (getattr(r, "name", "") or "").strip()
            addr = (getattr(r, "smtp", "") or getattr(r, "email", "") or "").strip()
            if name and addr and name.lower() != addr.lower():
                parts.append(f"{name} <{addr}>")
            else:
                parts.append(name or addr)
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        for v in (m.display_to, m.display_cc, m.display_bcc):
            if v:
                parts.append(v)
    return "; ".join(p for p in parts if p)


def _body_text(m) -> str:
    try:
        t = m.best_text() or ""
    except Exception:  # noqa: BLE001
        t = ""
    if not t.strip():
        try:
            h = m.best_html() or ""
            if h:
                from . import rtf
                t = rtf.html_to_text(h)
        except Exception:  # noqa: BLE001
            t = ""
    t = re.sub(r"[ \t]+", " ", t.replace("\r\n", "\n").replace("\r", "\n"))
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:MAX_BODY]


def _attachment_text(m, depth: int = 0) -> Tuple[List[str], List[str], int]:
    """(names, texts, count) for a message's attachments, embedded messages included."""
    names: List[str] = []
    texts: List[str] = []
    count = 0
    try:
        atts = m.attachments()
    except Exception:  # noqa: BLE001
        return names, texts, count
    total = 0
    for a in atts:
        count += 1
        try:
            fname = a.filename or ""
        except Exception:  # noqa: BLE001
            fname = ""
        if fname:
            names.append(fname)
        if total > MAX_ATT_TEXT:
            continue
        try:
            if a.is_embedded_message:
                sub = a.embedded_message
                if sub is None or depth >= MAX_EMBED_DEPTH:
                    continue
                head = " / ".join(x for x in (sub.subject, sub.sender_name or sub.sender_email) if x)
                if head:
                    names.append(head)
                body = _body_text(sub)
                sn, st, sc = _attachment_text(sub, depth + 1)
                names.extend(sn)
                count += sc
                piece = "\n".join(p for p in ([f"• {fname or sub.subject or 'message'}", head, body] + st) if p)
            else:
                data = a.data
                mime = getattr(a, "mime_type", "") or ""
                txt = extract.extract(fname, data, mime, depth=depth)
                piece = f"• {fname}\n{txt}" if txt else ""
        except Exception:  # noqa: BLE001
            piece = ""
        if piece:
            texts.append(piece)
            total += len(piece)
    return names, texts, count


# ---------------------------------------------------------------------------
# query parsing
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r"""(?P<neg>[!-])?(?:(?P<field>[A-Za-z]+):)?(?:"(?P<q>[^"]*)"?|(?P<w>\S+))""")

FIELD_COLUMNS = {
    "from": "{sender}", "sender": "{sender}",
    "to": "{recipients}", "cc": "{recipients}", "recipient": "{recipients}", "recipients": "{recipients}",
    "subject": "{subject}", "subj": "{subject}",
    "body": "{body}", "text": "{body}",
    "att": "{attachments att_names}", "attachment": "{attachments att_names}", "attachments": "{attachments att_names}",
    "file": "{att_names}", "filename": "{att_names}", "name": "{att_names}",
    "content": "{attachments}",
}


def _fts_term(text: str, column: str = "", prefix: bool = False) -> str:
    """One safe FTS5 fragment: words are quoted (so punctuation never breaks the
    parser); a quoted group becomes a phrase; prefix adds the trailing *."""
    words = re.findall(r"\w+", text, re.UNICODE)
    if not words:
        return ""
    phrase = '"' + " ".join(words) + '"'
    if prefix:
        phrase += "*"
    return f"{column}: {phrase}" if column else phrase


def _parse_date(text: str, end: bool) -> Optional[float]:
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$", text.strip())
    if not m:
        return None
    y = int(m.group(1))
    mo = int(m.group(2) or 1)
    d = int(m.group(3) or 1)
    try:
        start = _dt.datetime(y, mo, d, tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    if not end:
        return start.timestamp()
    # before:X means strictly before the start of the next unit
    if m.group(3):
        nxt = start + _dt.timedelta(days=1)
    elif m.group(2):
        nxt = _dt.datetime(y + (mo == 12), 1 if mo == 12 else mo + 1, 1, tzinfo=_dt.timezone.utc)
    else:
        nxt = _dt.datetime(y + 1, 1, 1, tzinfo=_dt.timezone.utc)
    return nxt.timestamp()


def parse_query(text: str, live: bool = False) -> Query:
    q = Query(text=text)
    tokens = list(_TOKEN.finditer(text or ""))
    for i, m in enumerate(tokens):
        neg = bool(m.group("neg"))
        fld = (m.group("field") or "").lower()
        quoted = m.group("q") is not None
        val = m.group("q") if quoted else m.group("w")
        val = val or ""
        last = i == len(tokens) - 1 and not text.endswith((" ", '"'))
        if fld in ("in", "folder"):
            q.folder.append(val.lower())
            continue
        if fld in ("source", "mailbox", "store"):
            q.source.append(val.lower())
            continue
        if fld == "after" or fld == "since":
            q.after = _parse_date(val, end=False)
            continue
        if fld == "before" or fld == "until":
            q.before = _parse_date(val, end=True)
            continue
        if fld == "has":
            if val.lower().startswith("att"):
                q.has_att = not neg
            continue
        if fld == "is" and val.lower() in ("attached", "attachment"):
            q.has_att = not neg
            continue
        column = FIELD_COLUMNS.get(fld, "")
        if fld and not column:
            # unknown prefix: treat "word:thing" as plain text
            val = f"{fld} {val}"
        frag = _fts_term(val, column, prefix=live and last and not quoted and not neg)
        if not frag:
            continue
        (q.negative if neg else q.positive).append(frag)
    return q


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    indexed_at REAL,
    complete INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    folder TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    recipients TEXT NOT NULL DEFAULT '',
    ts REAL,
    size INTEGER NOT NULL DEFAULT 0,
    att_count INTEGER NOT NULL DEFAULT 0,
    att_names TEXT NOT NULL DEFAULT '',
    message_class TEXT NOT NULL DEFAULT '',
    indexed_at REAL,
    UNIQUE(source_id, key)
);
CREATE INDEX IF NOT EXISTS messages_source ON messages(source_id);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS texts (
    id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    subject TEXT, sender TEXT, recipients TEXT, body TEXT, attachments TEXT, att_names TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    subject, sender, recipients, body, attachments, att_names,
    content='texts', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3'
);
CREATE TRIGGER IF NOT EXISTS texts_ai AFTER INSERT ON texts BEGIN
  INSERT INTO docs(rowid, subject, sender, recipients, body, attachments, att_names)
  VALUES (new.id, new.subject, new.sender, new.recipients, new.body, new.attachments, new.att_names);
END;
CREATE TRIGGER IF NOT EXISTS texts_ad AFTER DELETE ON texts BEGIN
  INSERT INTO docs(docs, rowid, subject, sender, recipients, body, attachments, att_names)
  VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body, old.attachments, old.att_names);
END;
CREATE TRIGGER IF NOT EXISTS texts_au AFTER UPDATE ON texts BEGIN
  INSERT INTO docs(docs, rowid, subject, sender, recipients, body, attachments, att_names)
  VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body, old.attachments, old.att_names);
  INSERT INTO docs(rowid, subject, sender, recipients, body, attachments, att_names)
  VALUES (new.id, new.subject, new.sender, new.recipients, new.body, new.attachments, new.att_names);
END;
"""

# bm25 weights: subject, sender, recipients, body, attachments, att_names
_WEIGHTS = "10.0, 3.0, 2.0, 1.0, 2.0, 4.0"
_MARK_L, _MARK_R = "\x01", "\x02"


def _with_db(fn):
    """Give the calling thread a pooled connection for the duration of the call
    (nested calls share it); the connection goes back to the pool afterwards, so
    short-lived worker threads do not each leave one open."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self._enter()
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._leave()
    return wrapper


class Indexer:
    """One index file. Safe to use from several threads: connections are pooled
    and handed to whichever thread is inside a public method."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or default_db_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._pool_lock = threading.Lock()
        self._pool: List[sqlite3.Connection] = []
        self._all: List[sqlite3.Connection] = []
        self._closed = False
        self._init_schema()

    # -- connections -------------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA temp_store=MEMORY")
        return c

    def _enter(self) -> None:
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            with self._pool_lock:
                if self._closed:
                    raise RuntimeError("the index has been closed")
                c = self._pool.pop() if self._pool else None
                if c is None:
                    c = self._open()
                    self._all.append(c)
            self._local.conn = c
        self._local.depth = depth + 1

    def _leave(self) -> None:
        depth = getattr(self._local, "depth", 1) - 1
        self._local.depth = depth
        if depth == 0:
            c = self._local.conn
            self._local.conn = None
            with self._pool_lock:
                if not self._closed and len(self._pool) < 4:
                    self._pool.append(c)
                    return
            try:
                c.close()
            except sqlite3.Error:
                pass

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            raise RuntimeError("Indexer internals used outside a public method")
        return c

    @_with_db
    def _init_schema(self) -> None:
        c = self._conn()
        with self._write_lock:
            c.executescript(_SCHEMA)
            row = c.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None:
                c.execute("INSERT INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('created', ?)", (str(time.time()),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(f"index was written by a different version of {APP}; "
                                   f"use Index > Clear index (or delete {self.db_path})")
            c.commit()

    def close(self) -> None:
        with self._pool_lock:
            self._closed = True
            conns = list(self._all)
            self._pool.clear()
            self._all.clear()
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    # -- bookkeeping -------------------------------------------------------
    @_with_db
    def sources(self) -> List[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM sources ORDER BY name COLLATE NOCASE").fetchall()

    @_with_db
    def source_state(self, path: str) -> Optional[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM sources WHERE path=?", (os.path.abspath(path),)).fetchone()

    @_with_db
    def is_current(self, path: str) -> bool:
        """True when the mailbox at path is fully indexed and unchanged since."""
        st = self.source_state(path)
        if st is None or not st["complete"]:
            return False
        size, mtime = _source_stat(path)
        return size == st["size"] and abs(mtime - st["mtime"]) < 1.0

    @_with_db
    def stats(self) -> dict:
        c = self._conn()
        n_src = c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        n_msg = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_att = c.execute("SELECT COALESCE(SUM(att_count),0) FROM messages").fetchone()[0]
        n_att_text = c.execute("SELECT COUNT(*) FROM texts WHERE attachments <> ''").fetchone()[0]
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        created = c.execute("SELECT value FROM meta WHERE key='created'").fetchone()
        return {
            "path": self.db_path,
            "sources": n_src,
            "messages": n_msg,
            "attachments": n_att,
            "messages_with_attachment_text": n_att_text,
            "bytes": size,
            "created": float(created["value"]) if created else None,
            "pdf_engine": extract.pdf_engine(),
            "sqlite": sqlite3.sqlite_version,
        }

    @_with_db
    def remove_source(self, path: str) -> int:
        c = self._conn()
        with self._write_lock:
            row = c.execute("SELECT id FROM sources WHERE path=?", (os.path.abspath(path),)).fetchone()
            if row is None:
                return 0
            n = c.execute("SELECT COUNT(*) FROM messages WHERE source_id=?", (row["id"],)).fetchone()[0]
            c.execute("DELETE FROM texts WHERE id IN (SELECT id FROM messages WHERE source_id=?)", (row["id"],))
            c.execute("DELETE FROM messages WHERE source_id=?", (row["id"],))
            c.execute("DELETE FROM sources WHERE id=?", (row["id"],))
            c.commit()
            return n

    @_with_db
    def prune_missing(self) -> List[str]:
        """Forget mailboxes whose files no longer exist."""
        gone = [r["path"] for r in self.sources() if not os.path.exists(r["path"])]
        for p in gone:
            self.remove_source(p)
        return gone

    @_with_db
    def clear(self) -> None:
        c = self._conn()
        with self._write_lock:
            c.execute("DELETE FROM texts")
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM sources")
            c.commit()
            try:
                c.execute("VACUUM")
            except sqlite3.OperationalError:
                pass

    @_with_db
    def optimize(self) -> None:
        c = self._conn()
        with self._write_lock:
            c.execute("INSERT INTO docs(docs) VALUES ('optimize')")
            c.commit()

    # -- indexing ----------------------------------------------------------
    @_with_db
    def index_source(self, source, progress: Optional[ProgressFn] = None,
                     cancel: Optional[threading.Event] = None, force: bool = False) -> IndexResult:
        """Bring one opened MailSource up to date in the index."""
        t0 = time.time()
        path = os.path.abspath(source.path)
        label = source.name
        res = IndexResult(source=label)
        size, mtime = _source_stat(path)
        c = self._conn()
        st = self.source_state(path)
        if st is not None and not force and st["complete"] and st["size"] == size and abs(st["mtime"] - mtime) < 1.0:
            res.unchanged = True
            res.skipped = st["messages"]
            res.seconds = time.time() - t0
            return res

        with self._write_lock:
            if st is None:
                c.execute("INSERT INTO sources(path, kind, name, size, mtime, complete) VALUES (?,?,?,?,?,0)",
                          (path, getattr(source, "kind", "generic"), label, size, mtime))
                source_id = c.execute("SELECT id FROM sources WHERE path=?", (path,)).fetchone()["id"]
            else:
                source_id = st["id"]
                c.execute("UPDATE sources SET kind=?, name=?, complete=0 WHERE id=?",
                          (getattr(source, "kind", "generic"), label, source_id))
            c.commit()

        existing: Dict[str, int] = {r["key"]: r["id"] for r in
                                    c.execute("SELECT id, key FROM messages WHERE source_id=?", (source_id,))}
        if force and existing:
            with self._write_lock:
                c.execute("DELETE FROM texts WHERE id IN (SELECT id FROM messages WHERE source_id=?)", (source_id,))
                c.execute("DELETE FROM messages WHERE source_id=?", (source_id,))
                c.commit()
            existing = {}

        if progress:
            progress(0, 0, f"Listing {label}…")
        rows = []
        try:
            for r in source.all_message_rows():
                rows.append(r)
                if cancel is not None and cancel.is_set():
                    break
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"listing stopped: {exc}")
        # orphans (PST messages whose folder is gone) are still searchable
        try:
            if hasattr(source, "orphan_messages"):
                for r in source.orphan_messages():
                    rows.append(r)
        except Exception:  # noqa: BLE001
            pass
        total = len(rows)

        seen: Dict[str, int] = {}
        pending = 0
        now = time.time()
        for i, row in enumerate(rows):
            if cancel is not None and cancel.is_set():
                res.cancelled = True
                break
            key = message_key(row)
            if key in seen:
                seen[key] += 1
                key = f"{key}#{seen[key]}"
            else:
                seen[key] = 0
            if key in existing:
                res.skipped += 1
                del existing[key]
                if progress and i % 50 == 0:
                    progress(i + 1, total, f"{label}: {i + 1:,} / {total:,} (already indexed)")
                continue
            if progress and (i % 10 == 0 or i == total - 1):
                progress(i + 1, total, f"{label}: {i + 1:,} / {total:,}  {row.subject[:60] if row.subject else ''}")
            try:
                rec = self._record(row)
            except Exception as exc:  # noqa: BLE001
                res.errors += 1
                rec = self._record_from_row(row, note=f"could not read: {exc}")
            res.attachments += rec["att_count"]
            with self._write_lock:
                cur = c.execute(
                    "INSERT INTO messages(source_id, folder, key, subject, sender, sender_email, recipients, ts, size, "
                    "att_count, att_names, message_class, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (source_id, rec["folder"], key, rec["subject"], rec["sender"], rec["sender_email"],
                     rec["recipients"], rec["ts"], rec["size"], rec["att_count"], rec["att_names"],
                     rec["message_class"], now))
                c.execute("INSERT INTO texts(id, subject, sender, recipients, body, attachments, att_names) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (cur.lastrowid, rec["subject"], rec["sender"] + " " + rec["sender_email"], rec["recipients"],
                           rec["body"], rec["att_text"], rec["att_names"]))
            res.added += 1
            pending += 1
            if pending >= COMMIT_EVERY:
                with self._write_lock:
                    c.commit()
                pending = 0

        with self._write_lock:
            if not res.cancelled and existing:
                ids = list(existing.values())
                for j in range(0, len(ids), 500):
                    chunk = ids[j:j + 500]
                    marks = ",".join("?" * len(chunk))
                    c.execute(f"DELETE FROM texts WHERE id IN ({marks})", chunk)
                    c.execute(f"DELETE FROM messages WHERE id IN ({marks})", chunk)
                res.removed = len(ids)
            n_msgs = c.execute("SELECT COUNT(*) FROM messages WHERE source_id=?", (source_id,)).fetchone()[0]
            c.execute("UPDATE sources SET size=?, mtime=?, indexed_at=?, complete=?, messages=? WHERE id=?",
                      (size, mtime, time.time(), 0 if res.cancelled else 1, n_msgs, source_id))
            c.commit()
        res.seconds = time.time() - t0
        return res

    @_with_db
    def index_many(self, sources: Iterable, progress: Optional[ProgressFn] = None,
                   cancel: Optional[threading.Event] = None, force: bool = False) -> List[IndexResult]:
        out = []
        for s in sources:
            if cancel is not None and cancel.is_set():
                break
            out.append(self.index_source(s, progress=progress, cancel=cancel, force=force))
        if out and any(r.added or r.removed for r in out):
            try:
                self.optimize()
            except sqlite3.Error:
                pass
        return out

    def _record_from_row(self, row, note: str = "") -> dict:
        sender = row.sender or ""
        email = ""
        m = re.search(r"<([^<>]+@[^<>]+)>", sender) or re.search(r"\b([\w.+-]+@[\w.-]+\.\w+)\b", sender)
        if m:
            email = m.group(1)
        return {
            "folder": row.folder.path_str,
            "subject": row.subject or "",
            "sender": sender,
            "sender_email": email,
            "recipients": row.to or "",
            "ts": _ts(row.date),
            "size": int(row.size or 0),
            "att_count": 0,
            "att_names": "",
            "att_text": "",
            "body": note,
            "message_class": row.message_class or "",
        }

    def _record(self, row) -> dict:
        m = row.open()
        subject = m.subject or row.subject or ""
        sender_name = (m.sender_name or "").strip()
        sender_email = (m.sender_email or "").strip()
        sender = sender_name or sender_email or row.sender or ""
        recipients = _recipients_text(m) or row.to or ""
        body = _body_text(m)
        names, texts, count = _attachment_text(m)
        att_text = "\n\n".join(texts)[:MAX_ATT_TEXT]
        date = m.date or row.date
        try:
            size = int(m.size or row.size or 0)
        except Exception:  # noqa: BLE001
            size = int(row.size or 0)
        return {
            "folder": row.folder.path_str,
            "subject": subject,
            "sender": sender,
            "sender_email": sender_email,
            "recipients": recipients,
            "ts": _ts(date),
            "size": size,
            "att_count": count,
            "att_names": "; ".join(n for n in names if n),
            "att_text": att_text,
            "body": body,
            "message_class": getattr(m, "message_class", "") or row.message_class or "",
        }

    # -- searching ---------------------------------------------------------
    @_with_db
    def search(self, text: str, limit: int = 500, live: bool = False, offset: int = 0) -> List[Hit]:
        q = parse_query(text, live=live)
        return self.run(q, limit=limit, offset=offset)

    @_with_db
    def run(self, q: Query, limit: int = 500, offset: int = 0) -> List[Hit]:
        c = self._conn()
        where: List[str] = []
        args: list = []
        for f in q.folder:
            where.append("LOWER(m.folder) LIKE ?")
            args.append(f"%{f}%")
        for s in q.source:
            where.append("(LOWER(s.name) LIKE ? OR LOWER(s.path) LIKE ?)")
            args.extend([f"%{s}%", f"%{s}%"])
        if q.after is not None:
            where.append("m.ts >= ?")
            args.append(q.after)
        if q.before is not None:
            where.append("m.ts < ?")
            args.append(q.before)
        if q.has_att is not None:
            where.append("m.att_count > 0" if q.has_att else "m.att_count = 0")

        select = ("m.id, m.source_id, s.path AS source_path, s.kind AS source_kind, s.name AS source_name, "
                  "m.folder, m.key, m.subject, m.sender, m.sender_email, m.recipients, m.ts, m.size, "
                  "m.att_count, m.att_names, m.message_class")
        if q.positive:
            match = " AND ".join(q.positive)
            if q.negative:
                match = f"({match}) NOT ({' OR '.join(q.negative)})"
            sql = (f"SELECT {select}, bm25(docs, {_WEIGHTS}) AS score, "
                   f"snippet(docs, 0, ?, ?, '…', 12) AS s_subject, "
                   f"snippet(docs, 1, ?, ?, '…', 12) AS s_sender, "
                   f"snippet(docs, 2, ?, ?, '…', 12) AS s_recip, "
                   f"snippet(docs, 3, ?, ?, '…', 24) AS s_body, "
                   f"snippet(docs, 4, ?, ?, '…', 24) AS s_att, "
                   f"snippet(docs, 5, ?, ?, '…', 12) AS s_names "
                   f"FROM docs JOIN messages m ON m.id = docs.rowid JOIN sources s ON s.id = m.source_id "
                   f"WHERE docs MATCH ?")
            params: list = [_MARK_L, _MARK_R] * 6 + [match] + args
            if where:
                sql += " AND " + " AND ".join(where)
            sql += " ORDER BY score LIMIT ? OFFSET ?"
            params += [limit, offset]
        else:
            sql = (f"SELECT {select}, 0.0 AS score, '' AS s_subject, '' AS s_sender, '' AS s_recip, "
                   f"'' AS s_body, '' AS s_att, '' AS s_names "
                   f"FROM messages m JOIN sources s ON s.id = m.source_id")
            params = list(args)
            if q.negative:
                where.append("m.id NOT IN (SELECT rowid FROM docs WHERE docs MATCH ?)")
                params.append(" OR ".join(q.negative))
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY m.ts DESC LIMIT ? OFFSET ?"
            params += [limit, offset]
        try:
            rows = c.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "syntax" in str(exc).lower() or "no such column" in str(exc).lower():
                return []
            raise
        hits = []
        for r in rows:
            where_, snip = _pick_snippet(r)
            hits.append(Hit(
                id=r["id"], source_id=r["source_id"], source_path=r["source_path"], source_kind=r["source_kind"],
                source_name=r["source_name"], folder=r["folder"], key=r["key"], subject=r["subject"],
                sender=r["sender"], sender_email=r["sender_email"], recipients=r["recipients"], ts=r["ts"],
                size=r["size"], att_count=r["att_count"], att_names=r["att_names"], message_class=r["message_class"],
                where=where_, snippet=snip, score=r["score"]))
        return hits

    @_with_db
    def count(self, text: str) -> int:
        q = parse_query(text)
        c = self._conn()
        if q.positive:
            match = " AND ".join(q.positive)
            if q.negative:
                match = f"({match}) NOT ({' OR '.join(q.negative)})"
            try:
                return c.execute("SELECT COUNT(*) FROM docs WHERE docs MATCH ?", (match,)).fetchone()[0]
            except sqlite3.OperationalError:
                return 0
        return c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    # -- getting back to the real message ---------------------------------
    @_with_db
    def message_text(self, hit_id: int) -> Optional[sqlite3.Row]:
        return self._conn().execute("SELECT * FROM texts WHERE id=?", (hit_id,)).fetchone()


def _pick_snippet(r: sqlite3.Row) -> Tuple[str, str]:
    """Which field carried the match, and a readable snippet with [brackets]."""
    order = (("attachment", r["s_att"]), ("body", r["s_body"]), ("subject", r["s_subject"]),
             ("attachment name", r["s_names"]), ("sender", r["s_sender"]), ("recipients", r["s_recip"]))
    for where, s in order:
        if s and _MARK_L in s:
            snip = re.sub(r"\s+", " ", s).replace(_MARK_L, "[").replace(_MARK_R, "]").strip()
            return where, snip
    return "", ""


# ---------------------------------------------------------------------------
# from a hit back to a listing row (so preview / export work on index results)
# ---------------------------------------------------------------------------
class Resolver:
    """Maps hits to live rows, opening mailboxes on demand and caching them.
    `opened` may be seeded with the sources the GUI already has open."""

    def __init__(self, opened: Optional[Dict[str, object]] = None):
        self.opened: Dict[str, object] = {os.path.abspath(k): v for k, v in (opened or {}).items()}
        self._maps: Dict[str, Dict[str, object]] = {}
        self.errors: Dict[str, str] = {}
        self.auto_opened: List[object] = []

    def source_for(self, hit: Hit):
        p = os.path.abspath(hit.source_path)
        src = self.opened.get(p)
        if src is not None:
            return src
        if p in self.errors:
            return None
        from .sources import open_found
        try:
            src = open_found(hit.source_kind, p)
        except Exception as exc:  # noqa: BLE001
            self.errors[p] = f"{type(exc).__name__}: {exc}"
            return None
        self.opened[p] = src
        self.auto_opened.append(src)
        return src

    def _map(self, src) -> Dict[str, object]:
        p = os.path.abspath(src.path)
        m = self._maps.get(p)
        if m is None:
            m = {}
            seen: Dict[str, int] = {}
            rows = list(src.all_message_rows())
            try:
                if hasattr(src, "orphan_messages"):
                    rows.extend(src.orphan_messages())
            except Exception:  # noqa: BLE001
                pass
            for row in rows:
                key = message_key(row)
                if key in seen:
                    seen[key] += 1
                    key = f"{key}#{seen[key]}"
                else:
                    seen[key] = 0
                m[key] = row
            self._maps[p] = m
        return m

    def row_for(self, hit: Hit):
        src = self.source_for(hit)
        if src is None:
            return None
        return self._map(src).get(hit.key)

    def rows_for(self, hits: Iterable[Hit]) -> List[Tuple[Hit, object]]:
        out = []
        for h in hits:
            r = self.row_for(h)
            if r is not None:
                out.append((h, r))
        return out

    def adopt(self, src) -> None:
        """A source the caller now owns (e.g. shown in the window): keep the row map, stop tracking it as ours."""
        p = os.path.abspath(src.path)
        self.opened[p] = src
        self.auto_opened = [s for s in self.auto_opened if s is not src]

    def forget(self, path: str) -> None:
        """The caller closed this source: drop it and its row map."""
        p = os.path.abspath(path)
        self.opened.pop(p, None)
        self._maps.pop(p, None)
        self.errors.pop(p, None)
        self.auto_opened = [s for s in self.auto_opened if os.path.abspath(s.path) != p]

    def close_auto_opened(self) -> None:
        for s in self.auto_opened:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
            self.opened.pop(os.path.abspath(s.path), None)
            self._maps.pop(os.path.abspath(s.path), None)
        self.auto_opened.clear()


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
