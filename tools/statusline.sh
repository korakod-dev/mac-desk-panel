#!/usr/bin/env bash
# Claude Code status line — 3-line HUD
#   line 1:   workspace / model / context window
#   lines 2-3: subscription usage limits, 5h then 7d
# stdin: statusLine JSON

input=$(cat)

eval "$(printf '%s' "$input" | jq -r '
  @sh "CWD=\(.workspace.current_dir // .cwd // "")",
  @sh "MODEL=\(.model.display_name // "")",
  @sh "EFFORT=\(.effort.level // "")",
  @sh "CTX=\(.context_window.used_percentage // -1 | floor)",
  @sh "H5=\(.rate_limits.five_hour.used_percentage // -1 | floor)",
  @sh "H5R=\(.rate_limits.five_hour.resets_at // 0 | floor)",
  @sh "D7=\(.rate_limits.seven_day.used_percentage // -1 | floor)",
  @sh "D7R=\(.rate_limits.seven_day.resets_at // 0 | floor)"
' 2>/dev/null)"

CTX=${CTX:--1}; H5=${H5:--1}; H5R=${H5R:-0}; D7=${D7:--1}; D7R=${D7R:-0}
NOW=$(date +%s)

# ── usage windows ────────────────────────────────────────────────────────
# rate_limits is a snapshot of the headers of the last API response, and Claude
# Code replays it unchanged for the rest of the session — it has no notion of
# the snapshot going out of date. So two corrections are needed:
#   · nothing arrives at all until the session's first response → use the cache
#   · a reading whose reset time has passed describes a window that is over,
#     however it reached us; the window after it starts empty
CACHE="$HOME/.claude/statusline-usage.cache"
H5S=0; D7S=0   # 1 = carried over or inferred rather than freshly reported

int() {  # value, fallback → the value when it is a plain integer, else fallback
  case $1 in
    ''|*[!0-9]*) printf '%s' "$2" ;;
    *)           printf '%s' "$1" ;;
  esac
}

cH5=-1; cH5R=0; cD7=-1; cD7R=0
if [ -r "$CACHE" ]; then
  read -r cH5 cH5R cD7 cD7R <"$CACHE"
  cH5=$(int "$cH5" -1); cH5R=$(int "$cH5R" 0)
  cD7=$(int "$cD7" -1); cD7R=$(int "$cD7R" 0)
fi

# per window, so a payload carrying only one of them keeps the other alive
if [ "$H5" -lt 0 ] && [ "$cH5" -ge 0 ]; then H5=$cH5; H5R=$cH5R; H5S=1; fi
if [ "$D7" -lt 0 ] && [ "$cD7" -ge 0 ]; then D7=$cD7; D7R=$cD7R; D7S=1; fi

# the reset time of the fresh window is unknown until a response reports it
if [ "$H5" -ge 0 ] && [ "$H5R" -gt 0 ] && [ "$H5R" -le "$NOW" ]; then
  H5=0; H5R=0; H5S=1
fi
if [ "$D7" -ge 0 ] && [ "$D7R" -gt 0 ] && [ "$D7R" -le "$NOW" ]; then
  D7=0; D7R=0; D7S=1
fi

if [ "$H5" -ge 0 ] || [ "$D7" -ge 0 ]; then
  printf '%s %s %s %s\n' "$H5" "$H5R" "$D7" "$D7R" >"$CACHE" 2>/dev/null
fi

R=$'\033[0m'; B=$'\033[1m'
c() { printf '\033[38;5;%sm' "$1"; }
SEP="$(c 238)▕${R}"

# ── thermometer ramp: cool → hot across the bar ──────────────────────────
RAMP=(48 84 120 156 191 220 214 208 202 196)

bar() {  # pct, cells
  local p=$1 n=$2 filled i out=""
  filled=$(( (p * n + 50) / 100 ))
  [ "$filled" -gt "$n" ] && filled=$n
  [ "$filled" -lt 1 ] && [ "$p" -gt 0 ] && filled=1
  for ((i = 0; i < n; i++)); do
    if [ "$i" -lt "$filled" ]; then
      out+="$(c "${RAMP[$(( i * 10 / n ))]}")▓"
    else
      out+="$(c 240)░"
    fi
  done
  printf '%s%s' "$out" "$R"
}

pct() {  # pct  → bold, colored by severity, right-aligned
  local p=$1 col
  if   [ "$p" -lt 60 ]; then col=84
  elif [ "$p" -lt 85 ]; then col=220
  else col=203; fi
  printf '%s%s%3s%%%s' "$B" "$(c $col)" "$p" "$R"
}

at() {  # epoch, format → local time; -r is BSD, -d is GNU, so try both
  date -r "$1" "+$2" 2>/dev/null || date -d "@$1" "+$2" 2>/dev/null
}

