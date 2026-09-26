#!/usr/bin/env python3
"""Frozen entry point for PST Exporter (PyInstaller builds).

Handles what a double-clicked, windowed executable has to handle: Finder's
-psn argument, a missing stdout, and start-up failures that would otherwise
be silent.

  PST Exporter                   launch the window
  PST Exporter FILE.pst          launch with a file loaded
  PST Exporter selftest          run the self-test, write pst-exporter-selftest.txt, exit
  PST Exporter export ...        command-line export (see --help)
"""
import multiprocessing
import os
import sys
import traceback


def _app_dir():
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        parts = exe.split(os.sep)
        for i, p in enumerate(parts):
            if p.endswith(".app"):
                return os.sep.join(parts[:i]) or os.sep
        return os.path.dirname(exe)
    return os.path.dirname(os.path.abspath(__file__))


def _fix_streams():
    """A windowed build has no console: sys.stdout and sys.stderr are None."""
    class _Null:
        def write(self, *_a):
            return 0

        def flush(self):
            pass

        def isatty(self):
            return False

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, _Null())
        else:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def _crash(exc):
    path = os.path.join(_app_dir(), "pst-exporter-crash.log")
    body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(body + "\n" + "-" * 60 + "\n")
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("PST Exporter could not start",
                             f"{type(exc).__name__}: {exc}\n\nDetails written to:\n{path}")
        root.destroy()
    except Exception:
        pass
    print(body, file=sys.stderr)


def main():
    multiprocessing.freeze_support()
    _fix_streams()
    # Finder passes -psn_0_12345 to a bundle; a strict parser would exit(2)
    # here and the window would never appear.
    args = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from pstlib.cli import main as cli_main
        return cli_main(args)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        _crash(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
