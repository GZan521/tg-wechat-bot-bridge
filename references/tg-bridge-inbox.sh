#!/usr/bin/env bash
# Telegram bridge watch (v2, hook-driven polling).
#
# Borrowed from wechat-ilink-muse-bridge's production hook design:
# each run of this script IS one getUpdates long poll (server holds
# up to 25s). There is no resident bridge process to die. Poll is
# two-phase: tg_poll.py poll only fetches; this script queues the
# batch atomically into tg-pending.json and only then commits
# (seen ids first, then the offset). Any crash re-fetches the batch:
# at-least-once, never silently lost.
#
# Legacy compat: while the old daemon bridge.py is still running,
# this script falls back to the v1 behavior (watch inbox.jsonl line
# count, wake new_messages). Once the daemon is stopped, poll mode
# takes over automatically.
#
# Alert closed loop (alert-state.json in the bridge dir):
#   - token invalid/missing: wake once, then at most hourly
#   - poll errors: wake only after 10 min continuous, then hourly
#   - recovery after an alerted outage: wake once with "recovered"
#   - worker processing stuck >10 min: batch returns to pending,
#     the next pending wake carries stuck_alert (hourly at most)
# Liveness is a heartbeat file touched every run, not pgrep patterns
# (the v1 full-path pgrep mismatch wedged the down flag on
# 2026-11-17 and silently swallowed group messages).
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

BRIDGE_DIR="$HOME/workspace/telegram-bridge"
CLIENT="$BRIDGE_DIR/tg_poll.py"
STATE_DIR="$HOME/hooks/state"
PENDING="$STATE_DIR/tg-pending.json"
PROC="$STATE_DIR/tg-processing.json"
COMMIT_PENDING="$STATE_DIR/tg-commit-pending.json"
HEARTBEAT="$STATE_DIR/tg-bridge-heartbeat"
ALERT="$BRIDGE_DIR/alert-state.json"
POLLLOG="$BRIDGE_DIR/poll.log"
LASTWAKE_F="$STATE_DIR/tg-last-wake"
DRY=${HATCH_HOOK_DRY_RUN:-0}
NOW=$(date +%s)
mkdir -p "$STATE_DIR"

# Wake throttling: a woken worker needs ~1 min to boot and claim the
# queue. Re-waking every 10 s while a batch sits unclaimed floods the
# worker queue with stale runs that then drain one by one (this
# wedged the first two poll-mode test messages on 2026-11-17).
# Fresh batches always wake immediately; pending reminders wait.
mark_wake() { [ "$DRY" != "1" ] && echo "$NOW" > "$LASTWAKE_F" || true; }
last_wake_age() {
  local lw=0
  [ -f "$LASTWAKE_F" ] && lw=$(cat "$LASTWAKE_F" 2>/dev/null || echo 0)
  case "$lw" in ''|*[!0-9]*) lw=0 ;; esac
  echo $((NOW - lw))
}

# Single-instance lock: overlapping runs exit quietly.
exec 9>"$STATE_DIR/tg-watch.lock"
if ! flock -n 9; then
  exit 0
fi

# Heartbeat: every run that got the lock proves the watch is alive.
if [ "$DRY" != "1" ]; then
  touch "$HEARTBEAT"
fi

now_str() { date '+%Y-%m-%d %H:%M:%S'; }

trim_poll_log() {
  [ "$DRY" = "1" ] && return 0
  [ -f "$POLLLOG" ] || return 0
  local n
  n=$(wc -l < "$POLLLOG" 2>/dev/null || echo 0)
  if [ "$n" -gt 500 ] 2>/dev/null; then
    tail -n 500 "$POLLLOG" > "$POLLLOG.trim.$$" && mv "$POLLLOG.trim.$$" "$POLLLOG"
  fi
}

log_line() {
  [ "$DRY" = "1" ] && return 0
  echo "[$(now_str)] $1" >> "$POLLLOG"
  trim_poll_log
}

# ---- legacy daemon mode -------------------------------------------------
if pgrep -f "python3 bridge\.py" >/dev/null 2>&1; then
  INBOX="$BRIDGE_DIR/inbox.jsonl"
  SEEN_F="$STATE_DIR/tg-bridge-seen.txt"
  DOWN_F="$STATE_DIR/tg-bridge-down.flag"
  if [ -f "$DOWN_F" ] && [ "$DRY" != "1" ]; then
    rm -f "$DOWN_F"
  fi
  total=0
  [ -f "$INBOX" ] && total=$(wc -l < "$INBOX" | tr -d ' ')
  seen=0
  [ -f "$SEEN_F" ] && seen=$(cat "$SEEN_F")
  if [ "$total" -gt "$seen" ]; then
    if [ "$DRY" != "1" ]; then
      echo "$total" > "$SEEN_F"
    fi
    wake "new_messages" "{\"new_count\":$((total - seen)),\"from_line\":$((seen + 1)),\"to_line\":${total}}"
  else
    silent "legacy daemon mode, no new telegram messages" "{\"total\":${total}}"
  fi
  exit 0
