#!/usr/bin/env bash

# Process-group identity and marker helpers shared by bot.sh and its executable
# regressions.  macOS Bash 3.2 has no BASHPID, so the dispatcher publishes `$!`
# plus the kernel-reported start time and gives every handler its own group.

process_start_token() {
  local pid="${1:-}" start
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  start=$(ps -o lstart= -p "$pid" 2>/dev/null) || return 1
  start=$(printf '%s' "$start" \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  [ -n "$start" ] || return 1
  printf '%s\n' "$start"
}

process_group_id() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' '
}

process_identity_matches() {
  local pid="${1:-}" token="${2:-}" actual
  [ -n "$token" ] || return 1
  actual=$(process_start_token "$pid") || return 1
  [ "$actual" = "$token" ]
}

process_is_descendant() {
  local candidate="${1:-}" root="${2:-}" current parent
  [[ "$candidate" =~ ^[0-9]+$ ]] || return 1
  [[ "$root" =~ ^[0-9]+$ ]] || return 1
  [ "$candidate" != "$root" ] || return 1
  kill -0 "$candidate" 2>/dev/null || return 1

  current="$candidate"
  while [ "$current" -gt 1 ] 2>/dev/null; do
    parent=$(ps -o ppid= -p "$current" 2>/dev/null | tr -d ' ')
    [ -n "$parent" ] || return 1
    [ "$parent" = "$root" ] && return 0
    current="$parent"
  done
  return 1
}

process_is_descendant_of_identity() {
  local candidate="${1:-}" ancestor="${2:-}" token="${3:-}"
  process_identity_matches "$ancestor" "$token" || return 1
  process_is_descendant "$candidate" "$ancestor"
}

process_group_has_members() {
  local pgid="${1:-}"
  [[ "$pgid" =~ ^[0-9]+$ ]] || return 1
  ps -axo pgid=,state= 2>/dev/null \
    | awk -v expected="$pgid" \
      '$1 == expected && $2 !~ /^Z/ { found = 1; exit } END { exit !found }'
}

process_group_is_owned() {
  local leader="${1:-}" token="${2:-}" root="${3:-}" pgid
  process_identity_matches "$leader" "$token" || return 1
  pgid=$(process_group_id "$leader") || return 1
  [ "$pgid" = "$leader" ] || return 1
  if [ -n "$root" ]; then
    process_is_descendant "$leader" "$root" || return 1
  fi
}

terminate_owned_process_group() {
  local leader="${1:-}" token="${2:-}" root="${3:-}"
  process_group_is_owned "$leader" "$token" "$root" || return 1
  # Give wrappers a brief TERM window first.  Codex/GPT wrappers use their own
  # SIGTERM handlers to reap detached tool sessions that are outside this PGID.
  kill -TERM -- "-$leader" 2>/dev/null || return 1
  local count=0
  while process_group_has_members "$leader" && [ "$count" -lt 100 ]; do
    sleep 0.02
    count=$((count + 1))
  done
  process_group_has_members "$leader" || return 0
  # Freeze before the hard stop: nothing can fork between membership check and
  # KILL.  If the leader has exited, the PGID cannot be reused while surviving
  # members still hold it; the identity check before TERM remains the trust
  # anchor for the group.
  kill -STOP -- "-$leader" 2>/dev/null || return 1
  process_group_has_members "$leader" || return 0
  kill -KILL -- "-$leader" 2>/dev/null || true
  return 0
}

terminate_member_process_group() {
  local member="${1:-}" root="${2:-}" leader token
  process_is_descendant "$member" "$root" || return 1
  leader=$(process_group_id "$member") || return 1
  [ "$leader" != "$root" ] || return 1
  process_is_descendant "$leader" "$root" || return 1
  token=$(process_start_token "$leader") || return 1
  terminate_owned_process_group "$leader" "$token" "$root"
}

dispatch_marker_publish() {
  local marker="${1:-}" pid="${2:-}" token="${3:-}" tmp
  [ -n "$marker" ] || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [ -n "$token" ] || return 1
  tmp="${marker}.tmp.$$.$RANDOM"
  (umask 077; printf '%s\t%s\n' "$pid" "$token" > "$tmp") || return 1
  mv -f -- "$tmp" "$marker"
}

dispatch_marker_pid() {
  local record pid token
  record=$(dispatch_marker_record "${1:-}") || return 1
  IFS=$'\t' read -r pid token <<< "$record"
  printf '%s' "$pid"
}

