# Jarvis Heartbeat

Tasks are checked every 10s. Each task runs only when its interval has elapsed.
All due tasks are batched into a single Claude call.
If nothing needs attention, reply HEARTBEAT_OK — no message is sent.

## Tasks

### eigenflux-feed-triage
- interval: 10m
- pre: tasks/eigenflux_feed_pre.sh
- post: tasks/eigenflux_feed_post.py
- prompt: |
    [EIGENFLUX FEED TRIAGE]
    You are the user's personal assistant. Do NOT just relay information. Your job is to THINK first, then give ACTION RECOMMENDATIONS.
    Before composing your response:
    1. Check your memory of the user: their portfolio, projects, goals, contacts, calendar
    2. For each item, ask: does this affect their holdings? projects? goals? network?
    3. Only push items where you have a concrete action recommendation
    For each item, assign:
    - score: -1 (spam), 0 (neutral), 1 (valuable), 2 (high-value)
    - action: "push" (has action recommendation), "hold" (retry next cycle), "discard" (never show)
    For "push" items, write a user_message that leads with the ACTION RECOMMENDATION, not the information.
    Return JSON: {"feedback":[{"item_id":"<id>","score":<int>,"action":"<push|hold|discard>","reason":"<brief>"}],"user_message":"<markdown or empty>"}

### eigenflux-messages
- interval: 10m
- pre: tasks/eigenflux_messages_pre.sh
- prompt: |
    [EIGENFLUX MESSAGES]
    New private messages received on EigenFlux. Review in context of our conversation
    and suggest how to respond. If nothing actionable, say so briefly.

### checkin
- interval: 30m
- pre: tasks/checkin_pre.sh
- prompt: |
    [CHECKIN — User has free time]
    The user has free time right now.
    Based on your memory of them — their interests, goals, current projects, and recent conversations:
    1. Ask ONE short casual question to understand their current mood/energy
    2. Suggest 2-3 lightweight but meaningful activities they could do right now
    3. Keep it warm and casual, like a friend checking in
    Reply in the user's language. Keep it under 100 words.

### memory-consolidate
- interval: 24h
- pre: tasks/memory_consolidate_pre.sh
- post: tasks/memory_consolidate_post.py
- prompt: |
    [DAILY MEMORY CONSOLIDATION]
    Review the memory files and today's context below. Identify new learnings to persist.
    For each update needed, output a line in this exact format:
    → UPDATE: <filename>.md: <what to add or change>
    Then output a brief diary summary of what changed today.
    If nothing new, reply: HEARTBEAT_OK

### memory-hourly
- interval: 1h
- pre: tasks/memory_hourly_pre.sh
- post: tasks/memory_hourly_post.py
- prompt: |
    [HOURLY INDEX]
    Write a brief INDEX of the last hour's conversation. This is NOT a summary — it's a lookup key.
    Format: 1-3 short lines, each under 15 words. Like a table of contents entry.
    If nothing happened, reply HEARTBEAT_OK.

### memory-daily
- interval: 12h
- pre: tasks/memory_daily_pre.sh
- post: tasks/memory_daily_post.py
- prompt: |
    [DAILY INDEX]
    Compress the hourly entries below into a single day-level index.
    Format: 3-6 bullet points, each under 20 words. Like a diary's table of contents.
    Drop trivial items. Keep only what you'd need to find the right conversation later.
    If anything should update permanent memory, add: "→ UPDATE: <file>: <what changed>"

### memory-weekly
- interval: 5d
- pre: tasks/memory_weekly_pre.sh
- post: tasks/memory_weekly_post.py
- prompt: |
    [WEEKLY DIGEST UPDATE]
    Merge the daily entries below with the existing long-term digest.
    The digest is a compressed timeline — what happened, what changed, what matters going forward.
    Format: 5-10 bullet points covering the period. Each under 25 words.
    Drop anything already captured in permanent memory files. Keep under 300 words.

### memory-monthly
- interval: 30d
- pre: tasks/memory_monthly_pre.sh
- post: tasks/memory_monthly_post.py
- prompt: |
    [MONTHLY ARCHIVE UPDATE]
    Merge the weekly digest below with the existing monthly archive.
    This is the longest-term memory layer — a compressed timeline of months.
    Format: 1-2 bullet points per week, each under 30 words.
    Focus on: milestones, turning points, evolving patterns.
    Keep under 500 words total.

### eigenflux-profile
- interval: 24h
- pre: tasks/eigenflux_profile_pre.sh
- post: tasks/eigenflux_profile_post.py
- prompt: |
    [EIGENFLUX PROFILE REFRESH]
    Compare the user's current EigenFlux profile with the latest memory.
    Has anything changed that should be reflected in their network profile?
    Return JSON: {"should_update":true/false,"agent_name":"<optional>","bio":"<full bio>","reason":"<brief>"}
    If no significant changes, return {"should_update":false}

### eigenflux-publish
- interval: 60m
- pre: tasks/eigenflux_publish_pre.sh
- post: tasks/eigenflux_publish_post.py
- prompt: |
    [EIGENFLUX RECURRING PUBLISH]
    Based on our conversation history, do you have any useful signal worth broadcasting?
    Rules: genuinely useful to other agents, NO private info/credentials, factual only.
    Return JSON: {"should_publish":true/false,"content":"<text>","notes":{"type":"info","domains":["<1-3>"],"summary":"<100chars>","expire_time":"<ISO8601 7 days from now>","source_type":"original"}}
    If nothing worth sharing, return {"should_publish":false}
