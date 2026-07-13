# PRD：统一感知层 / Unified Perception & Ingestion Layer

> **一句话**：把"任何我能拿到的上下文"——飞书各群/邮件/文档/妙记、report 改动、EigenFlux、行情持仓、健康、web——统一灌进 Personal Agent 的感知里；**新增一类信息 = 加一条配置,而不是写一对新脚本**。
>
> Status: Draft **v2**(经一轮对抗式多视角审查 + 事实核对后定稿) · Owner: Pascal · 调研日期: 2026-06-09 · 适用仓库: `pascal-jarvis`(= Jarvis 本身)
>
> ⚠️ **实现状态声明**:本 PRD 描述的 `core/perception.py`、`sources/`、`core/perception_route.py`、`sources.yaml`、`perception-collect` heartbeat 任务、`load_tiered_memory(purpose=)` 参数、`config.py` 的 `perception/secrets` 段、以及所有 `inbox_*.md` / `perception_seen.jsonl` / `perception_state.jsonl` / `perception_delivery.jsonl` 文件**全部为净新增,今天尚不存在**(已逐一核对代码)。凡引用现有文件处均带 `file:line` 证据;凡为新增设计处均如此标注。**别把本 PRD 的目标态当成已建态。**

---

## 0. 为什么是现在(北极星)

这个 repo 在某种意义上**就是 Jarvis/Claude 本身**——靠和 Pascal 的交互持续进化(见跨session记忆 `project_self_evolving_repo`)。它的价值取决于一件事:**它对 Pascal 的世界有多"在场"(perception)**。

调研里反复出现同一个词:**"blind"**。EigenFlux feed-triage 的 prompt 自己写道:

> "Scoring a relevant item low is a BUG: it teaches the network to stop sending good matches, **and Pascal goes blind**" — `HEARTBEAT.md:49`
> "its whole job is that **Pascal stops feeling blind**" — `HEARTBEAT.md:62`

但今天"不瞎"这件事**只在 EigenFlux 这一条管线上被认真对待**。其余信息——团队群、邮件、飞书文档、会议妙记、report 改动、持仓行情、健康——要么完全没接,要么各自一套硬编码脚本。Pascal 的原话(历史挖掘):

> "Memory 吸收 cross session 这事跟我也没有任何关系,完全应该是你去做。**最好是不用任何得到我授权就可以做的**。"
> "下次我觉得建议你自动的就去把这个告诉我,不用问动了什么,你就是给我汇报的话。**你如果是秘书的话,你肯定会跟我汇报改了什么,对吧?**"

诉求很清楚:**一个秘书级的、自治的感知层**。把能拿到的全往里灌,它自己消化、去重、判断该不该惊动我;新增一类信源时,我(或你)只改一条配置,不重写管道。

本 PRD 定义这个层。

---

## 1. 现状盘点(AS-IS)

### 1.1 已经在"灌"的东西

今天约 **32 个 heartbeat 任务**在做 ingestion,每个都是一对手写脚本(`tasks/<name>_pre.sh` 采集 → Claude → `tasks/<name>_post.py` 落地)。已接入的信源:

| 信源 | 触发 | 采集方式 | 落到哪 | 硬编码点 |
|---|---|---|---|---|
| Phronesis 群 | 10m, 9–23h | `lark-cli im +chat-messages-list`,增量 `.phronesis_last_ts` | card → outbox | **单群 `CHAT_ID`、单人 `PASCAL_ID` 字面量写死在 .sh**(`phronesis_monitor_pre.sh:5-8`) |
| 日历 | 30m, Tier-0 | `lark-cli calendar +agenda` 30 天 | `hot/calendar_today.md` | 仅本人主日历、7d 窗口写死 |
| EigenFlux feed | 10m | CLI poll+enrich, 打分 | feedback→网络 / card / `needs_research.jsonl` | EF 专属 |
| EigenFlux PM/好友 | 10m, priority | CLI fetch + `entity_resolve.py` | card | EF 专属 |
| EigenFlux 实时流 | 常驻 ws | `ef_stream_loop.py`,seen-set 去重 | Lark + outbox | EF 专属 |
| 跨 session | 10m | `cross_session_pre.sh:8,15` 扫 `$HOME/.claude/projects/**/*.jsonl`(近 24h) | `system/cross_session_digest.md` | 路径 glob 固定 |
| 多 repo git | 2h | 遍历 `$REPOS_DIR` fetch+log+diff | digest(经 consolidate) | `REPOS_DIR` 走 **env var + fallback**(`${WORK_DIR:-…/repos}`),非字面量硬编码——与 phronesis 不同 |
| 内容推荐 | 1h, 9–23h | `yt-dlp` 18+ 类目轮询 | card | 类目/口味写死在脚本 |
| 活动日志 | 45m | 日历+对话推断 | `system/activity_log.jsonl`(静默) | 45min 窗口写死 |

**结论**:每条信源都重复实现了"发现→拉取→增量状态→归一→打分→投递→去重→落地",但**没有任何共享抽象**。`phronesis_monitor_pre.sh` 就是要被泛化的模板——它的全部"配置"(群 ID、本人 ID、作息门、状态文件名)都钉死在 shell 里。

### 1.2 感知是怎么发生的(关键事实)

> **感知 = memory 加载,而 memory 是无条件全量注入。**

- `core/memory.py:load_tiered_memory()` 把 `hot/ → warm/ → system/ → timeline/` 全部 `.md` **无条件**拼成一个串(`MAX_MEMORY_CHARS=200000`,1M context 时代),注入**每一次** Claude 调用。**今天该函数签名无任何过滤参数**(`core/memory.py:40`)。
- 前台对话:`core/prompt.py:build_system_prompt()` 注入 memory + 最近 turns + session compact。
- 后台 heartbeat:`core/heartbeat.py:231` 调 `load_tiered_memory(self.memory_dir)`(无参),同样注入全量 memory。
- 后台→前台桥:`heartbeat_outbox.jsonl` 被 `core/session.py:build_recent_turns()` 合并(读末 10 条),所以前台下一轮能看到 heartbeat 发过的卡片。

**推论(整个 PRD 的地基)**:任何被灌入的信号,**只要写进 `memory/system/` 或 `timeline/` 里的某个文件,下一次前台回合或 heartbeat 调用就会被"感知"**。不存在独立的"新信号 inbox"——落地区就是 memory 目录本身。这既是杠杆(§6),也是风险:**今天全量注入意味着私密内容会进入对外任务的上下文(§3.4)**。

### 1.3 已经成熟、可复用的东西

EigenFlux 管线是全仓库**最成熟的 ingestion 契约**,事实上已经是通用形态:

```
Poll/Stream → Enrich(全文/实体) → Score(LLM, -1..2) → Route(push/fyi/hold/silent)
            → Gate(quiet-hours 批延) → Persist(jsonl) → 可选 Research 二段深挖
```