fi

# ---- alert state ---------------------------------------------------------
astate() { jq -r ".$1 // 0" "$ALERT" 2>/dev/null || echo 0; }
aset() {
  [ "$DRY" = "1" ] && return 0
  local tmp="$ALERT.tmp.$$"
  if [ -f "$ALERT" ]; then
    jq --argjson v "$2" ".$1 = \$v" "$ALERT" > "$tmp" 2>/dev/null || echo '{}' > "$tmp"
  else
    jq -n --argjson v "$2" "{\"$1\": \$v}" > "$tmp" 2>/dev/null || echo '{}' > "$tmp"
  fi
  mv "$tmp" "$ALERT" 2>/dev/null || true
}

# ---- queue helpers -------------------------------------------------------
# Count msgs in a queue file. A file that exists but is not a valid
# {"msgs":[...]} object is quarantined, never treated as an empty
# queue (treating it as empty used to stall ilink-style queues).
queue_count() {
  local path=$1 n iso
  [ -s "$path" ] || { echo 0; return; }
  if ! jq -e 'type == "object" and (.msgs | type == "array")' "$path" >/dev/null 2>&1; then
    iso="$path.corrupt-$(date +%s)-$$"
    if mv "$path" "$iso" 2>/dev/null; then
      log_line "QUEUE_CORRUPT: $path quarantined as $iso"
      log "QUEUE_CORRUPT: quarantined $path as $iso"
    fi
    echo 0
    return
  fi
  n=$(jq '[.msgs[]?] | length' "$path" 2>/dev/null || echo 0)
  echo "${n:-0}"
}

# Retry a commit that failed last round. While it keeps failing we
# do not poll: an uncommitted batch must not overlap a fresh fetch.
flush_commit_pending() {
  [ -f "$COMMIT_PENDING" ] || return 0
  [ "$DRY" = "1" ] && return 0
  local iso
  if ! jq -e 'type == "object"' "$COMMIT_PENDING" >/dev/null 2>&1; then
    iso="$COMMIT_PENDING.corrupt-$(date +%s)"
    mv "$COMMIT_PENDING" "$iso" 2>/dev/null || rm -f "$COMMIT_PENDING"
    log_line "COMMIT_PENDING_CORRUPT: quarantined as $iso; batch will be re-fetched"
    return 0
  fi
  if python3 "$CLIENT" commit < "$COMMIT_PENDING" >/dev/null 2>&1; then
    rm -f "$COMMIT_PENDING"
    log "retried pending commit OK"
    return 0
  fi
  log_line "COMMIT_RETRY_FAILED: poll skipped this round"
  return 1
}

# ---- crash recovery: stale processing goes back to pending ---------------
pending_msgs=$(queue_count "$PENDING")
if [ -f "$PROC" ] && [ "${pending_msgs:-0}" -eq 0 ] && \
   [ -n "$(find "$PROC" -cmin +10 2>/dev/null || true)" ]; then
  # ctime, not mtime: mv-claiming refreshes ctime, so a batch that
  # merely waited >10 min in pending is not mistaken for stuck.
  mv "$PROC" "$PENDING"
  log "recovered stale processing file to pending"
  if [ "$(astate stuck_since)" -eq 0 ]; then
    aset stuck_since $((NOW - 600))
  fi
  pending_msgs=$(queue_count "$PENDING")
fi

# An empty pending file must not block polling forever; self-heal.
if [ -f "$PENDING" ] && [ "${pending_msgs:-0}" -eq 0 ]; then
  if [ "$DRY" != "1" ]; then rm -f "$PENDING"; fi
  pending_msgs=0
fi

# ---- pending backlog: wake, do not poll ---------------------------------
if [ "${pending_msgs:-0}" -gt 0 ] 2>/dev/null; then
  extra=""
  stuck_since=$(astate stuck_since)
  stuck_last=$(astate stuck_last)
  if [ "$stuck_since" -gt 0 ] && [ $((NOW - stuck_since)) -ge 600 ] && \
     [ $((NOW - stuck_last)) -ge 3600 ]; then
    aset stuck_last "$NOW"
    extra=",\"stuck_alert\":true,\"stuck_minutes\":$(( (NOW - stuck_since) / 60 ))"
  fi
  wage=$(last_wake_age)
  if [ "$wage" -lt 120 ]; then
    silent "pending already woken ${wage}s ago, waiting for worker" '{}'
    exit 0
  fi
  mark_wake
  wake "pending_telegram_messages" "{\"count\":${pending_msgs},\"pending_file\":\"$PENDING\"$extra}"
  exit 0
fi

# Queue drained: close out any stuck bookkeeping.
if [ ! -f "$PROC" ] && [ "$(astate stuck_since)" -gt 0 ]; then
  aset stuck_since 0
  aset stuck_last 0
fi

if ! flush_commit_pending; then
  silent "commit retry pending, poll skipped" '{}'
  exit 0
fi

# ---- one long poll --------------------------------------------------------
out=$(timeout 60 python3 "$CLIENT" poll 2>/dev/null || true)