dispatch_marker_token() {
  local record pid token
  record=$(dispatch_marker_record "${1:-}") || return 1
  IFS=$'\t' read -r pid token <<< "$record"
  printf '%s' "$token"
}

dispatch_marker_record() {
  local marker="${1:-}"
  [ -f "$marker" ] || return 1
  awk -F '\t' 'NR == 1 && NF >= 2 { print $1 "\t" $2; exit }' "$marker" 2>/dev/null
}

dispatch_marker_is_owned() {
  local marker="${1:-}" expected_pid="${2:-}" expected_token="${3:-}" record
  record=$(dispatch_marker_record "$marker") || return 1
  [ "$record" = "${expected_pid}"$'\t'"${expected_token}" ]
}

dispatch_marker_wait_owned() {
  local marker="${1:-}" pid="${2:-}" token="${3:-}" attempts="${4:-100}"
  local count=0
  while [ "$count" -lt "$attempts" ]; do
    dispatch_marker_is_owned "$marker" "$pid" "$token" && return 0
    sleep 0.01
    count=$((count + 1))
  done
  return 1
}

dispatch_marker_remove_owned() {
  local marker="${1:-}" pid="${2:-}" token="${3:-}"
  dispatch_marker_is_owned "$marker" "$pid" "$token" || return 0
  rm -f -- "$marker"
}

dispatch_marker_handoff_owned() {
  local old="${1:-}" new="${2:-}" sidecar="${3:-}"
  local pid="${4:-}" token="${5:-}" tmp
  dispatch_marker_is_owned "$old" "$pid" "$token" || return 1
  dispatch_marker_publish "$new" "$pid" "$token" || return 1
  tmp="${sidecar}.tmp.$$.$RANDOM"
  if ! (umask 077; printf '%s' "$new" > "$tmp") || ! mv -f -- "$tmp" "$sidecar"; then
    rm -f -- "$tmp"
    dispatch_marker_remove_owned "$new" "$pid" "$token"
    return 1
  fi
  # Removing the old marker is deliberately last.  At every interruption point
  # at least one discoverable marker survives, and duplicate owned markers are
  # harmless because cleanup removes every marker for this identity.
  dispatch_marker_remove_owned "$old" "$pid" "$token"
}

dispatch_markers_remove_owned() {
  local directory="${1:-}" pid="${2:-}" token="${3:-}" marker
  [ -d "$directory" ] || return 0
  for marker in "$directory"/.dispatch_*; do
    [ -f "$marker" ] || continue
    dispatch_marker_remove_owned "$marker" "$pid" "$token"
  done
}

terminate_registered_group() {
  local marker="${1:-}" root="${2:-}" record pid token
  record=$(dispatch_marker_record "$marker") || return 1
  IFS=$'\t' read -r pid token <<< "$record"
  terminate_owned_process_group "$pid" "$token" "$root"
}

session_lock_publish() {
  local path="${1:-}" pid="${2:-}" owner="${3:-}" start
  start=$(process_start_token "$pid") || return 1
  [ -n "$owner" ] || return 1
  printf '%s\t%s\t%s\n' "$pid" "$start" "$owner" > "$path"
}

session_lock_identity() {
  local path="${1:-}" line pid start owner
  line=$(awk -F '\t' 'NR == 1 && NF >= 3 { print $1 "\t" $2 "\t" $3; exit }' "$path" 2>/dev/null) \
    || return 1
  IFS=$'\t' read -r pid start owner <<< "$line"
  [ -n "$owner" ] || return 1
  process_identity_matches "$pid" "$start" || return 1
  printf '%s\t%s' "$pid" "$start"
}

session_lock_identity_for_handler() {
  local path="${1:-}" handler_pid="${2:-}" handler_start="${3:-}"
  local line pid start owner expected_prefix
  process_identity_matches "$handler_pid" "$handler_start" || return 1
  line=$(awk -F '\t' 'NR == 1 && NF >= 3 { print $1 "\t" $2 "\t" $3; exit }' "$path" 2>/dev/null) \
    || return 1
  IFS=$'\t' read -r pid start owner <<< "$line"
  expected_prefix="${handler_pid}|${handler_start}|"
  case "$owner" in
    "$expected_prefix"*) ;;
    *) return 1 ;;
  esac
  process_identity_matches "$pid" "$start" || return 1
  process_is_descendant_of_identity "$pid" "$handler_pid" "$handler_start" || return 1
  printf '%s\t%s' "$pid" "$start"
}
