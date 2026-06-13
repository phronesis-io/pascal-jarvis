# PRD：Jarvis 系统迭代 v2.0（查漏补全 · Intent 闭环 · Dashboard · 管理后台）

- 版本：v2.0（2026-06-13）
- 作者：Claude（基于 55-agent 全模块审计：10 子系统地图 + 8 维度交叉审计 + 对抗验证）
- 编号：延续 prd_interaction_quality.md（v1.0 用掉 REQ-01~29），本期 REQ-30 起
- 状态：**P0 全部上线 + P1 大部上线（2026-06-13）**
  - ✅ 已实施并上线：REQ-30~52、REQ-58、REQ-53（引擎件：failed 死路/日历 dedup/坏 datetime 拒绝/信封契约单源化/list 值修复——其中 list 值 str() 损坏项与多任务信封一并由 parse-failure 重构覆盖）、REQ-57（部分：EF 游标迁出 /tmp；fsync 助手未做）
  - ⏳ 留下期：REQ-54/55（dashboard engagement 页 + ops 页）、REQ-56（睡眠建模）、REQ-57（atomicio fsync 助手）、P2 路线图全部
  - 测试：523 → **638 全绿**（+115）；8 个 commit；重启上线见验证记录

---

## 0. 背景与数据基础

v1.0（6/11-12）解决了"交互质量与可靠性"的 29 项需求。本次审计（2026-06-13 凌晨，55 个 agent、~380 万 token、1054 次工具调用）对**全部模块**做了地图级走读 + 交叉审计 + 对抗验证，共产出 **244 项发现**（131 项来自子系统读者、113 项来自维度审计员），其中 8 项 P0/P1 bug 经独立对抗验证确认、0 项被驳倒。

### 核心数据结论（全部来自生产实测，2026-06-13 00:22-00:58）

| 指标 | 数值 | 含义 |
|---|---|---|
| 一次性意图触发后静默死亡率 | **50%**（18 executed vs 18 auto-expired） | 每两个承诺就有一个无声违约 |
| 闭环 follow-up 引擎累计派生次数 | **1 次，且该次也死了**（int_7a545cc10d__fu） | 闭环机器建成后从未真正转动 |
| 闭环问题覆盖率 | 13/84 moment（**15%**） | 创建侧大量 category='none' 直接绕过闭环 |
| cron 意图静默跳天 | 每日康复训练停 **6 天**、每晚intent复盘停 **2+ 天**、夜间深工**从未触发** | 安全网（夜审）本身就死在同一管道里 |
| cron 星期错位 | dow 0=周一 vs 标准 cron 0=周日，**全部周任务错一天** | 模型在 last_error 里自我诊断了却无人读（int_fb4fcab91d） |
| 多任务 JSON 信封解析失败 | 7 次/2 天，每次**丢弃全部任务输出但记成功** | 熔断器永远看不到这类失败 |
| Force trigger 全量触发 | 6/12 一天 **32 次**，每次激活全部 32 任务 | 周任务 7 小时跑两次；batch cap 每轮挤掉 18-19 个任务 |
| repos-sync pre 脚本超时 | **19/19（100%）**，水位监控却报"一切正常" | 合成 last_run 水印掩盖了真死状态 |
| Dashboard 死亡时长 | **23 天**无人察觉（5/21-6/12） | 全系统四份"该跑什么"清单各自漂移，没一份包含 :3457 |
| Dashboard /intentions 页 | **每次加载 500**（naive/aware datetime 相减） | 意图系统唯一可视化界面完全不可用 |
| Dashboard thinking/agent-calendar 页 | 文件存在但**从未注册路由**，生产 404 | 首页还挂着这两个死链 |
| Dashboard 写 API（9 个 POST/PATCH） | **全部 422**（FastAPI bare `request` 参数） | 27 个书签全部卡死 inbox，无法流转 |
| Admin "Stop Task" 按钮 | **杀不掉进程但删光活锁**（锁格式漂移） | 既无效又危险，可致并发 resume 写坏会话 |
| Admin "Run Now" 按钮 | 写了任务名但**无人读**，实际全量触发 32 任务 | 与 force trigger 风暴同源 |
| Admin EF Settings 保存 | 单选框匹配不上自由文本偏好 → **一键摧毁手调路由规则** | 写 user_settings.json，无版本控制无法恢复 |
| 自诊断任务（系统唯一健康告警） | 6/12 起进 SILENT_TASKS，**告警结构性静音** | prompt 承诺"always report"，输出永远到不了 Pascal |
| 备份 | 27 天静默失败（TCC）；memory/、jarvis.db **从未在备份范围内** | 系统最不可再生的数据零保护 |
| cancel_job | killpg 打到**整个 bot 进程组** | 用户取消一个任务 = 重启整个产品 |

### 用户原话锚点

