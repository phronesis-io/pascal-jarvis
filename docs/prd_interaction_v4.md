# Jarvis 交互质量 PRD v4.0 — 交付可靠性与自监控盲区

- 日期：2026-07-01（评审定稿 v4.1）
- 状态：**评审通过**（三路独立评审：必要性/简单性、证据真实性、风险回归；裁决记录见 §6）
- 编号：REQ-78 ~ REQ-90（接续 v3 的 REQ-59~77，v3 已全部上线并封版 v1.0.0）
- 方法：5 路并行数据挖掘（audit_issues / session_messages 原始对话 / intentions 闭环 / sched_events+日志 / backlog 与用户反馈）+ 1 路代码现状核实 + 3 路红队评审。窗口 2026-06-15 → 07-01。每条需求挂真实数据证据，证据经独立复算核实。

## 0. 本轮主题判断

v3（6/15）修的是"交互层的烦"（纳格、重复、假成功）。这两周的数据说明当前最大的问题变了：**是"说了会做的事没有真的发生，而且没人知道"**。三个标志性事实：

1. **信用卡 ¥12,345.67 还款提醒（7/2 到期）和 Tushare token 到期提醒都没按时发出**，靠事后补发道歉（heartbeat_outbox 实录）。根因链：daemon 停摆 >6h → cron occurrence 被判 stale 静默 skip → skip 事件全仓无消费者 → 自诊断也看不见。
2. **对话审计管线 6/18 起停摆 13 天**——不是坏了，是从来就只有手动 CLI 入口，没人发现。
3. **intent_fired 1,953 次 vs 真正 executed 349 次**，intention-check 失败率 57%，且 866 次失败事件全部不带错误信息、文本日志只留 2.5 天——6/19 一夜 ~247 连败已无从考证。

评审沉淀的总原则（贯穿所有技术方案）：**优先删掉源头，而不是加一层机制**；补发/告警一律复用既有通道（breach 队列、REQ-39 告警通道）继承防纳格守卫，不开旁路。

## 1. 数据基础（摘要，数字经独立复算）

| 数据源 | 窗口 | 关键数字 |
|---|---|---|
| session_messages（去重 1824 条） | 6/12–6/18（存档止于 6/18） | 响应中位 6s / p90 86s，链路不慢；13 个用户不满实例 |
| audit_issues 218 条 / 28 runs | 最后 run 6/18 11:56 | spend-limit 事故占 47%；open 141 / fixed 77 |
| intentions 159 行 | 5/21–7/1 | 有效闭环率 ~76%；僵尸 0、卡死 0、re-ask 5/5 转化 |
| sched_events ~60k 事件 | 6/15–7/1 | task_skip 44,472（circuit_open 18,318 = 41%）；20 段 >2h 停摆共 ~58h |
| jarvis.log(.1-3) | 仅 6/29–7/1 | 553 次调用全部 primary/opus，0 次降级 |

