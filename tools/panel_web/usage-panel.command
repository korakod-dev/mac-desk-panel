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
# Finder runs a .command from the home directory, not from the folder it is in,
# so the path has to be worked out rather than assumed.

cd "$(dirname "$0")" || exit 1
exec /usr/bin/python3 serve.py "$@"
