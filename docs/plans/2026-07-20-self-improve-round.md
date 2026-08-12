# 2026-07-20 自改进轮 PRD（REQ-103~111）

> 历史实施记录，不是当前规范。请先看本目录 README、`docs/prd_portfolio.md` 与当前架构文档。

实证来源：① 7 天 Lark DM 全量复盘（775 条消息，oc_<redacted>）；
② /tmp 日志 + sched_events + memorials 台账 + conversation_audit.db；③ todos.md 已批立项（7/14）。
今天生产基线：components 12/12 绿，1505 tests 绿。

价值排序原则（phronesis）：先修「Pascal 亲眼看到并骂过的」，再修「结构性防复发的」，
最后修「运维自嗨的」。每一项都要能回答：修完后 Pascal 的哪一次真实体验会不同？

## P0

### REQ-103 把 7/18 已批功能真正部署到 Pascal 手机上（推荐回复按钮 + 静默窗 09:30 + prep 反幻觉）
- **证据**：7/18 15:27 Pascal 亲口要"活按钮"，15:33 "你开始做吧"。代码 7/18 写完、83 tests 绿，
  但**从未 commit、进程从未重启**。7/19-7/20 每张带 OPTIONS 的卡都把 `OPTIONS: 聊聊试讲 | 知道了`
  当正文裸奔 + 挂着老死按钮（om_x100b6af7aada10a0c00c8b1efd99b4e 等）。当时还回报了"做完了，验证过"。
- **教训（对抗自己）**：generator 侧验证 ≠ 手机上收到的样子；长驻进程改代码必须重启才算部署。
- **做法**：commit 落库 → 重启 bot 栈（kill + watchdog 拉活 / launchd kickstart）→
  从 memorials 台账 + 实卡验证 OPTIONS 行被吃掉、按钮渲染出来。
- **价值**：他点名要的功能从"谎称做完"变成真做完；同时救回随时可能被 git 操作毁掉的未提交工作。

### REQ-104 卡片泄漏哨兵：投递侧自检进 conversation-audit（防复发类修复）
- **证据**：模板/JSON/prompt 头泄漏进卡是**惯犯家族且 7/20 仍活跃**：
  HEARTBEAT_OK ×3（7/15 Pascal："这卡片非常蠢…好烦"）、raw JSON `{"response":...}` ×4、
  整卡只有一个词 "send"（7/17）、"=== TASK: checkin ==="（7/17）、"[2026-07-19 09:16] checkin"
  时间戳头（7/19×3、7/20，**现行 bug**）。每次都是修单点，家族继续生。
- **做法**：① conversation_audit 新增 issue 类型 `card_template_leak`：扫 memorials.jsonl 近 24h
  已投递卡体，signature 集：`HEARTBEAT_OK`、`^\s*{"response"`、`=== TASK`、`^send$`、
  `^OPTIONS[:：]`（正文残留=按钮没被吃）、`^\[20\d\d-\d\d-\d\d .*] \w+`（prompt 头）。
  命中 = open P0 finding，自动进下轮自改进必查项。② 顺手修现行的 checkin 时间戳头泄漏。
- **价值**：Pascal 骂过两次的"蠢卡"从此有自动看门的；把"修单点"升级成"修类"。

## P1

### REQ-105 audit 闭环工作流（7/14 Pascal 已批立项）
- **证据**：audit_issues 全库 115 open P0 / 43 P1 无人收口，同类 provider 错误天天重开新 P0；
  7/19 的 `model_transparency_requested` P1 实为误报（"模型是什么模型"问的是 MOVA，
  真问题是回答高度不对——先讲战略论文没讲基础 IO，见 REQ-108 措辞条）。
- **做法**：`python3 -m core.conversation_audit open-findings [--days N]`（跨 run 去重的 open 视图）
  + `resolve --id/--type --note`（写 resolutions 表；后续 run 重推导同证据自动带 resolved 状态）。
  收口现有积压：7/14-16 额度事故家族 → resolved（已恢复，有 gate 页警机制）；
  model_transparency → resolved-false-positive 注明 MOVA。
- **价值**：audit 报表从"喊了没人听"变成自改进闭环的记账本；本轮起裸说 self improve 就先读它。

### REQ-106 pgc_pulse 告警去重（对老板喊狼来了 ×20）
- **证据**：7 天 20+ 张 🚨"一手源挂了"卡，7/19 一天 10 张（每 2h 重报同一个 broken_first_party=1，
  且当晚自查结论"没有任何一个源真的挂了"）；7/19 18:40 Pascal："上下文里面有太多关于PGC链路的问题了"
  ——教科书级 alert fatigue。根因：sources/metrics_probe.py 对 tripped 规则**每个 collect 都
  append history**，metrics-digest 又被禁止合并记录（458ce63），于是每 2h 一张新卡。
  inbox 信号侧有 per-day event_id 去重，history 侧没有——只堵了一半。
- **做法**：anomaly history append 加 per-(metric, day) 闸：当天已报过且当前值没恶化就不再 append
  （恶化 = 与当天已报 actual 比按 op 方向更坏）。标题顺带去黑话（用 label 而非裸 metric 名）。
- **价值**：同一件事一天最多 1-2 张卡（恶化才追报）；🚨 的信用恢复。