- "希望你查漏补全……全面的检查，详细的检查各个模块"（6/13，本期立项）
- "去更新这个 Dashboard……还有管理后台……这两个都可以做得远远比现在更好"（6/13）
- "你甚至可以去想为什么 Intent 没有闭环，然后怎么办……什么策略"（6/13）
- "承诺必须闭环：静默违约是最伤信任的行为"（v1.0 产品原则 5——本次审计证明恢复代码本身在违反它）
- "一切从他的真实数据出发"（数据优先工作原则——本 PRD 每条需求都带生产实测证据）

---

## 1. 产品原则（v1.0 六条全部继承，新增三条）

7. **LLM 只产内容，不产状态**：任何状态转移必须由确定性代码持有；信封缺席本身就是确定性信号（manifest 对账），绝不能让状态机依赖一次完美的 LLM JSON 往返。
8. **"该跑什么"是数据不是代码**：组件清单（components.yaml）单一事实源，四个监督面（daemon/doctor/restart --status/self-diagnostic）消费同一份清单——清单外的组件不允许存在。
9. **告警路径必须独立于被告警对象**：自诊断的告警不得经过会吞掉它的同一条静音管道；Lark 告警必须有本机兜底（osascript）。

---

## 2. P0 需求（本期实施）

### 战线 A：Intent 闭环（核心交付）

#### REQ-30 执行确认改为 manifest 对账，信封缺席不再杀意图【LLM 只产内容不产状态】
- **问题**：mark_triggered 在 Claude 调用**之前**执行，而 mark_executed 依赖 Claude 回传完美 JSON 信封。四条路径跳过 post 脚本把意图永久卡死在 triggered：① 回复恰好是 HEARTBEAT_OK（而 HEARTBEAT.md:556 和 batch wrapper **指示** Claude 这么回——prompt 契约直接抵触状态机）；② 多任务信封解析失败；③ 单任务切片缺失 `continue`；④ 超时/__KILLED__/空响应。生产后果：50% 触发即死。
- **方案**：
  a) `intentions_pre.sh` mark_triggered 后原子写入 `data/.intention_inflight.json`（run_id、ids、attempt 计数）；
  b) `core/heartbeat.py` 新增 `ACK_REQUIRED_TASKS={'intention-check'}`：HEARTBEAT_OK 早退、kill、空响应、解析失败四条路径上，仍以 stdin=`__NO_ENVELOPE__` 调用该任务的 post 脚本；
  c) `intentions_post.py` 收到 `__NO_ENVELOPE__` 或不可解析输入时读 manifest 确定性对账：信封覆盖的 id 正常 mark；未覆盖的 id 按 REQ-31 重试策略处置；
  d) HEARTBEAT.md intention-check prompt 改为"必须返回覆盖每个 id 的信封，无话可说用 action:silent，**永不回 HEARTBEAT_OK**"；
  e) 删除 `_resolve_single_triggered_id` 猜测兜底（manifest 使其过时）。
- **验收**：模拟四条失败路径（HEARTBEAT_OK/解析失败/切片缺失/空响应），意图均不卡 triggered；信封正常时行为不变。✅ 单测覆盖

#### REQ-31 lifecycle_sweep：有界重试 + 违约必告知，废除"触发即过期"【承诺必须闭环】
- **问题**：`reset_stale_triggered` 把任何卡 triggered>10min 的过期 date 意图直接 expired——而 date 意图**触发那一刻就必然过期**（所以才触发），等于一次执行失败 = 永久死亡，零重试零通知。18/19 expired 全部死于此机制。受害者包括 Pascal 正在等的活线（int_bfc7d242de B站SESSDATA——而 cookie 其实已经到了）。
- **方案**：重写为 `lifecycle_sweep` 分级策略（新增 `attempt` 列）：
  ① 卡死>10min 且 attempt<3 且距触发<2h → 回 pending 重试（2h 宽限封死复活风暴）；
  ② attempt≥3 或距触发≥2h → expired + 把违约写入 `data/.intent_breach_queue.jsonl`，下一轮 intention-check 卡片告知 Pascal"我没能按时把「X」提醒出来，内容是：…，还需要吗？"（原 prompt 附上，提醒价值不丢失）；
  ③ 首扫即古董（距触发>24h，6/8 风暴类）→ 静默过期（保持现状）；
  ④ 闭环载体意图最终过期时仍走闭环轴转移（REQ-33）——提醒失败不代表事件没发生。
  每条路径 emit sched_event `intent_expired`/`intent_retry`（带 attempt）。
- **验收**：单次失败的意图在下轮重试成功；三连败的意图产生违约卡片；2026-01-01 类垃圾意图不复活。✅ 单测覆盖