可直接复用的共享原语(全部已存在、已验证):

- **`core/safety.py`** — `parse_json_response` / `looks_like_error` / `sanitize_for_user` / `salvage_field` / `summarize` / `atomic_write`。**所有 post 脚本必须走这里**(硬约定:别再逐文件手写 JSON 解析,见跨session记忆 `feedback_llm_output_boundary`)。
- **`core/card.py`** — `build_card` / `build_rich_card` / `linkify_bare_urls`(裸 URL 自动可点)。
- **`tasks/_ef_delivery.py`** — 确定性 quiet-hours 门控 + 早晨摘要批延(墙钟,非 LLM)。
- **`scripts/entity_resolve.py`** — 分层身份解析(agent_id > 姓名 > 别名 > 拼音),对 `data/contacts.jsonl` + `team.md`。
- **`core/task_protocol.py`** — `TaskState` / `CircuitBreaker` / `effective_interval`(失败隔离 + 互动驱动调频)。
- **`core/jsonl.py`** — 原子 append JSONL。
- **`core/intentions.py`** — 延时动作(给"灌进来后要在 X 点跟进"用)。
- **`tasks/memory_consolidate_post.py`** — `→ UPDATE:` / `→ REPLACE: old ||| new` 指令,就地改 memory(带路径穿越防护、不匹配即 no-op),是 **durable 感知的落地契约**。

---

## 2. 目标与非目标

### 目标
1. **一个声明式 source registry**:新增信源 = 在配置里加一个 source 块。不碰 `HEARTBEAT.md`,不碰 `core/heartbeat.py` 的硬编码集合,不写新 pre/post 对。
2. **一条通用管线**:把 EigenFlux 的 `Poll→Enrich→Score→Route→Gate→Persist` 提升到模块级,所有信源共用。
3. **统一的 Signal 信封**:pre→post 之间有类型化契约,post 端共享解析与落地。
4. **把"全部能拿到的"接进来**:飞书全表面 + report 改动 + 行情 + 健康 + web(**§7 信源目录**)。
5. **跨源去重 + 敏感度/出口防护**:同一事件不重复惊动;私密内容不经全量 memory 泄露到对外任务。
6. **秘书级自治**:默认自己消化、自己判断该不该惊动,低风险动作无需授权(跨session记忆 `feedback_autonomous_memory_absorption`)。

### 非目标
- 不重建检索 pipeline / encoding gate(跨session记忆 `feedback_memory_design_truememory` 明确:唯一 gap 是 consolidation 门控+矛盾消解,不是检索)。
- 不引入数据库存 memory(`TODO.md`:memory 保持 flat md/jsonl,git 友好)。
- 不做多用户。
- MVP 不追求实时 webhook(10s 轮询足够;`urgent` 走即时推送即可)。

---

## 3. 核心问题(每条都有证据)

| # | 问题 | 证据 | 严重度 |
|---|---|---|---|
| 3.1 | **没有 source registry**。加源要拷两个脚本 + 改 `HEARTBEAT.md` + 改 `core/heartbeat.py` 里 `TIER0_TASKS`/`PRIORITY_TASKS`/`EMPTY_RETRY_DELAYS` 硬编码集合 | `core/heartbeat.py:116/132/139`(按任务名 keyed 的 dict/set) | 高 |
| 3.2 | **单群/单人硬编码是承重的**。`CHAT_ID=oc_907...`、`PASCAL_ID=ou_6cdf...` 字面量钉在 `phronesis_monitor_pre.sh` | `tasks/phronesis_monitor_pre.sh:5-8` | 高 |
| 3.3 | **无跨源去重 / 统一事件身份**。每个孤岛按自己的 id 去重;同一条 Phronesis 消息会经"前台 @提及流"+"phronesis 拉取"**到达两次**,无对账 | `bot.sh:941` vs `phronesis_monitor_pre.sh` | 高 |
| 3.4 | **无隐私/敏感度层**。memory 全量注入**每一次**调用,**包括对外**任务(`eigenflux-publish`、auto-reply)。接邮件/DM/文档后,私密内容可能漏进对外广播 | `core/memory.py:40`(无 purpose 参数)、`core/heartbeat.py:231`(全量注入)、`core/config.py` 无 secrets 段(已核对 `config.py:1-137`) | 高 |
| 3.5 | **consolidation 瓶颈**。`memory-consolidate` 是唯一多源 reconciler,**1×/天、21–22h 窗口**(`memory_consolidate_pre.sh:12`),消费 **3 个输入概念**(跨session digest + repo 活动 + 当日对话,经 nullglob 读 hot/warm/system,非"3 个硬编码 adapter")。N 源涌入时矛盾最长滞留 24h | `memory_consolidate_pre.sh:12,17-18` | 中 |
| 3.6 | **感知延迟无界**。全 pull-based:前台只在**用户下次发消息**时才看到 heartbeat 新信号(outbox 在 `build_recent_turns` 里读)。10:05 进来的紧急邮件/行情可能一直不被看见 | `core/session.py:build_recent_turns` | 中 |
| 3.7 | **状态文件散落、无回放**。`.phronesis_last_ts`/`.feed_poll_state`/`calendar_event_mapping.json`/`/tmp/jarvis-ef-seen` 各一套,无统一 schema、无 downtime 后补采。quiet-hours 早晨 flush 若 daemon 在 9–12 点宕机就**永久丢失**当晚摘要 | `_ef_delivery.py:111-125` | 中 |
| 3.8 | **路由分级锁死在 EigenFlux 内**。push/fyi/hold/silent + quiet-hours 这套优秀模型没被泛化,其他源享受不到 | `eigenflux_feed_post.py` + `_ef_delivery.py` | 中 |

### 3.9 非功能需求(NFR)

一个触达 10+ 信源、每周期可能 LLM 打分的感知层,生死取决于预算、延迟、吞吐、可观测性。MVP 必须显式声明边界:

1. **内存/Token 预算(硬不变量)**:`MAX_MEMORY_CHARS=200KB`(`core/memory.py:30`)不可破。所有 `inbox_*.md` + `system/` + `hot/` + `warm/` 之和须 **<150KB**(给 timeline + 未来增长留 buffer)。每个分域 inbox 只留**近 500 行或 7 天(先到为准)**——见 §5.4/§6 的权威保留策略;已被 reconciler 消费的行就地删除,未消费却超界的溢出行归档到 `memory/warm/perception_archive_YYYYMM.md`(仅显式查询时读,绝不自动注入)。
2. **延迟 SLO**:per-source collect — P0 ≤5s / P1 ≤15s;dedup <1s;route <2s。**一个慢的 Lark 调用不能阻塞 outbox 投递**——adapter 在 `ThreadPoolExecutor` 里跑,带 per-adapter 超时,超时则产 `status='timeout'` 的 Signal。`perception-collect` 周期 P99 <10s。
3. **打分成本门(关键)**:`route.score` 默认 **`none`**(P1/P2 源);仅高价值源(phronesis、reports、mail)开 `llm`。**批量打分**:把本周期所有 due 源合进**一个** multi-source triage prompt,一次打完(摊薄成本)。每周期并发打分源 **上限 10**(可配),超出顺延下周期。每周期记 token 估算到日志。manifest 据此设默认:`holdings→score=rule`、`eigenflux_feed→score=llm`。
4. **吞吐/配额**:飞书群 API 配额 ~2000 req/天;N 群 ≈ 2N req/天;MVP 预算 **≤100 群**。每源带 `quota_cost`,当日预算将超则 skip+log。
5. **可观测性**:每源每周期记 `{source_id, ts, status:'collected|skipped|error|timeout', signals_count, bytes, cursor_checkpoint}` 到 `perception_state.jsonl`;提供运维 CLI `python3 -m core.perception status [--source <id>]` 打印 `source | last_run | signals_today | errors_recent | circuit_status`。

