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
# Re-run it after changing serve.py or index.html, or after moving house; the
# installed copy is a copy, panel.json included.

set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/t-display-panel-web"

mkdir -p "$DEST"
cp "$SRC/serve.py"             "$DEST/"
cp "$SRC/index.html"           "$DEST/"
cp "$SRC/desk-panel.command"   "$DEST/"
cp "$SRC/../fonts/IBMPlexSansThai-Regular.ttf" "$DEST/"
chmod +x "$DEST/desk-panel.command"

# The launcher this one was renamed from. Left behind it would be a second
# thing to double-click that still works, off whatever it was installed with --
# which is the worst way for a rename to go wrong.
rm -f "$DEST/usage-panel.command"

# Where the weather is. The panel gets it out of include/secrets.h at compile
# time; the installed server is not next to that file and could not read it
# there anyway, so the four values are lifted into panel.json here. Same source,
# read once, at the one moment something can still see both.
/usr/bin/python3 - "$SRC" "$DEST" <<'PY'
import json, os, sys
sys.path.insert(0, sys.argv[1])
import serve

if serve.PLACE:
    with open(os.path.join(sys.argv[2], "panel.json"), "w") as f:
        json.dump(serve.PLACE, f, indent=2)
    print("weather: %s" % (serve.PLACE.get("place") or "configured"))
else:
    print("weather: no WEATHER_LAT in include/secrets.h -- the line will say so")
PY

echo "installed to:"
echo "    $DEST"
echo
echo "Double-click desk-panel.command in there. To keep it somewhere handier,"
echo "make an alias of it (right-click, Make Alias) rather than a copy -- a"
echo "copy on the Desktop lands back behind the same wall."