#### REQ-32 cron 意图补课制 + 星期对齐【正确性】
- **问题**：① cron 触发要求 get_due_intents 恰好跑在匹配的那一分钟（无 last-fire 追踪无补课），intention-check 又不在 PRIORITY_TASKS 里会被 batch cap 挤掉 → 每日康复训练停 6 天、夜审停 2 天、夜间深工从未触发；② `cron_matches` 用 `dt.weekday()`（0=周一）对标准 cron（0=周日）→ 全部周任务错一天，生产已实锤误触发。
- **方案**：① 新增 `next_fire_at` 列：创建时和每次成功执行后用纯 Python next-occurrence 计算（逐分钟迭代 cron_matches，366 天上限）；due 判定变为 `now >= next_fire_at`——错过的分钟在下一轮补火；staleness 上限 6h（睡了一晚上的笔记本不在凌晨 3 点发 21 点内容，skip 并 emit `intent_occurrence_skipped`）；② dow 比较位改 `(dt.weekday()+1)%7` 且 max 放宽到 7（0 和 7 都是周日）；③ intention-check 加入 PRIORITY_TASKS（其 pre 是亚秒级 sqlite，免豁免成本）；④ 迁移脚本回填 10 条存量 cron 行的 next_fire_at 并审计星期表达式。
- **验收**：'30 14 * * 2' 只匹配周二；错过触发分钟后下一轮补火；停摆 6 天的康复训练恢复每日触发。✅ 单测覆盖

#### REQ-33 闭环引擎激活：全终态派生 + awaiting TTL + closed_at【闭环机器必须转】
- **问题**：闭环 follow-up 只在 mark_executed 里派生 → 所有 REQ-30/31 受害者完全绕过闭环轴；唯一 awaiting 行（int_7a545cc10d）是永久僵尸（healing 类不再浮出、follow-up 已死、无 TTL）；record_closure 不记时间戳，闭环数据无法度量。
- **方案**：① 抽出 `_on_moment_terminal(intent, how)`：executed 和最终 expired 都调用（expired 仅限 hard/external 类，保住 healing 永不催的承诺）；② lifecycle_sweep 增加 awaiting TTL：无活 follow-up 且超龄（hard 7d / external 14d / healing+autonomous 3d）→ closure_status='na' + closed_at + 面包屑；③ 新增 `closed_at` 列，record_closure / TTL 转移都打戳；④ `intent_stats()` 扩展闭环块（按 category：created/fired/executed/expired/closed/中位闭环时长），CLI `stats --closure`；⑤ 每晚intent复盘 prompt 增加消费 stats --closure 的指令（done-rate<20% 的类别两周 → 提议改 CLOSURE_POLICY 或停止创建该形态）。
- **验收**：moment 过期后（external 类）次日 11 点仍收到"后来怎么样"跟进；awaiting 僵尸 3-14 天内必然终结；stats --closure 输出每类闭环漏斗。✅ 单测覆盖

#### REQ-34 闭环按钮 + 回复归因：Pascal 的回答有机器路径回到意图【确定性闭环】
- **问题**：intention-check 卡片 35% 回复率（23/66），但回复与意图零结构关联——闭环全靠主会话 LLM 自觉跑 CLI（史上发生 2 次，都是 Pascal 主动提）。而确定性回路的全部基建已存在（card.action.trigger sidecar 6/12 上线实证）。
- **方案**：① **闭环按钮**（主路径，零 LLM）：`core/card.py` build_card 增加 buttons 参数；intentions_post 给 follow-up/DECISION 卡附 ✅做了 / ❌没做·改天 / 🚫不用追了 三键（value={'action':'intent_close','id','outcome'}）；lark_event_sidecar on_card 新增 intent_close 分发 → `python3 -m core.intentions close ...` → toast"闭环已记录"。record_closure 已自动取消待发 follow-up（不二次催）。② **回复归因**（自由文本兜底）：intentions_post 发卡时把 intent_ids 记入 `data/.intent_card_ledger.jsonl`；heartbeat_loop 送达后把真实 message_ids 回填；bot.sh 引用回复分支查 ledger，命中则注入 `[REPLY_TO_INTENT id=… closure_question=…]` 提示词强指令。
- **验收**：卡片按钮一击闭环（DB closure_status 落账 + follow-up 取消）；引用回复闭环卡时主会话收到结构化提示。✅ 单测 + sidecar 回归

#### REQ-35 意图全链路遥测【可观测性】
- **问题**：intentions_post 的 8 类失败诊断全在 stderr，而 run_script 仅在非零退出码时记日志（post 永远 exit 0）→ 杀死一半意图的失败**零日志行**；sched_events 只有 task 级事件，意图级转移不可见；cron 星期误触发的自我诊断在 last_error 里躺了 3 天没人读。
- **方案**：① run_script 改为 stderr 非空即记 warning（一行改动）；② sched_events 新增 intent_* 事件族：intent_fired/intent_executed/intent_retry/intent_expired/intent_closure(带 via=button|reply|followup|review|cli)/intent_occurrence_skipped，在 core/intentions.py 各转移点 emit；③ self_diagnostic_pre.sh 增加日检：昨日 auto-expired 计数>0 即 ⚠️（经 REQ-39 确定性告警路径送达）。
- **验收**：每个意图转移在 sched_events.jsonl 可回放；intentions_post 诊断可 grep。✅ 单测覆盖

