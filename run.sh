#!/bin/sh

VENV_DIR=".venv"

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Activate virtual environment
. $VENV_DIR/bin/activate

# Change to project directory and add it to PYTHONPATH
cd "$SCRIPT_DIR"
export PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH"

# HOME decides where FileStorageService keeps its JSON data, because it
# resolves ~/.config/webzfs through Path.home(). Set it explicitly so a
# manual start reads the same data as the managed service.
#
# This matters on FreeBSD and NetBSD, where WebZFS runs as root: without
# it, root's home is /root and a manual start would silently use
# /root/.config/webzfs while the rc.d service uses the install prefix.
# On Linux the webzfs user is created with the install prefix as its
# home, so this assignment simply matches what already happens.
export HOME="$SCRIPT_DIR"


gunicorn -c config/gunicorn.conf.py
