#!/usr/bin/env python3
"""PST Exporter - read and export Outlook PST / OST files without Outlook.

  python pst_exporter.py                  open the window
  python pst_exporter.py FILE.pst ...     open the window with files loaded
  python pst_exporter.py export ...       export from the command line
  python pst_exporter.py list FILE        print the folder tree
  python pst_exporter.py selftest         build synthetic PSTs, read and export them
  python pst_exporter.py --help           all options
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pstlib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