### 战线 B：心跳调度正确性

#### REQ-36 信封解析失败 = 失败，不再记成功【熔断器必须看见】
- **问题**：多任务信封 json.loads 失败后只记 info 日志，然后照常给**每个任务** record_success + last_run=now——熔断器永远数不到这类失败，输出全灭却零重试（7 次/2 天，其中 3 次毁掉 memory-hourly 的小时索引）。
- **方案**：解析失败分支改为：每任务 record_failure + **不**推进 last_run（下轮重试）+ task_finish status=parse_failed + 日志升 warning。空响应路径已正确处理（record_failure），对齐之。
- **验收**：连续解析失败 5 次触发熔断；单次失败下轮重试。✅ 单测覆盖

#### REQ-37 Force trigger 限定单任务，全量风暴终结【调度即契约】
- **问题**：`/tmp/jarvis-heartbeat-trigger` 是纯存在性标记：admin "Run Now" 明明把任务名写进了文件，heartbeat_loop 却从不读内容，force=True 让全部 32 任务同时 due（仅 60s force cooldown 拦截）→ 6/12 32 次全量风暴：周任务 7h 跑两次、memory-hourly 中位间隔 4.8min、batch cap 每轮挤掉 18-19 任务、intention-check 实际间隔 31min（配置 60s）。聊天回复里每个 [ACTION:heartbeat] 都在引爆全量。
- **方案**：heartbeat_loop 读 trigger 文件内容：内容是合法任务名 → `run_cycle(force=True, only_task=名)`（rotation 路径已有该参数）；空/`all` → 保留全量但加 10min 全量 force 冷却；actions.py 的 [ACTION:heartbeat] 改为写具体任务名（无名则不再全量，改为仅触发 intention-check + 该动作关联任务）。
- **验收**：admin Run Now 只跑指定任务；32 任务风暴在日志中绝迹。✅ 单测覆盖

#### REQ-38 cancel_job 不再炸毁整个 bot【稳定性】
- **问题**：`cancel <job>` 的 killpg 打到整个 bot 进程组（job 子进程未 setsid 分组）→ 用户取消一个后台任务 = 重启整个产品。
- **方案**：job spawn 时 `start_new_session=True`（自立进程组）；cancel 校验目标 pgid ≠ 自身 pgid 才 killpg，否则降级 kill 单 PID + 子进程枚举。
- **验收**：cancel 只杀 job 进程树，bot/心跳/admin 全部存活。✅ 单测覆盖

### 战线 C：自监控与告警

#### REQ-39 自诊断告警去静音：确定性 post 直发【告警路径独立】
- **问题**：self-diagnostic 在 SILENT_TASKS 里，输出无条件丢弃——而它的 prompt 承诺"STARVED channels / open circuits / delivery failures ... always report"。系统唯一健康告警结构性失声；dashboard 死 23 天就是这个类别的代价。
- **方案**：检测与投递分离：新增 `tasks/self_diagnostic_post.py`（纯确定性 Python 无 LLM）：扫描 pre 输出中的 ⚠️ 行，有则直接 lark-cli 发一条聚合告警（4h 去重戳 `.diag_last_alert.json`）——post 副作用路径天然绕过 SILENT_TASKS 与 looks_like_error 闸门。LLM 摘要保持静默。加规则测试：SILENT_TASKS 中 prompt 含 "report" 的任务必须有确定性 post 告警路径。
- **验收**：人为制造 STARVED 通道 → 4h 内 Lark 收到告警；重复告警 4h 去重。✅ 单测覆盖

#### REQ-40 组件清单 components.yaml：四个监督面共用一份事实源【该跑什么是数据】
- **问题**：daemon/doctor.sh/restart --status/self-diagnostic 四份硬编码组件清单各自漂移，没一份包含 :3457（dashboard 死 23 天的结构性根因）；ef_stream 和 admin.py 死了无人重启无人告警；生效的 launchd plist 只活在 ~/Library（仓库里那份是 /usr/bin/python3 起不来的崩溃循环）。
- **方案**：① 仓库新增 `components.yaml`（dashboard/daemon/bot/heartbeat/ef-stream/admin/sidecar/backup/launchd 任务，每项 check 类型 pid|http|file_age|launchctl + critical 标记）；② 新增 `core/components.py` 实现四类检查，self_diagnostic_pre.sh 消费全量（4h 深检）、daemon 消费 critical 子集（30s）、doctor.sh 与 restart --status 消费全量（操作员视图）；③ bot.sh watchdog 30s 循环扩展到 STREAM_PID/ADMIN_PID 拉活（4 次/10min 熔断）；④ daemon 增加 :3456/:3457 探针（仅告警不重启，4h 去重）；⑤ 把三份生效 plist 收编进 `scripts/launchd/`（含 TCC 三约束：日志写 /tmp、Homebrew Python、bash 需 Python 包层）。
- **验收**：`launchctl bootout` 干掉 dashboard → launchd 10s 内拉活 + 若拉不活 4h 内 Lark 告警；杀 ef_stream → watchdog 30s 内拉活。✅ 单测 + 实机验证

