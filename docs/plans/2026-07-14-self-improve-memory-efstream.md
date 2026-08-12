# PRD — 2026-07-14 自改进轮：system 记忆层饥饿 + ef-stream 重连卫生

> 历史实施记录，不是当前规范。请先看本目录 README、`docs/prd_portfolio.md` 与当前架构文档。

来源：2026-07-14 自改进巡检（日志 + 实测 loader + 代码走读）。
两个主题，六个需求（REQ-91 ~ REQ-96，接 v4 的 REQ-90）。

---

## 主题 A：system 记忆层长期饥饿 —— heartbeat 看不见 mail inbox / issue 文件

### 问题

`load_tiered_memory` 每轮都在 warn：`system tier truncated — dropped ~24k chars
(budget 40000)`。实测（2026-07-14，live memory dir）：

| tier | 实际加载 | budget | 结果 |
|---|---|---|---|
| hot | 24,484 | 25,000 | 勉强放下（98% 满，一次日历大日就静默切 identity） |
| warm | 119,353 | 余量 ≈132k | 完整放下，还剩 ~13k 闲置 |
| system | **63,782** | **40,000** | **每轮丢 ~24k** |
| timeline | 3,103 | 15,000 | 放下 |

被丢的段（按优先级从尾部丢）：`inbox_ops (partial)`、**`inbox_private_mail`（全丢
——mail-triage 的产出 heartbeat 从来看不见）**、两个 issue 文件、6/20 审计报告。

### 根因（三层，互相咬合）

1. **常量之间算术上不自洽**：todos cap 20k + open_threads ~12k + roadmap ~9k +
   两个 inbox 各 12k load-cap + 杂项 ——合计 ~64k，而 SYSTEM_BUDGET=40k。
   即使一切按设计运转，尾部也必然永久不可见。谁改 cap 都没对过总账。
2. **perception `_trim_inbox` 的保留量和 loader cap 不同单位**：磁盘保留
   500 行（≈35k chars），loader 只注入尾部 12k chars——2/3 的保留内容永远进不了
   prompt（纯暗物质）；且按行切割导致文件头部是半截 entry（已实测观察到）。
3. **告警有环无人接**：`tier_truncated` 结构化 warn 注释里写"so selfmon/alerting
   can see it"，但 selfmon 的 `LOG_FAILURE_SIGNATURES` 里没有它——多日截断只活在
   log 里。

### 需求

**REQ-91 预算校账**
- `SYSTEM_BUDGET` 40,000 → 56,000；`HOT_BUDGET` 25,000 → 30,000。
- 依据：REQ-92 落地后 system 稳态 ≈ 6.1(open_threads) + 12.9(todos) + 0.4 + 2.0
  + 2.7 + 8.9(roadmap) + 0.9 + 0.8 + 8+8(两 inbox 新 cap) + ~3(活 issue) ≈ 53.7k，
  56k 放下且留 slack；hot 30k 给 identity 留出日历大日余量。
- 总账：over-budget 时 used ≈ 30+56+15=101k 楼板，warm 余量 ≥99k；当前 warm
  实际 119k → 最坏挤掉 warm 尾部 ~5k（warm 自带 21 天降级 + 优先序，可接受；
  实际稳态下 system 只用 ~54k，warm 几乎不受挤）。
- 新增**常量自洽测试**：`TODOS_MAX_CHARS + sum(_SYSTEM_FILE_CAPS.values()) +
  非 cap 文件经验余量(≤20k) ≤ SYSTEM_BUDGET`，防止下次改 cap 再欠账。

**REQ-92 inbox 保留量与注入量对齐（修 perception `_trim_inbox`）**
- 磁盘保留从"500 行"改为"**按 entry 边界的 char 预算**"：保留最新的整 entry
  （`### ` 开头为界），总量 ≤ `core.memory._SYSTEM_FILE_CAPS[buffer]`（单一事实源，
  import，不复制数字）；无 cap 的 buffer 沿用 500 行旧规则。