---

## 4. 架构总览

一个 **感知层(Perception Layer)**:配置即信源,一条通用管线,三个落地区。

```
                        sources.yaml  (声明式 registry —— 加源=加配置)
                              │   ← config.py:load_perception_sources() 读取
        ┌─────────────────────┴──────────────────────┐
        │   core/perception.py  (通用 runtime)         │
        │                                             │
   [COLLECT]   每个 source 按 type 动态 import           │   adapters (sources/*.py):
   per-source  sources.<type>.collect(cfg,state)        │   lark_chat · lark_mail · lark_doc
   并行+超时    → 产出 list[Signal] + 新 state            │   lark_base · lark_minutes · lark_calendar
        │      (增量 last_ts/cursor)                    │   file_watch · git_repo · claude_sessions
   [NORMALIZE] → 校验统一 Signal 信封                     │   http_poll(行情/RSS) · cli_stream(EF) · …
        │                                             │
   [DEDUP]     全局 seen-store(perception_seen.jsonl)   │
        │       主键 event_id;跨源 content_hash 聚簇      │
   [ENRICH]    entity_resolve 解析发件人 → 已知身份       │
        │                                             │
   [SCORE]     批量 LLM 打分(可关) —— 复用 EF 分级        │
        │                                             │
   [ROUTE]     push / fyi / hold / silent              │
        │                                             │
   [GATE]      quiet-hours 批延(墙钟)+ urgent 直推       │
        │                                             │
   [PERSIST]   perception_delivery.jsonl(审计)+ seen   │
        └──────────────┬──────────────────────────────┘
                        │  三个落地区(= 感知)
        ┌───────────────┼────────────────────────────────┐
        ▼               ▼                                 ▼
  ① 感知缓冲           ② Reconciler(泛化版)              ③ 出口桥
  system/inbox_*.md    source-agnostic UPDATE/REPLACE     heartbeat_outbox.jsonl
  (无条件注入 →        → 落到 warm/ 项目文件(durable)     → 前台下一轮可见的卡片
   下一回合即感知)      (去重/消矛盾就地, 响应式触发)      + 敏感度出口防护:
                                                          对外任务拿到的是脱敏视图
   状态节点(jsonl): perception_state(checkpoint) · perception_seen(去重) · perception_delivery(审计)
```

**关键集成决策(Option B —— 零 `heartbeat.py` 改动)**:
- heartbeat runner **不再为每个源注册任何东西**。在 `HEARTBEAT.md` 加**一个** Tier-0 任务 `perception-collect`(interval=10s):pre = `python3 -m core.perception collect-due`(内部按 registry 各源 effective interval 判断谁 due),post = `python3 -m core.perception route`。**所有 per-source 调度、EMPTY_RETRY、circuit 状态都活在 `core/perception.py` + `heartbeat_state.json['perception_sources'][source_id]` 里**,`heartbeat.py` 的 `TIER0_TASKS`/`PRIORITY_TASKS`/`EMPTY_RETRY_DELAYS` 类常量**不改**。这样 10m phronesis、30m git 的不同间隔都装进 registry,不污染 heartbeat 任务常量。
- EigenFlux **不再是特例**,而是这套管线的**参考实例**(把现有 `_ef_delivery.py` 的分级/批延抽到 `core/perception_route.py`,EF 变成 registry 里 `type: cli_stream` + `type: http_poll` 的几条配置)。

**审计轨(Audit Trail)**:`[PERSIST]` 写 `memory/system/perception_delivery.jsonl`,schema `{ts, source_id, source_type, event_id, score, action:'push|fyi|silent', reason, seen_before:bool, delivery_ts}`,保留 30 天后归档到 `perception_delivery.bak.jsonl`,供 self-diagnostic 消费(查重复失败/see-but-ignore 模式)。详见 §8.2。

---

## 5. 核心契约(本 PRD 的心脏)

### 5.1 Signal 信封(pre→post 的类型化契约)

每个 adapter 的 `collect()` 产出 `Signal` 列表(`list[dict]`,符合 §5.4 的 `Signal` TypedDict;不是类实例)。这是 §3.3 缺的"统一事件身份"和 §3.8 缺的"共享解析契约":

```python
Signal = {
  "source_id":    "phronesis",          # registry 里的唯一 id(必填)
  "source_type":  "lark_chat",          # adapter 类型(必填)
  "event_id":     "om_xxx",             # 源内稳定唯一(必填;Lark=message_id)。快路径去重主键
  "content_hash": "sha256(title[:80]+body[:100])",  # 必填。跨源同事件聚簇键(= §8.0 的 CLUSTER_KEY,单一权威公式)
  "ts":           "2026-06-09T17:10:00+08:00",  # 必填,ISO8601 带时区
  "actor":        {"raw": "ou_abc", "resolved": "梁琛奇", "ref": "team.md#liang"},  # 必填,经 entity_resolve
  "sensitivity":  "internal",           # 必填,枚举 public|internal|private|secret(决定出口策略)
  "title":        "PGC 供给侧 X 卡住了",  # 必填
  "summary":      "一行,用于卡片/去重簇",  # 必填
  "body":         "归一化后的正文(≤2KB,可空串)",  # 截断到预算;可为 ""
  "url":          "https://…",          # 可点源链接,可为 ""
  "payload":      { … }                 # adapter 专属原始体,二段深挖用。
                                        # NOTE: [SCORE]/[ROUTE] 的 LLM 调用【不含】payload
                                        # (只有 title/summary/body 计入 token)。adapter 自控大小
                                        # (建议 <50KB/signal),超大原始数据留指针不内联。
}
```

**必填字段**:`event_id, source_id, source_type, ts, title, summary, body, actor, sensitivity, url, content_hash, payload`——除 `body`/`url` 可为空串外均不得为空。`content_hash` 是 Signal 契约的一部分(**不是** §8 才出现的旁路机制),保证跨源去重在信封层就有依据。

