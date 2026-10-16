#!/bin/sh
# Starts the reader, then opens the page in your browser.
# macOS: double-click start_reader.command instead (it calls this).
# Linux: run ./start_reader.sh  or  sh start_reader.sh

cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' >/dev/null 2>&1; then
  PY=python
else
  echo ""
  echo "Python 3 was not found."
  echo "Install it from https://www.python.org/downloads/"
  echo "(on a Mac, running  xcode-select --install  in Terminal is enough)"
  echo "then run this again."
  echo ""
  printf "Press Return to close."
  read _pause
  exit 1
fi

exec "$PY" server.py --open
