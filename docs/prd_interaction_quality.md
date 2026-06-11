# PRD：Jarvis 交互质量与可靠性（Interaction Quality & Reliability）

- 版本：v1.0（2026-06-11）
- 作者：Claude（基于全量过往交互审计）
- 状态：P0 全部上线；P1 中 REQ-11/12/13/25/26/28 已上线、REQ-15 部分上线、REQ-22 验证已存在；其余为路线图（多数需 Pascal 形态决策）
- 补充：2026-06-11 对今日全部改动做了对抗性审查（2 高/5 中/6 低全部修复或显式记录残留），commit 39dc8c6

---

## 0. 背景与数据基础

本 PRD 基于对 pascal-jarvis 全部过往交互的四路审计（2026-06-11）：

1. **代码审计**：bot.sh / daemon.py / core / tasks 全量走读（21 项发现，6 项高危）
2. **会话审计**：31 个 Claude Code 会话 jsonl（2026-04-18 ~ 06-10，527 条用户消息、2429 条助手消息）
3. **Engagement 审计**：engagement_log.jsonl 502 行（7 天窗口，287 条主动发送）、jarvis.db、daemon.log
4. **记忆体系审计**：两套 memory 目录、HEARTBEAT.md 33 个任务、timeline、24+ 反馈文件

### 核心数据结论

| 指标 | 数值 | 含义 |
|---|---|---|
| 主动消息真实回复率 | **28%**（81/287） | 72% 的主动消息是无效打扰 |
| 空响应/无响应投诉 | **≥15 次**（4-6 月） | 5/26 单日爆发 8 次，用户原话："你写那么多测试，这测不到" |
| 推送断流靠用户人肉发现 | **≥7 次** | eigenflux feed ×3、好友请求、checkin、推荐 |
| daemon 健康检查失败 | **387 次**；110 次自动重启，22 次"需人工干预" | 84% 是 Heartbeat stale |
| 同一报错卡重复发送 | **7 次 / 12 小时**（6/10 403 事件） | 错误闸门被 markdown 头部绕过 + 无去重 |
| 已终结意图中过期占比 | **35%**（37/106） | "提醒看两篇文章"建两次、过期两次 |
| 深夜（0-9 点）发送回复率 | **≤6%**（34 条仅 ≤3 回） | 黄金窗 10-11/13-14/17-18 点为 70%+ |
| 自适应调频建议落地次数 | **0**（两个月） | 关键词不匹配 + 状态文件竞态双重失效 |
| 用户被迫当运维 | "重启"指令 ≥15 次 | "需要重启吗？"成为用户口头禅 |

### 用户原话锚点（需求合法性来源）

- "一跑跑 3 个小时，其中我就用不了这个飞书机器人了，这个体验是完全没有办法接受的"（4/23，最严厉）
- "用一个比较弱的模型做非常快速的总结再发给我…它搜了什么网站、有没有真的去看，还是在幻觉"（5/29）
- "卡片按钮点击我在手机上从来没有成功过"（5/27）
- "synchronization 一定要高频…但不要轻易打扰我"（5/8）
- "任何消息…尽可能带上你的思考和行动建议"（5/20）
- "现在的 checkin 会给我重复的消息，请你从机制上改进"（5/2）

---

## 1. 产品原则（从反馈中提炼，所有需求的判据）

1. **送达即承诺**：消息生成 ≠ 任务完成；未确认送达等于没做。
2. **打扰需配额**：高频同步、低频打扰；每一条主动消息都要挣回自己的注意力成本。
3. **错误永不裸发**：任何基础设施错误不得以原文触达用户；用户看到的只有正常内容或（持续故障时）一条聚合告警。
4. **问题可一句话回答**：所有 checkin/review 类提问必须给出具体选项。
5. **承诺必须闭环**：说了"22:15 给你闭环卡"就必须发出或显式改道，静默违约是最伤信任的行为。
6. **系统自己当运维**：用户发"hi"探活、喊重启，都是产品失败。

---

## 2. P0 需求（本期已实现，2026-06-11 上线）

### REQ-01 会话锁原子化【稳定性】
- **问题**：锁文件在 Claude fork 后才写入，两条几乎同时到达的消息会并发 `claude --resume` 同一会话，写坏 session jsonl（bot.sh:519-565）。
- **方案**：noclobber 原子抢锁，spawn 前持锁；占位内容非数字，所有读锁方（restart.sh / stop 命令）经 `kill -0` 守护安全降级。
- **验收**：并发双消息仅一个进入 Claude，另一个排队等待 ✅

