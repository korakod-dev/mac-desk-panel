#!/bin/bash
# Raise a banner on the T-Display-S3 for the two things worth looking up for:
# Claude waiting on an answer, and a long turn finishing while you are away.
#
# Wired to four hooks in settings.json, each handing this script the hook JSON
# on stdin:
#
#   Notification     -> panel-notify.sh alert    "it needs you now"
#   UserPromptSubmit -> panel-notify.sh start    stamps the turn, retracts
#   PostToolUse      -> panel-notify.sh clear    retracts, once you have answered
#   Stop             -> panel-notify.sh finish   posts, if the turn ran long
#
# An alert waits for a button by design, but the thing it was waiting for is you
# — and once you have answered, the button press is pure chore. Both retracting
# hooks exist because there are two ways to answer: typing a prompt fires
# UserPromptSubmit, while approving a permission fires nothing at all until the
# tool it unblocked runs, which is PostToolUse.
#
# The far end is tools/mac_stats_server.py in ~/Desktop/T-Display-S3, which
# holds one message for the panel to poll. Nothing here fails loudly: a banner
# that cannot be delivered is not worth interrupting a turn over.
#
# Short turns stay silent on purpose. A panel that flashes while you are sitting
# there reading the answer is noise, and noise is what gets it unplugged.

set -u

PORT=8789
MIN_SECONDS=60                    # turns shorter than this finish without a word
BANNER_TTL=20                     # how long a "done" banner stays up
STAMP_DIR="${TMPDIR:-/tmp}"

# Which session currently has a banner up, or absent for none. Exists so the
# retracting hooks can answer "is there anything to take down?" without a round
# trip — clear runs after every single tool call, and a curl per tool call to
# learn "no" almost every time is a cost the panel has no business imposing on
# the turn.
ALERT_FLAG="$STAMP_DIR/claude-panel-alert"

payload=$(cat)

field() {
  printf '%s' "$payload" | jq -r "$1" 2>/dev/null
}

post() {
  # $1 message, $2 kind, $3 ttl
  curl -sf -m 2 -X POST "http://127.0.0.1:$PORT/notify" \
       --data-urlencode "msg=$1" -d "kind=$2" -d "ttl=$3" -o /dev/null
}

session() {
  local s
  s=$(field '.session_id // ""')
  [ -n "$s" ] || s=default
  printf '%s' "$s"
}

# Take down a banner this session raised. Scoped to the session on purpose: a
# second Claude in another window answering its own permission prompts must not
# clear a banner that is still waiting on you over here.
retract() {
  [ -f "$ALERT_FLAG" ] || return 0
  [ "$(cat "$ALERT_FLAG" 2>/dev/null)" = "$1" ] || return 0
  rm -f "$ALERT_FLAG"
  post "" info 0
}

case "${1:-}" in
  alert)
    msg=$(field '.message // ""')
    [ -n "$msg" ] || msg="Claude needs your input"
    # ttl 0: an alert stays up until it is acknowledged, which is the whole
    # difference between an alert and a note. What acknowledges it is now
    # usually you answering, not you reaching over to the panel.
    post "$msg" alert 0
    session > "$ALERT_FLAG"
    ;;

  start)
    # A prompt of yours is an answer to whatever the banner was asking, so it
    # comes down on submit rather than at the end of the turn it starts.
    retract "$(session)"
    date +%s > "$STAMP_DIR/claude-turn-$(session)"
    ;;

  clear)
    # Runs after every tool call, so it must cost nothing in the common case:
    # no flag, no jq, no curl, straight out.
    [ -f "$ALERT_FLAG" ] || exit 0
    # A tool that just ran is proof the permission it was waiting on has been
    # granted — there is no hook for the approval itself, and this is the first
    # thing that happens after one.
    retract "$(session)"
    ;;

  finish)
    # An alert still standing at the end of a turn was asking you for something
    # that has since been answered — the turn could not have ended otherwise.
    # Retracting it matters beyond tidiness: an alert waits for a button, and
    # while one is up the panel shows nothing else and stops picking its own
    # pages. Done before the early exits below, since a short turn can answer a
    # question just as well as a long one.
    #
    # start and clear will normally have got there first. This stays as the
    # backstop for the alerts they cannot see: one raised before the flag file
    # existed, or by a session that has since gone away. It asks the server
    # rather than the flag for exactly that reason, and is cheap here because a
    # turn ends once.
    if [ "$(curl -sf -m 2 "http://127.0.0.1:$PORT/notify" | jq -r '.kind // ""' 2>/dev/null)" = alert ]; then
      post "" info 0
    fi
    rm -f "$ALERT_FLAG"

    stamp="$STAMP_DIR/claude-turn-$(session)"

    # No stamp means this Stop did not follow a prompt of yours — a resume, a
    # compact, a session older than this hook. Nothing to measure, so nothing
    # to say.
    [ -f "$stamp" ] || exit 0
    began=$(cat "$stamp")
    rm -f "$stamp"

    elapsed=$(( $(date +%s) - began ))
    [ "$elapsed" -ge "$MIN_SECONDS" ] || exit 0

    if [ "$elapsed" -ge 60 ]; then
      took="$((elapsed / 60))m $((elapsed % 60))s"
    else
      took="${elapsed}s"
    fi

    # Which session finished, for when more than one is running. The directory
    # name is what you would call it out loud.
    where=$(field '.cwd // ""')
    [ -n "$where" ] && where="$(basename "$where"): "

    post "${where}done in $took" info "$BANNER_TTL"
    ;;
esac

exit 0
