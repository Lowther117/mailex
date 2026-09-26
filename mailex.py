#!/usr/bin/env python3
"""Mailex - open any mailbox, export it any way.

  python mailex.py                  open the window
  python mailex.py FILE.pst ...     open the window with files loaded
  python mailex.py export ...       export from the command line
  python mailex.py list FILE        print the folder tree
  python mailex.py selftest         build synthetic PSTs, read and export them
  python mailex.py --help           all options
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mailexlib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
