#!/bin/sh
# Double-click this in Finder to put the panel's usage page on the network.
#
# A web page cannot listen on a port -- browsers give a page no way to accept a
# connection -- so the thing you double-click has to be something that can start
# a process. This is that, and nothing else: it starts serve.py in the window
# Finder opens for it, and closing that window takes the server down with it.
#
# `exec` matters. Without it the shell stays as serve.py's parent, and closing
# the window kills the shell while the python carries on holding the port.
#
# Two places to look for the server, because of where this repo lives. Finder
# hands a double-clicked .command to Terminal, and Terminal is granted the file
# that was clicked and nothing else around it -- so out of ~/Desktop, which is
# TCC-protected, it can run this script and then cannot read serve.py sitting
# beside it. That is an EPERM on open, not a mode bit, and no chmod will move
# it. install.sh puts a copy under ~/Library/Application Support, which is not
# protected, and this falls through to it.
#
# Beside-me first so that running it out of the repo picks up edits. -r rather
# than -f: the file being unreadable is exactly the case being handled here,
# and it is not the same as the file being absent.
#
# Finder runs a .command from the home directory, not from the folder it is in,
# so the path has to be worked out rather than assumed.

DIR="$(dirname "$0")"
INSTALLED="$HOME/Library/Application Support/t-display-panel-web"

if [ -r "$DIR/serve.py" ]; then
  exec /usr/bin/python3 "$DIR/serve.py" "$@"
fi

if [ -r "$INSTALLED/serve.py" ]; then
  exec /usr/bin/python3 "$INSTALLED/serve.py" "$@"
fi

echo "Cannot read serve.py, either beside this script or at:"
echo "    $INSTALLED"
echo
echo "Run tools/panel_web/install.sh from a shell that can read the repo."
echo
printf "Press Return to close. "
read _
exit 1