#### REQ-41 备份覆盖修复：记忆和 DB 进入备份面【数据不可再生】
- **问题**：备份 27 天静默失败（TCC）刚修好，但即使成功也只备份一个 slug 的顶层 *.jsonl——memory/（用户画像、时间线，全系统最不可再生数据）、jarvis.db（85 意图 + 27 书签，且 WAL 692KB>主文件，裸拷贝会丢 6h 事务）、心跳状态、jarvis.yaml 全部零覆盖。
- **方案**：重写 backup_sessions.sh：① 三个 memory 目录全量 rsync；② jarvis.db 用 `sqlite3 .backup`（WAL 安全）；③ 心跳状态/活跃会话/interval_overrides/engagement_log/sched_events/jarvis.yaml(chmod 600)；④ 两个 slug 的会话转写；⑤ 成功后写 `.last_backup_ok` 时间戳，self-diagnostic 检查 >48h 即 ⚠️（走 REQ-39 路径）。
- **验收**：跑一次备份，验证 memory/ 与可打开的 jarvis.db 副本在产物中；时间戳检查项生效。✅ 实机验证

#### REQ-42 重启链路单一消费者 + daemon 热重载【系统自己当运维】
- **问题**：① .restart_trigger 两个消费者赛跑：heartbeat_loop 10s 内 SIGTERM 自己整个进程组然后**等 daemon 发现**（1-15 分钟黑暗）vs bot.sh 只在恰好来新消息时才消费（快但靠运气）；② daemon 永不感知自身磁盘代码已更新，6/12 旧 daemon 按旧规则两次杀死健康 bot 并锁死"需人工干预"。
- **方案**：① 单一消费者：heartbeat_loop 见 trigger → `Popen(['bash','restart.sh','--yes'], start_new_session=True)` 后正常退出循环（不 SIGTERM 进程组）；删除 bot.sh:1264 消费分支。预期重启延迟 1-15min → ~15s 确定性；② daemon 每轮对比 `__file__` mtime，磁盘更新即 log + exit 0（launchd KeepAlive 秒拉新代码）；③ 部署护栏：restart.sh 进入时 touch `.deploying`、结束删除；daemon 见 30min 内的 .deploying 一律视为健康（终结部署窗口互殴——6/12 当天 10 条 0.0s 假失败全源于此）。
- **验收**：admin 点 Restart → 20s 内全栈回来；改 daemon.py 后 60s 内新代码生效；restart 期间 daemon 零干预。✅ 单测 + 实机验证

### 战线 D：Dashboard（:3457）

#### REQ-43 Dashboard PR1"先诚实"：修 500/404/422/GET 副作用【先能用】
- **问题**：/intentions 每次加载 500（naive-aware datetime 相减，try 只接 ValueError 接不住 TypeError）；thinking/agent_calendar 写完从未注册（app.py:34 没 import，首页死链 404）；9 个写 API 全部 422（bare `request` 参数被 FastAPI 当 query 字段）；GET /bookmarks 渲染即推进间隔重复状态（今晚冒烟探测就误推了 6 个书签）；27/27 书签卡死 inbox 无生命周期控件。
- **方案**：① `_parse_trigger_when` 用 core.intentions._coerce 对齐时区再相减；② app.py 注册 thinking + agent_calendar；③ api.py 全部端点改 `request: Request` 类型注解（或 Body(...) 模型）；④ /bookmarks 渲染只读，surfaced_count 推进移到显式"已读"按钮；⑤ 书签卡片加 inbox→reading→done/archived 流转按钮（直连 db 函数，不过残废 API）。
- **验收**：全部 7 页 200；写 API 2xx 落库；GET 渲染前后 SUM(surfaced_count) 不变。✅ 新增路由级测试套件（TestClient 渲染全部页面 + 双格式 datetime 种子钉死时区修复）

#### REQ-44 心跳任务健康板：sched_events 驱动的真实运行视图【最高价值缺失视图】
- **问题**：系统主导失败类是"任务静默死亡"（repos-sync 19/19 超时、memory-monthly 从未跑、batch 挤兑），但**没有任何界面渲染 sched_events.jsonl**——这份秒级新鲜、自带 run_id/skip 原因/真实时长的回放日志。agent_calendar 现在是用 interval×5% 编造条宽的假甘特图。
- **方案**：重做 /tasks 健康板（合并 agent_calendar）：每任务——最后**真实**运行（max task_finish ts，非合成水印）、24h spawn/finish/skip 火花行、失败率、p50/max 时长、头号 skip 原因带计数（"repos-sync: 12× empty_pre, 0 spawns——疑似死亡"）、heartbeat_state 熔断状态 + disabled_until 倒计时。红旗头条：interval<6h 且 3×interval 内零 spawn → "⚠ 静默死亡" 横幅。数据层：增量 tail-reader（缓存 file_offset，每刷新只读追加字节），ui.timer(15s)。
- **验收**：repos-sync 类问题在页面可见红旗；与 sched_events.jsonl 手工统计一致。✅ 测试覆盖 tail-reader 与聚合逻辑