> 对抗评审后附注（7/20 深夜）：红队 10 发现全数处置——CJK 正则吃正文/resolve --id 幽灵重现/
> 非每日 cron 误吞 prep 三个 P1 已修；升级档吞告警/坏 state 崩溃/stamp 截断死循环/查询失败降级已修；
> 部署必须连 daemon 一起重启（旧 daemon+新 post 会复活 8h 接力）；存量重复 prep 已手工取消，
> 代码闸只防新增（有意为之）。

### REQ-107 晨间康复 anchor 双发
- **证据**：7/19 08:16+08:46、7/20 08:17+08:45 同内容两张卡——int_d22aea912e（7/18 建）与
  当晚补的日历日程两条链路各发一遍。
- **做法**：查明两条发射源后收敛为一条（优先留 intent 链路，日历侧那条 prep 对同日同题去重
  或直接删重复日程）；数据修 + 最小代码闸。
- **价值**：他自己要的晨间锚点不再像复读机。

### REQ-108 证据诚实全局规则（把"按事故打补丁"升级为"按失败类立法"）
- **证据**：5 天 4 起同根事故：7/13 无 commit+空日历 → 断言"闲了一天"（"今天不是一直在工作吗？"）；
  7/17 日历块 → 断言"你人在世博展览馆"（"我今天没去"）；7/17 stale 缓存报已解决的冲突；
  7/17 智谱访谈把他和对方专家角色写反。7/16 humanlaya 幻觉后 bot 自己承认：6/12 就抓过同类，
  但护栏修在单任务里。7/18 的 NEVER-INFER 规则也只写进了 calendar-prep 一节——又是单点。
- **做法**：HEARTBEAT.md 全局章（所有任务生效）：① 无信号≠闲着（缺 commit/日历只是"没观测到"）；
  ② 日历块≠人真在场；③ 陌生实体要么当轮真查要么明说不知道（把 7/18 prep 规则提升为全局）；
  ④ 断言他的状态/角色前先问"我的证据链是什么、多新鲜"。回答高度条：他说"教我 X"时先给
  最基础的是什么/怎么进出，再谈战略（7/19 MOVA 事故的措辞版）。
- **价值**：这个失败类 5 天烧了他 4 次信任；类级立法 + REQ-104 哨兵是本轮"防复发"的两根柱子。

### REQ-110 🔧 工具噪声治理（220 条/7 天）
- **证据**：7 天 220 条 🔧 状态消息（7/16 一天 83 条、峰值 39 条/小时），多为英文内部计划碎片，
  6 条裸 "🔧 Execution error"，1 条自相矛盾（"我还没有执行任何工具调用"）。违背 no-filler-ack
  （f0dfa04 只删了 thinking-ack，没管工具面）。
- **做法**（保守，不动交互骨架）：默认不发单条工具narration；仅保留：出错时一条人话错误
  （禁止裸 "Execution error"）+ 超长任务的转后台通知。实现点在 bot.sh 工具状态发射处，先读代码再动手。
- **价值**：DM 信噪比直接翻倍以上（220/775 = 28% 的消息是这个）。

## P2

### REQ-109 自诊断代发风暴（每 8h 一条"它自己的告警没发出去"）
- **证据**：daemon 6 天代发 6 次同一条警告；机制 = daemon 代发写 stamp → post 因同窗静默 →
  4h 后 daemon 再代发，无限接力，且每条都对 Pascal 重复指控主路径坏了。
- **做法**：stamp 已存 lines——代发/自发前先比内容：warning 集合无新增且 <24h 就不再发；
  措辞去掉"没发出去"指控（多数时候是 dedup 挡的，不是路径坏）。

### REQ-111 降级透明标记
- **证据**：7/14-16 主备两头挂，静默换模型/换商，audit 连开 5 天 P0；gate trip 已会页警一次，
  但降级期间的每条回复无从分辨。（7/19 那句"什么模型"是问 MOVA，不构成直接需求，权重下调。）
- **做法**：仅在 gate≠primary 或实际模型≠opus 时，回复末尾加一枚极简标记（如「·备用通道」）。
  正常状态零变化——符合他的 no-noise 铁律。

### 运维顺手账（部署时一并）
- 杀 7/13 测试 clone 遗留的孤儿 admin.py（PID 25825，:3459，一周了）。
- dashboard 日志文件被删但进程还握着 inode → kickstart dashboard，恢复 /tmp/jarvis-dashboard.log。

## 本轮明确不做（记录判断，防止下轮重查）
- **envelope parse_failed（16 次/7 天）**：extract_json 已认 ```json 围栏；近期失败是模型输出
  截断/缺括号，拒绝解析是刻意设计（防半截内容发给人），5min 快重试全部自愈、零用户影响。
  audit 侧 resolve 记"working as designed"。
- **日历凌晨时间渲染（8/9 03:00 吃饭）**：疑似全天事件/时区 artifact，需要复现窗口，立单独查，本轮不盲修。
- **EigenFlux 每条消息双卡、手机闹钟打通、EF onboarding 漏斗**：功能设计题，进 backlog 等 Pascal。
- **notify-intent 闭环**（7/17 承诺）：实现时先查 git——若已修则只补验证，未修列下轮 P1。

## 验收
1. Pascal 下一张决策卡上出现内容相关的推荐回复按钮（从台账 + 实卡双侧验证）。
2. `open-findings` 输出 ≤ 真实活跃问题数；115 条陈年 P0 全部带 resolution note。
3. pgc_pulse 同一持续故障一天 ≤2 张卡。
4. 晨间 anchor 每天恰好一张。
5. 全测试绿 + 12/12 组件绿 + 对抗评审后再 push。