- `_SYSTEM_FILE_CAPS` 两个 inbox 12,000 → 8,000（mail triage 消费节奏下 8k ≈
  2-3 天窗口，足够；省出的 8k 给 issue 文件和 slack）。
- 溢出照旧归档到 `warm/archive/perception_archive_YYYYMM.md`（loader 不读），
  归档先落盘再替换正文（沿用 archive-not-delete 契约）。
- 效果：文件头永远是完整 entry；磁盘内容 ≈ 注入内容，不再有暗物质。

**REQ-93 已解决 issue 文件自动归档（memory-tidy）**
- `memory_tidy_post` 新步骤：`system/*.md` 且 YAML frontmatter `status:` ∈
  {fixed*, resolved*, closed, done} 且 mtime > 7 天 → 移到
  `memory/archive/system/`（mkdir -p；同名加日期后缀防覆盖）。
- 每移一个打一行 `[memory-tidy]` 日志。无 status 字段的文件永不自动动。
- 一次性手工：`stale_cleanup_audit_2026-06-20.md`（type: reference，误入
  system/）手工移入 archive/system/，不为它写规则。

**REQ-94 截断告警接入 selfmon**
- `core/selfmon.py` `LOG_FAILURE_SIGNATURES` 增加 `"tier_truncated"`——
  持续截断从此出现在 selfmon report / dashboard 的 silent-failures 计数里。

### 非目标
- 不动 MAX_MEMORY_CHARS（200k 总量是成本/延迟决策，另议）。
- 不动 warm 的降级/排序逻辑。
- 不给 tier_truncated 单独做 paging（selfmon 可见已够；要不要页由 Pascal 看报表后定）。

### 验收
1. live memory dir 实测 loader：system tier 无截断 warn（REQ-92/93 清理后）。
2. 单测：entry 边界 trim（半截头文件修复、archive 内容完整、锁语义）、
   status 归档（fixed 走 / open 不走 / 无 frontmatter 不走 / 7 天内不走）、
   常量自洽、selfmon signature 计数。
3. 全量测试套件绿。

---

## 主题 B：ef-stream 静默日重连卫生 + probe 日志语义

### 问题

2026-07-14 全天：stall watchdog 每 30 分钟杀一次无输出的 stream 子进程（设计内、
防半开 TCP），但每次 kill 走的是统一失败路径——`failures` 只增不减（已到 #27）、
backoff 长到顶格 300s。安静日（零 PM 本来就正常）的代价：
- 每 35 分钟盲 5 分钟，全天累计 ~2h 不在线；
- `failure #27` 让人误判为持续故障（本轮巡检实际耗时定位）。

`failures/backoff` 只在**收到消息**时归零——安静日永远没有消息，永远不归零。

另：spend-limit 闸门 tripped 期间，每 30 分钟的选举 probe 失败打出
`Claude exited with code 1` + spend-limit 原文（warn），和真实故障一字不差——
同样造成误判（7/7 事故教训是不能降回 info，但可以标注语义）。

### 需求

**REQ-95 按连接寿命重置重连状态（ef_stream_loop）**
- 记录每次连接寿命；连接结束时：
  - 若寿命 ≥ `HEALTHY_CONN_S`（600s）→ `failures=0, backoff=1`，日志打 info：
    `lived Ns — treating as healthy churn`。（实现时收紧：不采用"收到过输出即重置"
    ——服务端若每次吐一行错误就断连，会变成 1s 重连风暴；消息级重置本就存在于读循环内）；
  - **例外**：`Connection replaced`（另一会话接管）不重置，仍走指数退避——
    否则两个会话 1s 互抢，ping-pong 风暴；
  - 其余（短命退出）照旧 `failures+=1` + 指数退避。
- 判定抽成纯函数（如 `_reconnect_policy(lifetime_s, got_output, replaced)`），单测覆盖
  四象限 + replaced 例外。
- auth exit 4 路径不变。

