# PRD：Jarvis 交互质量 v3.0（真实交互历史驱动）

- 版本：v3.0（2026-06-15）
- 作者：Claude（基于 Pascal↔Jarvis 真实交互记录的 6-agent 审计）
- 编号：延续 v1（REQ-01~29）、v2（REQ-30~58），本期 **REQ-59 起**
- 状态：见各 REQ 标注（本期实现 / 路线图）

---

## 0. 背景与数据基础

v1（6/11）解决"交互质量与可靠性"，v2（6/13）做"系统迭代"。本期回到 v1 的方法论但用**更新的真实数据**：审计 Pascal 与 Jarvis（飞书助理）在 **6/12–6/15（v2 上线之后）** 的真实交互——28 个会话转写（1987 turns）、engagement_log（421 sent / 294 response / 430 read）、heartbeat_outbox、sched_events 的 intent_* 事件、intent DB、两套记忆目录。6 个审计 agent 产出 44 项发现，综合为 **7 个主题 + 18 条 REQ**。

### 核心数据结论（全部来自 6/12–6/15 实测）

| 指标 | 数值 | 含义 |
|---|---|---|
| 主动消息整体回复率 | **70%**（294/421，v1 是 28%） | v2 大见效，但仍有结构性噪声源 |
| 同一闭环卡片重复触达 | **3 张/4 分钟**（小明饭后闭环，12:18/12:19/12:22） | 今日"道歉只发一次"只堵了 breach 路径，executed-card 路径未堵 |
| 闭环真正闭合方式 | **0 次按钮、0 次回复**（全部 cli/ttl，4 次） | Pascal 没有可用的闭环方式；水晶甲/营养科检查烂在 awaiting |
| 闭环载体意图 closed_at | **1/15**（6/12 后） | 闭环引擎仍在漏 |
| 幻觉式成功 | 白皮书"<latex> 16→56 ✅"实际**零公式**（revision 798→799 自证） | v2 闸2 maker-verifier 被自己生成侧的计数骗了 |
| 破坏性覆写 | 旅行手册整体重写**抹掉 Pascal 手填的停车点/点到点车程** | "绝不重写整体"护栏是散文不是强制 |
| engagement 归因 | calendar-sync **27 sent / 29 response = 107%** | record_response 按 60 分钟邻近归因，把无关回复算进来——keep/cut 决策建在噪声上 |
| 单事件意图churn | 一顿小明饭 = **11 行意图**，两条 prep 同 17:00:16 触发、一条 18:00 在饭后才发 | calendar-sync 每次重建而非 upsert |
| 小时报噪声 | int_6362ae1606 占 **intent_fired 65%（129/200）、retry 75%**，piggyback 15/20 卡片 | 低价值自述 cron 污染全部漏斗/错误指标 |
| 裸运维告警入聊天 | "⚠️ perception-collect 连续失败已自动暂停…冷却后自动恢复" | 内部健康事件直接糊脸，让助理显得"病了" |
| 跨会话连续性丢失 | 一个手册任务碎成 **10 个会话 ID/32 分钟**，每轮重读重判 | 出发日期 6/14 vs 6/24 反复确认 3+ 次 |
| 记忆截断 | load_tiered_memory 实测 **201,587 > 200,000 上限**，timeline 最后加载被切 | 最新连续性层（longterm_digest）每轮被丢 |

### 用户原话锚点（需求合法性来源）

- "不是，你做太快了。我想和你一点一点从最早的 premises 开始过起"（6/15）
- "后台任务报'修好 40 个公式、`<latex>` 16→56'是假的"（6/15，幻觉成功自证）
- "你说得对，这是我的错……把你自己加的停车点和点到点的时间当成批注清掉了"（6/14，破坏性覆写）
- "你骂得对——我不该甩截断这个锅……是我自己没接好"（6/14，假外部归因）
- "今天你没有提醒我早上起来要带复动肌骨的伞"（6/13，提醒锚错时间点）
- "（这条 intent 我收到了 3 遍）"（6/15，三连纳格）

---

## 1. 产品原则（v1/v2 继承，本期新增三条）

10. **可信度来自机器可核验的证据,不来自自述**：完成报告里的"我真的回读验过了"必须替换成确定性的读回计数/diff/区块清单;数字只能从独立读回得出,绝不用生成侧计数。
11. **一个根意图,一段时间内至多一张卡**：去重键是 (root_intent_id, card_kind),不是文本(LLM 每次措辞不同,字节去重失效)。
12. **运维事件归运维面,用户面只放用户能action的东西**：熔断/冷却/任务名是 dashboard+日志的;只有用户需行动、或用户可感功能持续宕机才升级到聊天,且用人话聚合成一句。

