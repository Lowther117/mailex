"""Drag and drop, both ways, through tkinterdnd2 when it is installed.

Out: the message list is a drag source. Dragging selected rows onto Finder,
Explorer or any folder window drops real files - `.eml` for messages, `.vcf`
for contacts - written to a per-session temporary folder the moment the drag
starts, and removed when the program exits.

In: a mailbox file or folder dropped anywhere on the window is opened.

Everything here degrades to nothing when tkinterdnd2 is missing: `available()`
is False, `make_root()` gives a plain tk.Tk(), the two `enable_*` calls return
False and the window behaves exactly as it did before drag and drop existed.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
from typing import Any, Callable, List, Optional, Sequence

MAX_DRAG = 200          # messages per drag; more than this is an export job, not a drag

try:  # the package cannot be assumed - it is a wheel with a compiled Tcl extension inside
    from tkinterdnd2 import COPY, DND_FILES, TkinterDnD  # type: ignore
    _HAVE = True
except Exception:  # noqa: BLE001 - ImportError, or a wheel without a binary for this platform
    TkinterDnD = None  # type: ignore
    DND_FILES = "DND_Files"
    COPY = "copy"
    _HAVE = False


def available() -> bool:
    return _HAVE


def make_root():
    """A Tk root that can take part in native drag and drop, when possible."""
    global _HAVE
    import tkinter as tk       # here, not at the top: the command line and the self-test never need Tk
    if _HAVE:
        try:
            return TkinterDnD.Tk()
        except Exception:  # noqa: BLE001 - the tkdnd library failed to load for this Tk
            _HAVE = False
    return tk.Tk()


class DragFolder:
    """The temporary folder that dragged-out files are written into."""

    def __init__(self):
        self._dir: Optional[str] = None
        self._lock = threading.Lock()
        self._taken: set = set()
        atexit.register(self.cleanup)

    @property
    def path(self) -> str:
        with self._lock:
            if self._dir is None or not os.path.isdir(self._dir):
                self._dir = tempfile.mkdtemp(prefix="mailex-drag-")
                self._taken = set()
            return self._dir

    def contains(self, p: str) -> bool:
        if self._dir is None:
            return False
        try:
            return os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(os.path.abspath(self._dir)) + os.sep)
        except Exception:  # noqa: BLE001
            return False

    def write(self, name: str, data: bytes) -> str:
        """Write data under a safe, unique name (name-2, name-3...) and return the path."""
        from .export import fs_path, sanitize
        base, ext = os.path.splitext(sanitize(name, 100))
        folder = self.path
        with self._lock:
            n = 1
            while True:
                cand = os.path.join(folder, f"{base}{'' if n == 1 else f'-{n}'}{ext}")
                key = os.path.normcase(cand)
                if key not in self._taken and not os.path.exists(cand):
                    self._taken.add(key)
                    break
                n += 1
        with open(fs_path(cand), "wb") as fh:
            fh.write(data)
        return cand

    def cleanup(self):
        d = self._dir
        self._dir = None
        if d:
            shutil.rmtree(d, ignore_errors=True)


def export_for_drag(rows: Sequence, folder: DragFolder) -> List[str]:
    """Write each row as .eml (contacts as .vcf) into the drag folder; returns the paths."""
    from . import contacts as _contacts
    from .export import eml_bytes, message_basename
    paths: List[str] = []
    for row in rows:
        try:
            msg = row.open()
            if _contacts.is_contact(msg):
                c = _contacts.parse_contact(msg)
                paths.append(folder.write(c.name + ".vcf", _contacts.vcard(c).encode("utf-8")))
            else:
                paths.append(folder.write(message_basename(msg, row) + ".eml", eml_bytes(msg)))
        except Exception:  # noqa: BLE001 - one unreadable message must not spoil the drag
            continue
    return paths


def enable_drag_out(widget: Any, rows_fn: Callable[[], Sequence], folder: DragFolder,
                    status: Callable[[str], None], began: Optional[Callable[[], None]] = None) -> bool:
    """Make `widget` a drag source whose payload is the files for rows_fn()."""
    if not _HAVE:
        return False
    try:
        widget.drag_source_register(1, DND_FILES)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False
    # tkdnd hangs its press/motion bindings on a TkDND_Drag1 bind tag placed
    # after the widget's own tag. Put it first so the widget's own
    # <ButtonPress-1> handler can stop the Treeview from collapsing a
    # multi-selection (returning "break") without also stopping the drag.
    try:
        tags = list(widget.bindtags())
        dnd = [t for t in tags if str(t).startswith("TkDND_Drag")]
        if dnd:
            widget.bindtags(tuple(dnd + [t for t in tags if t not in dnd]))
    except Exception:  # noqa: BLE001
        pass

    def on_init(_event):
        if began is not None:
            began()
        rows = list(rows_fn())
        if not rows:
            return "refuse_drop"
        if len(rows) > MAX_DRAG:
            status(f"Dragging the first {MAX_DRAG} of {len(rows)} selected messages - use Export selected… for more.")
            rows = rows[:MAX_DRAG]
        paths = export_for_drag(rows, folder)
        if not paths:
            status("Nothing could be written for that drag.")
            return "refuse_drop"
        status(f"Dragging {len(paths)} file{'s' if len(paths) != 1 else ''} - drop them in a folder to save.")
        # A tuple becomes a proper Tcl list, each path its own element (braced
        # when it holds spaces), which is what tkdnd wants for DND_Files.
        return (COPY, DND_FILES, tuple(paths))

    try:
        widget.dnd_bind("<<DragInitCmd>>", on_init)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False
    return True


def enable_drop_in(widgets: Sequence[Any], on_paths: Callable[[List[str]], None],
                   ignore: Optional[Callable[[str], bool]] = None) -> bool:
    """Accept files and folders dropped on any of `widgets`."""
    if not _HAVE:
        return False
    ok = False
    tk_ref = widgets[0]

    def on_drop(event):
        try:
            # %D is a Tcl list; splitlist copes with braced paths that hold spaces
            paths = list(tk_ref.tk.splitlist(event.data))
        except Exception:  # noqa: BLE001
            return None
        paths = [p for p in paths if p and os.path.exists(p) and not (ignore and ignore(p))]
        if paths:
            on_paths(paths)
        return event.action if hasattr(event, "action") else COPY

    for w in widgets:
        try:
            w.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            w.dnd_bind("<<Drop>>", on_drop)  # type: ignore[attr-defined]
            ok = True
        except Exception:  # noqa: BLE001
            continue
    return ok
