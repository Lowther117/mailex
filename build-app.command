#!/bin/bash
# ---------------------------------------------------------------------------
#  Mailex - macOS build. Double-click this file.
#  Installs Homebrew and Python if they are missing, installs every
#  dependency, and produces dist/Mailex.app.
# ---------------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
LOG="$HERE/build-mac-log.txt"
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

# A double-clicked .command starts with a bare PATH that has no Homebrew in it.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export HOMEBREW_NO_AUTO_UPDATE=1
export HOMEBREW_NO_INSTALL_CLEANUP=1

echo "==============================================="
echo "  Building Mailex for macOS"
echo "  Full log: build-mac-log.txt"
echo "==============================================="
echo

die() {
  echo
  echo "==============================================="
  echo "  BUILD FAILED: $*"
  echo "==============================================="
  echo
  echo "Press return to close."
  read -r
  exit 1
}

ensure_brew() {
  if command -v brew >/dev/null 2>&1; then return 0; fi
  echo "Homebrew is not installed. Installing it now - it will ask for your password once."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/tty \
    || die "Homebrew would not install."
  export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
  command -v brew >/dev/null 2>&1 || die "Homebrew installed but brew is still not on the PATH."
}

brew_install() {
  ensure_brew
  echo "  brew install $*"
  brew install "$@" < /dev/null || die "brew install $* failed."
}

# Apple's /usr/bin/python3 links against system Tk 8.5, which PyInstaller
# cannot bundle: the app builds and then never opens a window. Only accept an
# interpreter whose tkinter reports 8.6 or newer. Resolve candidates with
# readlink rather than running python3, because on a clean Mac running
# python3 pops up the Xcode command line tools installer.
pick_python() {
  local c real
  if [ -n "$MAILEX_BUILD_PYTHON" ] && [ -x "$MAILEX_BUILD_PYTHON" ]; then
    if "$MAILEX_BUILD_PYTHON" -c 'import tkinter,sys;sys.exit(0 if tkinter.TkVersion>=8.6 else 1)' 2>/dev/null; then
      echo "$MAILEX_BUILD_PYTHON"; return 0
    fi
  fi
  for c in /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
           /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 /opt/homebrew/bin/python3.9 \
           /usr/local/bin/python3.14 /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
           /usr/local/bin/python3.11 /usr/local/bin/python3.10 /usr/local/bin/python3.9 \
           /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.9/bin/python3; do
    [ -e "$c" ] || continue
    real="$(readlink -f "$c" 2>/dev/null || echo "$c")"
    [ -x "$real" ] || continue
    case "$real" in /usr/bin/python3) continue ;; esac
    if "$real" -c 'import tkinter,sys;sys.exit(0 if tkinter.TkVersion>=8.6 else 1)' 2>/dev/null; then
      echo "$real"; return 0
    fi
  done
  return 1
}

echo "[1/7] Finding a Python with a usable Tk..."
PY="$(pick_python)" || {
  echo "      None found. Installing Python and Tk through Homebrew..."
  brew_install python python-tk
  PY="$(pick_python)" || die "Even after installing python and python-tk, no interpreter reports Tk 8.6+."
}
echo "      using $PY"
"$PY" -c 'import sys,tkinter;print("      python",sys.version.split()[0],"tk",tkinter.TkVersion)'

echo "[2/7] Creating a clean build environment..."
VENV="$HERE/.venv-build-mac"
rm -rf "$VENV"
"$PY" -m venv "$VENV" || die "Could not create the build environment."
VPY="$VENV/bin/python"

echo "[3/7] Installing dependencies (wheels only, no compiling)..."
"$VPY" -m pip install --upgrade pip wheel >/dev/null || die "pip would not upgrade."
"$VPY" -m pip install --only-binary :all: -r "$HERE/requirements.txt" || die "A dependency has no wheel for this Mac."
"$VPY" -m pip install --only-binary :all: pyinstaller || die "PyInstaller would not install."

echo "[4/7] Checking the code before packaging..."
"$VPY" -c "import sys; sys.path.insert(0,'.'); import mailexlib.ui, mailexlib.export, mailexlib.pdfout; print('      imports ok')" \
  || die "The application does not import cleanly."

echo "[5/7] Packaging (this takes a minute or two)..."
rm -rf "$HERE/build" "$HERE/dist"
"$VPY" -m PyInstaller --noconfirm --clean --windowed \
  --name "Mailex" \
  --osx-bundle-identifier "uk.lowther.mailex" \
  --collect-all reportlab \
  --collect-all PIL \
  --collect-all tkinterdnd2 \
  --collect-submodules mailexlib \
  --hidden-import tkinter \
  --hidden-import tkinter.ttk \
  --hidden-import tkinter.filedialog \
  --hidden-import tkinter.messagebox \
  --exclude-module pytest \
  "$HERE/mailex_app.py" || die "PyInstaller failed - see the log above."
[ -d "$HERE/dist/Mailex.app" ] || die "dist/Mailex.app was not produced."

echo "[6/7] Clearing quarantine and signing locally..."
# Without these two an app built on Apple silicon is killed the moment it opens.
xattr -cr "$HERE/dist/Mailex.app" || true
codesign --force --deep --sign - "$HERE/dist/Mailex.app" || die "Ad-hoc signing failed."

echo "[7/7] Running the self-test on the built app..."
echo "      It builds three small PST files, reads them back and exports every"
echo "      format. About half a minute, and it will look idle while it works."
rm -f "$HERE/dist/mailex-selftest.txt"
"$HERE/dist/Mailex.app/Contents/MacOS/Mailex" selftest || true
sleep 2
if [ -f "$HERE/dist/mailex-selftest.txt" ]; then
  echo
  cat "$HERE/dist/mailex-selftest.txt"
  if grep -q "PROBLEMS FOUND" "$HERE/dist/mailex-selftest.txt"; then
    echo
    echo "==============================================="
    echo "  BUILT, BUT THE SELF-TEST REPORTED PROBLEMS."
    echo "  See dist/mailex-selftest.txt above."
    echo "==============================================="
    echo
    echo "Press return to close."
    read -r
    exit 2
  fi
else
  echo "      (no self-test report was written - check it by hand)"
fi

echo
echo "==============================================="
echo "  DONE.  dist/Mailex.app is ready."
echo "  Drag it to your Applications folder."
echo "==============================================="
echo
echo "Press return to close."
read -r