### REQ-02 心跳状态跨进程互斥【稳定性·根因级】
- **问题**：常驻 heartbeat_loop 与会话轮转路径（bot.sh 另起进程跑 `run_cycle(force=True)`）同时读-改-写 heartbeat_state.json，互相吞掉 last_run/熔断状态——这是 daemon 387 次 "Heartbeat stale" 的最可疑根因。
- **方案**：`run_cycle` 全程持 `heartbeat_state.lock` 的 flock（LOCK_EX|LOCK_NB），抢不到锁直接跳过本轮；进程死亡自动释放。
- **验收**：双进程并发 run_cycle 只有一个执行 ✅（回归测试覆盖）

### REQ-03 错误内容双层闸门【错误永不裸发】
- **问题**：`**Intent** | Failed to authenticate. API Error: 403` 连发 7 张卡——markdown 头部绕过了行首检查。
- **方案**：① core/safety.py 增加高信号子串（"Failed to authenticate. API Error"、"API Error: 401/403/429/500/529"、"Request not allowed"）；② 出站去重（REQ-04）兜底。
- **验收**：带头部包裹的 403 文本被拦截 ✅（回归测试覆盖）

### REQ-04 出站消息内容去重【打扰需配额】
- **问题**：同文重发无任何防线（403 卡 ×7、5/2 重复 checkin、5/19 重复广播）。
- **方案**：发送前对照 heartbeat_outbox.jsonl 最近 30 条，同文 6 小时窗口内只发一次。
- **验收**：窗口内同文第二次发送被抑制并记日志 ✅（回归测试覆盖）

### REQ-05 自适应调频闭环（W3.1 最后一公里）【打扰需配额】
- **问题**：engagement-analyze 两个月只产建议从未生效——①建议文案用"降频"而关键词表只有"降低/减少"；②post-hook 写进 state 文件后被 run_cycle 周期末的整文件回写抹掉。
- **方案**：改用 `interval_overrides.json` sidecar（绕开 state 竞态），心跳每轮读取，优先级：override → legacy effective_interval → HEARTBEAT.md 默认；关键词表补全中英文变体；重复建议在现值上复利、夹在 5min~48h。
- **首个应用**：free-time-nudge（0% engagement, n=12）1h → 2h。
- **验收**：override 文件生效、复利与钳制正确 ✅（回归测试覆盖）

### REQ-06 语音消息不再炸死监听循环【稳定性】
- **问题**：`set -u` 下未防护的 `$OPENAI_API_KEY`，一条语音消息可终止整个 Lark 监听管道。
- **方案**：`${OPENAI_API_KEY:-}` 默认值。✅

### REQ-07 重启链路全面安全化【系统自己当运维】
- **问题**：①消息循环内 `kill 0` + `exec` 自杀式重启，实际靠 daemon 扶尸；②restart.sh 在非交互环境因 `read` EOF + `set -e` 静默 abort。
- **方案**：①重启触发改为委托 `restart.sh --yes`；②restart.sh 新增 `--yes/-y`，非交互且有活跃会话时拒绝并明确提示（不再静默死）。✅

### REQ-08 运行期资源回收【稳定性】
- **问题**：jarvis.log 只在启动时轮转（已超限 708KB）；tmp/ 5.5MB 无任何清理。
- **方案**：watchdog 每小时 copytruncate 轮转（保留子进程 O_APPEND fd 有效性）+ tmp/ 7 天过期清理。✅

### REQ-09 心跳任务清单去噪【打扰需配额】
- watchlater-remind 僵尸任务删除（自标 DISABLED 仍空跑 21 次 Claude 调用）✅
- eigenflux-profile 1h → 24h（profile 极少变化）✅
- thinking-review prompt 重写为"可一句话回答"格式（5/26 被点名批评后唯一原样留存的 prompt）✅
- heartbeat_state.json 孤儿 entry 清理（watchlater-remind / worklog-sync / eigenflux-supply-demand）✅（上线时执行）

### REQ-10 工程卫生【质量基线】
- engagement 反馈日志改 jq 生成（防 JSON 注入坏行）✅
- 单测全面断网：idle_judge 在所有测试工厂默认关闭（修复 flaky）✅
- intent closure WIP（+1038/-118）随本期一并提交，结束"生产跑在未提交代码上"的状态 ✅

---

## 3. P1 需求（下一期，按 ROI 排序）