### 5.2 Source manifest(声明式 registry —— 加源就是加这一块)

放在 `sources.yaml`(或 `jarvis.yaml` 的 `perception.sources:` 段;加载见 §5.4 / `config.py:load_perception_sources()`)。**新增一类信息 = 新增一个 source 块,零代码**:

```yaml
perception:
  defaults:                      # 全局默认,各 source 可覆盖
    waking_hours: [8, 23]
    quiet_hours: true
    sensitivity: internal
    score: none                  # 默认不打分(成本门, §3.9.3);高价值源显式开 llm
  sources:
    # —— 把今天写死的 Phronesis 泛化成一条配置 ——
    - id: phronesis
      type: lark_chat
      enabled: true
      label: "Phronesis 团队群"
      collect: { chat_id: "oc_xxxxxxxx…", exclude_self: true }
      schedule: { interval: 10m, priority: true }
      route:   { score: llm, tiers: [push, fyi, silent] }
      perceive: { buffer: system/inbox_team.md, consolidate: true }

    # —— N 个群只是多几条 ——
    - id: xvc_group
      type: lark_chat
      collect: { chat_id: "oc_…", exclude_self: true }
      schedule: { interval: 15m }
      perceive: { buffer: system/inbox_team.md }

    # —— 团队成员 DM(今天前台只接 @提及)——
    - id: team_dms
      type: lark_chat
      collect: { dm_with: ["梁琛奇", "文远"], resolve_names: true }
      # 名字解析 fallback:某名歧义/解析不到 → adapter 记 WARN、跳过该名(不 fail 整个源)、
      # 把未解析名写进 perception_state.jsonl,供改配置。

    # —— "任何 report 的改动"(用户明确点名)——
    - id: reports
      type: file_watch
      label: "Ship report / handoff / 诊断文档"
      collect:
        globs: ["~/Desktop/jarvis/*REPORT*.md", "~/Desktop/jarvis/*HANDOFF*.md",
                "~/Desktop/jarvis/repos/pascal-jarvis/docs/*.md"]
        on: change            # mtime+hash 检测,只在变更时产 Signal
      schedule: { interval: 30m }
      perceive: { consolidate: true }     # 改动直接进 reconciler

    # —— 飞书邮件(Phase 2,最敏感)——
    - id: lark_mail
      type: lark_mail
      collect: { folders: [inbox], unread_only: true }
      sensitivity: private    # → 不进对外任务视图(§6)
      route: { score: llm, tiers: [push, fyi, silent] }

    # —— 会议妙记(每次会后自动生成纪要+待办)——
    - id: minutes
      type: lark_minutes
      collect: { since: last_run, with: [summary, todos, chapters] }
      perceive: { consolidate: true }

    # —— 行情/持仓(今天 portfolio 是静态 memory)——
    - id: holdings
      type: http_poll
      collect: { tickers: ["恒生科技", "大成匠心", "华宝甄选黑翼"], market_calendar: true }
      schedule: { interval: 30m, waking_hours: [9, 16] }
      route: { score: rule }   # 规则打分:只在 ±阈值 / NAV 变动时 push,不耗 LLM

    # —— EigenFlux 收敛成 registry 里的两条(参考实例)——
    - id: eigenflux_feed
      type: cli_stream
      collect: { cmd: "eigenflux feed poll", enrich: feed_get }
      route: { score: llm, tiers: [push, fyi, hold, silent], research_queue: true }
```

**多源同事件去重示例**:一场会议可能同时经 `lark_mail`(会议通知)+ `lark_calendar`(日程)+ `lark_minutes`(会后纪要)三源到达。三条 Signal 的 `event_id` 各不同,但 `content_hash`(标题+正文前缀哈希)在 ±2h 窗口内聚簇 → 只惊动一次(§8)。

字段语义:
- `type` → 选哪个 adapter(动态 import `sources.<type>`,见 §5.3)。
- `collect` → adapter 专属采集参数(群 id / glob / ticker / cmd…)。
- `schedule.interval / waking_hours / priority` → **把今天硬编码的作息门、优先级变成 per-source 配置**(修 §3.2)。
- `route.score`(`llm|rule|none`)+ `tiers` + `research_queue` → **复用 EF 分级**(修 §3.8);默认 `none`(成本门)。
- `sensitivity` → 出口策略(修 §3.4)。
- `perceive.buffer / consolidate / dedup` → 落到哪个感知缓冲、是否进 reconciler、去重策略。

### 5.3 Adapter 接口与自动发现

**接口(每个 type 实现一个纯函数)**:

```python
# sources/<type>.py
def collect(cfg: dict, state: dict) -> tuple[list[Signal], dict]:
    """增量采集,幂等。
    Args: cfg(sources.yaml 里的 source 块), state(该源的持久化状态)
    Returns: (signals, new_state)
    Error handling: 绝不 raise。auth/network 失败时返回 ([], state),并置
                    state['error_type'] = 'auth'|'network'|'timeout'|'rate_limit'|'crash'。
    Idempotency: 同 state 调两次产同样 signals。daemon 重启后从
                 [last_ts - 5min, now] 重采(容忍时钟漂移/网络延迟),
                 重叠部分交给全局 seen-store 去重。
    State: 增量(last_ts/cursor),非全量快照;state 损坏/丢失时做一次
           有界 full rescan,让 seen-store 去重重叠。
    """
```

**自动发现(零代码加源的技术保证)**:perception runtime **不维护硬编码 adapter 注册表**。`sources.yaml` 写 `type: lark_chat` 时,运行时动态 `import sources.lark_chat` 并调其 `collect()`;模块不存在则 pre-script **大声报错**(fail-early)。由此加源分两种:
- **(a) 复用已有 adapter 类型**(如再加一个群):**仅改 `sources.yaml`**,零代码。
- **(b) 全新源类型**(如某新 API):加 `sources/<type>.py` 实现 `collect()` + 一条 `sources.yaml`,**无任何 `core/heartbeat.py` 改动**。

**模块布局**(净新增):
```
sources/
  __init__.py        # get_adapter(type) → dynamic import
  lark_chat.py       # collect(cfg, state)
  lark_mail.py
  file_watch.py
  git_repo.py
  claude_sessions.py
  cli_stream.py      # EigenFlux 参考实例
  http_poll.py
core/perception.py        # runtime: 调度/采集/去重/打分/路由/落地
core/perception_route.py  # 从 _ef_delivery.py 抽出的通用分级+quiet-hours
```

### 5.4 数据模型与持久化(统一 schema 参照)

所有状态/缓冲/审计文件**集中定义一处**,防止 adapter 各发明各的格式(全部净新增):

