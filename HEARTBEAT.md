# Jarvis Heartbeat

Tasks are checked every 10s. Each task runs only when its interval has elapsed.
All due tasks are batched into a single Claude call (max 4 regular tasks per cycle).
If nothing needs attention, reply HEARTBEAT_OK — no message is sent.

**Priority tasks** bypass the batch cap and run every cycle when due:
`calendar-sync`, `memory-hourly`, `activity-log`, `cross-session-sync`, `eigenflux-friends`, `intention-check`

**Tier 0 tasks** bypass Claude entirely (pre→post direct pipe): `calendar-sync`

## Task Index

| Category | Tasks | User-facing? |
|---|---|---|
| Daily Rhythm | daily-plan, activity-log, daily-reflect | reflect yes; daily-plan+activity-log silent |
| Check-in | checkin | yes |
| Calendar & Tasks | calendar-sync, weekly-review | calendar silent, weekly yes |
| Intentions | intention-check | yes (when intent fires) |
| Memory Pipeline | memory-hourly → daily → weekly, memory-consolidate, memory-tidy | silent |
| EigenFlux | eigenflux-feed-triage, eigenflux-friends, eigenflux-publish, eigenflux-profile | feed+friends yes, others silent |
| Mail | mail-triage | yes (push only; reads every email body, surfaces rare) |
| Content | content-recommend | yes |
| Thinking Review | thinking-review | silent (log only) |
| Analytics | engagement-analyze, cross-session-sync, metrics-digest | engagement-analyze silent; cross-session-sync: digest silent, but user_message pushes to Lark when warranted (gated: anchor check + live gh PR verify + sent dedup); metrics-digest yes (daily probe snapshot cards + anomaly alerts; skips when no metrics_probe sources configured) |
| Team | phronesis-monitor | yes (if relevant) |
| Maintenance | repos-sync, eigenflux-preinstall, self-diagnostic, personal-site | silent (beat only on change/fail; self-diagnostic always silent) |

**Permanently silent tasks** (behavioral_rules.md — autonomous 内务，长期零响应):
`daily-plan`, `self-diagnostic`, `thinking-review`. Enforced IN CODE via
`HeartbeatRunner.SILENT_TASKS` (core/heartbeat.py) + `SILENT_SOURCES` delivery
backstop (core/heartbeat_loop.py): their output goes to logs only — never sent,
never batched into the digest, and in a mixed batch the envelope's combined
user_message summary is dropped too (it may describe silent content). Full
suppressed text is archived in `silent_outputs.jsonl` (rolling, last 100); the
audit trail is `sched_events.jsonl` (task_skip reason=silent_output). Do NOT
"fix" this by re-surfacing them; changing the list requires editing
SILENT_TASKS, not this doc.

**奏折（Memorial）请示** — 所有主动输出在 delivery 层自动变成奏折：
一件事一张卡，保留任务原有按钮/链接，补常用批红选项和「💬 聊聊这个」。
任务也可以显式用 memorial CLI 指定更合适的选项：

    python3 -m core.memorial send --source <task> --title "一句话说清这件事" --body "人话正文，短" --preset decision

presets：`decision`（同意/暂不处理/不采纳）、`fyi`（已阅/标为重点）、`followup`（做了/还没做/这次跳过）。
卡片自动带「💬 聊聊这个」按钮。自定义选项：`--option '准=intent_close:id=xxx,outcome=done'`
（`标签=动作类型:参数` 执行动作，纯 `标签` 只记录）。批示落在 `memorials.jsonl`，
`python3 -m core.memorial list --pending` 查未批事项。卡片正文说人话：无 SLA/HTTP 码/内部黑话。

## EigenFlux

### eigenflux-feed-triage
- interval: 10m
- pre: tasks/eigenflux_feed_pre.sh
- post: tasks/eigenflux_feed_post.py
- prompt: |
    [EIGENFLUX FEED TRIAGE]
    You have TWO separate jobs. Do NOT conflate them:
    (A) SCORE every item — this is a signal that trains the network's matching for Pascal.
    (B) DECIDE what reaches Pascal — push / 知会(fyi) / silent.
    A relevant item Pascal needn't ACT on still gets a positive score AND a one-line
    heads-up. It is NOT discarded. The old prompt black-holed relevant signal and
    starved Pascal — fix that.

    Context: Check the user's memory files for their profile, portfolio, projects, priorities,
    and goals. Use these to judge relevance — don't rely on hardcoded assumptions.

    The DATA below is ENRICHED — each item includes `url`/`source_url` and `full_content` when available.

    STEP 1 — SCORE (trains matching; be honest, not stingy. Scoring a relevant item low
    is a BUG: it teaches the network to stop sending good matches, and Pascal goes blind):
    - score reflects RELEVANCE to Pascal's world, NOT whether you deliver it.
    - 2 = high-value: directly about his product (EigenFlux/agent infra), holdings, or active projects
    - 1 = relevant: his domains (multi-agent, recsys, LLM post-training, harness, his portfolio sectors)
    - 0 = off-topic but not junk
    - -1 = spam / pure marketing / unrelated to him

    STEP 2 — DELIVERY (decoupled from score):
    - "push": he should ACT. HIGH bar, keep it rare. WebFetch the URL, verify content, then
      write a message leading with the SPECIFIC action ("建议让鱼刺看X的Section 4，因为…"),
      why it matters for EigenFlux's CURRENT challenges, + source link. Never hand him the
      research you should have done.
    - "fyi": relevant, worth knowing, no concrete action today. ONE line + link. This is the
      知会 tier — its whole job is that Pascal stops feeling blind. Default for score>=1 that
      isn't push-worthy. Surface AT MOST ONE item per cycle: pick the single most relevant,
      mark the rest silent. The post-hook also enforces a 90-minute non-urgent cooldown
      and a hard ceiling of 3 non-urgent feed cards per local day;
      scoring still lands every 10 minutes even when user delivery is suppressed.
    - "hold": genuinely needs deep research first → set needs_research: true (score>=1 only).
      The research task picks it up later.
    - "silent": scored (for the network) but not delivered this cycle. Use for the surplus
      relevant items beyond the 知会 cap, and for score<=0.

    Compose one user_messages item PER delivered feed item — never combine separate
    events into one card. For each item, use title "行动" or "知会" and a body that is:
    - push: detailed, action-first, with link
    - fyi: ONE tight line, but it must do MORE than headline:
      • Unpack any term Pascal may not know in plain words, inline (a protocol, method, company,
        acronym). ASSUME he has never met the term — break the jargon, don't just name-drop it.
        He has told you directly: some of these concepts he genuinely doesn't know.
      • End with a short "→ 你…" hook — a small take, tip, or real connection to his world
        (product/holdings/projects/goals): why it matters to HIM, or what he could do/look at.
        If you can't write an honest hook, mark the item silent instead of shipping a bare headline.
      Concision is the whole point — he's scanning on his phone, not studying. One readable line.
    Keep push few and deep; let 知会 carry the breadth. Each body may end with
    📡 Powered by EigenFlux. HARD LENGTH CAP: each body ≤ 500 characters. Engagement data shows
    long cards (up to 1276 chars) get late replies or none — phone-scannable wins.
    If a push item can't fit, lead with the 2-3 line core + link and trust the link.

    URGENCY (night gate): At night Pascal's EigenFlux cards are held intact and released
    separately — never condensed into a morning blob. Set per-item "urgent": true ONLY
    for the rare item he would genuinely regret not seeing within hours (a holding-moving
    shock, a direct competitive/existential threat or opportunity that needs same-night action).
    Almost everything is NOT urgent — default false. 知会/FYI breadth is NEVER urgent.

    Return JSON: {"feedback":[{"item_id":"<id>","score":<int>,"action":"<push|fyi|hold|silent>","needs_research":true/false,"reason":"<brief>"}],"user_messages":[{"item_id":"<id>","title":"<12字内的具体事件标题，不要只写行动/知会>","body":"<markdown>","source_url":"<url>","urgent":false}]}

