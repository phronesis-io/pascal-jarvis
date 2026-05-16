#!/usr/bin/env bash
# Calendar write-back helper — create, update, or delete Lark calendar events.
# Used by the main conversation Claude when Pascal confirms a schedule change.
#
# Usage:
#   calendar_write.sh create --summary "会议" --start "2026-05-14T10:00:00+08:00" --end "2026-05-14T11:00:00+08:00"
#   calendar_write.sh update --event-id "abc_0" --calendar-id "feishu.cn_xxx" --summary "新标题" --start ... --end ...
#   calendar_write.sh delete --event-id "abc_0" --calendar-id "feishu.cn_xxx"
#   calendar_write.sh lookup --summary "康复训练" --date "2026-05-14"
#
# The 'lookup' command searches calendar_event_mapping.json to find event_id by summary + date.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MAPPING_FILE="$JARVIS_DIR/calendar_event_mapping.json"

action="$1"
shift

case "$action" in
  create)
    summary="" start="" end="" desc="" attendees=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --summary) summary="$2"; shift 2 ;;
        --start) start="$2"; shift 2 ;;
        --end) end="$2"; shift 2 ;;
        --description) desc="$2"; shift 2 ;;
        --attendees) attendees="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    [ -z "$summary" ] || [ -z "$start" ] || [ -z "$end" ] && { echo "ERROR: --summary, --start, --end required"; exit 1; }
    args=(lark-cli calendar +create --as user --summary "$summary" --start "$start" --end "$end")
    [ -n "$desc" ] && args+=(--description "$desc")
    [ -n "$attendees" ] && args+=(--attendee-ids "$attendees")
    "${args[@]}" 2>&1
    ;;

  update)
    event_id="" calendar_id="" data_json="{}"
    while [ $# -gt 0 ]; do
      case "$1" in
        --event-id) event_id="$2"; shift 2 ;;
        --calendar-id) calendar_id="$2"; shift 2 ;;
        --summary) data_json=$(echo "$data_json" | python3 -c "import json,sys; d=json.load(sys.stdin); d['summary']='$2'; print(json.dumps(d))"); shift 2 ;;
        --start) data_json=$(echo "$data_json" | python3 -c "import json,sys; d=json.load(sys.stdin); d['start_time']={'datetime':'$2','timezone':'Asia/Shanghai'}; print(json.dumps(d))"); shift 2 ;;
        --end) data_json=$(echo "$data_json" | python3 -c "import json,sys; d=json.load(sys.stdin); d['end_time']={'datetime':'$2','timezone':'Asia/Shanghai'}; print(json.dumps(d))"); shift 2 ;;
        --description) data_json=$(echo "$data_json" | python3 -c "import json,sys; d=json.load(sys.stdin); d['description']='$2'; print(json.dumps(d))"); shift 2 ;;
        *) shift ;;
      esac
    done
    [ -z "$event_id" ] || [ -z "$calendar_id" ] && { echo "ERROR: --event-id and --calendar-id required"; exit 1; }
    lark-cli calendar events patch \
      --as user \
      --params "{\"calendar_id\":\"$calendar_id\",\"event_id\":\"$event_id\"}" \
      --data "$data_json" 2>&1
    ;;

  delete)
    event_id="" calendar_id=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --event-id) event_id="$2"; shift 2 ;;
        --calendar-id) calendar_id="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    [ -z "$event_id" ] || [ -z "$calendar_id" ] && { echo "ERROR: --event-id and --calendar-id required"; exit 1; }
    lark-cli calendar events delete \
      --as user \
      --params "{\"calendar_id\":\"$calendar_id\",\"event_id\":\"$event_id\"}" 2>&1
    ;;

  lookup)
    summary="" date=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --summary) summary="$2"; shift 2 ;;
        --date) date="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    [ ! -f "$MAPPING_FILE" ] && { echo "ERROR: no event mapping file"; exit 1; }
    python3 -c "
import json, sys
mapping = json.load(open('$MAPPING_FILE'))
results = [e for e in mapping
           if ('$summary' == '' or '$summary'.lower() in e['summary'].lower())
           and ('$date' == '' or e['date'] == '$date')]
if results:
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
else:
    print('No matching events found')
" 2>/dev/null
    ;;

  *)
    echo "Usage: calendar_write.sh {create|update|delete|lookup} [options]"
    echo ""
    echo "Commands:"
    echo "  create  --summary <title> --start <ISO8601> --end <ISO8601> [--description <text>]"
    echo "  update  --event-id <id> --calendar-id <id> [--summary <title>] [--start <ISO>] [--end <ISO>]"
    echo "  delete  --event-id <id> --calendar-id <id>"
    echo "  lookup  [--summary <keyword>] [--date <YYYY-MM-DD>]"
    exit 1
    ;;
esac