| 工件 | 路径 | Schema | 策略 |
|---|---|---|---|
| **Signal** | (内存,§5.1) | TypedDict:`source_id, source_type, event_id, content_hash, ts(ISO8601), actor, sensitivity∈{public,internal,private,secret}, title, summary, body≤2KB, url, payload` | normalize() 校验 |
| **Per-source 状态** | `heartbeat_state.json['perception_sources'][source_id]` | `{last_ts:ISO8601, last_cursor:opaque, error_type:null|str, error_count:int, last_error_time:int, last_run_ts:int, adapter_state:{}}` | load/save 在 `core/perception.py`;原子写 |
| **Seen-store** | `memory/system/perception_seen.jsonl` | `{event_id, source_id, content_hash, action:'deliver'|'dedup', ts}` | append-only,上限 10K 条,>30d 启动时清 |
| **感知缓冲** | `memory/system/inbox_<domain>.md`(team/market/ops) | Markdown,每条 `### <event_id> | <source> | <actor> | <ts> | <sensitivity> | <action>` + 正文 | **权威保留策略**:留近 **500 行或 7 天**(先到为准);已被 reconciler 消费的行就地删除,未消费的溢出行归档到 `warm/perception_archive_YYYYMM.md`(仅显式查询时读) |
| **投递审计** | `memory/system/perception_delivery.jsonl` | `{ts, source_id, source_type, event_id, score, action, reason, seen_before, delivery_ts}` | 30 天后归档 |
| **配置** | `sources.yaml` 或 `jarvis.yaml#perception` | `perception.defaults + sources[].{id,type,collect,schedule,route,sensitivity,perceive}` | per-adapter 校验 |

**Registry 加载**(`core/config.py` 新增方法):
```python
def load_perception_sources(self) -> list[dict]:
    sources = self._raw.get('perception', {}).get('sources', [])
    if sources:
        return sources
    sources_file = self.jarvis_dir / 'sources.yaml'
    if sources_file.exists():
        with open(sources_file) as f:
            data = yaml.safe_load(f) or {}
        return data.get('perception', {}).get('sources', [])
    return []
```
(已核对 `config.py:7` 的 `_DEFAULTS` 今天**无** `perception`/`secrets` 键——本节为新增。)

### 5.5 端到端 worked example(一条信号走完全程)

**正常件**:徐小刚 2026-06-09 17:10(上海)在 Phronesis 群发"PGC供给侧 X卡住了, @Reviewer审一下"。

| 步 | 动作 | 具体值 |
|---|---|---|
| 1 COLLECT | `sources.lark_chat.collect()` 增量拉取 | `Signal{event_id=om_aabbcc11, source_id=phronesis, ts=2026-06-09T17:10:30+08:00, actor.raw=ou_xugang, sensitivity=internal, title="PGC供给侧 X卡住了", body="…@Reviewer审一下", content_hash=sha256(...)}` |
| 2 NORMALIZE | 校验信封必填字段 | 通过 |
| 3 DEDUP | 查 `perception_seen.jsonl` PRIMARY=(phronesis,om_aabbcc11) | 未命中 → 继续 |
| 4 ENRICH | `entity_resolve` 解析 actor | `actor.resolved="徐小刚", ref="team.md#xugang"` |
| 5 SCORE | 批量 triage,LLM 给分 | `score=1, reason="供给侧进展信息,与 PGC 相关"` |
| 6 ROUTE | score 1 → `fyi` 档 | tier=fyi |
| 7 GATE | 17:10 不在 quiet hours | 直接放行 |
| 8 PERSIST | 追加 delivery 审计 + seen | `perception_delivery.jsonl += {...action:fyi}`;`perception_seen.jsonl += {...action:deliver}` |
| 9a 感知缓冲 | 追加 `inbox_team.md` | 下一次 Claude 调用(前台/heartbeat)经 `load_tiered_memory` 即感知 |
| 9b consolidate | `perceive.consolidate=true` → 进 reconciler 队列 | 攒够 K 条或 21–22h 触发,可能产 `→ UPDATE: warm/projects.md: PGC 供给侧 X 受阻` |
| 10 用户感知 | Pascal 18:00 打开 Jarvis → 上下文里已有;若发于 23:30 → 持有,并入次日 08:30 晨间摘要卡 | — |

**对照(私密件)**:同流程但 `sensitivity=private`(如 `lark_mail`)→ 第 9a 步落到 `system/inbox_private_mail.md`;当 `eigenflux-publish` 等对外任务以 `load_tiered_memory(purpose='outbound')` 取上下文时,该 inbox **被跳过**,邮件内容在每一跳都不进对外视图(§6)。

### 5.6 术语表(Glossary)

| 术语 | 含义 |
|---|---|
| **Signal** | 在途的类型化信封(§5.1) |
| **event_id** | 源内稳定唯一的消息标识(去重快路径) |
| **content_hash** | `sha256(title[:80]+body[:100])`,跨源同事件聚簇键(§8.0 复用,单一权威公式) |
| **source_id / source_type** | 配置级标签 / adapter 类型 |
| **adapter** | `sources/<type>.py`,实现 `collect()` 的类型专属采集器 |
| **感知缓冲(inbox_*.md)** | 落地区①,被 `load_tiered_memory` 自动注入 |
| **consolidate** | 走 reconciler 把 durable 事实落到 `warm/` 的路径 |
| **sensitivity** | `public/internal/private/secret`,出口闸 |
| **seen-store** | `perception_seen.jsonl`,跨源去重日志 |
| **dedup_window** | content_hash 聚簇的 ±2h 时间窗 |
| **quiet_hours / urgent** | 墙钟投递闸 / 绕过闸的紧急标记 |
| **purpose** | `inbound`(全量)vs `outbound`(脱敏)的注入模式 |
| **checkpoint state** | per-source 的 `last_ts/cursor` |

---

## 6. 感知与落地模型(怎么让"灌进来"真的变成"感知")

三个落地区,全部基于已验证机制:

**① 感知缓冲 `memory/system/inbox_*.md`**
新信号的"鲜货"surface。被 `load_tiered_memory()` 无条件注入 → 下一回合前台/heartbeat 即感知(§1.2)。按域分文件(`inbox_team.md` / `inbox_market.md` / `inbox_ops.md`)。**权威保留策略**:滚动保留**近 500 行或 7 天**(先触发者为准)。两种离场:① 已被 reconciler 消费的行就地清理(`→ UPDATE` 删行);② 未消费却已超 500 行/7 天边界的溢出行,归档到 `warm/perception_archive_YYYYMM.md`(仅显式查询时读,绝不自动注入)。memory-consolidate 校验清理防 orphan。**这是今天 `cross_session_digest.md` 已在用的模式,只是泛化。**