if [[ "$out" == TOKEN_INVALID* ]] || [[ "$out" == NO_TOKEN* ]]; then
  reason="token_invalid"
  [[ "$out" == NO_TOKEN* ]] && reason="token_missing"
  exp_since=$(astate exp_since)
  exp_last=$(astate exp_last)
  if [ "$exp_since" -eq 0 ]; then
    aset exp_since "$NOW"
    aset exp_last "$NOW"
    if [ "$DRY" != "1" ]; then
      wake "$reason" '{}'
    else
      silent "dry-run: would wake $reason"
    fi
  elif [ $((NOW - exp_last)) -ge 3600 ]; then
    aset exp_last "$NOW"
    if [ "$DRY" != "1" ]; then
      wake "$reason" "{\"repeat\":true,\"hours\":$(( (NOW - exp_since) / 3600 ))}"
    else
      silent "dry-run: would repeat $reason alert"
    fi
  else
    silent "token problem, repeat alert throttled" '{}'
  fi
elif [[ "$out" == POLL_ERROR* ]] || \
     ! printf '%s' "$out" | jq -e 'type == "object" and has("msgs")' >/dev/null 2>&1; then
  # Poll error, or the client crashed (empty/invalid output). A crash
  # must enter the alert machine, never read as "no new messages".
  log_line "POLL_FAIL: $(printf '%s' "${out:-POLL_CRASH:empty-or-invalid-output}" | head -c 200)"
  err_since=$(astate err_since)
  if [ "$err_since" -eq 0 ]; then
    aset err_since "$NOW"
    err_since=$NOW
  fi
  err_last=$(astate err_last)
  err_dur=$((NOW - err_since))
  if [ "$err_dur" -ge 600 ] && \
     { [ "$err_last" -eq 0 ] || [ $((NOW - err_last)) -ge 3600 ]; }; then
    aset err_last "$NOW"
    if [ "$DRY" != "1" ]; then
      wake "poll_failing" "{\"minutes\":$((err_dur / 60))}"
    else
      silent "dry-run: would wake poll_failing"
    fi
  else
    silent "poll error" '{}'
  fi
else
  # Poll succeeded: recovery bookkeeping first.
  recovered=""
  exp_since=$(astate exp_since)
  err_since=$(astate err_since)
  if [ "$exp_since" -gt 0 ]; then
    recovered="{\"kind\":\"token\",\"minutes\":$(( (NOW - exp_since) / 60 ))}"
    aset exp_since 0
    aset exp_last 0
  elif [ "$err_since" -gt 0 ]; then
    if [ "$(astate err_last)" -gt 0 ]; then
      recovered="{\"kind\":\"poll_error\",\"minutes\":$(( (NOW - err_since) / 60 ))}"
    fi
    aset err_since 0
    aset err_last 0
  fi
  cnt=$(printf '%s' "$out" | jq '[.msgs[]?] | length' 2>/dev/null || echo 0)
  commit_json=$(printf '%s' "$out" | jq -c '.commit // empty' 2>/dev/null || true)
  do_commit() {
    if [ "$DRY" != "1" ] && [ -n "$commit_json" ]; then
      local ctmp="$COMMIT_PENDING.tmp.$$"
      printf '%s' "$commit_json" > "$ctmp" && mv "$ctmp" "$COMMIT_PENDING"
      if python3 "$CLIENT" commit < "$COMMIT_PENDING" >/dev/null 2>&1; then
        rm -f "$COMMIT_PENDING"
      else
        log_line "COMMIT_FAILED: payload kept in $COMMIT_PENDING, retried next round"
      fi
    fi
  }
  if [ "${cnt:-0}" -gt 0 ] 2>/dev/null; then
    if [ "$DRY" = "1" ]; then
      silent "dry-run: $cnt messages fetched, left uncommitted for live poll" '{}'
    else
      tmp="$PENDING.tmp.$$"
      printf '%s' "$out" | jq -c '{msgs}' > "$tmp"
      mv "$tmp" "$PENDING"
      log "received $cnt messages"
      # Light the typing indicator the moment the batch lands; the
      # worker's own typing call later is just a refresh.
      first_chat=$(printf '%s' "$out" | jq -r '.msgs[0].chat_id // empty' 2>/dev/null || true)
      if [ -n "$first_chat" ]; then
        python3 "$CLIENT" typing "$first_chat" >/dev/null 2>&1 || true
      fi
      do_commit
      rec_extra=""
      [ -n "$recovered" ] && rec_extra=",\"recovered\":$recovered"
      mark_wake
      wake "new_telegram_messages" "{\"count\":$cnt,\"pending_file\":\"$PENDING\"$rec_extra}"
    fi
  elif [ -n "$recovered" ]; then
    do_commit
    if [ "$DRY" != "1" ]; then
      wake "recovered" "$recovered"
    else
      silent "dry-run: would wake recovered"
    fi
  else
    do_commit
    silent "no new telegram messages" '{}'
  fi
fi
