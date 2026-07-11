#!/usr/bin/env python3
"""Calendar transition detection for checkin_pre.sh.

Reads a lark_freebusy JSON response on stdin and prints the transition
context: 'BUSY' (skip this check-in) or newline-joined signal lines.
Extracted from the former inline heredoc so the long-event filter below
is unit-testable.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

# freebusy blocks carry only start/end/rsvp_status — no all-day flag — so
# duration is the only way to tell a trip/all-day marker from a meeting.
# A 17-day Iceland event satisfied start<=now<end continuously and muted
# every check-in 6/25–7/8; no genuine meeting spans 20h.
LONG_EVENT_HOURS = 20


def transition_context(items, now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    signals = []

    just_ended = None       # meeting that ended in last 15 min
    next_event = None       # next upcoming event

    for item in items:
        start = datetime.fromisoformat(item['start_time'])
        end = datetime.fromisoformat(item['end_time'])

        # Trip/all-day markers must not count as BUSY — and must not feed
        # next_event either, or a future long event starting within 2h
        # would re-trigger the tight-window BUSY branch below.
        if end - start >= timedelta(hours=LONG_EVENT_HOURS):
            if start <= now < end:
                signals.append(f"multi_day_event: until {item['end_time']} (not treated as busy)")
            continue

        # Currently in a meeting → skip checkin
        if start <= now < end:
            return 'BUSY'

        # Meeting ended in the last 15 min → transition moment!
        if end <= now and (now - end) < timedelta(minutes=15):
            just_ended = item
            signals.append(f'transition: meeting ended {int((now - end).total_seconds() / 60)}m ago')

        # Next upcoming event
        if start > now and (next_event is None or start < datetime.fromisoformat(next_event['start_time'])):
            next_event = item

    if next_event:
        next_start = datetime.fromisoformat(next_event['start_time'])
        gap_min = int((next_start - now).total_seconds() / 60)
        signals.append(f'next_event_in: {gap_min}m')
        if gap_min < 20:
            signals.append('tight_window: true (less than 20m, maybe skip)')
        elif gap_min > 90:
            signals.append(f'large_free_block: {gap_min}m available')
    else:
        signals.append('no_upcoming_events: rest of day is clear')

    if just_ended:
        signals.append('best_moment: post-meeting transition')
    elif next_event and int((datetime.fromisoformat(next_event['start_time']) - now).total_seconds() / 60) < 20:
        # Too close to next meeting — bad time to interrupt
        return 'BUSY'

    return '\n'.join(signals)


def main(now=None):
    try:
        data = json.load(sys.stdin)
        items = data.get('data') or []
        print(transition_context(items, now=now))
    except Exception as e:
        print(f'calendar_error: {e}')


if __name__ == '__main__':
    main()