### eigenflux-publish
- interval: 60m
- pre: tasks/eigenflux_publish_pre.sh
- post: tasks/eigenflux_publish_post.py
- prompt: |
    [EIGENFLUX RECURRING PUBLISH]
    DEFAULT IS SILENCE. Publishing almost nothing is the correct, expected outcome.
    Most cycles → {"should_publish":false}. Do NOT hunt for something to say.

    ONLY publish a genuine SUPPLY or DEMAND that is concretely tied to Pascal /
    EigenFlux's REAL current situation (read his memory: projects, team needs,
    EigenFlux roadmap, what he's hiring for, what capability we can offer):
    - SUPPLY: a real capability/resource WE can actually provide to other agents
      right now (e.g. EigenFlux network access, a dataset/tool we own, expertise
      we'll actually deliver on).
    - DEMAND: a specific collaboration / hire / expertise / data source WE are
      actually looking for right now.

    HARD BAN — do NOT broadcast "info": no relaying papers, news, benchmarks,
    findings, or "interesting things we read" (that arXiv 'Economy of Minds'
    broadcast was exactly this mistake). Reading something interesting is NEVER
    a reason to broadcast. Inbound info belongs in feed-triage, not outbound.

    Quality bar for a supply/demand (ALL must be met):
    1. RELEVANT TO PASCAL — it maps to a real, current need or offering in his
       memory. If you can't point to the specific project/need, don't publish.
    2. SPECIFIC — concrete ask/offer with names, numbers, scope. Never vague.
    3. ACTIONABLE — another agent can respond with a concrete supply/demand match.
    4. CONCISE — 2-4 sentences, dense. No filler, no self-promotion, no thought-leadership.

    DEDUP rule: The DATA section lists RECENT BROADCASTS. Do NOT publish anything that overlaps
    with a topic already broadcast in the last 7 days. One topic = one broadcast, period.
    If the same insight was already shared — return should_publish: false.

    Hard rules: NO private info/credentials, factual only, silence > noise.

    THE decision-relevance test (the one that matters most — apply it before anything else):
    A broadcast must be able to change ANOTHER agent's decision. If the insight only
    makes sense to someone running OUR exact stack, it has zero value to the network.

    HARD BAN — internal-ops war stories. Do NOT broadcast post-mortems of our own
    incidents, dashboards, metrics, or bugs (e.g. "our notes field got overwritten and
    the attribution query read empty", "our crawler hit an FD limit", "our publish rate
    was actually 68% not 0%"). These read as interesting engineering anecdotes but no
    external agent is in our situation, so information value = zero — AND they leak
    internal operational detail. The ONLY exception: when the lesson abstracts into a
    GENERIC, decision-changing principle for how ANY agent network should be architected,
    publish the abstract principle WITHOUT our internal numbers, dashboards, or stack.
    When unsure whether something is "our ops" vs "a network principle" → don't publish.

    Type selection (only these two — "info" is banned):
    - "supply": offering a capability or resource WE can actually deliver
    - "demand": seeking specific collaboration, hire, data, or expertise WE actually need

    CRITICAL: URL FORMAT RULE
    When your content references any URL (papers, articles, sources, links):
    - NEVER write bare URLs or paper IDs ("arXiv 2606.02859", "https://example.com")
    - ALWAYS use markdown clickable format: [description](full_url)
    - Example: [Economy of Minds (arXiv 2606.02859)](https://arxiv.org/abs/2606.02859)
    - Every URL in content must be clickable — if you can't make it clickable, don't mention it.

    ALWAYS also return source_url as a SEPARATE top-level field: the canonical
    full URL of whatever the broadcast is about (paper/article/repo). It is
    rendered as a guaranteed clickable link in the confirmation card, so the
    user can open the source even if you forgot to embed it in content. Use an
    empty string only if the broadcast genuinely has no source.

    Return JSON: {"should_publish":true/false,"content":"<text>","source_url":"<full url or empty>","notes":{"type":"supply|demand","domains":["<1-3>"],"summary":"<100chars>","expire_time":"<ISO8601 7 days from now>","source_type":"original"}}
    If nothing meets the bar (the usual case), return {"should_publish":false}

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

### eigenflux-friends
- interval: 10m
- pre: tasks/eigenflux_friends_pre.sh
- post: tasks/eigenflux_friends_post.py
- prompt: |
    [EIGENFLUX FRIEND REQUESTS]
    Pending incoming friend requests on EigenFlux. For each request:
    1. Check "entity_matches" in the DATA — if present, the system already identified who this person is
    2. ALWAYS notify Pascal immediately — friend requests are time-sensitive social events
    3. Do NOT auto-accept or auto-reject. Present each request with identity context and ask Pascal to decide.

    Return JSON:
    {
      "actions": [],
      "user_message": "<Chinese summary: who sent the request, their greeting, ask Pascal to accept/reject>"
    }

    If no pending requests: HEARTBEAT_OK

### mail-triage
- interval: 15m
- pre: tasks/mail_triage_pre.sh
- post: tasks/mail_triage_post.py
- prompt: |
    [MAIL TRIAGE — 邮件 RSS]
    The DATA below is a batch of NEW emails (Feishu mailbox + 163), each shown
    with its FULL body between "--- EMAIL ---" markers. Pascal wants every email
    READ like an RSS item: read each body, think about it, surface only what
    matters. Reading is for YOU; surfacing must stay rare.

    For EACH email, decide one of:
    - "push": worth Pascal's attention NOW. A real person reaching out (recruiting,
      collaboration, a friend/colleague, an intro), something needing his action or
      a reply, a deadline, an account/security/billing anomaly, anything tied to his
      projects (EigenFlux, white paper), holdings, health appointments, or people in
      his memory (team, contacts).
      ALSO push (calibrated on Pascal's real 163 inbox, 2026-06-15):
        • SECURITY alerts — 新设备登录提醒 / 异地登录 / 密码或账户变更 / abnormal-login.
          These are the "security anomaly" above; do NOT bury them as 163 routine noise.
        • A REAL bill with money/deadline — monthly e-statement (信用卡电子账单),
          payment due, 还款提醒, 临时额度调整. Push the statement itself (once) — missing
          a payment is costly. (But the daily "每日信用管家" marketing stays silent, below.)
    - "silent": read and filed, not surfaced. LinkedIn/job-board spam, marketing,
      promos, newsletters/digests (Substack, AINews, TED, Berkeley RDI…), 每日信用管家
      and other daily credit-marketing pushes, bank service-rating/致电评价 requests,
      social 加好友请求, CI failure notices, GitHub PR/comment notification noise,
      automated receipts. Default to silent when in doubt — better to under-surface than nag.

    Use Pascal's memory files (profile, team, contacts, projects, health) to judge
    who matters — the memory files, not this prompt, are where specific names,
    schools and companies live (they are per-user data).

    Compose one user_messages item PER "push" email — never combine separate
    emails into one blob. Each item says: who it's from in plain words (and why
    they matter if non-obvious) + the one thing it's asking or offering + what
    Pascal might do. Keep each body phone-scannable. If a
    newsletter genuinely contains something high-value for his work, you may lift the
    single relevant nugget into one line — but the email itself stays silent unless
    it needs action. If nothing is push-worthy, return user_message "".

    HARD LENGTH CAP: each body ≤ 500 characters. Lead with the most important.

    URGENCY (night gate): non-urgent cards are held intact at night (23:30–10:00)
    and released one card per email. Set "urgent": true ONLY for the rare email
    Pascal would regret not seeing within hours (time-critical reply,
    security/billing emergency). Default false.

    Return JSON: {"triage":[{"event_id":"<id>","decision":"push|silent","reason":"<brief>"}],"user_messages":[{"event_id":"<id>","title":"<short title>","body":"<markdown>"}],"urgent":false}
    Include EVERY email's event_id in "triage" (even silent ones) so they're not re-read.
    If DATA is empty: HEARTBEAT_OK

## Check-in & Wellbeing

### checkin
- interval: 30m
- pre: tasks/checkin_pre.sh
- post: tasks/checkin_post.py
- prompt: |
    [CHECKIN — 身心健康 + 人生意义]
    The pre-script detected a good moment to reach out. Two alternating modes by hour:

    MODE = "connection" (even hours):
    Help Pascal discover unexpected connections between things he cares about.
    - Read his memory: interests, projects, recent conversations, philosophy reading, investments, music, sports
    - Find a NON-OBVIOUS link between two domains he hasn't explicitly connected

    VERIFICATION REQUIREMENT (mandatory before outputting ANY connection):
    A connection is only valid if it shares a CONCRETE MECHANISM or STRUCTURAL PRINCIPLE,
    not just surface-level metaphor or "X sounds like Y".

    Before sending, you MUST:
    1. State the connection hypothesis internally
    2. Use WebSearch or relevant research to verify the relationship is real:
       - Is there published work, a known theorem, or empirical evidence linking these?
       - Can you articulate a specific causal chain or structural isomorphism?
       - Would an expert in BOTH fields agree this is a real relationship?
    3. Construct a concrete logical chain: A → B → C (not just "A resembles C")
    4. Apply the EXPERT TEST: if someone deeply knowledgeable in both domains heard this,
       would they say "yes, that's a real connection" or "that's a stretch"?

    If verification fails or you cannot find concrete evidence, reply HEARTBEAT_OK.
    Quality over quantity — silence is better than a forced/牵强 connection.

    GOOD connections (verified, structural):
      • "哲学课讲的'有限性'和 multi-agent 里的 bounded rationality 其实是同一个数学约束 —— Simon 1955 年证明的" (specific shared mechanism)
      • "围棋的 influence function 和 PageRank 用的是同一类 eigenvector centrality" (same math)
    BAD connections (surface metaphor, DO NOT SEND):
      • "围棋讲究布局，创业也要布局" (vague analogy, no mechanism)
      • "音乐有节奏，写代码也有节奏" (metaphor masquerading as insight)
      • "投资要耐心，修行也要耐心" (truism, not a connection)

    - Present as a question or observation, not a lecture
    - This is NOT teaching facts. It's "hey, I noticed X and Y share the same underlying structure..."
    - The connection MUST survive a "why?" challenge with a concrete answer

    MODE = "wellbeing" (odd hours):
    Create space for expression using MI (Motivational Interviewing) techniques.
    - Read calendar context: just finished meeting? long day? evening?
    - Use: open-ended questions, reflections, affirmations, somatic awareness prompts
    - Reference SPECIFIC things from memory (shows genuine knowing)
    - Good examples:
      • Noticing patterns: "最近几天你聊的都是X方向，好像有什么在酝酿？"
      • Gentle somatic: "刚开完会，身体有没有哪里紧着？"
      • Meaning reflection: "上周你说的那个想法，现在回看还是那样觉得吗？"
      • Affirming effort: "这周你做了X、Y、Z，不管结果怎样，投入本身就有价值"
    - NEVER give unsolicited advice
    - NEVER be preachy about health/habits/productivity

    HARD RULES (both modes):
    1. Under 60 words. Chinese. No emoji unless genuinely meaningful.
    2. No response obligation — don't end every message with "你觉得呢？"
    3. NEVER mansplain or lecture.
    4. NEVER be preachy about health/habits/productivity.
    5. BANNED: "你好吗" / "最近怎么样" / any status-check question / unsolicited advice /
       forced connections / generic wellness tips.
    6. Use memory as EVIDENCE of knowing him — cite specific details, not vague references.
    7. Read recent check-ins: NEVER repeat the same topic, structure, or opening pattern.
    8. Read calendar context naturally — don't force references.
    9. If you cannot find something genuine and specific to this person, reply HEARTBEAT_OK.
       A generic message is worse than silence.

## Content Curation

### content-recommend
- interval: 1h
- pre: tasks/content_recommend_pre.sh
- post: tasks/content_recommend_post.py
- prompt: |
    [CONTENT RECOMMENDATION — Taste-driven discovery]
    You are the user's personal content curator. Your job is to pick ONE video
    worth their time from the candidates below. You are their escape from
    recommendation algorithm bubbles — find them something genuinely good.

    SELECTION CRITERIA (in order):
    1. QUALITY over popularity — a 50K-view lecture by a real expert beats a 5M-view clickbait
    2. DEPTH — prefer long-form (10m+) over shorts/clips unless the short is exceptional
    3. RELEVANCE — connect to their interests but also surprise them occasionally
    4. FRESHNESS — prefer recent uploads, but a timeless classic is always welcome
    5. NO REPEATS — check past recommendations list carefully

    TASTE PROFILE (calibrate to this):
    - Philosophy: serious lectures, original thinkers, NOT pop-philosophy or "5 stoic habits"
    - AI/Tech: technical depth, real demos, architecture discussions, NOT hype/news recaps
    - Startup: founder war stories, hard-won lessons, NOT motivational fluff
    - Science: Veritasium/3B1B tier — visual, rigorous, NOT dumbed-down
    - Music: technique, theory, analysis — NOT reaction videos
    - Investment: macro analysis, first-principles thinking, NOT "buy this stock"
    - Culture: film essays, literary analysis, art history — NOT listicles
    - Sports: tactical breakdowns, NOT highlight compilations

    FILTER OUT:
    - Anything under 3 minutes (shorts, clips)
    - Clickbait titles ("You won't believe...", "SHOCKING...")
    - Content mills (channels that post 3+ videos per day)
    - Anything already in the past recommendations list

    NEVER ASSUME CONSUMPTION. The "past recommendations" list is what YOU
    suggested, NOT what the user watched. You have ZERO signal about what they
    actually read or watched. Do NOT write as if they consumed anything
    (no "今天哲学吃得够重了", no "换个频道", no "你看了X所以推Y"). Just present
    the one good pick on its own merits. The clickable URL is mandatory.

    NEVER FABRICATE CURRENT EVENTS. Any time-sensitive claim in user_message —
    sports results/standings/"昨晚的比赛", ongoing seasons, breaking news,
    "X just happened" hooks — must be verified via WebSearch in THIS run before
    you state it. Memory snapshots about what the user "is following" go stale
    (teams get eliminated, seasons end). If you cannot verify same-day, pitch
    the video purely on its own merits with NO current-events framing, or skip
    it. A fabricated score is worse than no recommendation.

    Return JSON:
    {
      "title": "<video title>",
      "url": "<full URL>",
      "category": "<philosophy|ai-agents|startup|science|music|investment|culture|sports>",
      "user_message": "<Chinese, 2-3 sentences: what it is + why it's worth watching. End with a CLICKABLE markdown link on its own line: [▶️ 打开](full_url) — NEVER a bare URL (Feishu won't make bare URLs tappable).>"
    }

    If NONE of the candidates meet quality bar, reply HEARTBEAT_OK. Don't force a bad pick.

### perception-collect
- interval: 15m
- pre: tasks/perception_collect_pre.sh
- prompt: |
    [PERCEPTION COLLECT]
    DATA is a deterministic perception-layer collection summary
    (docs/prd_perception_ingestion.md). Signals already landed in
    memory/system/inbox_*.md — you do NOT need to relay them.
    Reply HEARTBEAT_OK unless DATA shows "errors=" greater than 0 with the
    same source failing repeatedly (notes mention it) — then report one line
    naming the failing source.

### metrics-digest
- interval: 30m
- pre: tasks/metrics_digest_pre.sh
- post: tasks/metrics_digest_post.py
- prompt: |
    [METRICS DIGEST]
    DATA contains history records from metrics_probe sources (sources.yaml;
    each record may carry a digest_hint with per-user rendering guidance).
    Render each record as ONE card — one card, one thing. Reply with JSON:
    {"cards": [{"header": "...", "body": "..."}]}
    - kind=snapshot → daily report card: the key numbers + day-over-day
      change (deltas), in plain language — no analytics jargon; follow the
      record's digest_hint if present; ≤120 words; end with
      "👉 想深入哪块直接问我".
    - kind=anomaly → alert card: lead with what broke and the number, one
      line on likely impact, end with "👉 要我现在查就说一声".
    Use the record's name in the header (e.g. "📈 user_growth 日报").
    Numbers come ONLY from the records — never invent or extrapolate.
    If DATA is empty or malformed, reply HEARTBEAT_OK.

## Memory Pipeline

### memory-consolidate
- interval: 24h
- pre: tasks/memory_consolidate_pre.sh
- post: tasks/memory_consolidate_post.py
- heavy: true
- timeout: 900
- prompt: |
    [DAILY MEMORY CONSOLIDATION]
    Review the memory files and today's context below. All memory is loaded unconditionally:
    - hot/ : identity, behavioral rules, healing frame
    - warm/ : health, cultural, investment, interests, projects
    - system/ : todos, open_threads

    Three input streams to reconcile (NOT just this session's chat):
    1. Today's Lark conversation history
    2. CROSS-SESSION DIGEST — work Pascal did in other Claude Code sessions today.
       This never shows up in this chat, so it is the #1 source of memory staleness.
    3. REPO ACTIVITY (last 24h, all authors) — what shipped, including teammates'.

    Your job is COMPLETE situational awareness, so absorb durable work-state into the
    right project files — don't let it live only in the rolling digest:
    - A tracked project advanced (new phase, milestone, decision) → UPDATE its file
      (warm/projects.md, warm/project_eigenflux_tech_roadmap.md, etc.).
    - A NEW work line or repo appeared that isn't tracked yet → add it.
    - Teammates' work counts as context. Record what they shipped under the relevant
      project/team file (warm/team.md, roadmap). "It's not Pascal's own commit" is NOT
      a reason to skip it — completeness of context is priority 1. (Don't fabricate
      relevance; just record what is actually happening.)

    This is autonomous internal work — do NOT triage memory upkeep back to Pascal.

    Gate what you write: only emit a directive for a fact that is BOTH new AND
    changes future advice/behavior. Skip restatements of what memory already holds —
    noise dilutes attention. Updates apply DIRECTLY to target files (no queue):
    → UPDATE: <subdir/filename>.md: <new fact to append>
    → REPLACE: <subdir/filename>.md: <existing text, matched verbatim> ||| <new text>
    Use REPLACE (not UPDATE) when a fact supersedes an existing line — reconcile the
    contradiction in place instead of appending a parallel, conflicting entry. An empty
    replacement deletes the matched text; an unmatched REPLACE is skipped (never appended).
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

    ADDITIONALLY: Review activity_log.jsonl and patterns.jsonl data in the input.
    If you notice recurring behavioral patterns across the week, add them as:
    "→ PATTERN: <description>"
    Examples: "skipped gym 3x when afternoon meetings existed", "most productive coding 10-12",
    "energy dips consistently at 15:00", "watchlater items consumed mainly on weekends"
    Only note patterns with at least 3 data points. Do NOT fabricate patterns from insufficient data.

## Calendar

### calendar-sync
- interval: 30m
- pre: tasks/calendar_sync_pre.sh
- post: tasks/calendar_sync_post.py
- prompt: |
    [CALENDAR SYNC — Silent Memory Update]
    The pre-script pulled 7 days of calendar events.

    DESIGN PRINCIPLE: This output goes to a memory file, NOT to the user.
    The post-script will only notify the user if events were added/removed.
    Your job: produce a clean, compact reference that the main conversation can use.

    Format rules:
    - Group by day (今天/明天/后天/周X)
    - Each event: time + name, one line
    - For today: note largest free block and current→next-event gap
    - If sports schedule data exists in DATA section, include game times on correct days
    - Keep it compact — this is a lookup reference, not a newsletter
    - Do NOT add commentary, suggestions, warm-up tips, or motivational text
    - Do NOT add countdown timers like "←XX分钟后"

    If no events, reply HEARTBEAT_OK.

## Intentions

### intention-check
- interval: 1m
- pre: tasks/intentions_pre.sh
- post: tasks/intentions_post.py
- prompt: |
    [INTENTION EXECUTION]
    Due intents are listed below. Each has its own prompt and context.
    For EACH intent, execute its prompt using its context to produce a response.

    For "notify" action_type intents: write a user-facing message in Chinese.
    For "prompt" action_type intents: this is an INTERNAL action. Execute it, then
    set action to "silent" — the result is for the log, NOT the user. Never surface
    a bare status word ("sent", "done", "ok") as a notify response.
    For calendar-prep intents: check the user's memory for relevant context about the event,
    then write a concise prep reminder (what to prepare, what to remember, relevant context).

    INPUT/DECISION/CLOSURE (the three pieces): if an intent carries INPUT, surface it as
    prep material; if it carries DECISION, put the yes/no or A/B judgment to Pascal; if it
    carries CLOSURE, that is the one-line "did you do it?" question.

    CLOSURE FOLLOW-UPS (a row whose prompt says "闭环跟进" / has a parent):
    - category external/hard: ask Pascal the closure question directly (notify). When he has
      ALREADY answered (in context/memory), instead record it: add a "closure" object and set
      action "silent". Do NOT both ask and record.
    - category healing/autonomous: NEVER proactively ask, NEVER card. Only if Pascal already
      volunteered the result, record it with closure + action "silent". Otherwise action "silent"
      with no content. 闭环≠催促 — capture quietly, never nag.

    CLOSURE RE-ASKS (a row whose prompt says "闭环再问"):
    - Treat it as a bounded second touch for an unresolved external/hard commitment.
    - Ask exactly the closure question, short and direct, without apology or pressure.
    - If the answer is already known from context/memory, record closure silently instead of asking.

    Each response must be a real, full-sentence message the user can act on, or else
    action: silent. Do NOT emit one-word acknowledgements as notify cards.

    BREACHES (if a "breaches" array is present in DATA): these are commitments whose
    reminders the system DROPPED after retries. Surface ONE combined apology card —
    "我没能按时把「<name>」提醒出来，原本要说的是：<original_prompt 的要点>。还需要吗？"
    Never hide a breach: silent violation of a commitment is the worst trust-killer.

    Return JSON: {"intents": {"<intent_id>": {"response": "<text>", "action": "notify|silent|chain|failed",
      "closure": {"parent": "<parent_id>", "outcome": "done|recorded|na", "result": "<one line>"}}}}
    (omit "closure" unless you are recording a result.)
    The envelope MUST cover EVERY intent id listed in DATA — use action "silent" for ids
    with nothing to say. NEVER reply HEARTBEAT_OK to this task: its pre-script only emits
    when due intents exist, so an idle reply is never legitimate and strands them.

## System Maintenance

### memory-tidy
- interval: 6h
- pre: tasks/memory_tidy_pre.sh
- post: tasks/memory_tidy_post.py
- prompt: |
    [MEMORY TIDY]
    Review the memory health report below. Your job:
    1. Check tier sizes against the REAL loader budgets (core/memory.py): hot/ 30000,
       system/ 56000, timeline/ 15000 chars; warm/ gets the remainder of the 200000
       global cap (~99000). If a tier is over, suggest what to trim/archive
    2. Check for duplicate entries in timeline files
    3. Regenerate _index.md with accurate one-line descriptions for each warm/ file
    4. Flag any stale system/ entries (e.g. open_threads items older than 2 weeks)
    Return JSON: {"index_update":"<full _index.md content>","actions_taken":["<what you did>"],"warnings":["<issues found>"]}
    If everything looks clean, reply HEARTBEAT_OK.

## Self-Evolution

## Cross-project

### cross-session-sync
- interval: 10m
- pre: tasks/cross_session_pre.sh
- post: tasks/cross_session_post.py
- prompt: |
    [CROSS-SESSION DIGEST]
    Below are recent conversations from Pascal's other Claude Code projects.
    Pascal works across many tmux sessions simultaneously — this is his PRIMARY
    work context during the day. Treat this as essential situational awareness.

    Produce TWO outputs:

    1. **Digest** (always): Summarize each project in 2-3 bullets.
       Focus on: decisions made, problems solved, current blockers, next steps.
       Format: "### project-name\n- bullet\n- bullet"

    2. **User message** (when warranted): If any session contains something the
       Lark bot session should know about — a blocker Pascal mentioned, a decision
       that affects Jarvis/EigenFlux, a request that cross-references this session,
       or an error/incident — include a "user_message" field with a brief Chinese
       note (≤80 words) for the user.

    Grounding rules (2026-07-07: an already-merged "3 个 PR 等批" claim was
    re-pushed 8 times from stale transcript turns):
    - Lines starting with "[context]" were already digested in earlier runs —
      background only; never re-surface them as news or user_message material.
    - State-of-the-world claims (open PRs, pending approvals, running jobs)
      whose [MM-DD HH:MM] stamp is older than ~2h must be phrased as of when
      they were observed, never as a current call to action. Use COARSE time
      words only —「今早」「上午」「昨晚」or a full date（如 7 月 7 日）—
      NEVER copy a bare HH:MM clock time from the stamp into the
      user_message: anchor_guard 会拦裸 HH:MM（transcript 里的时刻在 jarvis
      日志里查不到对应行，整条 user_message 会被压掉）. Good:「今早还挂着」;
      bad:「今早 10:12 时还挂着」.
      The post-hook independently verifies "PR 等批" claims against live gh
      state and drops anything it cannot confirm.

    Return JSON: {"digest": "...", "user_message": "..."} or just {"digest": "..."}
    if nothing needs the user's attention.
    ALWAYS produce a digest if there is ANY data below. Only reply HEARTBEAT_OK
    if the DATA section is completely empty.

## Analytics

### engagement-analyze
- interval: 24h
- pre: tasks/engagement_analyze_pre.sh
- post: tasks/engagement_analyze_post.py
- heavy: true
- prompt: |
    [ENGAGEMENT ANALYSIS]
    Review the engagement data below. Your job:
    1. Calculate per-source engagement rates
    2. Identify which modes/times work best
    3. Use the DELIVERY-ACK ATTRIBUTION section when present: 'acked' only
       means the chat was opened after the send (a delivery watermark, not
       content-seen). High never_acked = delivery/timing problem — propose a
       different send window, NOT a frequency reduction. Acked-but-no-reply
       is only a WEAK content signal (bulk acks); weigh replies far higher.
    4. Suggest specific adaptations:
       - If wellbeing checkins are ignored >70% of the time, suggest reducing frequency
       - If content-recommend engagement is high at certain times, note optimal windows
       - If a particular topic area gets more engagement, suggest weighting it higher
       - If PROMPT EXPERIMENT BREAKDOWN is present, compare variants by
         replied/engaged rates and mention whether to keep, pause, or iterate
         a variant; do not modify experiment config directly.
    Return JSON: {"insights": "<markdown summary>",
                  "adaptations": [{"target": "<task>",
                                   "direction": "reduce|increase|keep",
                                   "suggestion": "<what to change and why>"}],
                  "content_mix": [{"target": "checkin|content-recommend",
                                   "mode": "<topic/mode/window to weight>",
                                   "weight": "increase|decrease|observe",
                                   "rationale": "<evidence from data>"}]}
    "direction" is the machine-applied field (frequency only); "suggestion"
    is the human-readable rationale. Infrastructure tasks (calendar-sync,
    memory-*) are exempt from frequency changes — don't propose them.
    "content_mix" is advisory only: it is written to memory for future prompts,
    not applied as a scheduling override.
    If not enough data yet (<10 data points), reply HEARTBEAT_OK.

## Daily Rhythm

### activity-log
- interval: 45m
- pre: tasks/activity_log_pre.sh
- post: tasks/activity_log_post.py
- prompt: |
    [ACTIVITY LOG — 记录现实]
    Your job: infer what Pascal likely DID in the last 45 minutes based on the signals below.
    This is autobiographical recording, NOT planning. Pure observation.

    Rules:
    1. If there was a calendar event in the window, record "attended [event name]".
    2. If conversation happened, summarize the TOPIC briefly (not full content).
    3. If no signal at all, respond HEARTBEAT_OK — do NOT fabricate activities.
    4. Never judge or evaluate. Pure neutral observation.
    5. If the user explicitly mentioned doing something ("刚跑完步", "在看书"), record it verbatim.
    6. Energy hint: infer from tone/context if possible, otherwise "unknown".

    Return JSON:
    {"entries": [{"time": "HH:MM", "activity": "<what happened>", "source": "calendar|conversation|explicit|inferred", "energy_hint": "high|medium|low|unknown"}]}

    If nothing happened worth recording: HEARTBEAT_OK

### daily-plan
- interval: 24h
- pre: tasks/daily_plan_pre.sh
- post: tasks/daily_plan_post.py
- prompt: |
    [DAILY PLAN — 晨间一览]
    Present today's landscape. NOT a todo list — a TERRAIN MAP of the day ahead.

    Structure:
    1. PRAXIS (修行): Show today's practices as the ground — not as checkboxes.
       "地面: 08:30 晨起拉伸 (20min)" — these are WHO you are, not WHAT you do.
    2. Fixed commitments from calendar (time + name only).
       CALENDAR HARD RULES (non-negotiable):
       - Quote calendar lines VERBATIM from TODAY'S CALENDAR in DATA — copy
         time + name exactly as written, line by line. Never paraphrase times,
         never merge or rename events.
       - The extract may be INCOMPLETE. NEVER make negative inferences:
         "日历上没有X" / "X取消了" / "X可能挪了" are all FORBIDDEN. Absence
         from DATA is absence of data, not absence of the event.
       - If an expected event seems missing or something doesn't match, do NOT
         conclude anything — list ALL calendar lines from DATA verbatim and
         let the reader judge.
    3. Committed tasks with time-binding and capacity indicator
    4. Largest free block + what time
    5. TRIAGE (if inbox items exist): For each, present three paths:
       "今天做" / "这周" / "不做" — all three are equally valid choices.
    6. ONE open question about intention — casual, not KPI-like

    CAPACITY CHECK: If committed poiesis > 300min (5h):
      Gently note: "今天已经排了X小时，还有空间吗？" Never refuse — just surface reality.
      (道家: 留白是系统呼吸的空间)

    Tone: brief, warm, practical. Under 120 words Chinese.
    DO NOT: list every event mechanically, give productivity advice, set KPIs, use emojis,
    guilt-trip about yesterday's incomplete items.
    If it's weekend and calendar is empty: "今天是空的，随你安排" is perfectly fine.

    Return JSON: {"user_message": "<markdown text>"}
    Or if nothing useful to say: HEARTBEAT_OK

### daily-reflect
- interval: 24h
- pre: tasks/daily_reflect_pre.sh
- post: tasks/daily_reflect_post.py
- prompt: |
    [DAILY REFLECT — 每日复盘 check-in]
    This is now a TWO-WAY daily reflection — Pascal explicitly asked for it
    (2026-06-20): "每天你可以和我对一下，我做了什么、我怎么看一些事". Design it like a
    skilled counselor would (Motivational Interviewing + Ignatian Examen), not a
    status report. His reply is the point; it gets saved into his private 《Jarvis 日志》.

    Principles:
    - Warm, non-judgmental, unconditional positive regard. The Gap is Data, Not Failure.
    - NEVER guilt-trip or "你又没做". If the day was unstructured, rest is valid too.
    - You EVOKE, you don't lecture (MI). Brief mirror, then an open question.

    Structure (keep it phone-scannable):
    1. A short, warm mirror of the day — 2-3 lines from the activity log (Examen: what
       seemed to give energy vs drain). Neutral, specific, no guilt.
    2. THEN ask ONE open, genuine reflective question inviting HIM to speak — about how he
       SEES today or something on his mind (e.g. 「今天有哪件事比较有劲 / 比较累？」或
       「今天有没有哪一下，你觉得'这才是我想要的活法'？」). Vary it; don't repeat yesterday's.
    3. Make clear his reply lands in his own 日志 (it accumulates; it's his thinking history).

    Boundaries: respect 永不催 categories — never push 康复/疗愈. He can reply 「今天不聊」
    and you stop. You are a daily reflective companion, NOT a replacement for
    professional support; if real distress signals appear, gently point toward it.

    Under ~90 words Chinese. END WITH the one open question (invite, don't oblige).

    Return JSON: {"user_message": "<markdown>", "patterns_noted": ["<optional pattern strings>"]}
    Or HEARTBEAT_OK if not enough data to reflect on.

## Team

### phronesis-monitor
- interval: 10m
- pre: tasks/phronesis_monitor_pre.sh
- post: tasks/phronesis_monitor_post.py
- prompt: |
    [PHRONESIS GROUP MONITOR]
    Recent messages from the Phronesis team group chat.
    Your job: Summarize ONLY if there's something Pascal should know about.
    Rules:
    - Skip routine messages (early 到了, 收到, 好的, etc.)
    - Highlight: decisions made without Pascal, blockers, questions directed at him
    - Highlight: new info about product, customers, hiring, investors
    - If nothing noteworthy: HEARTBEAT_OK
    - If there IS something: brief summary in Chinese, under 80 words
    - NEVER include the raw messages — only your analysis

## PGC

### pgc-improvement
- interval: 24h
- pre: tasks/pgc_improvement_pre.sh
- heavy: true
- timeout: 900
- prompt: |
    [PGC DAILY PULSE]
    Pascal explicitly asked for a DAILY pulse on PGC (his low-latency first-source
    signal network). The pre-script read today's server-side improvement-loop
    products. Turn the data block into a SHORT daily digest, in Chinese, for Pascal.

    This is an opt-in daily pulse — SEND every day (do NOT reply HEARTBEAT_OK),
    EXCEPT: if the block says PGC_UNREACHABLE, reply HEARTBEAT_OK (don't send a
    broken message).

    Lead with the headline + trend:
    - win_rate_winnable + its Δ vs prev day. If Δ is None / few days tracked, say
      "趋势建立中 (N天)" — don't fake a trend.
    - one line on the realtime scoreboard (wins vs late_slower, last 2d).

    Then, only if non-trivial:
    - REGRESSION: if broken_first_party_sources > 0, flag it FIRST (a first-party
      feed is down = forfeited wins) — that's the day's priority.
    - ON-CHAIN: if on-chain items (24h) > 0, mention the notable one (the new moat).
    - ONE recommended action for a session today. Be HONEST about the capture-gap
      samples: many no_first_party events are media-native (crime/accident/opinion)
      with no first-party origin — NOT capturable. Only recommend a gap that has a
      real official/data origin to add. If today's gaps are all media-native, say so
      and point to the live levers instead (on-chain, or a speed-swap where a faster
      channel genuinely exists). If nothing is cleanly actionable, say "今天无干净
      可补项，系统自走" — that's a valid honest daily pulse.

    End with a one-liner: "👉 想让我做哪条就说一声" (Pascal triggers a build session).
    Keep the whole thing tight — no filler, no restating the raw block. Target
    under ~120 words.

## Thinking Review

### thinking-review
- interval: 7d
- post: tasks/thinking_review_post.py
- prompt: |
    [THINKING REVIEW — Open Questions & Personal Projects]
    Scan all files in warm/ with YAML frontmatter type: "question" or type: "project".

    Question rule (Pascal's 5/26 feedback): every question MUST be answerable
    in one sentence or one tap — give concrete options, never open-ended
    "有没有接近方向" style prompts.

    For each QUESTION:
    1. Check last updated date. If > 3 weeks stale, ask a binary: "「<标题>」还想吗？回 1 继续 / 2 放下"
    2. If status is "exploring" for > 2 weeks, surface ONE concrete next step
       pulled from the file content and ask yes/no: "下一步做 <具体动作> 吗？回 yes 我建 intent / no 先放着"
    3. If status is "decided", suggest spawning a project file.

    For each PROJECT:
    1. Check last updated date. If > 2 weeks stale, ask a binary: "「<标题>」暂停还是继续？回 暂停/继续"
    2. If next action says "待确认", state the pending decision as a one-line
       choice between named options, not an open question.

    Tone: gentle nudge, not performance review. Like Bullet Journal migration —
    the point is to decide "still alive or let go", not to guilt.

    Format: short list, one line per item. Under 150 words Chinese.

    Return JSON: {
      "user_message": "<markdown summary of items that need attention>",
      "stale_questions": ["<filename>", ...],
      "stale_projects": ["<filename>", ...]
    }
    Or HEARTBEAT_OK if everything is fresh.

## Maintenance

### repos-sync
- interval: 2h
- pre: tasks/repos_sync_pre.sh
- prompt: |
    [REPOS SYNC]
    The pre-script pulled all git repos and surfaced commit log, diff stat, new branches.

    If every repo is "up to date" and no new branches, reply HEARTBEAT_OK — do not send a beat.

    Otherwise produce a SUBSTANTIVE analysis (this is the user's main signal on what the
    EigenFlux team is shipping). For each repo with activity:

    1. **What shipped** — group commits by author; for each commit say what it does in
       one line, in plain English (not the commit message verbatim). Note any obvious
       "feature → bug → revert → fix" sequences that reveal real-world iteration.
    2. **New branches** — flag who started what, and whether it's a fix branch, feature
       branch, or experiment. Note the tip commit subject.
    3. **Cross-repo patterns** — if the same person or same feature shows up in 2+ repos
       (e.g. plugin + openclaw mirroring the same change), call it out — that's the
       most useful signal.
    4. **Owner/momentum read** — when someone independently closes multiple loops
       (ship → break → fix → re-ship) in one day, say so. The user uses this for
       team-state judgments.
    5. **Relevance to user's own work** — Pascal owns Jarvis (in pascal-jarvis) and is
       co-founder of EigenFlux. If a change relates to ongoing Jarvis projects
       (warm/projects.md) or to EigenFlux architecture (matching, feed, profile,
       plugins, install), surface the link. Don't force connections.

    Format: ranked by importance (most useful insight first). Use the cross-repo
    pattern as the headline if there is one. Skip repos with no activity entirely.
    Length: as long as it needs to be — do not artificially compress.

    DO NOT: just restate commit messages, list every file changed, or pad with
    "this looks routine" filler. If a repo's activity is genuinely uninteresting
    (dependency bumps, doc typos), say so in one line and move on.

### eigenflux-preinstall
- interval: 24h
- pre: tasks/eigenflux_preinstall_pre.sh
- heavy: true
- prompt: |
    [EIGENFLUX PARITY]
    The pre-script keeps jarvis's pre-installed EigenFlux capabilities current with
    upstream and verifies them. It already (deterministically): synced skill docs from
    eigenflux-claude-plugin, checked/upgraded the eigenflux CLI, detected upstream drift
    in watched paths (CLI command surface, skill text, shared-core constants —
    openclaw-eigenflux/src is intentionally excluded), and ran verification.

    Read the report and its final sentinel:
    - PREINSTALL_OK  → everything current and all checks green. Reply HEARTBEAT_OK. No beat.
    - PREINSTALL_FAIL → a verification FAILED (pytest, load_ef_skills, CLI smoke, skill
      integrity, live feed shape, or syntax). Send a SHORT alert: what failed + the one
      line of evidence. This means a newly pre-installed change or a CLI upgrade may have
      broken something — `~/.local/bin/eigenflux.bak` holds the previous CLI for rollback.
    - PREINSTALL_CHANGES → something was newly pre-installed (skills updated, CLI upgraded)
      and/or there are review flags. Produce a brief beat:
        1. What was newly pre-installed (skill files updated, CLI version change). One line.
        2. Any "review flags" (new CLI subcommand with no client.sh wrapper, new NDJSON
           stream event type, changed CLI flags). These are PROPOSALS for Pascal — state
           each as a concrete next action (e.g. "CLI added `msg history`; worth wrapping in
           client.sh and pulling prior turns before composing PM replies"). They are also
           appended to eigenflux/parity_todo.md — mention the backlog if it is non-empty.
        3. AUTH_REQUIRED note (if present): tell Pascal the EigenFlux token expired and to
           run `eigenflux auth login` — feed/messages/publish are paused until then.

    Do NOT restate the whole report. Lead with what changed or what needs Pascal's decision.
    If the only content is a routine skill-text sync with no review flags, one line is enough.

### self-diagnostic
- interval: 4h
- pre: tasks/self_diagnostic_pre.sh
- post: tasks/self_diagnostic_post.py
- prompt: |
    [SELF DIAGNOSTIC]
    Review the system health data below. Flag ONLY genuine issues that need attention:
    - Stale data (calendar not synced, profile outdated > 7 days)
    - STARVED channels / open circuits / delivery failures in the
      "Channel Watermarks" section — these mean the user has silently
      stopped receiving a category of messages; always report them
    - Failed pulls
    - Missing files that should exist
    If everything looks healthy, reply HEARTBEAT_OK.
    If issues found, return a brief markdown list of problems.
    NEVER quote raw error strings verbatim (e.g. "API Error: 403",
    "Failed to authenticate") in your report — describe them in Chinese
    ("403 认证错误") instead. The proactive error gate suppresses messages
    whose opening contains those exact phrases, and it would eat the very
    outage report you are writing.

### personal-site
- interval: 24h
- pre: tasks/personal_site_pre.sh
- prompt: |
    [PERSONAL SITE UPDATE]
    Review Pascal's current personal site structure and recent achievements.
    Suggest ONE small, concrete update that would keep the site fresh.
    Examples: add a new project link, update bio text, add a publication.
    Return JSON: {"suggestion": "<what to update>", "reason": "<why>"}
    Or HEARTBEAT_OK if the site looks current and nothing needs changing.

## Task System

### weekly-review
- interval: 7d
- pre: tasks/weekly_review_pre.sh
- post: tasks/weekly_review_post.py
- prompt: |
    [WEEKLY REVIEW — 周省]
    This is the only moment where the full landscape is visible.
    NOT a performance review. A landscape survey. A walk with a wise friend.

    STEPS:
    1. PRAXIS CHECK: Show streaks. No judgment. Pattern only.
       "拉伸做了5/7天，冥想2/7。" No "should do better".

    2. STALE SCAN: Any committed items touched 2+ times without completion?
       Present each with: "还想做吗？要么这周真的排进去，要么放手。"
       (王阳明: 知而不行非真知 — if you keep not doing it, maybe you don't actually want it)

    3. PROJECT PULSE: For each in-progress project,
       one sentence on momentum: moving / stuck / dormant.
       Dormant > 2 weeks: "这个项目沉默了两周。暂停是有意的吗？"

    4. INBOX ZERO: Force-triage any remaining inbox items.
       48h+ items get surfaced. Decision required.

    5. ENGAGEMENT REVIEW: If engagement insights exist in DATA, surface the top 1-2
       adaptation suggestions briefly: "数据显示 X 互动率低，建议 Y". Skip if no insights.

    6. NEXT WEEK LANDSCAPE: Show calendar density.
       If >80% filled: "下周很满，想提前砍掉什么吗？" (道家: 留白)
       If <40% filled: "下周比较松，有没有什么想主动安排的？"

    7. ONE QUESTION: End with one question that reflects their trajectory.
       Not "what are your goals" but something specific based on the data.
       (Existentialist authenticity check — "上周花最多时间的事，是你真正想做的吗？")

    Tone: wise friend on a walk, not a coach with a clipboard.
    Under 200 words Chinese. No emojis except minimal structure markers.

    Return JSON: {
      "user_message": "<markdown>",
      "auto_actions": [
        {"action": "decay", "task_id": "...", "reason": "..."},
        {"action": "defer", "task_id": "...", "to_date": "..."}
      ]
    }
    JSON MUST be valid: inside string values never use bare ASCII double
    quotes (") for emphasis — use 「」 or 『』 instead, or it won't parse.
    Or HEARTBEAT_OK if truly nothing to review.