**② Reconciler(泛化 `memory-consolidate`,响应式)**
把"3 个输入概念"改成"消费感知缓冲里所有 `consolidate:true` 的 Signal"。沿用 `→ UPDATE:` / `→ REPLACE:` 指令契约(`memory_consolidate_post.py` 落地逻辑不动),让 durable 事实就地落到 `warm/` 项目文件、去重消矛盾。**触发器**(修 §3.5):
```python
RECONCILE_TRIGGERS = {"min_buffered_items": 10, "time_since_last_hours": 1, "consolidate_now_file": True}
# 满足任一即跑:某 consolidate:true 源攒够 ≥10 条新信号 / 距上次 >1h / 存在 .consolidate_now 哨兵
# 21–22h 仍保留为 fallback。改 memory_consolidate_pre.sh 额外检查 backlog 文件以提前触发。
```
**"轻量 reconcile" 定义**:仍跑 LLM 的 UPDATE/REPLACE 解析,但**只过新信号 backlog 增量**,不重扫整棵 memory 树。

**③ 出口桥 `heartbeat_outbox.jsonl`**
用户可见卡片走这里 → 前台下一轮可见(`build_recent_turns` 读末 10 条;>3 天或 >100 条归档到 `heartbeat_outbox_archive.jsonl` 防无界增长)。复用 `core/card.py`。

**敏感度 / 出口防护(修 §3.4)**
给 `load_tiered_memory()` 加 `purpose` 参数(`core/memory.py:40` 改签名):
```python
def load_tiered_memory(memory_dir, purpose: str = "inbound") -> str:
    # 加载 system/ 时:purpose=="outbound" → 跳过名字以 inbox_private_/inbox_secret_ 开头的文件
    #                (以及 warm/* 标 private 的)
```
**三阶段向后兼容迁移(避免泄露窗口)**:
1. **加参数,default='inbound'**——零行为变化,全部既有调用点自动安全。
2. **验证所有内部任务无改动**(parity)。
3. **最后**才把对外任务(`eigenflux-publish`/auto-reply)切到 `purpose='outbound'`。
> BEFORE/AFTER 明示:**今天 = 每次调用无条件全量注入**(§3.4 的泄露风险);**改后 = purpose 门控**。一个尚未迁移的对外任务**fail-safe**(过滤没开 = 不额外过滤,而非泄露新数据)→ 零 regression 风险。调用点:`build_system_prompt()` 默认 `inbound`;`heartbeat.py:231` claude_call 传 `inbound`;`perception.route()` 给对外任务建上下文时传 `outbound`。

**secret 级存储**:`sensitivity='secret'` → 全文写 `memory/system/perception_secret_safe.md`(**永不**为 outbound 加载、**永不**写审计日志);索引指针写 `perception_secrets_index.md`(仅 inbound)列 `|ID|Date|Source|Type|`。访问靠 `python3 -m core.perception get-secret --id secret_001`(仅用户主动)。`store_secret_signal()` 返回 `secret_id=f'secret_{uuid4().hex[:8]}'`。Phase 2:静态加密(`.md.gpg`,解锁才看)。

**quiet-hours 形式化**:23:00–08:00(本地)累积的 silent/hold 信号**不投递**,直到 08:30 墙钟 flush(单一 08:30–09:00 窗口);08:30 前的多批合并为**一张**摘要卡;08:30 后用即时路由;`urgent` 完全绕过。**hold backlog 带 checkpoint 持久化**,崩溃在 flush 前 → 重启后重投(经 seen-store 幂等)——修 §3.7 "永久丢失"。

---

## 7. 信源目录(直接回答"把一切能拿到的灌进来")

按 adapter 归类 + 优先级。**P0 = MVP 必接;P1 = 紧随;P2 = 视价值**。

### 飞书表面(已装 ~30 个 lark-* skill,但 client 今天只包了 IM+日历)
| 信源 | adapter | 优先级 | 说明 |
|---|---|---|---|
| 其他团队群(N 个) | `lark_chat` | **P0** | 泛化 phronesis;config 列 chat_id |
| 团队成员 DM | `lark_chat` | **P0** | 今天前台只接 @提及,漏私聊 |
| 飞书邮件 | `lark_mail` | P1 | 创始人最大的未监控表面;`sensitivity: private`,须 MVP Test 4 通过后接 |
| 会议妙记/VC 纪要 | `lark_minutes`/`lark_vc` | P1 | 会后自动纪要+待办,巨大感知缺口 |
| 飞书文档/wiki 改动+评论 | `lark_doc`/`lark_wiki` | P1 | 团队把最 durable 的东西写在文档里 |
| 多维表格(Base) | `lark_base` | P2 | PGC 运营/CRM 结构化数据 |
| 飞书任务 | `lark_task` | P2 | 与自有 todos.md 并行,需对账 |
| 团队/共享日历 | `lark_calendar` | P2 | 今天只同步本人主日历;含跨时区归一 |
| 审批/OKR/考勤 | `lark_approval/okr/attendance` | P2 | 组织态,目前完全空白 |

### 非飞书
| 信源 | adapter | 优先级 | 说明 |
|---|---|---|---|
| **report 文件改动** | `file_watch` | **P0** | 用户明确点名"任何 report 的改动";今天无 watcher |
| 多 repo git | `git_repo` | **P0** | 泛化 `repos-sync` |
| 跨 Claude session | `claude_sessions` | **P0** | 泛化 `cross-session`(主工作上下文) |
| EigenFlux(流+feed+PM+好友) | `cli_stream`/`http_poll` | **P0** | 收敛为 registry 参考实例,功能不回退 |
| 行情/持仓 | `http_poll` | P1 | portfolio 今天是静态;含市场日历(修周末误报,见跨session记忆 `system/todos`) |
| web/news/RSS 常驻 | `http_poll` | P1 | 今天只有 EF 网络信号 + YouTube;无通用 web 哨兵 |
| 健康/可穿戴/运动 | `health` | P2 | "变强的基础是先疗愈"是 agent 身份核心,却零实时健康输入 |
| 联系人实时刷新 | `lark_contact` | P2 | 今天 contacts.jsonl 静态;新人/改名解析不到 |

---

## 8. 横切机制

### 8.0 跨源事件身份与去重(修 §3.3 的"双发"核心 bug)
- **PRIMARY_KEY = (source_id, event_id)** 精确匹配(快路径:Lark message_id、mail message_id)。
- **FALLBACK CLUSTER_KEY = 信号的 `content_hash`(= `sha256(title[:80]+body[:100])`,§5.1 定义,单一权威公式)** 在 **±2h 窗口**内匹配跨源同事件。
- **算法**:每条信号 → ①查 PRIMARY 是否在 `perception_seen.jsonl`;②命中 → skip;③否则算 CLUSTER_KEY 扫近窗;④簇命中 → 标 duplicate(产出但不路由,score=silent);⑤把 PRIMARY 追加 seen。
- **集成点**:heartbeat `perception-collect` 采集后写 seen;**前台 `bot.sh` 的 @提及 handler 在收到消息时也写** `{event_id=message_id, user_id, ts}` 到 seen-store → 后续 heartbeat 拉取时已 seen,自动去重。**这样前台不需要具备完整 ingest 能力**,就解决了"同一条 Phronesis 消息到达两次"。
- **GC**:seen 条目 >30 天启动时清;append-only,上限 10K。