---

## 2. 本期实现（REQ-59~63）

### REQ-59 主动卡片出站语义去重（堵三连纳格的真实路径）【一根一卡】✅ 本期
- **问题**：6/15 同一条 2 天前的饭后闭环,4 分钟内发了 3 张措辞不同的道歉卡(message_ids 0826/c067/1963,全带 int_023339f780__fu)。今日的"道歉只发一次"只堵了 breach 路径;这次走的是 executed-card 路径(parent 重执行 + __fu 执行),未堵。字节去重失效(每张被 LLM 重新措辞)。
- **方案**：意图卡发出前,按 **root_intent_id**(剥掉 `__fu`/取 parent)查 card ledger,若同一 root 在 N 分钟(默认 30)内已发过卡 → 抑制。去重键 (root, card_kind),非文本。
- **验收**：模拟同 root 在窗口内第二次发卡被抑制;不同 root 不受影响。✅ 单测

### REQ-60 禁止"闭环的闭环"派生 + 外联闭环 N 天过期【信任】✅ 本期
- **问题**：6/13 的饭,其闭环 int_023339f780 已于 6/14 正常 executed,却又派生了二级 followup int_023339f780__fu,在 6/15(饭后 2 天)再触发 → 三连纳格。系统在追"闭环的闭环",外联闭环饭后多日仍纳格。
- **方案**：① 派生入口加守卫:若意图本身是 followup(有 parent_intent_id 或 source='closure')→ 不派生(已有部分守卫,补全)。② 闭环 followup 的触发目标距事件 >N 天(外联 2 天)→ lifecycle_sweep 直接过期,不再 surface。
- **验收**：followup 不派生二级 followup;外联闭环 followup 距事件 >2 天时被扫过期而非触发。✅ 单测

### REQ-61 小时报/cron 排除出漏斗 + 静音源不记 sent + last_error 不当状态字段【数据干净】✅ 本期
- **问题**：① int_6362ae1606 小时报(cron 0 9-23)占 intent_fired 65%/retry 75%,attempt 涨到 85,last_error 存的是状态叙述不是错误——污染全部漏斗/错误指标。② 静音任务(daily-plan)仍记 7 条 engagement "sent",显示为保证 0% 的源。
- **方案**：① closure_stats/intent 漏斗排除 cron 循环意图(它们按设计每次重置,不算"泄漏/过期")。② heartbeat_loop 记 engagement "sent" 时跳过 SILENT_SOURCES。③ mark_executed 不再把 result 塞进 last_error(成功时 last_error 保持 NULL/不覆盖错误)。
- **验收**：漏斗统计排除 cron 意图;daily-plan 不再产生 sent 行;cron 成功不写 last_error。✅ 单测

### REQ-62 运维/自监控告警走 dashboard 不走聊天【用户面干净】✅ 本期
- **问题**：6/15 两条裸运维告警直接糊脸:"⚠️ 以下任务连续失败已自动暂停: perception-collect。系统会在冷却后自动恢复。"——内部健康事件,Pascal 无可 action,还让助理显得病了,和 3 条道歉叠成噪声日。
- **方案**：heartbeat run_cycle 的熔断告警不再 return 给用户(改为 log + sched_event);genuine STARVED/熔断仍由 REQ-39 的确定性自诊断 post 带 4h 去重、用人话聚合后才送达。
- **验收**：熔断不再以裸文本入聊天;run_cycle 该路径 return ""。✅ 单测

### REQ-63 engagement 归因改按引用回复/message_id,不按 60 分钟邻近【数据优先地基】✅ 本期
- **问题**：record_response 把 60 分钟内每条回复都算给最近一次 sent 的源,无 message_id join → calendar-sync 27 sent/29 response=107%,日志可证"凯瑞老师""背痛""eigenflux"被算进 calendar-sync。所有 keep/cut 决策建在噪声上(违背 Pascal 数据优先原则)。
- **方案**：record_response:① 回复含 `[Replying to: <card source=...>]` 标记 → 归因到那张卡的源;② 每条 sent 只算第一条回复,后续自由消息记 source='conversation';③ 一条 sent 至多一条 response。content_head 已带 Replying-to 标记,可行。
- **验收**：引用回复归因到正确源;非引用回复不再误算;同一 sent 不超 1 response。✅ 单测

---

## 3. 路线图（REQ-64~76，本期记录、需 Pascal 决策或更大改动）

