#!/bin/sh
# Install the usage web panel where a double-clicked .command can read it.
#
# ~/Desktop is TCC-protected. Terminal is not granted it, and a .command opened
# from Finder is granted only the file that was clicked -- so a launcher sitting
# in this repo can start, and then cannot read the server sitting next to it.
# The same wall the launch agent for usage_server.py hit, and the same way
# round it: keep the copy that actually runs under ~/Library/Application
# Support, which nothing guards.
#
# Run this from a shell that can read the repo -- your own terminal if it has
# been granted the Desktop, or whatever is already editing these files.
# Re-run it after changing serve.py or index.html; the installed copy is a copy.

set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/t-display-panel-web"

mkdir -p "$DEST"
cp "$SRC/serve.py"             "$DEST/"
cp "$SRC/index.html"           "$DEST/"
cp "$SRC/usage-panel.command"  "$DEST/"
cp "$SRC/../fonts/IBMPlexSansThai-Regular.ttf" "$DEST/"
chmod +x "$DEST/usage-panel.command"

echo "installed to:"
echo "    $DEST"
echo
echo "Double-click usage-panel.command in there. To keep it somewhere handier,"
echo "make an alias of it (right-click, Make Alias) rather than a copy -- a"
echo "copy on the Desktop lands back behind the same wall."