### 8.1 失败隔离与错误分类
扩展 `core/task_protocol.py:CircuitState`,加 `error_category ∈ {auth, network, timeout, rate_limit, crash}`。恢复策略**按类分化**(今天一刀切):
- **auth** → 先试一次重登,再不行则 1×/天重试 + log。
- **network/timeout** → 指数退避,连失 3 次才 trip circuit。
- **rate_limit** → 退避到下个 quiet/quota 窗口(尊重 `Retry-After`)。
- 连失 5 次 → 禁用该源 1h;首次成功即 reset。**adapter 崩溃被 subprocess 隔离捕获**,记为该源错误,绝不拖垮整个周期。
- 告警阈值:某源 >2h 无成功运行 → log WARN(不 trip)。Phase 2:发 Lark 卡"mail adapter 连失 3 次,circuit open,45min 后重试"。

### 8.2 审计与合规
被记录的工件 + 保留:
- `perception_state.jsonl`(每周期每源健康)、`perception_seen.jsonl`(去重)、`perception_delivery.jsonl`(路由/投递审计)、`memory_change_audit.jsonl`(每次 `memory/*/*.md` 被改时记 `{ts, file, old_hash, new_hash, directive}`)。
- 保留 30–90 天后归档 `memory/timeline/archive/`。
- **隐私不变量**:`sensitivity='secret'` 内容**绝不**写入这些日志(只在内存计数)。
- 运维审计查询 CLI:"lark_mail 在 2026-06-08 灌了什么?" / "两日间 consolidate 解了哪些矛盾?"——解锁调试与信任核验(私密数据是否进过 memory blob)。

### 8.3 与自进化(harness-evolve)+ 意图(intentions)的交互
非冲突反馈环,各阶段幂等、由 seen-store/consolidation checkpoint 把关:
**perception 灌信号 → consolidator 问 Claude"这要不要更新 open_threads/profile?"→ 产 `→ UPDATE` 指令 → harness-evolve 读到更新的事实 → 可能提一条 intention → `core/intentions.py` 在到点触发延时动作。**
worked trace:① 邮件到 → 感知缓冲;② consolidator 产 `→ UPDATE: open_threads.md`;③ 自进化读到;④ 提 intention"1h 后跟进 CEO";⑤ intentions 在 1h 触发提醒。**职责清晰:perception 路由、evolution 提议、intentions 执行**——无循环。

---

## 9. 复用映射(每个设计点都落到已有原语)

| 设计元素 | 复用 | 路径 |
|---|---|---|
| 通用管线骨架 | EF `Poll→Enrich→Score→Route→Gate→Persist` | `eigenflux_feed_pre/post.py` |
| 分级+批延(抽到 `perception_route.py`) | `_ef_delivery.py` 的 `in_quiet_hours/should_flush/hold/drain` | `tasks/_ef_delivery.py` |
| 注册 substrate | `HEARTBEAT.md` pre/post/interval + mtime 热重载 | `core/heartbeat.py:71` |
| LLM→卡片边界(必走) | `parse_json_response`/`looks_like_error`/`atomic_write` | `core/safety.py` |
| 身份解析 | `entity_resolve.py`(加 lark-contact 实时刷新) | `scripts/entity_resolve.py` |
| 输出+前台可见 | `build_card`/`build_rich_card` + outbox 桥 | `core/card.py`、`heartbeat_outbox.jsonl` |
| 感知 substrate | `load_tiered_memory`(+ purpose 参数 + 启用未用的 `context_files` 做选择性注入) | `core/memory.py`、`core/task_protocol.py:22` |
| durable 落地 | `→ UPDATE/REPLACE` 指令 | `memory_consolidate_post.py` |
| 失败/调频 | `CircuitBreaker`/`effective_interval`(扩 error_category) | `core/task_protocol.py` |
| 延时跟进 | `create_intent` | `core/intentions.py` |
| 去重 seen-set 模式 | EF 的 load/save_seen(泛化为全局) | `core/ef_stream.py` |

---

## 10. 迁移路径(增量,不大爆炸)

| 步 | 内容 | 工时估 |
|---|---|---|
| 1 | **建 runtime + registry,空跑**:`core/perception.py` + `sources/` + `sources.yaml` + `perception-collect` 任务。registry 初始为空,不影响现状 | 5–8h |
| 2 | **Phronesis → 配置**:改成 registry 一条 `lark_chat`,删硬编码。**验收:行为不回退 + 加第二个群只需加一条配置**(PRD 成立的证明) | 3–4h |
| 3 | **EF 作为参考实例收敛**:分级/批延抽到 `perception_route.py`,EF 变 registry 配置;严格保 parity(EF 全功能必须 native、测通再上线——跨session记忆 `project_eigenflux_native_parity`) | 4–6h |
| 4 | **折叠 cross-session / repos-sync / calendar 进 registry**,老 pre/post 退役 | 6–8h/源 |
| 5 | **接新源**:reports(file_watch)、mail、minutes…每个"加配置 + 写/复用一个 adapter" | 2–3h/adapter |

**MVP 总工时 ~25–35h。** 每步遵守"写路径要真跑、断言响应"(跨session记忆 `feedback_verify_write_paths`):外部写(邮件已读、好友处理)**真执行 + 断言响应**,LLM 产 id 一律 `str()`。

### 10.1 回滚安全
每步记:(a) 改了什么状态;(b) 怎么回退;(c) 数据丢失风险;(d) 回退耗时。
> **范例(Phronesis 迁移)**:新 `lark_chat` adapter 失败时 → ① `sources.yaml` 里 disable;② `HEARTBEAT.md` 重新启用旧 `phronesis_monitor`;③ 删 `memory/system/inbox_team.md`(仅 staging,无丢失);④ 旧 `.phronesis_last_ts` checkpoint 仍有效。**<5min 回退,零数据丢失。**
通则:seen-store append-only(可丢)、inbox 是 staging(可清)、checkpoint 须重新同步。

---

## 11. 分期与验收(可复现测试矩阵)

### MVP(P0)
- runtime + Signal 信封 + registry + `perception-collect` 任务。
- adapter:`lark_chat`、`file_watch`、`git_repo`、`claude_sessions`、`cli_stream`(EF)。
- 跨源 seen-store 去重;感知缓冲 `system/inbox_*.md`;敏感度出口视图(三阶段第 1–2 步,默认 inbound 不变行为)。

| 测试 | 前置 | 步骤 | 通过判据 | 类型 |
|---|---|---|---|---|
| T1 配置→感知 | 加一条 `lark_chat` 源 | 群里发条消息 | 2 周期内 `grep inbox_team.md` 出现该消息(带发件人+时间戳) | 手动 ~10min |
| T2 Phronesis parity | 新旧管线并跑 | 发 3 条测试消息 | 输出逐条 diff 一致、零丢失 | 手动/CI |
| T3 跨源去重 | 一条同时被前台 handler + phronesis 拉取可达的 @提及 | 跑 5 周期 | outbox 里**恰好 1 次** | CI(`phronesis_monitor_post.py` 加计数断言比对 seen-store) |
| T4 敏感度门控 | 发一封 private 邮件 | 确认 inbound memory 可见 → 触发 `eigenflux-publish` | 对外输出里**不含**邮件内容 | 手动/CI |