**不立项**（现状核实防重复造轮子）：
- provider 错误文本出站拦截：已完整覆盖（core/safety.py ERROR_PATTERNS 含 4 个 spend-limit 变体 + bot.sh:884/973 强制过滤 + 18 个 tasks/*_post.py 统一引用）。6/16-18 的 73 次泄漏发生在守卫上线前。
- 反馈卡片"点选无反应"（6/19 报障）：6/26 e8698d0 已修，剩端到端实证 → 降为 REQ-81 处置清单里的顺手活（成功路径加日志 + 删 dormant 的 card_callback_sidecar.py + 等一次真实点按）。
- 输出啰嗦/黑话/半英文：行为契约 §9/§10 已覆盖，不再工程化；审计恢复（REQ-82）后持续监测。
- PGC 信源时效/覆盖抱怨：移交 eigenflux-pgc 北极星工作流。
- 熔断器 6/22 三项设计债：已收口。

## 2. 需求清单（评审定稿）

### 主题 A：交付可靠性（P0 核心）

#### REQ-78【P0】cron occurrence 停摆补发与告警
- **证据**：6/30（09:30-10:00、14:00-14:30 两窗）+ 7/1（08:15-09:30）共 **8 个 occurrence（6 个不同 intent）**被 `_skip_stale_cron_occurrence` 静默跳过；信用卡 ¥12,345.67 与 Tushare token 提醒漏发后补发道歉；`intent_occurrence_skipped` 事件全仓无消费者，self_diagnostic 只查 `status='expired'`。
- **需求**：
  1. 停摆恢复后，对 24h 内被 skip 的 occurrence 分级：提醒/账单类（tag 白名单，不做分类器）补发并标注"迟到 X 小时"；一般类汇总一条"停摆期间跳过了 N 件事"。
  2. self-diagnostic 增加 `intent_occurrence_skipped` 24h 计数检查，>0 即 ⚠️。
- **风险红线（评审新增）**：补发必须走既有 breach 队列（继承 BREACH_MAX_SHOWS=1 防纳格）；skip 事件加 consumed 标记保证重启幂等（watchdog 反复拉活不得重复补发）；与 breach 卡/汇总/自诊断三路播报去重。
- **上线节奏**：汇总+自诊断计数先上（影子期一周）；billing 补发在幂等通过重启场景验证后开。
- **验收**：模拟 >6h 停摆 → 提醒类有且仅有一次补发（重启两次仍一次）；自诊断可见 skip 计数。

#### REQ-79【P0】共享调用失败不再连坐熔断
- **证据**：7/1 21:37 spawn batch=6 → 23:57 全批 failed（8372s）→ 同秒 3 任务 circuit_tripped（heartbeat.py:1004-1039 `if not raw:` 分支对全批 record_failure，与 :1146-1180 parse_failed 分支的共享计数器不对称）；16 天窗口内 circuit_open 造成 18,318 次 skip（41%），perception-collect 在 8 个不同天触发熔断（最长单次 ~16h）；envelope 解析失败 streak 实测 9x 仍无拆批路径。
- **需求**：
  1. `if not raw:` 分支改为共享计数器记账，不对单任务 record_failure；**同时新增共享级冷却/退避**（评审红线：否则共享失败将无任何熔断路径，每分钟对挂掉的 API 无限重试）。
  2. **仅限 parse_failed** streak ≥3 时下一周期临时把 batch size 钳到 1（复用现有执行路径，不建第二套执行逻辑；每周期单跑上限 2）。共享调用失败**不拆批**（评审红线：API 故障期拆批 = 调用量放大 6 倍，spend-limit 成本回归）。
- **上线节奏**：79.1 单独先上 + 24h 观察窗；79.2 观察一周后再上。
- **验收**：单测覆盖两分支；重放 7/1 场景不触发无辜任务熔断；共享失败有退避不无限重试。

#### REQ-80【P1】任务失败可观测：错误摘要落盘
- **证据**：866 次 failed 的 task_finish 事件（截至 7/1 复算）全部不带错误信息；6/19 一夜 ~247 连败无从考证；jarvis.log 轮转仅覆盖 2.5 天。
- **需求**（评审拍死方案）：task_finish failed/parse_failed 事件附带错误摘要（stderr/异常首行，截 500 字符，剥离 secrets）写入 sched_events.jsonl（持久、不受 /tmp 清理影响）；日志轮转 backupCount 适度调大。**不**接线从未使用的 agent_log/task_executions DB 表（过度工程）；**不**承诺 14 天文本日志（撞 /tmp TCC 约束，macOS 3 天清理）。
- **验收**：制造一次任务失败，sched_events 中可查到错误原因。

#### REQ-81【P1】心跳任务大扫除
- **证据**（16 天）：eigenflux-messages（skip 1,631 全 empty_pre）、eigenflux-research（623）、memory-monthly（273）、task-triage（87）、harness-evolve（18）零执行；eigenflux-friends 失败率 92%、memory-weekly 89%（周记忆没在工作）、daily-reflect 59%、memory-tidy 44%。
- **需求**（评审修订姿态）：
  1. 5 个零执行僵尸任务**默认下线**（16 天零执行且没人想念即不该存在；同步清理 PRIORITY_TASKS/SILENT_TASKS 等硬编码集合——eigenflux-messages/friends 在 PRIORITY 名单里）。想恢复时一行加回。
  2. 仅对有真实消费者的 memory-weekly / memory-tidy / daily-reflect 做根因修复（memory-tidy 承担 auto→heartbeat 记忆单向同步，修坏=记忆架构断流，独立 commit 可回滚）。依赖 REQ-80 错误数据积累 ≥7 天再诊断，不拍脑袋。
  3. 顺手活吸收：反馈卡片成功路径日志 + 删 card_callback_sidecar.py（删前确认零引用）。
- **验收**：9 个任务各有处置记录；修复类 7 天窗失败率 <20%。

### 主题 B：自监控盲区

#### REQ-82【P1，评审从 P0 降级】conversation-audit 接入调度 + 新鲜度告警
- **证据**：最后 run 6/18 11:56（手动 CLI，全仓无调度挂载）；停摆 13 天但 Pascal 无直接感知伤害（故降 P1——观测层不与漏账单同级）。
- **需求**：① daily 调度接入，**必须走 Tier0 直连或独立 cron，不得进 Claude 合批**（评审核实：审计是纯正则引擎零 LLM 成本，但接成普通 HEARTBEAT 任务会白烧 opus 且撑爆 envelope）；② self-diagnostic 检查 audit db mtime >48h 即 ⚠️。~~③ P0 issue 推送~~（评审砍除：审计库 status 已被证明不可信，推不可信告警=喊狼来了）。
- **验收**：连续两天自动产生新 run；人为停掉后自诊断报警。

#### REQ-83【P1】calendar user-token 探针 + 失败与空日程区分
- **证据**：6/29-30 user token 失效 ×7，calendar_sync_pre.sh:43 `2>/dev/null` 吞错渲染成"(no events)"；doctor.sh 只探测 bot 身份。日历是 prep/daily-plan 上游。
- **需求**：① self-diagnostic 加 `--as user` 日历探针（告警 4h 去重）；② calendar_sync 区分"没日程"和"命令失败"，失败时保留上次成功快照，**快照必须带"数据截至 X 时"标注**（评审红线：防陈旧日历被静默当今天用）。
- **附带动作（立即执行，不等开发）**：提示 Pascal 跑 `lark-cli auth login`（token 已失效 3 天）。
- **验收**：吊销 token 场景下自诊断报警、旧快照带时戳保留。

#### REQ-84【P2，评审缩水】删除即弃的晨报卡片生成
- **证据**：daily_plan_post.py:52-62 每天生成晨报卡片，heartbeat.py:167 SILENT_TASKS 名单每天拦截丢弃（6/12 幻觉事故决策）——每天白拼装白归档。**注意**（评审更正）：省的是无谓拼装非 LLM 调用；PLAN_LOG 有真实消费者（daily_reflect_pre.sh 读当日计划做晚间对照），任务本体和 PLAN_LOG append 必须保留。
- **需求**：只删卡片构建代码。~~silent_outputs 纳入自诊断摘要~~（评审砍除：给设计上就不发的东西建观测=为观测建观测）。晨报要不要恢复发送 → 附录 §4 问 Pascal。
- **验收**：daily-plan 正常跑、PLAN_LOG 正常写、不再生成卡片。

### 主题 C：交互摩擦点

#### REQ-85【P0】calendar-prep 源头修：全天状态块不生成 prep + dedup key 修正
- **证据**："Prep: 请假" 15 次 create→cancel（6/30 一天 5 次），4 夜人工清理 8 条，请假块连到 7/11 还会产噪；根因核实：(a) 全天事件被渲染成 00:00-00:00，intentions.py:1314 无过滤；(b) dedup key `cal:{date}:{title[:20]}`（:1347）按单日切，多天事件每天新 key。另全库唯一 expired 的 "Prep: 发散" 暴露 date 类 prep 静默过期路径。
- **需求**（评审修订：结构修 key，不加启发式缓存）：
  1. 00:00-00:00 全天事件跳过 prep 生成（配合请假/婚假/leave 关键词双条件，单测防误杀跨天有时刻的真事件）。
  2. dedup key 改为按事件+日期范围键控，多天事件整程只生成一次 prep。~~cancel≥2 负缓存~~（评审砍除：为同一 bug 建第二套启发式防御）。
  3. date 类 prep 静默过期留可见记录（并入 REQ-78 汇总通道）。
- **时效**：请假持续到 7/11，本条有硬 deadline，批次 2 优先。
- **验收**：7/2-7/11 请假不再产生任何 prep intent；跨天真事件 prep 不受影响（单测）。

#### REQ-86【P1】协作层双向日志收尾
- **证据**（评审更正现状）：journal 钩子已 commit（bot.sh:1546-1551 + tasks/journal_capture.py），但**只捕获"引用回复 daily-reflect 卡片"场景**，Pascal 不引用直接回就漏——"他怎么看一些事"仍进不了《Jarvis 日志》。
- **需求**：扩展捕获到 daily-reflect 卡片发出后 N 小时内该会话的 Pascal 回复（范围钉死，不泛化到所有对话——评审红线：归属误判会把无关私密消息写进 Lark 文档）。扩展部分先 log-only 验证归属判断准确率再开写入。
- **验收**：一次真实 check-in（非引用直接回）后，日志文档含 Pascal 回答。

#### REQ-88【P1】"已记录"类承诺的写入核验（影子先行）
- **证据**：6/17 15:55 "？"×3 事故——Jarvis 自认"说'记进去了'但当时根本没写"（信任级）；doc_guard.py 零生产调用方（仅测试引用），memory 写入类型裸奔。
- **需求**（评审收窄）：bot.sh 回复后置核验，断言检测**收窄到第一人称完成式且明确指向持久化**的表述；检查目标覆盖全部写入面（三个 memory 目录 + tasks.jsonl + journal + intentions DB）；不符时**只提示不代写**，提示复用 REQ-59 去重。
- **上线节奏（评审红线）**：影子模式 1-2 周只记（断言, 检查结果）对账日志，假阳率 <5% 才开提示。宁漏勿误纠——doc_guard 零调用的教训就是别再建没人走的守卫。
- **验收**：影子日志可复盘；模拟"嘴上说记了没写"能抓住。

#### REQ-89【P2，评审改判】下线 free-time-nudge
- **证据**：11 发仅 1 条 late_reply 弱响应（"hi"，gap 912s），无有效 engagement；REQ-75 已收紧过一轮（内容闸+日限 2+块限 1）仍无起色；engagement_insights 连续两期建议收紧；pre 脚本无会话活跃检查。
- **需求**（评审改判：按"慢源即坏源要替换"逻辑，0 转化的主动打扰源直接删，不再"加闸+观察一期"）：从 components.yaml/HEARTBEAT 下线任务，保留脚本文件，想恢复一行加回。
- **验收**：任务不再被调度。

#### REQ-90【P2】意图闭环边角修（3 件打包）
- **证据**：① context 类目闭环黑洞 2 例（followup=None 且 TTL 只扫 awaiting，违反代码自己的"ALWAYS maintained"注释）；② 3 个 cron 意图带 closure_question 永不跟问（死字段）；③ 1 例 done 但 closure_result 空串。
- **需求**：① context 执行后直接置 na+closed_at（收口现存 2 例）；② cron 创建时拒收 closure_question；③ done 空 result 时 **coerce 为 na+日志，不 raise**（评审红线：抛错会让闭环写入失败制造新僵尸）。~~④ cancel_reason 字段~~（评审砍除：为不存在的监控做 schema 迁移）。
- **验收**：单测覆盖三条；现存 2 例黑洞收口。

## 3. 优先级与上线批次（风险评审定稿）

| 批次 | 内容 | 风险控制 |
|---|---|---|
| 1（外围，先让眼睛复明） | REQ-80、82、83、84、89 + REQ-78 的汇总/自诊断部分 + REQ-81.1 僵尸下线 + 81.3 顺手活 | 常规测试；全部不碰心跳失败分支和意图状态机 |
| 2（意图引擎，单独一批） | REQ-85（硬 deadline 7/11）、REQ-90 | 当天红队审查 |
| 3（心跳核心，单独上） | REQ-79.1（去连坐+共享冷却）；79.2 一周后 | 当天红队 + 24h 观察窗；一次只改一个变量 |
| 4（依赖数据/影子期满） | REQ-78 billing 补发、REQ-81.2 记忆任务修复、REQ-88 影子→启用、REQ-86 | 各自影子/数据门槛见需求内 |

总原则：78/79/88 分属意图引擎/心跳核心/回复链路三个层次，**不同一天上任意两条**；每批上线当天红队审查；生产重启一律 `launchctl kickstart`。

## 4. 附录：待 Pascal 拍板（不阻塞本轮）

1. realtime timer 的 TASK_SIGNAL_META 分类、feedback late_reply 权重——两条 decision-blocked 调参（6/22 起挂起）。
2. 晨报卡片要不要恢复发送（REQ-84 默认维持不发）。
3. 稳定性机制 6 条 backlog（6/15 设计，已上 2/8）。

## 5. 非目标

PGC 信源时效/覆盖（独立线）；EigenFlux 网络功能增强；语气/文风再工程化；对话延迟优化（数据证明链路不慢）。

## 6. 评审裁决记录（2026-07-01）

三路独立评审（必要性/简单性、证据真实性、风险回归）主要裁决：
- **证据**：全部行号级引用属实、关键数字可复算，无捏造；修正 5 处计数（5→8 occurrence、4→5 次请假、12→11 发 nudge、804→866 失败、"熔断 8 天"→"8 个不同天"）。
- **砍除**：REQ-87 独立立项（降为顺手活）、82.3 P0 推送、84 的 silent_outputs 观测、85.2 负缓存、90.4 cancel_reason、89 的"加闸观察"（改直接下线）。
- **系统性纠偏**："对每个问题倾向加一层机制而非删掉源头"——已按删源头原则修订。
- **新增红线**：78 走 breach 队列+幂等；79 拆批仅限 parse_failed 且需共享冷却；82 必须 Tier0/cron 接线；83 旧快照带时戳；88 影子先行只提示不代写；90.3 coerce 不 raise。
