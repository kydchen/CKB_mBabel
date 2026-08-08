#!/bin/bash
# Babel one-click launcher. Double-click in Finder (or right-click -> Open
# the first time to bypass Gatekeeper), or pin to the Dock.
cd "$(dirname "$0")/solution" || exit 1
exec .venv/bin/python main.py --share "$@"