### Phase 2(P1)
`lark_mail`(**须 T4 通过后才上**)、`lark_minutes`、`http_poll`(行情,**须先实现 rate-limit/quota 守卫**)、web/RSS 哨兵;响应式 reconciler;脏标记降延迟。
判据:妙记自动捕获 <5min;consolidation 滞后 <6h。
**依赖**:隐私模型(T4)+ seen-store 是 P2 的前置;adapter 本身可在 §5.3/§5.4 接口冻结后并行开发。

### Phase 3(P2)
`lark_doc/base/task/calendar`、health、contacts 实时;选择性 memory 注入(`context_files`)以控 token。
判据:文档变更感知 <10min;health 接入 <30min;联系人刷新 <1 周期。

---

## 12. 风险与缓解

| 风险 | 缓解 |
|---|---|
| **噪声/注意力稀释(最关键)**:5–10× 入流灌进 200KB 全量 memory | 分级路由提到模块级(score≠投递)+ per-source `fyi` 配额 + 需求锚定门控 + inbox 留近 500 行/7 天(消费即删、溢出归档,§5.4/§6);宁静>噪声 |
| **私密泄露到对外**:全量 memory 进 publish/auto-reply | `sensitivity` + `purpose=outbound` 出口视图;`secret` 永不进上下文/日志 |
| **敏感度出口适配风险(中)**:今天无 purpose 参数,要改全部调用点 | 三阶段迁移(§6):先加参数 default=inbound(向后兼容)→ 验内部 parity → 最后切对外任务。中途误启用 fail-safe(不过滤而非泄露),零 regression |
| **consolidation 滞后 24h** | reconciler 改 source-agnostic + 响应式触发(§6),21–22h 仅作 fallback;窗口内矛盾**记录不静默**,可手动 force-run |
| **同事件多源重复** | 全局 event_id + content_hash 簇去重;前台与 ingest 共享 seen-store(§8.0) |
| **打分成本爆炸**:N 源每周期 LLM | 默认 `score=none`;高价值源才 `llm`;批量单 prompt;并发上限 10/周期(§3.9.3) |
| **宕机丢信号** | per-source checkpoint 从 `last_ts-5min` 补采;quiet-hours backlog 持久化 + checkpoint,重启重投(seen 幂等) |
| **某 adapter 崩溃/毒信号** | subprocess 隔离 + circuit 不拖垮周期;毒 JSON 经 `parse_json_response` 捕获丢弃记日志,不重投 |
| **迁移回退风险** | 每步等价验证 + 写路径真跑(§10.1 回滚);EF 严格 parity |

### 12.5 成功指标(KPI)
| 指标 | 现状基线 | 目标 | 测量法 |
|---|---|---|---|
| 失明事件/月("我居然不知道 X") | ~5–8 | ≤1 | 人工 triage |
| 感知时延中位(事件→memory 可见) | 12–24h | P0 <2h | `event_ts` vs 首次 `memory_load_ts` |
| 配置→生效时延(加一个源) | 4–6h | <15min | 加 source 块到首次感知 |
| consolidation 滞后 P95 | 24h | <6h | reconcile 时间戳 |
| 跨源重复率 | ~10–20% | <2% | `1 - unique(event_id,content_hash)/total` |
| 私密漏到对外 | — | **0** | grep `perception_delivery.jsonl` 审计 |
| 覆盖信源数 | 6 | 15+(Phase 3) | registry 计数 |

**MVP 早赢验收**:(a) 加一个 Lark 群 → <5min 被感知;(b) 私密邮件绝不进 outbox 输出;(c) 双路 ingest 同事件 → outbox 恰好 1 次。

---

## 13. 决定与开放问题

**已拍板(2026-06-09):**
- **MVP 信源范围 = 其他群 + 团队 DM + report 改动 + 多 repo git + 跨 session + EigenFlux**。邮件等敏感源放 Phase 2,先把 `sensitivity`/出口防护打磨扎实(MVP T4 通过)再接。
- **交付 = 飞书云文档**(repo 内 `docs/prd_perception_ingestion.md` 保留为版本源)。

**仍待拍板(给 dev 临时 default,不阻塞实现):**
1. **敏感度默认** — 【临时 default:所有源 = `internal`】群/DM/文档/邮件一律 internal,除非配置显式 override;`secret` 仅用于手动标记的 credentials/.env 类。待 Pascal 确认:有没有群其实算 `public`(可进对外广播素材)?
2. **行情源** — 【临时 default:MVP 不接,Phase 2 补】待定用哪个数据源拉恒生科技/大成匠心/华宝净值(有偏好 API 否,还是我调研后定)。
3. **打扰阈值** — 【临时 default:`score>=1 → fyi` / `score==2 → push`,复用 EF 三级】待 Pascal 确认是否想更保守("几乎只 fyi、极少 push")。

---

## 附:调研方法与可信度

- 调研用 8 路并行 reader(workflow `jarvis-ingestion-prd-research`)深读 heartbeat/memory/EigenFlux/Lark/consolidation/task-framework 子系统 + 历史数据(git log 200、feedback 记忆、open_threads、longterm_digest),再过一道完整性 critic。
- 1 路(cross-session/repos/reports)因 socket 错误失败,该域由人工第一手精读 `cross_session_pre.sh`、`memory_consolidate_post.py`、报告文件位置补齐。
- **v2 定稿前经一轮对抗式多视角审查**(workflow `prd-adversarial-review-r1`:builder/feasibility、completeness-vs-ask、consistency-correctness、architecture-risk-nfr、prd-craft 五视角 + 综合),并对所有 `file:line` 断言做了代码核对——consistency critic 判定事实层 **clean**;builder/architect/PM 提出的阻塞项已逐条并入(adapter 接口、Option B 集成、purpose 三阶段迁移、registry 加载、模块布局、NFR、成功指标、测试矩阵、数据模型、worked example、去重算法)。
- **跨session记忆引用说明**:`project_self_evolving_repo` / `project_eigenflux_native_parity` / `feedback_verify_write_paths` / `feedback_llm_output_boundary` / `feedback_autonomous_memory_absorption` / `feedback_memory_design_truememory` 等位于 `~/.claude/projects/<jarvis-dir-slug>/memory/`(**非 repo 内文件**,repo-only 读者需到该目录查;相关规则已在正文内联)。
- 结论:**最该做对的是"声明式 source registry + EF 管线泛化 + 跨源去重 + 敏感度层"**;其余皆为实例化。