### REQ-11 端到端送达保障（Delivery ACK）✅ 已上线（2026-06-11，重试+账本+聚合告警；message_id 级 ACK 见 L4 残留）
- **数据**：≥15 次空响应投诉、3 次"后台已回前端没收到"、闭环卡因 403 静默违约。
- **需求**：
  1. `lark_send_*` 返回 message_id 并写入送达账本（sent_at / confirmed / failed）；
  2. 发送失败自动重试（指数退避 ×3），仍失败则降级为纯文本再试一次；
  3. 承诺型消息（intent 闭环卡）失败后改道主对话补发；
  4. 连续 N 次发送失败 → 给用户一条聚合告警（"过去 2 小时有 3 条消息可能没送到"），而不是让用户发 "hi" 探活。
- **验收**：人为断网 10 分钟，恢复后消息补达且用户收到一条（且仅一条）说明。

### REQ-12 推送管道水位监控（每通道心跳）✅ 已上线（2026-06-11，core/watermarks.py + self-diagnostic 4h）
- **数据**：eigenflux feed 断流 4 次全靠用户发现；用户 5/6 明确要求"每小时自检"。
- **需求**：每条推送通道（feed/checkin/推荐/好友请求/phronesis）维护 `last_success_ts` 水位；超过 2× 期望周期未成功 → self-diagnostic 任务自动诊断并上报一条聚合告警。
- **验收**：kill eigenflux stream 进程，≤1 个心跳周期内用户收到断流告警。

### REQ-13 夜间静默 + 黄金窗排队 ✅ 已上线（2026-06-11，夜队+晨间 digest；黄金窗分时段放行为后续细化）
- **数据**：0-9 点回复率 ≤6%，黄金窗 70%+；eigenflux 已有 quiet-hours gate（commit 3967f94），但 heartbeat 主链路没有。
- **需求**：非紧急（无 deadline 属性）的主动消息在 23:30-09:30 进入队列，按优先级在黄金窗（10:00/13:00/17:00）批量放行，与既有 night-batched morning digest 合流；紧急消息（日历冲突、闭环到点）直发。
- **验收**：深夜产生的推荐/feed 类消息次日上午合并送达。

### REQ-14 eigenflux-feed-triage 摘要化限流
- **数据**：占总发送量 23%（66 条/周），卡片最长 1276 字符，回复以 late_reply 为主；对照 phronesis-monitor（短、决策点明确、中位 293s 即回）。
- **需求**：逐条推送改为每日 2-3 个合并 digest；单条卡片硬上限 ~500 字符 + "展开全文"链接（依赖 REQ-17 卡片重构）；保留"高分信号即时直推"白名单通道。
- **验收**：feed 类周发送量降至 ≤25 条且回复率不降。

### REQ-15 响应归因修复（已读回执 + reply-to）🟡 部分上线（2026-06-11，read/reaction 事件已入 engagement_log；message_id join 与三态统计待做）
- **数据**：calendar-sync 响应数(27) > 发送数(21)，ignored 的 content_head 全是无关话题——归因是"归到最后一条 sent"，engagement-analyze 在用脏数据调参（而 REQ-05 刚让调参真正生效，脏数据危害放大）。
- **需求**：①注册 `im.message.message_read_v1` / reaction handler（同时消除 SDK Error 刷屏）；②回复归因优先用 Lark parent_id/reply 关系，其次时间窗就近；③已读未回 = 真 ignored，未读 = 未触达，分开统计。
- **验收**：engagement_log 含 read/replied/untouched 三态；calendar-sync 不再出现响应数>发送数。

### REQ-16 长任务异步化（任务卡片）
- **数据**：4/23 最严厉投诉"3 小时阻塞完全没法接受"；exit 143 watchdog 中断反复出现。
- **需求**：①预计 >2 分钟的工作自动转后台 job（jobs/ 目录已有雏形），会话立即释放；②发一张任务卡（进行中/完成/失败 + 耗时）；③完成后结果卡 + 中间过程摘要（REQ-18）。
- **验收**：跑一个 10 分钟任务期间，用户消息照常秒级响应。

### REQ-17 卡片体系重构
- **数据**：按钮"在手机上从来没有成功过"（5/27）、内容截断（5/16、6/3）、JSON 直出（5/6）。
- **需求**：①所有按钮回调端到端测试纳入 CI（含移动端真机 checklist）；②长内容自动转"摘要卡 + 跳转 admin 页面全文"（用户 5/17 已主动提出"卡片即应用"方案，admin 面板已在 3456 端口）；③卡片渲染前 schema 校验，原始 JSON 一律拦截（部分已有，补 CARD: 路径）。
- **验收**：移动端按钮成功率 100%；不再出现 "..." 截断投诉。

