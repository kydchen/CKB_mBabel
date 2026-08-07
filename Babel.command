#!/bin/bash
# Babel one-click launcher for macOS. Edit --device/--channels to match your
# Aggregate Device (see README "Audio routing"), or drop both for the plain mic.
cd "$(dirname "$0")/solution" || exit 1
exec .venv/bin/python main.py "$@"