**REQ-96 选举 probe 失败日志标注（heartbeat）**
- `gate_state == "probe"` 且 `not use_backup` 的失败，warn 行追加语义：
  `(elected primary probe while gate tripped — expected until limit resets; falling back to backup)`。
- 保持 warn 级别（7/7 教训：info 埋了 6.5h）。只改文案，不改控制流。

### 验收
1. 单测：reconnect policy 四象限 + replaced 例外；probe 标注文案出现在失败分支。
2. 部署后观察一个安静时段：stall kill 后 1s 内重连、无 failure 计数攀升。
3. 全量测试套件绿。

---

## 追加（同日，Pascal 指出的昨日遗留问题 + 红队评审产出）

**REQ-97 intentions CLI `create` 子命令**（Pascal 7/13 08:23「没能把这条埋进 intent（CLI 报错）」）
- 之前 CLI 只有 list/due/close 等，没有 create——agent 会话想落 intent 只能报错。
- argparse 实现（不是手写 flag 扫描——打错 flag 必须响亮 rc=2，不能静默吃掉）；
  trigger_type 白名单（未知类型会插入 check_due 永远不触发的僵尸行，REQ-53 类）；
  --priority 强类型；--expires-at ISO 校验（cleanup 是字典序比较，非 ISO 值
  要么永不过期要么立即过期）；create_intent 的 ValueError → 干净 rc=2。

**REQ-98 checkin 活动证据 + JSON 信封解包**（Pascal 7/13 09:09「下次自进化注意一下」+ 7/14「联系卡片不知道在讲什么」）
- checkin_pre 注入实时活动证据（/tmp/jarvis-last-msg mtime + 当日 Quote reply 计数
  + 奏折互动计数），并明确「信号缺失≠闲着」；弃用 conversation_audit.db
  （每日 04:20 才灌一次，晚上查必然是 0——红队抓住的自引入回归）。
- checkin_post 解包模型误用的 {"response","action"} JSON 信封（裸 JSON 直接上了
  Pascal 的卡片）；action=silent/skip/none 时不发卡。

**REQ-99 日程变动卡片完整化**（Pascal 7/14「日程变更功能不完善」）
- 每行带日期+星期；同标题增删配对合并为一行「改期：X — 旧 → 新」；
  超出显示上限计数而非静默丢弃；标题「变动」→「日程变动」。

**红队评审驱动的收紧**（对角线 A/B/C + cleanup 四路 agent，采纳 17 项）
- expected=true 结构化标记贯穿三个消费面（selfmon/admin/dashboard）：
  选举 probe 失败与 warm 挤压不再染红运维面板。
- probe 标注仅在 backup 真可用且非 137/143 时打（backup env 缺失=真事故，必须继续报警）。
- ef-stream 增加 quiet_streak 计数：长寿命零输出连接（安静日 vs 服务端哑火
  协议上不可分）立即重连但保持可见，连续 6 次升 warn。
- 邮件 inbox 是工作队列不是显示缓冲：<48h 条目保护不裁（mail-triage 每轮最多消费
  15 条，突发/停机时裁掉=静默丢邮件）；>7 天条目强制离场（补上 PRD §5.4 从未实现的
  年龄界）；trim 加 flock+size 复核（对齐 todos 的 2026-07-08 竞态修复）。
- REQ-93 frontmatter 判定行锚定 + 大小写不敏感（开头是 markdown 分割线的运维笔记
  不会被误归档；人手写的 Status: Fixed 也能匹配）。
- HEARTBEAT.md memory-tidy prompt 里的预算数字同步更新（旧数字会让 tidy LLM
  按 25k/40k/120k 误判各 tier）。

## 交付顺序

1. REQ-94（一行）→ REQ-91 → REQ-92 → REQ-93（同主题连续交付，改 memory 面）
2. REQ-95 → REQ-96
3. 一次性手工归档（audit 文件）+ live 验证 loader 不再截断
4. 对抗评审（大修复后必须，2026-07-02 规则）→ 全量测试 → 提交