### REQ-18 中间过程弱模型摘要流
- **数据**：用户 5/29 给出完整方案："用比较弱的模型做非常快速的总结…它搜了什么网站、有没有真的去看，还是在幻觉"。现有 20s 轮询工具流（bot.sh）只发工具名。
- **需求**：把现有 `🔧 工具列表` 升级为 haiku 一句话叙事（"正在读 X 的 API 文档，已确认 Y"），每 60s 最多一条，错误/幻觉风险点显式标注。
- **验收**：长任务期间用户能看懂 agent 在干什么、信源是什么。

### REQ-19 意图系统形态分流
- **数据**：已终结意图 35% 过期；过期项一半是"阅读/跟进"类；"提醒看两篇文章"建两次死两次；27 条 active 曾 0 闭环（6/9 已重做引擎，本期已提交）。
- **需求**：①"阅读类"意图不再走定时 intent，进 watchlater 队列由 free-time-nudge 消化，过期静默回收；②闭环卡送达失败自动改道（依赖 REQ-11）；③同文意图重建时提示"上次建过且过期了，要改成 watchlater 吗"。
- **验收**：月度 expired 占比 <15%。

### REQ-20 记忆体系修缮
- **数据**：24+ 根目录 feedback 文件对 heartbeat 侧是悬空 wikilink；MEMORY.md 漏索引；diet_tracking 与 checkin 规则明文矛盾；digest 同事件重复 5 遍。
- **需求**：①memory-tidy 同步范围扩到根目录 feedback_*.md；②索引一致性检查进 memory-tidy（文件↔索引双向 diff）；③矛盾规则显式 supersede 机制（保留一方，另一方标记废弃原因）；④digest 内容级去重（同事件保最新）。
- **验收**：heartbeat 会话内随机抽 10 个 wikilink 全部可解析。

---

## 4. P2 需求（路线图）

- **REQ-21 感知摄入 MVP**（已有 v2 PRD：docs/prd_perception_ingestion.md，25-35h）：所有信息类别（repo 改动、其他飞书群、其他 Claude session）模块化灌入记忆——同时是 TODO P1 硬编码问题的系统解。用户 6/9 立项，待开工。
- **REQ-22 引用回复上下文**（5/28）✅ 经查已实现（bot.sh:1108 quote-reply 注入）：用户引用一条消息回复时，把被引用消息注入上下文。（小改动，可提前到 P1。）
- **REQ-23 预测式日历助手**（5/8、5/28）："看更长时间的日程…提前做心理状态的准备"；与 calendar-sync Tier-0 合流，做 7 天前瞻 + 模式识别（文化轮转、康复周期）。
- **REQ-24 双层日报合并**：cron「每日日报/小时报」与 heartbeat daily-plan/daily-reflect 二选一（daily-plan 当前 0% 回复，合并时重新设计形态——行动建议优先而非全量日程，5/8 + 5/20 反馈）。
- **REQ-25 统一 engagement 存储** ✅ 已上线（2026-06-11，dashboard 改读 engagement_log.jsonl 事实源）：SQLite engagement_events/agent_log 自 5/21 死亡（3 行 vs jsonl 502 行），修写入或删表，dashboard 不得读假数据。
- **REQ-26 日志归档化** ✅ 已上线（2026-06-11，jarvis.log.1..3 滚动归档）：`tail -500` 截断使失败率审计物理不可能；改为 `jarvis.log.1..3` 滚动归档，WARN 统计可回溯（REQ-08 的 copytruncate 是临时解）。
- **REQ-27 bot.sh 逻辑继续下沉 core/**：1407 行 bash 零测试，本次 6 项高危 bug 中 3 项在 bash 层；目标：消息解析、卡片回调、会话管理全部 Python 化 + bats 覆盖剩余 bash。
- **REQ-28 daemon pkill 收窄** ✅ 已上线（2026-06-11，daemon+restart.sh 全部路径锚定）：按 PID 文件 + cwd 匹配，不再误杀同机其他 lark-cli/eigenflux 进程。
- **REQ-29 admin 面板安全加固**：token 改 hmac.compare_digest、禁 query-string token；梳理与 session_dashboard.py 的 3456 端口冲突。

---

## 5. 度量（上线后跟踪）

| 指标 | 当前基线 | P1 完成目标 |
|---|---|---|
| 主动消息真实回复率 | 28% | ≥45% |
| 日均主动发送量 | 41 条 | ≤25 条 |
| 错误内容触达用户次数 | 7 次/周（403 事件周） | 0 |
| 用户探活/喊重启次数 | ~2 次/周 | 0 |
| Heartbeat stale 守护重启 | ~3 次/周 | ≤1 次/月 |
| 意图 expired 占比 | 35% | <15% |
| 深夜发送条数 | 34 条/周 | ≤5 条/周（仅紧急） |