clock() {  # epoch → HH:MM, or Ddd HH:MM once it is more than a day out
  local ts=$1
  [ "${ts:-0}" -le 0 ] 2>/dev/null && return
  if [ "$ts" -lt $(( NOW + 86400 )) ]; then at "$ts" "%H:%M"
  else at "$ts" "%a %H:%M"; fi
}

countdown() {  # epoch → "2d 5h" / "2h 13m" / "13m"
  local d=$(( $1 - NOW )) dd hh mm
  [ "$d" -le 0 ] && return
  dd=$(( d / 86400 )); hh=$(( (d % 86400) / 3600 )); mm=$(( (d % 3600) / 60 ))
  if   [ "$dd" -gt 0 ]; then printf '%dd %dh' "$dd" "$hh"
  elif [ "$hh" -gt 0 ]; then printf '%dh %02dm' "$hh" "$mm"
  else printf '%dm' "$mm"; fi
}

waitcol() {  # pct, resets_at, window_seconds → color for the time remaining
  # How long is left only costs anything once the quota is running out — at 5%
  # used the timer is trivia. And in the last tenth of a window the reset is
  # near enough to be relief rather than a warning, however full the window is.
  local p=$1 ts=$2 win=$3
  if [ "$win" -gt 0 ] && [ $(( (ts - NOW) * 10 )) -le "$win" ]; then printf 48; return; fi
  if   [ "$p" -lt 60 ]; then printf 245
  elif [ "$p" -lt 85 ]; then printf 220
  else printf 203; fi
}

gauge() {  # label, pct, resets_at, cells, stale, window_seconds
  local label=$1 p=$2 ts=$3 n=$4 st=$5 win=$6 t cd i
  if [ "$p" -lt 0 ]; then
    printf '%s%s %s' "$(c 240)" "$label" "$(c 240)"
    for ((i = 0; i < n; i++)); do printf '░'; done
    printf '%s %s ──%%  %swaiting for first response%s' "$R" "$(c 240)" "$(c 236)" "$R"
    return
  fi
  printf '%s%s%s %s ' "$(c 245)" "$label" "$R" "$(bar "$p" "$n")"
  # a tilde marks a reading no response has confirmed yet
  [ "$st" = 1 ] && printf '%s~%s' "$(c 240)" "$R"
  printf '%s' "$(pct "$p")"
  # the wall-clock reset stays reference material; the time left is the number
  # decisions get made on, so it carries the emphasis
  t=$(clock "$ts"); cd=$(countdown "$ts")
  [ -n "$t" ] && printf '%s ⟲ %s%s' "$(c 244)" "$t" "$R"
  [ -n "$cd" ] && printf '%s · %s%s%s%s' \
    "$(c 240)" "$B" "$(c "$(waitcol "$p" "$ts" "$win")")" "$cd" "$R"
}

join() {  # emit "$@" separated by SEP
  local out="" s
  for s in "$@"; do
    [ -n "$out" ] && out+=" $SEP "
    out+="$s"
  done
  printf '%s' "$out"
}

# ── line 1: where / what ─────────────────────────────────────────────────
case "$CWD" in
  "$HOME")   dir="~" ;;
  "$HOME"/*) dir="~/${CWD#"$HOME"/}" ;;
  *)         dir="$CWD" ;;
esac
case "$dir" in
  */*/*) dir="…/$(basename "$(dirname "$dir")")/$(basename "$dir")" ;;
esac

BRANCH=$(git -C "${CWD:-.}" branch --show-current 2>/dev/null)

top=()
[ -n "$dir" ] && top+=("$(c 45)◈${R} $(c 81)${dir}${R}")
[ -n "$BRANCH" ] && top+=("$(c 176)⑂ ${BRANCH}${R}")
if [ -n "$MODEL" ]; then
  m="$(c 141)${MODEL}${R}"
  [ -n "$EFFORT" ] && m+="$(c 240)·${EFFORT}${R}"
  top+=("$m")
fi
[ "$CTX" -ge 0 ] && top+=("$(c 245)CTX${R} $(bar "$CTX" 14) $(pct "$CTX")")

# ── lines 2 & 3: usage limits, one window per line ───────────────────────
[ ${#top[@]} -gt 0 ] && printf '%s\n' "$(join "${top[@]}")"
printf '%s' "$(gauge "5H" "$H5" "$H5R" 22 "$H5S" 18000)"
# an `x && printf` here would leave the script exiting 1 whenever the 7d window
# is simply absent, reporting a normal render as a failed one
if [ "$D7" -ge 0 ]; then
  printf '\n%s' "$(gauge "7D" "$D7" "$D7R" 22 "$D7S" 604800)"
fi