#### REQ-45 意图漏斗页：闭环泄漏可视化【度量即验收】
- **问题**：50% 静默死亡这个全系统最差产品 bug 之所以活到今天，就是因为没有视图展示状态转移——/intentions 只有列表没有漏斗。
- **方案**：修好的 /intentions 页加漏斗头：7d 窗口 created→fired→executed→closure-asked→closed，泄漏率高亮（"本周 fired 12, executed 6, 静默丢弃 6 ⚠"）；下方"过期尸检"列表（auto-expired 行 + 一键 re-arm 按钮：status='pending'、trigger 10min 后）；awaiting 超龄标旗（僵尸可见）；创建表单补 category/closure_question 字段（堵创建侧 'none' 漏洞）。ui.timer(30s)。
- **验收**：漏斗数字与 SQL 手工对账一致；re-arm 后意图下轮触发；表单建出的意图带 category。✅ 路由级测试

#### REQ-46 Home/Settings 去虚假：活数据时间线 + 诚实设置【不演戏】
- **问题**：Home"Recent Activity"读 agent_log 表（4 行，冻结于 5/21）而系统真实活动流被无视；状态点读虚构水印；Settings 页写 7 个键进 kv_store（史上 0 行）无任何消费者却声称"立即生效"——纯安慰剂。
- **方案**：① Home 时间线改读 sched_events tail + heartbeat_outbox（真实送达）+ 最近 intent_* 事件，ui.timer(10s)；状态点改读 sched_events 最新事件年龄；② Settings 页砍掉安慰剂键，只保留真实可写项（链接到 admin 的 HEARTBEAT.md 编辑与 interval_overrides 展示），页面顶部声明各项的真实生效机制；③ 删除死代码 event_bus.py/watcher.py（零订阅者 + 线程侧 emit 全被丢弃），统一 ui.timer 刷新惯用法。
- **验收**：Home 时间线与 tail -5 sched_events 一致；Settings 无不生效控件。✅ 路由级测试

### 战线 E：Admin（:3456）

#### REQ-47 五个执行器全部修为诚实【控制台不能比没有更危险】
- **问题**：14 个读路由全部正常，但 5 个控制全坏：stop_task 杀不掉还删活锁（int() 解析两段式锁必炸 ValueError，except 吞掉后无条件 unlink）；Run Now 全量触发（REQ-37 联动）；Restart 1-15min 不确定黑暗（REQ-42 联动）；EF Save 摧毁手调偏好（单选框匹配不上自由文本 → fallback 'Push everything' 覆写）；心跳编辑器无校验静默丢行。
- **方案**：① stop_task：`split()[0]` 取 PID、kill 成功且确认死亡才 unlink、UI 加确认弹窗；② Run Now 走 REQ-37 的 only_task 通路；③ Restart 走 REQ-42 单消费者，UI 轮询 /health 显示进度；④ EF Settings：feed_delivery_preference 改 textarea 展示现值 + 仅提交脏字段 + 单选作为快捷模板填充 textarea；⑤ 心跳编辑器：interval 数值校验、保存前 diff 预览、检测 HEARTBEAT.md mtime 变化拒绝盲写。
- **验收**：stop_task 真杀进程且不动他人锁；EF Save 后偏好原文无损；全部 5 控制有破坏性端点测试（现状：两个最毒 bug 都在未测试的 20% 里）。✅ 新增 test_admin_destructive.py

#### REQ-48 Admin 运维纵深：日志/事件/队列/意图浮出 + 最小安全加固【后台要能查问题】
- **问题**：这个以"静默失败"为主导失败类的系统，控制台只露出 ~1 行日志：无日志查看器、无 sched_events、无 night_queue/jobs/意图表面、jarvis.yaml 不可见；时间戳全 UTC（差 8h 对不上日志）；单线程 HTTPServer 一个慢请求冻结全站（还串联着 Lark 卡片里的 /view/ 链接）；POST 零防护（任意网页可 CSRF 重启 bot）；RichView 模型生成 HTML 未转义直出（存储型 XSS 直通无鉴权破坏性 API）。
- **方案**：① 新增 GET：/api/logs（tail/grep jarvis.log+daemon.log，已知 info 级失败串高亮）、/api/sched_events（按任务过滤）、/api/queues（night_queue/breach_queue/jobs 概览）、/api/intents（列表+cancel+re-arm，复用 core.intentions）；② 时间戳统一本地化（API 层转换）；③ ThreadingHTTPServer 一行替换 + EF status 60s 缓存；④ POST 安全三件套：Host 校验（仅 localhost）+ X-Admin-Token 头（启动时生成写 .admin_token，前端注入——强制 CORS preflight 挡跨站）+ Content-Type 校验；⑤ RichView html 段 html.escape 白名单放行（仅允许受控标签）。
- **验收**：日志/事件/意图三视图可用；跨域 POST 被 403；XSS 载荷被转义。✅ test_admin_api.py 扩展