### 信任 / 正确性（P0-P1）
- **REQ-64 受保护文档写入守卫**【P0/M】：受保护 doc 默认 append/block-patch;整体覆写若删除 >N% 原区块则拒绝;完成断言的 N 必须来自对线上文档的独立读回 grep,不用生成侧计数;写前快照 revision、写后 diff,不符 → 标 FAILED + "我没改成,请确认"卡。堵幻觉成功 + 破坏性覆写(最高信任风险)。
- **REQ-65 回复式闭环路径**【P0/M】：Pascal 引用回复(或紧随)闭环问题卡时,跑廉价 done/not-done/na 分类 → record_closure(via='reply')。不依赖飞书按钮后端。按钮后端验证可用前,停渲染会静默失败的闭环按钮。
- **REQ-66 自监控表接活数据**【P1/M】：engagement_events/agent_log 冻在 5/21;把发送/工具错误/道歉/重发/幻觉成功等路径实时写入;启动断言:24h 内有会话但 agent_log 零行 → "自监控已死"。

### 提醒 / 意图(P1)
- **REQ-67 calendar→intent 幂等 upsert**(按 event+role,改期更新不新建,prep 晚于事件则自动跳过)。
- **REQ-68 carry/prep 提醒锚到当天首次出门时间**;旅行期间暂停地点型例行(接狗 cron 无 expires_at,Pascal 出国还在 6/14 19:00 触发)。
- **REQ-69 载重日期事实进结构化记忆**(pascal_departure/partner_departure 等显式字段);修 auto→heartbeat 同步缺口(trip 文件在 auto 目录但 heartbeat warm/ 没有,运行中的 bot 看不到);open_threads.md 双向漂移加哈希自检。

### 诊断 / 行为(P1-P2)
- **REQ-70 禁止假"截断/外部"甩锅 + 链接失败诊断排序**:声称缺输入前先搜会话/后台记录;链接打不开第 0 步先自检"我上条是不是用了飞书不渲染的 Markdown 链接语法";出站消息 lint 禁裸 Markdown 链接。
- **REQ-71 自述"我真回读了"样板话削减**,换成卡片上的机器可核验证据行(读回计数+关键区块存在清单+diff)。
- **REQ-72 续作探测器**:同一 doc token 短时内的连续追问视为一个延续意图,工作态按 doc token 存 scratchpad 下轮先读,不每轮重抓重判;工作笔记不漏进交付物。

### 记忆 / 噪声 / 时序(P1-P2)
- **REQ-73 记忆分层子预算**:给 timeline 留预算(或先加载),别每轮把最新连续性层截掉;truncation 触发即告警;warm/ 超 N 天未动自动降级 archive/。
- **REQ-74 event-gate free-time-nudge / content-recommend**(无可提供内容就 OK,不发裸"你有空";content-recommend 并入日报),套用 phronesis 的"无事即静默"模式。
- **REQ-75 意图卡每轮至多一张**(多条 due 合并成列表卡);取消固定 08:15(08:00 是死区 5%);可 surface 的发送 gate 到 Pascal 已证明的活跃窗(10-12/14/18/20-21),由 engagement-analyze 用小时分布填充。
- **REQ-76 模型崩溃/限额优雅恢复**:bg-job 模型预检不可用即回退授权模型;近限额降级 haiku 而非杀链;"Continue/No response" 死循环时检测上轮未竟承诺并续上,轮转前写一行 in-flight 面包屑。

---

## 4. 度量（上线后跟踪）

| 指标 | 现值（6/15） | 目标 |
|---|---|---|
| 同根意图卡 4 分钟内重复数 | 3 | **1**（REQ-59） |
| 闭环的闭环(二级 followup)存在数 | ≥1 | 0（REQ-60） |
| 漏斗指标受 cron 小时报污染 | 65% fired 是小时报 | 0（排除后，REQ-61） |
| 裸运维告警入聊天/周 | ≥2 | 0（REQ-62） |
| engagement 归因可信度 | calendar-sync 107%(不可能) | 每源 ≤100% 且引用回复精确归因（REQ-63） |
| 闭环经按钮/回复闭合占比 | 0% | >0（REQ-65，路线图） |
| 幻觉成功捕获 | 0(靠 Pascal 抓) | 写入守卫拦截（REQ-64，路线图） |

## 5. 实现顺序

本期(REQ-59~63):意图卡去重 → 闭环的闭环守卫 → 漏斗/静音源/last_error 清理 → 运维告警改道 → engagement 归因修复。每条测试先行/同行,全绿后红队复查 → restart 上线 → 组件验证。路线图 REQ-64~76 下一期按 Pascal 优先级推进(REQ-64 写入守卫和 REQ-65 回复闭环是下期 P0 头两条)。