### 战线 F：数据卫生与文档

#### REQ-49 状态文件与日志治理：轮转补全 + GC 落地【不再无限生长】
- **问题**：engagement_log 仅进程启动时裁剪且裁剪与并发 append 赛跑（窗口内追加被静默销毁——污染调频数据）；/tmp/jarvis-dashboard.log、/tmp/jarvis_restart.log 永不轮转；sched_events 仅 10MB 单代（回放窗口 31h，三天审计都做不了）；cleanup_old_jobs 是死代码（18/20 job 目录是空壳）；views/*.json 只在被访问时清理；仓库躺着 .dashboard.pid（死 PID）、watchdog.log（5/11 遗物）、dashboard.log、105KB intent purge 备份、/tmp 世界可读 debug dump（无消费者）。
- **方案**：① 裁剪移到 watchdog 每小时 tick 且 flock 化（appender 改 `flock -x` 包装）；② /tmp 两日志纳入同一 tick（500KB→tail-500）；③ sched_events 改按日轮转留 7 代；④ cleanup_old_jobs 接进 sweeper（完成>7d 的 job 目录与 registry 条目 GC）；⑤ views 加每日 GC（过期即删非访问触发）；⑥ 死文件清理：.dashboard.pid/watchdog.log/dashboard.log 删除，intent purge 备份移 data/archive/，session_compact.py 不再写 /tmp debug dump（零消费者）。
- **验收**：并发 append + 裁剪压力测试零丢行；GC 后空壳 job 目录消失；仓库无误导性死文件。✅ 单测覆盖

#### REQ-50 文档对齐与公共仓库卫生【文档即接口】
- **问题**：CLAUDE.md 指向不存在的 scripts/session_dashboard.py 且说 3456 是 dashboard（实为 admin）、记忆架构写 2 套实际 3 套；README daemon 节奏写 2min 实为 30s、空响应重试自相矛盾（2 vs 4）；TODO.md 滞后一个版本；jarvis.example.yaml 缺 lark.app_secret/event_backend/schedule/thresholds 整节；**公共仓库**追踪着 sources.yaml（真实 chat_id、open_id、运维邮箱）与 phronesis_monitor_pre.sh 硬编码身份。
- **方案**：① CLAUDE.md/README/TODO.md/jarvis.example.yaml 全量对齐现实（本 PRD 落地后状态）；② `git rm --cached sources.yaml` + .gitignore + 补 sources.example.yaml 脱敏模板；phronesis_monitor_pre.sh 身份改读 sources.yaml；③ 历史中已泄漏的 chat_id/open_id 评估轮换成本（**留给 Pascal 决策**：开 ID 不是密钥，但建议轮换群）。
- **验收**：git ls-files 无敏感文件；新克隆按 INSTALL.md 能起全栈；文档无已证伪陈述。✅ 测试守卫（git ls-files 断言进 CI）

---

## 3. P1 需求（本期尽力，未竟则入下期）

### REQ-51 水位监控读真实成功时间戳【监控不能被合成水印骗】
last_success_ts + last_attempt_status 入 TaskState；run_script 返回 (stdout, rc, timed_out)，pre 超时/非零进熔断；watermarks 按 last_success_ts 判饥饿 + "最近 3 次 pre 全败"专项行。验收：repos-sync 类 100% pre 超时在水位报告显示 STARVED。

### REQ-52 repos-sync 起死回生【19/19 超时】
pre 脚本拆分：git pull 全仓库循环移入后台 job（不占 60s pre 窗口），pre 只读上次 job 产物 + 触发新 job。验收：连续 3 天 sched_events 出现 repos-sync task_finish ok。

### REQ-53 引擎容错杂项
- intentions_post action:'failed' 死路修复（mark_failed 改计入 attempt，配合 REQ-31 重试）
- 日历桥 dedup 键去掉 start_time（改期=原位更新而非复制）；后闭环意图补 expires_at=event_end+36h
- date 意图空/坏 datetime 创建时即拒（ValueError）
- 多任务信封 list 值 str() 损坏修复（json.dumps 透传）
- 引擎信封契约单源化：ENVELOPE_CONTRACT 常量 + validate_envelope() + 测试断言 HEARTBEAT.md 同步

### REQ-54 Engagement 页 + EF ROI 页（dashboard 缺失视图 #3）
/engagement：每源 engaged/ignored/read 率 + 响应间隔分布 + 调频决策卡（interval_overrides + _meta）+ EF 漏斗（triaged→pushed→responded）。Pascal 数据优先原则的自我应用。

### REQ-55 Ops/日志浏览器页（dashboard 缺失视图 #4）
/ops：进程健康（含 :3456 探针与自身 uptime）、双日志 tail 带已知失败串过滤、night_queue/delivery_state/batch_flush 状态、24h failed 事件带。dashboard 独立于 bot 生命周期，是承载 bot 死亡报告的正确宿主。

### REQ-56 睡眠建模【假告警与污染数据】
heartbeat_loop 检测 wall-clock 跳变 emit sleep_gap 并标记跨睡眠 cycle（时长统计剔除）；daemon staleness 判定先查 2min 内 wake 事件，是则 180s 宽限；AC 供电时 caffeinate -s 包装（电池策略不动，是 Pascal 的电）。

### REQ-57 原子写与持久化
core/atomicio.py：write_atomic(path, data, durable=False)；heartbeat_state/active_sessions/jobs registry 用 durable=True（fsync+父目录 fsync）；compact.py 与 active_intents.md 改原子非持久档；EF cursor/seen 从 /tmp 迁入 $JARVIS_DIR/eigenflux/（重启不再丢游标，已被重启抹掉 3 次）。

### REQ-58 告警双通道
notify 辅助函数：lark-cli 失败（或 delivery consec_fails≥3）→ osascript 本机横幅 + alerts_deadletter.jsonl 死信（恢复后补发）。

---

## 4. P2 路线图（明确不在本期）

| 方向 | 内容 | 不做的原因 |
|---|---|---|
| Admin/Dashboard 合并 | :3456=API+机器面 / :3457=人类 UI，admin.html 退役 | 大迁移，先把两边各自修诚实再谈合并 |
| EF 流安全 | PM 内容不再裸进 `--dangerously-skip-permissions`（沙箱/白名单工具集） | 需要威胁建模 + EF 侧协同，单独立项 |
| 公共仓库历史清洗 | sources.yaml/聊天数据的 git 历史重写 + 群轮换 | 破坏性操作需 Pascal 决策 |
| 心跳并行执行 | 非管道任务并发跑（TODO P3 既有项） | 调度器刚做完正确性手术，稳定一周再动架构 |
| 记忆架构反转修正 | auto-memory 在每个审计点都是更陈旧侧，与 CLAUDE.md 宣称相反 | 需要与 memory-tidy 整体重设计，牵动两套目录 |
| lark_mail 游标 / git_repo 丢提交 / 邮件敏感闸门 | 感知层修缮 | 感知 PRD（prd_perception_ingestion）自己的下一期 |

---

## 5. 度量（上线后跟踪）

| 指标 | 现值（6/13 审计） | 目标（2 周） |
|---|---|---|
| 一次性意图触发后静默死亡率 | 50% | **0%**（重试耗尽必有违约卡片，静默=0） |
| 闭环 follow-up 周派生数 | 累计 1 | 每周 ≥ 闭环载体 moment 终态数 |
| 闭环记录率（hard/external） | 2/9 | ≥60%（按钮上线后） |
| cron 意图按日触发率 | 每日康复 0/6 天 | ≥95%（补课制） |
| 信封解析失败的熔断可见性 | 0%（记成功） | 100%（记失败+重试） |
| 全量 force 风暴次数/日 | 32 | 0（只允许 only_task） |
| 组件死亡 → Pascal 知晓时延 | 23 天（dashboard 实测） | ≤4h（诊断告警）/ ≤10s（launchd 拉活） |
| 备份覆盖 memory+DB | 0% | 100%，时间戳监控 48h |
| Dashboard 页面可用率 | 4/7（1×500 2×404） | 7/7 + 路由测试钉死 |
| Admin 控制器诚实率 | 0/5 | 5/5 + 破坏性端点测试 |

## 6. 实施顺序（依赖拓扑）

1. **意图核心**（REQ-30~33 + 35 + 36 + 53 引擎件）——同一组文件，一个迁移，一次提交
2. **调度与控制**（REQ-37、38、42、47①②③）——force/restart/cancel 三链路
3. **闭环交互**（REQ-34）——卡片按钮 + sidecar + ledger
4. **自监控**（REQ-39、40、41、51）——components.yaml + 告警 + 备份 + 水位
5. **Dashboard**（REQ-43~46 + 54、55）——先诚实再有用
6. **Admin**（REQ-47④⑤ + 48）——执行器 + 纵深 + 安全
7. **卫生与文档**（REQ-49、50、57）
8. **全量验证**：pytest 全绿 → 红队复查当日改动 → restart.sh 上线 → 组件清单逐项探活 → git 提交

每步带测试先行或同行；任何一步失败不进入下一步。
