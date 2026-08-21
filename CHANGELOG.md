# Changelog

All notable changes to Pascal Jarvis. This project tracks requirements as
`REQ-NN` across PRD cycles in `docs/` (prd_interaction_quality v1, REQ-01~29;
prd_system_iteration_v2, REQ-30~58; prd_interaction_v3, REQ-59~77;
prd_interaction_v4, REQ-78~90; self-improvement waves REQ-91~118). From
1.7.0 onward `docs/prd_portfolio.md` is the authority on which PRDs are
shipped, superseded, or rejected, and requirements are traced to evidence in
`docs/release_acceptance_2026-07-24.md`.

*（补记 2026-08-21：1.8.3 ~ 1.15.0 各段由已合并 PR #25~#96 回溯整理，
版本号是记账分组，未打 git tag。）*

## [Unreleased]

### Fixed

- 跨 Session 的重要发现现在带有可核验的工作收据，能够通过主动输出硬门禁，
  进入 ledger-only 台账并在晨间锚点攒批，不增加实时卡片打扰。
- EigenFlux 分析层拒绝 Claude/Codex 的 XML 工具调用与缺失工具结果转录，
  避免内部命令和路径进入用户卡片；原始来信仍会安全送达。
- EigenFlux 自动回复的活动账按服务端消息回执和入站事件双重去重；崩溃重放
  不再重复消耗自动回复速率额度。
- OpenAI Responses 的首轮文本输入统一使用列表结构，兼容要求严格的中转层；
  Claude 限额且 relay 超时时，只读心跳任务可以继续落到 GPT。
- Dashboard `:3457` 退役后的产品组合文档和上层 Agent 入口说明完成同步。

## [1.15.0] — 2026-08-20 — 系统学会说「我不在」+ 长卡片终于能读完

8/17 起卡片从 76 张/天跌到 2 张——不是代码坏了，是 MacBook 合盖断电 39 小时，
而 daemon 探到 38 次全部走 grace 静默放行。同一个失败类四处发作：没有任何面
看得见「宿主睡了」，于是每个面都赖它自己能看见的那个东西。这一轮把它变成
一件可测量、可报告、不占预算的事。

### Fixed

- **睡眠仪器一直在说谎**（#89）：睡眠计量改用 wall clock 减 monotonic
  （旧仪器对那段 39.4h 的合盖只记到 0.7h；wall-mono 漂移全程实测 39.69h，
  与 daemon 38 次 gap 合计对上）；grace 收归一处，只赦免「陈旧」
  不赦免「死活」；告警不再替检查下诊断。
- **宿主缺席从「探测到」变成「报告出来」**（#88）：wall-mono 漂移
  （hostclock）是唯一的睡眠尺；恢复后按与非静默时段的重叠 ≥3h 出一张缺席
  回执卡，过夜缺席不打扰；components 按醒着时长计龄，合盖 39h 不再被记成
  一串任务失败。
- **缺席回执不占注意力预算**（#91）：smoke 撞出的教训——醒来 23 分钟烧光
  当天 9 张预算，回执自己反被 global_daily_cap 吃掉。
- **被日额度丢掉的卡以前在任何面都不留痕**（#96）：8/19 丢 13 张、8/14 丢
  14 张就这么无声消失，其中有 decision 级。现在 cap 丢弃记 `ledger_only`
  进晨匣攒批，decision 级单独出行——cap 的意思是「今天没位置」，从来不是
  「不再成立」。
- **Guardian 先修再喊**（#94，契约文档对齐 #95）：对自己管的组件先精确修复、
  在有界宽限窗内验证恢复，修不好才呼叫；投递回执分清确认/覆盖/待定/真丢。
- **运行时与日历投递收尾**（#92）：优雅重启窗口过后强制收割残留进程
  （新旧 ef-stream 不再永远互相顶掉）；已过去的日程不再被报成「取消」
  （含跨月、跨年边界）。

### Added

- **「查看全文」**（#90）：被截断的奏折一键自动分片补发全文，带投递回执、
  去重、断点续传；主动卡和 Routine 提案必须先有具体已完成工作的回执才许
  上飞书；EigenFlux 私信在 WebSocket 旁补上轮询对账。
- **长卡片手机可读**（#93）：长飞书卡片按手机排版收拾。

## [1.14.0] — 2026-08-17 — 收 8/17 审计的账

### Fixed

- **投递挂在 keychain 上的根因收口**（#81）：Lark bot 投递与用户 OAuth
  解耦——后台拿不到 keychain 不再等于发不出消息。同一发布还打包了模型
  控制面 + 历史会话记忆、心跳内存写边界、投递中断显性化 + 日历重试设界、
  私有内存与 EigenFlux 契约加固。
- **供应商故障后例程能恢复**（#82）：模型传输失败走 fallback，并禁止对
  例程做不安全的模型重放。
- **运行时恢复闭环**（#86）：每条告警有持久事故身份；Lark 恢复经验证后
  对账终态投递失败；只重放未解决未过期的工作；真实请求的证据压过 canary
  小探针；真实传输不健康时扣发外部 dead-man 的「我还好」。
- **恢复补发也守注意力上限**（#87）：补课不许挤爆当下。

## [1.13.0] — 2026-08-14 — 逻辑会话 + 供应商链把故障当常态

### Added

- **逻辑会话生命周期**（#74，发布证据 #75）：以 Matter 为持久逻辑会话，
  飞书私聊 new/switch/current/list/reset/close 指令切换；提示词、compact、
  Claude session、Codex 线程、延迟任务、奏折 handoff 全部按逻辑上下文隔离，
  队列切换、群信任边界、迁移崩溃一律 fail-closed。
- **关系记忆可执行且私有**（#73）。

### Fixed

- **供应商链自愈**（#66/#67/#78）：Codex canary 失败报得准；Codex 限额
  正确落到最终 GPT 兜底；瞬时故障后路由自动恢复，不再需要人去搬。
- **`restart --full` 部署每一个常驻运行时**（#69）：不再有「重启了，
  但有进程还在跑旧代码」。
- **持续性与注意力缺口**（#77）：退役 Jarvis 自有 Tailscale、手机配对、
  bearer 网关，飞书是唯一移动决策面；九张/天的主动注意力预算落地；
  EigenFlux 协议健康 + digest 部署验证。
- 断供期间 intent 与本地资产不丢（#71）；intent 取消时陈旧卡片同步退场
  （#72）；SQLite 迁移事务化、具名化（#70）；消息处理器生命周期加固
  （#76）；闭环告警与生命周期政策对齐（#68）。

## [1.12.0] — 2026-08-12 — 注意力预算闭环 + 能力架构加固

### Fixed / Added

- **主动注意力闭环**（#57）：真正送达的飞书卡是唯一权威的存在感信号；
  日预算与突发预算原子执行；奏折生命周期在 thread 回复与卡片动作两侧补洞。
- **没人答的 decision 4 天就归档，不是 14 天**（#56）：等两周才留中等于
  没有死线。
- **能力架构**（#61，边界决策 #60）：222 项能力逐项配上可执行测试证据；
  cross-session 与 memorial 各自拆成小模块 + 兼容门面；import 环数预算
  成为硬回归闸门（CLAUDE.md 里那两条 import_graph 检查从这里来）。
- 拼接在一起的卡片指令能正确拆开（#58）；weekly-limit 供应商 failover
  修复（#59）；post-restart 运行时验证 live-safe（#62）；环境心跳更新
  只进账本不打扰（#63）；热路径失败可观测（#64）；恢复的 L3 信号正确
  闭环（#65）。

## [1.11.0] — 2026-08-11 — 信号胜于噪声：飞书成为唯一投递面（REQ-119~123）

14 天实证：lark 通道 235 卡已读 95.7%，web 通道 170 卡已读 1.8%，且 web
transport 无条件返回成功——一个只产假 delivered 账本的死通道。

### Changed

- **封死 web fallback**（#47，REQ-119/121）：decision/notice 一律飞书；
  假成功分支全数移除；ambient 源改 ledger-only（照进账本和晨匣，不再造
  投递信封）；按源降噪把日卡量压到 12-25 张。
- **手机网关 + funnel 退役**（#46/#49，REQ-120）：:3458、Tailscale funnel、
  配对/Web Push/发到手机按钮全链剥离——死路铁律：到不了的按钮不渲染。
- **账本口径合一**（#48，REQ-122/123）：pending+decided+lapsed == created
  的恒等式有 CLI 可复算；43 张幽灵卡一次性归档；匣子卡每个数字都来自
  同一个函数。

### Fixed / Added

- **Claude 账号级会话限额也能 failover**（#50）：session cap 被识别、
  所有模型入口一致走备份、原始报错不再漏到用户面前。
- **飞书运行时可在 Claude 与 Codex 之间切换**（#51）；跨会话上下文改为
  供应商中立（#53）：Claude Code 与 Codex 的近期工作（脱敏后）注入 owner
  私聊；watermark 保留设界（#54）。
- 截断卡的「聊聊」把缺的那段文字补给你，卡面文案去掉宫廷黑话（#52）；
  EigenFlux 健康口径与调度器对齐（#55）。

## [1.10.0] — 2026-08-10 — 飞书=产品本体拍板周

8/7 Pascal 拍板：「飞书里面没有卡片了，jarvis 就没有存在感」。数据同意：
dashboard 流量为零、所有互动事件都来自飞书，而 7/24-8/2 的投递悬崖跑了
十天，每个内部检查都是绿的。

### Added

- **存在感哨兵 + 晨匣攒批**（#39）：24h 内可验证到达飞书的卡 <5 张即报警
  （悬崖那种「全绿的静默」从此有哨兵）；归档类内容在晨间锚点攒批一行。
- **常设自进化周期**（#42，8/9 提频到每天 #43）：价值账本驱动选题，内部
  可逆的改进静默做完，方向性/不可逆的出提案卡；心跳只出日程不出模型预算。

### Fixed

- **回复按钮点了就有下文**（#38）：写着动作动词的按钮不再把那句话存起来
  等他先开口；「现在授权」接上真 device flow，点了真的去授权。
- **长变更批次合并成一张卡**（#41）：日历一拍推 7 张变动卡的教训——超过
  三条它就是同一件事：「你今天日程动了很多」。
- **「47 发 0 读」翻案**（#44）：direct-sent 卡从没记过 message_ids，那是
  归因盲区不是噪声；promoted job 不再静默收场，失败有诚实回执。
- **例程卡是知会不是待批**（#45）：pause 静音键不再把康复提醒提级成带
  48h 死线的 decision（51 张卡挂在匣子里的教训）。
- EigenFlux v0.10.2 契约同步（#40）；mirror 测试第三次被上游改写措辞
  打破后，改为断言禁令本身而非字符串。

## [1.9.0] — 2026-08-03 — 投递悬崖大修周末

Pascal 三句抱怨（飞书消息很少很少了 / 卡片系统经常打不开 / 信号我在手机上
不想打开），逐条从台账重建：**7/24 起飞书从 ~60 张/天跌到 1-7 张——卡被
路由到一台从没配对过的 phone desk**，十天无人察觉。

### Fixed

- **悬崖根修**（#33）：投递按「可达面」路由（desk_reachable 门禁）+ no-op
  诚实 toast + 广播批准确定性重试；从没配对的手机桌面不再无声吞卡。
- **精选信号回到聊天**（#35）：feed-triage 的一行简报是给人看的信号，不是
  监控排气，不再归档到一个 5.2s TTFB、他理性回避的网页。
- **checkin 是产品不是任务**（#32）：7/21 的「无任务不接触」把陪伴本身
  禁掉了——静默十天、体检全绿。重写为从互动中学习的 companion。
- **心跳队列公平调度**（#30/#34）：容量够（37 vs 80 runs/h），病在调度——
  按相对自身节奏的饥饿度排序、cap 只数有内容的任务；routine-run 不再被
  批次 cap 饿死还白白花掉 occurrence。
- CI 又一次死于测试日期腐烂（#31，delegation projection fixture 过期）。

### Added

- **奏折文风契约**（#36）：结论第一句、三行说清、不需要动作必须明说、
  按钮=动作动词 ≤6 字——注入在所有面向用户句子经过的唯一组装点；每张
  待批卡顶上先亮角色行（知道就行 / 等你拍 / 即时提醒）。
- **「看不懂」按钮**（#37）：看不懂也是信号，不再和不在乎混为一谈——点了
  立刻用 ≤60 字大白话单独重讲一遍；卡保持 PENDING，困惑不算回答。

## [1.8.3] — 2026-07-31 — 移动配对收尾 + EigenFlux 网络桌面

- 手机配对三修（#27/#28/#29）：外部链接不再丢配对、配对随使用续期、
  导航不再卡死。（这一整条手机面随 8/11 REQ-120 退役谢幕。）
- EigenFlux：持久网络桌面（#25）+ host skill 契约同步（#26）。

## [1.8.2] — 2026-07-29 — 心跳终于能看见你的私人邮件

例行体检发现 `tier_truncated` 每拍都在报，而且在恶化（早上丢 9,504 字 → 下午
14,270，多丢一整段 `inbox_team`）。**这是同一个事故的第三次复发**。

### Fixed

- **system 记忆层：预算的成员没有上界，那就不是预算。** 组装 74,270 字 / 预算
  60,000，尾部三个 inbox 文件**每一拍都被算术性地切光**——心跳根本看不见私人邮件
  和团队收件箱。根因和前两次修复的假设相反：**只有受害者被 cap 了**
  （inbox_ops / inbox_private_mail 各 8k），而 `open_threads`(18.4k)、
  `todos`(13.7k)、`engineering_roadmap`(11.0k) **一个都没 cap**，三个加起来
  43.1k = 预算的 72%，加载器还没走到 inbox 就没额度了。
  7/14 和 7/21 两次都在提预算（40k→56k→60k）+ cap 被丢的文件，所以无 cap 的文件
  一涨就复发。现在**每个文件都有 cap**，未声明文件走默认 cap 并预留额度，
  `tests/test_memory.py` 断言 `sum(caps) + 预留 ≤ SYSTEM_BUDGET`——把"不太可能"
  变成"算术上不可能"。
- **顺带修掉 cap 会踩的两个坑（都是本轮自查抓到的）**：
  ① cap 切的是**尾部**，而 `_collect_system` 没传 `keep`，全吃默认的 tail —— 那会让
  `open_threads` 保住「已归档」尾巴、切掉顶部的活线，正好颠倒它自己的优先级。
  已按文件区分 head/tail 保留。
  ② todos 是 tail-keep（新条目在底），8k 的 cap 会切掉它头部的「进行中」——
  也就是你当前在做的事。已给到 13k 容下整个文件。
  ③ per-file 的尾部对齐只认 `### ` 边界，不认 todos 的 `<!-- auto-update` 边界，
  尾部会从半条开始。已补齐，与 tier 级逻辑一致。
- **`tasks/memory_tidy_post.TODOS_MAX_CHARS` 20000 → 13000**，与装载 cap 对齐：
  原本磁盘留 20k 而加载器最多注入 13k，中间 7k 是**任何提示词都读不到的暗物质**
  （REQ-92 为 inbox 消除过同一问题）。仍是归档不删除。

### Fixed（记忆内容本身，非代码）

- **`open_threads.md` 里 11 条仍在活的条目被埋在「已归档（不再主动跟进）」标题之下**，
  其中 4 条明确卡在 Pascal 身上（高德 key、Tailscale 登录、输出信道 PRD 等拍板、
  EF onboarding 等批红）。它们是被逐条追加进去的，落在了那个标题后面，于是心跳
  **从来不会主动提起**。已提升回活线区并注明来历；7 条明确写着「此线关闭/✅」的
  搬进 `open_threads_archive.md`（不进上下文）。
- `todos.md` 的 `## 已完成` 段搬进归档文件，让 cap 切尾部时切的是死内容。

## [1.8.1] — 2026-07-29 — 不允许有死路

Pascal：「死路让我用不了这个产品了」。死路 = **界面点名了一个去处，或者花掉了你
一次点击，却不把你送到那里**。全站排查后修掉 4 条，并把这条规则钉进回归测试。

### Fixed

- **匣子的「去看看」根本不跳转**（本轮自引入，明早就会发出去）。写成了
  `action: None` 的纯记录选项：点它只会把匣子标成"已批"，**哪儿也不去**，
  而我自己的代码注释还写着"routes to the web desk"。改为走 `extra_buttons`
  的真实 URL 按钮。新增 `core.mobile_access.web_desk_url()` 在运行时从 tailnet
  入口解析绝对地址（**绝不硬编码**——那是每台机器各自的个人基础设施），
  且**解析不出地址时整个按钮不渲染**：没有按钮好过一个骗走点击的按钮。
- **「飞书入口暂不可用」把成功说成失败**（首页/事项列表/事项详情三处）。
  `memorial.chat()` 返回时**背景已注入、开场白已发出**——对话其实起来了。
  但三个页面都把它自己的诚实 toast 丢掉，只看 deep_link，没有就报"入口不可用"，
  于是：动作成功了、界面说失败了、还不给你下一步。改为如实报告
  （共享 `uiutil.chat_started_notice()`，三处统一）。
- **飞书兜底卡让你"去「事项」看"，卡上却没有任何入口**。手机上 web 桌面
  没法靠打字到达。现在有可达地址就带「打开事项」按钮，没有就**连这句话一起去掉**
  —— 绝不点名一个自己到不了的地方。
- **「可在「事项」里翻回」是说给站在事项页上的人听的**。改为点名筛选页签
  （「全部」）而不是目的地：你在页面上时它可直接操作，从飞书卡片上看也仍然有信息。

### 排查范围

同时核对了未发现问题的部分：dashboard 全部 18 条路由与全部跳转目标（含 f-string
拼的 `/items/{id}`、`/matters/{id}`、`/delegations/{id}`）**无一指向不存在的页面**；
push 通知里的相对路径（`/items`、`/signals`）由 PWA 按自身 origin 解析，不是死路；
飞书卡片上其余 URL 按钮均为绝对地址。其余 `ui.notify` 的否定文案都是输入校验
（"标题不能为空"），改完就能重试，不构成死路。

## [1.8.0] — 2026-07-29 — 奏折缴回制度 + 用户自建例程

按"皇帝到底怎么批奏折"重梳理奏折动线。真实机制有六环：通政司分流 → 内阁票拟
→ 批红多为「依议」→ 默认动作是「知道了」→ 缴回销档（留中也是明示状态）→
定时定量呈递。对照台账，Jarvis 断在**卡前**（分流/事由/票拟）和**卡后**（缴回）
两段：批率本身不低（decision 59%、alert 64%），问题是"该不该送到面前"和
"没批的怎么办"。

### Added

- **缴回制度** (`core/memorial.py`, `tasks/memorial_escrow.py`)：从来没有任何东西
  清扫过奏折台账。发出去没人点的卡滚出飞书后永远停在 `pending` —— 600 张里
  314 张，110 张超过一周，其中 **47 张是 decision 级**，真要拍板的事和没人打算
  回的通知混成一堆。新增终态 `lapsed`（留中）和每日**匣子**。
  死线基于私有生产台账的聚合分布设定：decision **48h**、alert **24h**、
  notice **7 天**；精确个人活动数据不进入公开仓库。
  两种归宿刻意不同：**留中**是终态、静默、计数，且明确**不是决定**——台账绝不
  能读成他批过了；**匣子**把逾期 decision **按来源归并**成一张早间卡，绝不逐张
  重推。按来源归并让积压可读，并限制单个异常来源占满整个界面。
  有界：4 天没人批自动留中，匣子带「全部留中」一键归档。留中卡**仍可点**，
  点了会复活。Tier-0 设计——没有任何模型调用参与判断"他是否回应过"。
- **票拟** (`RECOMMEND:` 指令)：decision 卡原本只列选项，等于把内阁的活推给皇帝。
  现在可在 OPTIONS 下一行写 `RECOMMEND: 标签 — 理由`，渲染成「建议：X — 理由」
  并把该按钮变成主按钮（不再是"第一个"这种排版意外）。**没有理由的建议会被直接
  丢弃**——不能审计的推荐只是穿着建议外衣的命令。标签对不上任何选项也整行作废。

### Fixed

- **事由行**：`_title_for_chunk` 根本不认识 `TITLE:` 指令，拿**含 `TITLE: ` 前缀的
  原始首行**去量长度。两面都坏：≤40 字时把字面 `TITLE: ` 泄漏进卡头；>40 字时
  （正是那 7 个字符撑爆的）退回模块标签、指令留在正文。**79 张卡头顶写着
  「Intent」，而它们自己写好的一句话事由就躺在正文里没人用**。改为先走
  `_extract_title_line`。另补：单段正文的卡原本必然退回模块标签，现在会在
  分句边界切出事由（切不干净就宁可不切——半截短语比模块标签更糟）。
  实测覆盖：103 张劣质标题里 90 张能提炼出真事由。
- **通政司**：`attention_roi._engaged()` 用"状态非 pending 即已互动"的否定式定义，
  一次错了三样。① `lapsed` 被算作互动——自动归档会让最吵的源假涨到接近 100%，
  从而**永久锁死降级**（该模块唯一的作用）；② `__external__` 被算作互动——
  eigenflux-friends 的 decision 卡测出 13/13 = **100% 互动，而 Pascal 一张都没点过**，
  全是 EigenFlux 上游自己处理掉的。这一处反转就是台账里表现最差的决策源
  从未被降级的原因；③ 它读的 `chat_started_at` 是**幽灵字段**，全代码库无人写入
  （真实字段是 `chat_ts`），所以「聊聊这个」这个最强信号**从来没被计入过**。
  改为正向定义：只有 Pascal 自己的动作（点按钮或开聊）才算。修复后
  eigenflux-friends 100% → 8%，进入降级条件。

### 同期并入：用户自建例程、注意力 ROI 治理、邮件草稿

三项都来自对 Town（a16z 2026-06 领投的个人 AI 助理）的产品拆解：它的内核是
用户自建 Routines + 三档自主级别 + 全量审计 + 按你的语气起草。对照下来
Jarvis 最大的结构差距是"加一个自动化 = 发一次版"。

#### Added

- **Routines** (`core/routines.py`, `core/routine_evidence.py`)：用户一句话就能
  建的长期例程。触发器复用 Intent 的 `next_fire_at` 追赶原语，不新增调度器；
  每次触发前由确定性代码采集声明好的只读证据，模型不许凭记忆写。
  三档自主级别 `observe` / `propose` / `act` 由代码按**数据库里存的那条记录**
  裁决 —— 模型在返回里声称自己是 act 不会改变任何事。`act` 的白名单只有
  建 intent / 记任务 / 写笔记三种内部可逆动作；发邮件、改日历、调外部接口
  一律拒绝并原样告诉用户。每次运行都落一条审计，包括模型漏掉的（记为
  `no_output`），不存在永远 `running` 的行。
  证据 provider 有路径护栏（越界、凭证文件、二进制一律拒读）、单源和总量
  双重截断，且截断会明说。
  面板 `/routines` 展示定义 + 审计流；`observe` 级例程的产出只在这里出现。
- **注意力 ROI 治理** (`core/attention_roi.py`)：`WEB_FIRST_SOURCES` 一直是人工
  编辑的常量 —— 有人审计一次、改一行、然后再没人回头看。现在按台账实测的
  回应率来判：某来源占着决策位却长期没人回（样本 ≥8 且 <25%），就降到知会位，
  它仍在事项/信号里、只是不再声称需要决策；回应率回到 ≥50% 再升回去。
  只降不升、绝不静音、绝不碰受保护来源（日历/告警/委派/意图/邮件/签到），
  每次调整发一张卡说清改了什么、东西去哪了。
- **邮件回复草稿** (`core/mail_draft.py`)：mail-triage 读了每封信却从不落笔，
  回信成本全在人身上。现在真需要回的信会附一版草稿，语气取自 per-user 配置
  （`jarvis.yaml mail.voice` 或 memory `warm/mail_voice.md`），仓库里不硬编码
  任何人的语气。提示词硬约束：不许替他承诺时间/价格/参加与否。
  **不含发信**——Jarvis 没有发信通道，所以按钮里根本不存在"已发送"这个状态，
  措辞是"就用这版，我去发"。真要发信要单独做 authority/回滚设计并走 Delegation。

#### Removed

- `dashboard/heartbeat_bridge.py` 和 SQLite `scheduled_tasks` 动态任务路径。
  它唯一能执行的 `action_type` 是 `notify`，而那是 cron Intent 的劣化重复
  （没有闭环、没有 breach、没有追赶），生产环境零行数据。用户自建的循环工作
  现在归 Routines，带着这条路径从来没有的东西：证据、授权契约、审计。

#### Fixed（收口其他 session 留下的未完成 bug）

审计台账 `data/conversation_audit.db` 里挂了 21 条 open finding（14 P0 / 7 P1），
逐条查到源头后收敛为 4 个根因；21 → 1（剩下那条要 Pascal 拍板，见下）。

- **`TITLE:`/`OPTIONS:` 指令行漏进用户卡片**（P0 ×4，7/22–7/27，最近一次昨天）。
  根因是剥离逻辑挂在单条入口上而不是挂在 body 上：`create()` 在调用方传了显式
  options 时整段跳过剥离（intentions 闭环卡原样发出），`adopt_card()` 两个提取器
  都没跑（daily-reflect 自建富卡片，TITLE 和 OPTIONS 一起发出去）。现在剥离在
  `create()` 里无条件执行——**"去掉残留"和"谁的按钮生效"是两个决定**，调用方的
  按钮仍然优先；`adopt_card` 另补精确提取，且显式 TITLE 优先于装饰性表头
  （"🌙 回顾" 命名的是来源，TITLE 命名的才是这张卡）。带自己 OPTIONS 的卡片不再
  被一卡一事拆分（拆了会把同一个 ask 复制多份）。
- **审计自己在报假 P0**（P0 ×5 + P0 ×4）。2026-07-27 给 `empty_reply` 加的
  "必须有 delivery 证据"闸没同时加到对称的 `provider_error` 检测器上，于是它一直
  拿本机 Claude Code 会话的转录当"发给用户的内容"报 P0——实测涉事 session 全部
  `reply_sent=0`，从没发出过任何东西。已补上同一道闸。真实情况不会变暗：
  `provider_fallback_exercised` P1 仍记录每一次探测到的 provider 错误，越过安全
  边界的仍是 `provider_error_as_answer` P0。
- **provider-canary 因为"干成了本职"而被熔断**。`core.provider_health probe` 在
  有 rung 不健康时 exit 1——对人是对的 CLI 语义，对心跳是错的信号（非零 = 任务
  失败 → 熔断）。真实的 backup1 故障（HTTP 402 余额耗尽）把探针自己的熔断打开，
  一天跳过 3843 次、**32 小时没再探测过**——监控恰恰在有东西要监控的时候变瞎。
  已在 pre-hook 这个心跳契约边界上把"发现问题"和"没能去看"分开：探针跑通并产出
  报告就 exit 0，只有崩溃/无输出/输出不可解析才算任务失败。
- **`routine-run` 的 pre-hook 没有执行位**（本轮自引入，且已经在线上）。
  编辑器和工具默认写 0644，常驻 bot exec 失败，5/5 记成 `pre_error`，功能一次都
  没跑成就熔断了。新增 `tests/test_heartbeat_hook_manifest.py`：HEARTBEAT.md 声明的
  每个 pre-hook 都必须存在、可执行、`bash -n` 通过——覆盖整类，对所有任务永久生效。

已确认**不是** bug、只是台账没收口的：`activity-log 一直在失败`（PR#18 a552dc4
已修，实测健康）、7/27–7/28 凌晨的一批 brain-dead 告警（provider 链故障，已自愈）。

#### Fixed（本轮自查抓到的自引入缺陷）

- 两个新功能的卡片按钮最初写成了 CLI 的 `type:k=v` 字符串形式，而
  `_execute_action` 取的是 `{"type", "params"}` 字典 —— 按钮会渲染、能点、
  然后在回调线程里抛异常什么都不做。已改为字典形式，并为两处各加了一条
  "按钮必须是执行器能派发的形状"的回归测试。
- `routine-run` 的 pre 脚本会消费 occurrence（推进水位线、开审计行），
  原本不在 `ACK_REQUIRED_TASKS` 里：Claude 调用一死，这些 run 就悬到
  60 分钟后的巡检才收尾。已加入，post 也识别 `__NO_ENVELOPE__`。
- routine 卡片走 `core.delivery` 自投递，不经过 `user_messages`，所以批次
  envelope 的 `has_card` 守卫看不见它们，合并摘要会把同一件事再说一遍。
  新增 `SELF_DELIVERING_TASKS` 抑制。
- 注意力治理最初读 `_default_attention` 来度量，那会把自己的降级读回来当作
  支持降级的证据；且升级判据看的是降级后已经没有数据的决策位，会导致每 6
  小时反复降级/升级。改为度量 `natural_attention`、升级判据看降级后实际所在
  的知会位。两条都有专门的回归测试。

## [1.7.1] — 2026-07-27 — provider canary reports the real failure

Found by the v1.7.0 post-release provider canary, which is exactly the
evidence step meant to catch this.

- The Claude CLI writes advisory notices to stderr even when the real failure
  is in its JSON result — a configured relay token legitimately produces
  "claude.ai connectors are disabled". `probe_provider` reported stderr first,
  so a backup relay failing authentication surfaced as a connector notice and
  pointed the operator at the wrong thing. The reported reason is now the
  run's own result, with the API status prefixed, and stderr only when the
  result says nothing. Verified against the live relay: the message went from
  a generic "connectors are disabled" notice to the relay's own
  `HTTP 403: Failed to authenticate` — the fact an operator can act on.

Not a code defect, but now visible and needing an owner action: **backup
relay 1 is down** — the relay account lacks access to the configured model
group. The chain still has a healthy primary and a healthy tool-capable GPT
final fallback; backup2 remains intentionally unconfigured.

## [1.7.0] — 2026-07-27 — verified delegation, one delivery pipeline, deploy-as-verify

The largest release since the project started: 70 commits, 279 files,
+52.7k/−2.8k, 40 new `core/` modules and 38 new test files, none of which had
ever been cut into a release. The through-line is **authority** — who is
allowed to declare a piece of work finished. Before this release a model
saying "done" was, in several paths, the only evidence that anything had
happened. It no longer is anywhere.

Requirement-to-evidence ledger: `docs/release_acceptance_2026-07-24.md`.
PRD status map: `docs/prd_portfolio.md`.

### Verified Delegation (VD-01~VD-10) — new subsystem
- A Delegation is captured only from an accepted responsibility, binds a
  stable target and principal before any mutation, and carries a risk class:
  R2 confirms, R3 approves, R4 never leaves human hands.
- Completion requires deterministic read-back from a named authority. Every
  connector (Git, runtime, Delivery, EigenFlux, Lark, calendar, doc) registers
  a verifier; model prose can no longer close anything.
- Retries, callbacks, and handoffs are idempotent on an action key plus source
  event key plus contract version, so a replayed callback cannot double-mutate.
- Partial success stays partial: a required-step DAG aggregates rather than
  rounding up. External waits are durable and resumable through a reconciler.
- Linked objects are one-way projections of the Delegation, not competing
  authorities. Automatic capture stays gated behind a Phase 0 threshold that
  needs 50 reviewed samples across 14 days and 5 connector classes.

### One delivery pipeline, one user inbox
- `core/delivery.py` is now the single policy and state machine for replies,
  proactive output, cards, web, and push. Producers no longer carry transport
  policy; low-level transports are adapters.
- Memorial-first Items: `/items` is the one inbox, Matter is the topic, Intent
  is the timer, and the old inboxes redirect. Intent lifecycle, scheduling,
  and closure moved to stable modules with the boundary enforced by tests.
- Retry is bounded (cumulative budget 9) with a terminal `failed` state and a
  dead letter only at terminal — a failed transport used to retry forever.

### Deploy as verify
- `core/release_gate.py` fails closed: a production restart is refused unless
  the revision is merged, reviewed, and green, bound to the merged SHA. Where
  branch policy intentionally requires zero approvals, an explicit admin-owner
  decision may substitute only when it names the SHA and records a reason.
- `core/deploy.py` adds `verify` and `smoke`. Code on disk is not deployed
  until the live process is proven to run that revision.

### Cross-device continuity and mobile access
- Authenticated mobile gateway on `:3458` with device pairing, preview-safe
  pairing links, VPN-free tailnet entry, and CA name constraints. It may proxy
  the dashboard, never Admin.
- Handoffs between surfaces are idempotent, claimable, and completable;
  surface identity comes only from the authenticated gateway header.

### Provider chain
- `backup_model` field, a second independent relay slot, and a tool-capable
  GPT agentic final fallback, with bounded canaries that record only provider
  labels, model labels, timing, and sanitized categories — never tokens,
  headers, or response content.

### Dashboard `:3457`
- Full design pass: unified visual system across 11 pages, dark-mode brand
  colors fixed at the root, every red number made trustworthy, and a home view
  that is directly actionable.

### Memory
- Root fix for chronic memory-tier truncation — the heartbeat had been losing
  behavioral rules (PRD R1-R6). Index integrity is mechanically checked and
  backups cover 6 previously missing state files, with a restore runbook.

### Reliability fixes in this wave
- The self-diagnostic alarm's Memorial path had been dead: the post-script was
  the only one of 30 task scripts importing `core` without putting the repo
  root on `sys.path`, so every real run silently degraded to plain text.
- Its pre-script derived `WORK_DIR` from `$JARVIS_DIR` one line before
  assigning it; standalone runs landed on `/` and reported 0 hot / 0 warm
  memory files on a machine holding 8 and 42.
- Two false-green gaps closed: protected-file mutations forgiven because the
  live bot is running are now printed in the terminal summary (with
  `JARVIS_TEST_STRICT_GUARD=1` to reproduce CI strictly), and
  `scripts/localtest.sh` shellchecks the same set CI does. A PR in this cycle
  had quoted a local pass while CI was red.
- The pre-commit hook no longer pipes into `rg`; on a machine without ripgrep
  the restart reminder silently never fired.

### Notes
- Minor, not major: the documented install and upgrade path is unchanged, old
  routes redirect, and `scripts/migrate-memory.sh` covers the memory layout.
- 7 heartbeat tasks that were superseded or produced no useful executions
  during the observation window were retired into `tasks/_quarantine/`, with a
  documented path back onto the roster.

## [1.6.0] — 2026-07-15 — input-channel connector layer + sentinel leak fix

The perception layer is now the official channel-plugin surface for a
multi-user install: contract spec in `sources/README.md`, optional
`validate_cfg` hook, and a generic `metrics_probe` connector type — any
command that prints `{"metrics": {...}}` JSON becomes a monitored channel
(daily snapshot card + threshold alerts) with all personal infra (hosts,
SQL) in gitignored config. 1497 tests green (+33).

### Connector layer
- `sources/metrics_probe.py`: generic probe adapter — day-over-day deltas,
  anomaly rules (`value` / `pct_of_prev` / `pct_of_baseline` with ≥3-day
  history so slow declines alarm too), one ✅ recovery signal when a
  previously-tripped metric evaluates clean, history jsonl for the digest.
- `metrics-digest` heartbeat task: probe records → one card per record
  (snapshot=daily report, anomaly=alert, absence=missing-report alert,
  recovery=all-clear), per-user rendering via `digest_hint`; watermark
  pending/promote handshake so a failed Claude cycle re-emits instead of
  losing the day's card. Prompt forbids merging/dropping records (a live
  2-record batch came back as 1 card on day one).
- Absence alerts: a probe with no daily snapshot by snapshot_hour+2h emits
  "日报缺席" once per day — silence must not look like health (the
  server-side improvement loop had been dead for 3 weeks unnoticed).
- `python3 -m core.perception --dry-run [--source ID]`: validate +
  trial-collect with nothing persisted, for setting up new sources.
- The PGC daily pulse migrated from the hardcoded `pgc-improvement`
  pre-script (ssh host + remote paths in tracked code) to a gitignored
  probe; legacy task paused via interval override, deleted after parity.
- `docs/prd_delivery_connectors.md`: design for unwelding the OUTPUT side
  from Lark (neutral card + per-backend renderers); awaiting scope sign-off.

### Fixed
- P0 idle-sentinel leak: "prose + trailing HEARTBEAT_OK" reached the user
  as a memorial card (post-scripts' exact match missed the trailing form —
  a recurrence of the 2026-06-08 intent-card leak). Now enforced at two
  choke points every path shares: card builders return "" and
  `_route_output` drops the whole output, so no individual post-script can
  leak the sentinel again.
- phronesis-monitor cross-cycle memory: surfaced cards append to a flagged
  ledger; the pre-hook injects the last 24h as context and the prompt
  forbids downgrading a serious flag's follow-up to routine chat (the
  smell/dizziness flag's AC-adjustment follow-up had been judged "nothing
  noteworthy" 20 minutes later).

## [1.5.0] — 2026-07-15 — group chat: privacy-bounded multi-party conversations (REQ-100~102)

(Entry backfilled from the annotated tag; released by a parallel session.)
Red-team pass, 15 tightenings closing group-chat privacy leak paths,
fail-closed: group sessions get a curated `group_context.md` and NEVER
personal memory; Claude restricted to WebSearch only in groups (no
Bash/file/WebFetch); actions (calendar/broadcast/jobs) owner-only with
non-owner markers stripped; @mention resolution with BOT_OPEN_ID dual-match.

## [1.4.0] — 2026-07-14 — self-improvement wave: memory-tier starvation fix, honest ops signals, card quality (REQ-91~99)

Driven by a full self-audit plus the user's own complaints from 7/13-7/14
(intent CLI error, unreadable checkin card, incomplete calendar card,
recurring false stream alarm, personal data in tracked scripts). Four-angle
adversarial review caught 17 tightenings including 3 self-introduced
regressions. 1440 tests green (+25).

### Memory system tier no longer starved (REQ-91~94) — biggest functional fix
- Measured: the system tier loaded 63.8k chars against a 40k budget, so
  `inbox_private_mail` (ALL of mail-triage's output) and issue files were
  silently invisible to heartbeat for days. The per-file caps never summed
  against the budget — now arithmetically consistent (SYSTEM 56k, HOT 30k)
  with a constants-consistency test so future cap edits stay honest.
- Perception `_trim_inbox` rewritten: entry-boundary char-cap retention
  aligned with the loader caps (disk ≈ injected; the old 500-line rule kept
  3× what could ever load and cut mid-entry). Entries <48h are protected
  (the mail buffer is a WORK QUEUE — mail-triage drains ≤15/cycle; trimming
  a burst would silently lose untriaged mail); entries >7 days age out
  (restores the PRD §5.4 bound that was documented but never implemented).
  flock + size recheck against concurrent writers.
- memory-tidy auto-archives `system/*.md` with resolved-family frontmatter
  `status:` after 7 days (line-anchored YAML detection, case-insensitive).
- `tier_truncated` now feeds selfmon; warm-tier squeeze (by design) is
  marked `expected` and skipped.

### Honest ops signals (REQ-95/96)
- ef-stream: a connection that lived ≥10min before dropping resets the
  reconnect backoff (a quiet day used to ratchet to permanent 300s blind
  windows — observed failure #27, ~2h/day blind). 'Connection replaced' is
  exempt (two live sessions would ping-pong). Long-lived ZERO-output
  connections stay visible via a quiet-streak counter that escalates to
  warn every 6 consecutive (~3h) — an idle day and an up-but-mute server
  are protocol-indistinguishable, so neither is silently blessed.
- heartbeat: elected primary-probe failures during a tripped spend-limit
  gate are annotated in the log line and flagged `expected` — selfmon,
  admin console and the ops dashboard all skip expected entries. Only
  annotated when a backup path actually exists; a missing backup env keeps
  alarming (that's a real outage).
- self-diagnostic: "Stream NOT running" now checks the supervisor loop
  before alarming — sampling inside a reconnect/deploy window (the recurring
  false alarm, also seen on collaborator first install) reports a
  self-healing window instead.

### Interaction quality (REQ-97~99)
- `python3 -m core.intentions create` — agents can file intents from
  sessions (the 7/13 "CLI 报错" failure). argparse with a trigger_type
  whitelist (unknown types used to insert never-firing zombie rows),
  typed --priority, ISO-validated --expires-at.
- checkin: live activity evidence in the prompt (last-message recency,
  today's reply count, memorial interactions) with an explicit "missing
  signal ≠ idle" rule — no more "you seem idle" cards on strategy-work
  days. The post-hook unwraps a stray {"response","action"} JSON envelope
  (raw JSON used to reach the user's card verbatim) and honors
  action=silent.
- calendar change card: every line carries date+weekday, same-title
  add+remove pairs render as ONE "改期 old → new" line, overflow beyond the
  display cap is counted, never dropped.

### Privacy: personal data is config, not code
- content-recommend interest queries (the user's whole interest profile)
  → gitignored `data/content_queries_personal.txt` (neutral starter set
  when absent); intent-categorizer personal keywords →
  `data/category_keywords_personal.json`; identifying names in comments
  and test fixtures neutralized. Hygiene test blocks regressions.
  Per-user config table added to INSTALL.md (Phase 5.5).

## [1.3.1] — 2026-07-13 — first-install fixes: portable launchd, honest health checks

Everything a collaborator hit installing 1.3.0 on a fresh machine (their
day-one self-diagnostic card listed 17 warnings), plus the portability
commit that just missed the 1.3.0 tag:

### Included from post-1.3.0 main
- All hardcoded user paths eliminated (`core/claude_projects.py`); **launchd
  plists are now templates** — install via `scripts/launchd/install.sh`,
  never by copying plists (the 1.3.0 tag shipped them with absolute paths).
- HEARTBEAT per-task overlay (`data/heartbeat_overlay/<task>.md`, gitignored).

### First-install health-check honesty
- `components.yaml` entries declare preconditions
  (`requires_cmd`/`requires_file`/`requires_config`); unconfigured optional
  features (EigenFlux, sidecar, admin, launchd services) report `○ skipped`
  instead of alarming `[critical]` forever — now consistent with doctor.sh.
- Never-run tasks get a fresh-install grace (2× interval from
  `data/.install_stamp`, created by setup.sh, self-healing) — no more six
  "has NEVER run" warnings minutes after install, including self-diagnostic
  reporting itself mid-first-cycle.
- Real bug: a dead ef-stream printed a bare "⚠️ 0" (grep -c double-zero)
  instead of "Stream NOT running" — affected production too.
- Personal-site checks read `jarvis.yaml personal_site.repo_dir` instead of
  a hardcoded owner repo (subject-less "⚠️ Repo not found" on every
  non-owner install; hygiene test now bans owner usernames in tracked files).
- `self_diagnostic_post.py` emergency send passes `--as bot` (user-identity
  fallback failed on exactly the installs where the fallback matters).
- `eigenflux-preinstall` skips off the maintainer machine; calendar/EigenFlux
  diag sections gate on the feature being configured.
- setup.sh/INSTALL.md: doctor + launchd supervision steps, documented
  first-install expectations (`○ skipped` ≠ broken). 1415 tests.

## [1.3.0] — 2026-07-13 — internal release: decision-first UI + collaboration readiness

The first release cut for the multi-collaborator model (everyone works on
their own `dev/<name>` branch; Pascal alone merges to `main`). Three threads:

### Decision-first cards & dashboard
- Memorial presets reworded to real decisions（同意/暂不处理/不采纳 ·
  已阅/标为重点 · 做了/还没做/这次跳过）; cards support button *groups* (rows)
  so choices, source links, and the chat affordance stop crowding one row
  (`core/card.py button_groups`).
- Dashboard: new `/memorials` inbox page + home page rebuilt around a pending-
  decisions panel (`dashboard/pages/memorials.py`, `dashboard/uiutil.py`);
  attention ranking puts direct asks above ambient feed signals, and a corrupt
  ledger row can no longer blank the decision surface.
- EigenFlux feed cards: hard ceiling of 3 non-urgent cards/day on top of the
  90-min cooldown; feed titles must name the event (no more bare 行动/知会).
- Memorial follow-ups closed out: engagement accounting for direct sends and
  verdicts, ledgers included in session backups, single-intent closure
  questions ride native memorial cards (dual-intent kept on legacy cards).
- Card-body clipping no longer cuts through a markdown link.

### Privacy scrub (pre-collaboration audit, 2026-07-13)
- A 78-agent audit swept the tracked tree before inviting collaborators:
  personal data (health schedule, financial figures, a real mailbox, real
  contact/family names, address) removed from code, prompts, tests, and docs.
  Principle now documented in CONTRIBUTING.md: **user-specific content lives
  in gitignored per-user files** (`data/checkin_personal.sh`,
  `data/checkin_topics_personal.txt`, `jarvis.yaml`), never in tracked files.
- Test fixtures fully synthetic; `tests/test_public_repo_hygiene.py` now also
  greps tracked content for real-mailbox and full-length Lark-ID shapes.
- Stale one-shot scripts with embedded personal data deleted
  (`scripts/seed_intentions.py`, `scripts/migrate_intent_closure.py`,
  `docs/conversation_audit_2026-06-16.md`).
- NOTE: pre-1.3.0 git history still contains the scrubbed content; see the
  release notes for the private-repo / history-rewrite decision.

### Collaboration & CI
- CONTRIBUTING.md (branch-per-user model, per-user-data rule, conventions);
  `.github/CODEOWNERS` routes every PR to Pascal; branch protection on `main`
  (PR + green `test` check + code-owner review; no force pushes).
- CI installs `requirements.txt` (the old pyyaml-only env silently skipped the
  entire dashboard suite), adds pip cache, 15-min timeout, per-ref concurrency.
- `pgc_improvement_pre.sh` honors empty-stdout=skip when Pascal's PGC host is
  unreachable — non-Pascal installs no longer burn a daily 900s Claude call.
- Time-bomb test fixture fixed (checkin busy-filter now takes `now=`); CI on
  `main` had been red since 7/11 because the fixture's end date passed.

## [1.2.0] — 2026-07-11 — memorial cards + mobile resilience（奏折 + 移动韧性）

Two workstreams born from one day (7/10, Pascal's directive): (1) **memorial
(奏折) cards** — every proactive output facing Pascal becomes ONE card per
event with quick-verdict buttons plus a「聊聊这个」hand-off into conversation
(long truncated text pushes are dead); (2) **mobile resilience** — an
8-dimension audit through the carry-the-laptop lens (lid-close sleep, offline,
captive portals, timezone jumps, power loss) confirmed 8 P1s via adversarial
verification; all eight approved item-by-item and fixed. 1380 tests passing
(was 960). Every fix red-teamed; the memorial framework's DOA P0 (sidecar
couldn't import core.memorial in production) was caught by red team before
deploy.

### Memorial cards (奏折)
- `core/memorial.py`: create / decide / chat; `memorials.jsonl` event ledger
  (O_APPEND, fold-by-id); presets decision/fyi/followup; every card auto-gets
  「💬 聊聊这个」. Ledger-before-action: a crash mid-action can never double-
  execute on re-tap; decide is idempotent, cards replaced in place with the
  verdict (`✅ 已批：… · HH:MM`).
- Sidecar generic routing (`value.action == "memorial"`); legacy
  feedback/watchlater/intent_close untouched; all sends run off the event-loop
  thread (the ws connection that carries Pascal's messages never blocks).
- 「聊聊这个」: opener message + memorial context injected via
  `jobs/pending_merge.jsonl`, consumed by the next user message — live-proven
  in prod 7/11 17:46→17:54 (tap → "SLA 到底是什么?" answered in context).
- Delivery: memorial cards ride the heartbeat pipeline (quiet hours, batching,
  dedup all apply); send timeouts are NOT assumed delivered — cards persist in
  `memorial_queue.jsonl` and drain ≤6 per window (`MEMORIAL_FLUSH_MAX_CARDS`).
- Surfaces: proactive outputs auto-wrap at the delivery layer; mail-triage
  push emits memorials (`send=False`, rides the CARD: path); EigenFlux
  feed/PM cards rate-floored at one per 90 min (urgent bypass);
  `python3 -m core.memorial send|list` CLI for any task; HEARTBEAT.md §奏折.

### Mobile resilience (audit 2026-07-10 — 8/8 confirmed P1s fixed)
- **Timezone**: `core/timeutil` re-resolves /etc/localtime on a 60s TTL (was:
  cached at import — the running heartbeat sat 8h behind after
  Reykjavik→Shanghai until this release's restart).
- **Zombie connections**: sidecar disconnect watchdog (exit → supervisor
  respawn, only after a successful first connect), SDK logs moved off the
  NDJSON stdout pipe; ef-stream stall watchdog kills a silent-but-alive child.
- **Outbound loss**: chat replies retry with backoff then dead-letter
  (`reply_send_failed`); ef-stream send failures dead-letter instead of being
  marked seen with a fake "Delivered"; night-queue send timeouts keep the
  queue (retry floor 900s) instead of unlinking 40 entries on a lie.
- **Alerting honesty**: brain-death suppression is now ledgered — a wedge
  surviving ≥2 wake windows or 1h cumulative suppression pierces the post-wake
  grace (7/10's 17.5h silent wedge would page in window two); a 2s
  reachability probe treats offline as grace (kills the flight-day false
  BRAIN-DEAD); new dead-letter kinds carry human labels.
- **Power-loss durability**: heartbeat `load_state` archives a torn state file
  and reseeds instead of silently killing every task forever; `save_state`
  fsyncs before rename; daemon singleton validates pidfile process identity
  (PID reuse no longer deadlocks boot).
- **Escape hatches**: provider-gate probe cycles fall back to backup on ANY
  primary failure (was: model-shaped errors only); heartbeat `run_script`
  kills the whole process group on timeout; the test suite is isolated from
  the production heartbeat trigger.

## [1.1.0] — 2026-07-02 — delivery reliability (v4, REQ-78~90)

Theme shift from "interaction annoyances" (v3) to **"promised actions that
silently never happen"**: a missed ¥66k credit-card reminder, the
conversation audit dead for 13 days unnoticed, a 57% intention-check failure
rate with zero diagnosable errors, and a 15-row Prep:请假 create/cancel churn.
Five parallel data-mining passes over the real interaction record + a
current-state verification pass + three independent red-team reviews
(necessity/evidence/risk). 960 tests passing (was 898 + 4 time-of-day flakes).

### Delivery reliability
- **Skip digest** (REQ-78 pt.1): stall-skipped cron occurrences surface as ONE
  breach-queue digest card (inherits BREACH_MAX_SHOWS=1, consumed-state-first
  idempotency) + a self-diagnostic 24h counter. Billing-class refire lands
  after a one-week shadow.
- **Shared-call failure no longer trips innocent circuits** (REQ-79.1): the
  `if not raw:` branch mirrors the parse_failed shared counter; ≥3 consecutive
  failed shared calls back off 5min→60min (Tier0 keeps running). Replayed
  against the real 7/1 and 7/2 outage batches: zero false trips.
- **Failed events carry error excerpts** (REQ-80): first error line,
  secret-redacted, on every failed/parse_failed task_finish; log rotation
  deepened 3→8 generations. Diagnosed the 7/2 DNS outage in seconds.
- **Zombie-task sweep** (REQ-81.1/.3): five tasks with 16 days of zero
  executions retired (eigenflux-messages/-research, memory-monthly,
  task-triage, harness-evolve) + hardcoded-roster guard test; card-callback
  success logging + dormant sidecar deleted.

### Self-monitoring blind spots
- **Conversation audit on its own launchd cron** (REQ-82): daily 04:20, pure
  regex (zero LLM), file_age 48h freshness alarm via components.yaml — the
  audit can never again die silently for 13 days.
- **Calendar failure ≠ empty agenda** (REQ-83): fetch failures keep the last
  good snapshot with a "data as of X" annotation (auto-cleared on recovery,
  proven live during the 7/2 DNS outage) + an `--as user` token probe in
  self-diagnostic.
- **Daily-plan card build removed** (REQ-84): was assembled daily and
  discarded daily by SILENT_TASKS since 6/12; PLAN_LOG (the real consumer)
  stays.

### Interaction friction
- **Prep:请假 churn eradicated** (REQ-85): multi-day events key on their TRUE
  start day via the calendar_event_mapping sidecar (key format unchanged,
  zero migration, legacy-key resurrection guard); all-day status blocks
  (请假/婚假/OOO) produce nothing at all; date-prep expiry now leaves a trace
  event.
- **Closure edge cases** (REQ-90): context-category intents close their
  closure axis (na + closed_at) instead of dangling forever; cron rows refuse
  closure_question; done-with-empty-result coerces to na — and the ✅ button
  now carries a result (the latent source of that very bug).
- **free-time-nudge retired** (REQ-89): 11 sends, zero real engagement.
- **Shadow instrumentation** (REQ-88/86): write-claim audit reconciles
  "已记录" claims against actual write-surface changes (log-only, promotion
  gated on <5% false positives); direct-reply journal attribution logged
  shadow-only.

Deferred on their own acceptance gates: REQ-78 billing refire (~7/9),
REQ-81.2 memory-task root-cause fixes (~7/9), REQ-79.2 parse-clamp (~7/9),
REQ-88 promotion (~7/14), REQ-86 write-enable (post-shadow).

## [1.0.0] — 2026-06-15 — first formal release

The first tagged release. Consolidates three PRD cycles of audit-driven
hardening of Pascal's resident Lark/飞书 assistant into one stable, fully
tested (779 passing), self-monitored, deployed system. Every requirement is
grounded in the real interaction record, not speculation.

### Intent / closure (the proactive core)
- **Closed-loop intents** (REQ-30~35): inflight-manifest execution ack (the
  LLM authors content, never state), bounded retry + a breach apology card on
  exhaustion, cron catch-up + standard-cron dow fix, closure-axis spawn on all
  terminal moments + awaiting TTL, lifecycle telemetry. Fixed the audited 50%
  silent-death rate of one-shot intents.
- **Reply-based closure** (REQ-64): a negation-aware classifier closes a loop
  from Pascal's chat reply (做了/没做/不用追) — no Feishu button backend
  needed; ambiguous replies defer to the LLM; only single-root, awaiting
  intents auto-close.
- **No nagging** (REQ-59/60, breach max=1): breach apology shown once not 3×;
  outbox dedup keyed on the closure-ask root kills reworded duplicate cards;
  closure-of-closure spawning blocked; external closures expire >2 days stale.
- **Calendar idempotency** (REQ-68): (date,title,role) is an at-most-one-row
  invariant across all statuses; a prep that would fire after its event is
  dropped (no more 11-row dinner churn).
- **Carry reminders** (REQ-70): "things to bring" fire in the morning before
  first leave (clamped, never after the event, expires after the fire time) —
  the umbrella-for-a-noon-clinic miss is fixed.

### Reliability / self-monitoring
- **components.yaml manifest** (REQ-40): single source of truth for "what
  should be running", consumed by daemon / self-diagnostic / doctor /
  restart --status. `python3 -m core.components` one-shot health.
- **Self-monitoring from live data** (REQ-67): `core/selfmon.py` computes
  noise-card count, same-intent re-fires, closure-overdue, crashes, silent log
  failures + a liveness assertion, from the live JSONL/state/DB (bounded +
  cached); dashboard panel.
- **Unmuted alarms** (REQ-39): self-diagnostic alerts via a deterministic post
  path (osascript fallback) instead of the silenced LLM summary; ops/circuit
  events stay off Pascal's chat (REQ-62).
- **Real backups** (REQ-41): memory dirs + WAL-safe DB + state, with a
  freshness check. **Restart hardening** (REQ-42): single-consumer restart,
  daemon hot-reload + deploy guard, watchdog covers ef-stream/admin,
  heartbeat-loop singleton lock.
- **Truth watermarks** (REQ-51): starvation reads last_success, not the
  synthetic last_run, so a 100%-failing pre-script can't look healthy.
- **Two-channel alerts** (REQ-58) + **graceful model fallback** (REQ-77):
  opus→sonnet→haiku on a model-unavailable/spend-limit error instead of an
  empty death-loop (Fable never in the chain).

### Memory
- **Per-tier budgets** (REQ-73): each tier has a reserved floor; the full
  payload loads when under the global cap (no throwing away headroom);
  truncation is observable; stale warm files demote to archive.
- **Structured dated facts** (REQ-71): `hot/structured_facts.md` + get/set_fact
  (sanitized, atomic) injected top-priority so load-bearing dates stop getting
  lost across sessions.
- **Trust** (REQ-65): `core/doc_guard.py` verifies protected-doc writes by
  multiplicity-aware block-diff + independent read-back counts — completion
  claims come from the live doc, never generation-side counts; destructive
  overwrites are rejected.

### UX / responsiveness / noise
- **Perceived latency** (responsiveness policy): first activity feedback within
  ~6s + a one-time "thinking" ack during long opus replies (the model is the
  wait, not the pipeline — diagnosed from real logs).
- **Engagement attribution** (REQ-63): one response per sent, quote-reply
  join, flock-serialized — fixed the impossible 107% per-source rates.
- **Event-gated nudges** (REQ-75): free-time-nudge stays silent unless there's
  real content; content-recommend standalone push gated off.
- **Behavioral rules** (REQ-69/72/74): no false truncation/external blame,
  Lark-unrenderable link self-check, continuation discipline (don't re-fetch +
  re-diagnose the same artifact every turn), evidence-over-narrative reports.

### Dashboard / Admin
- Dashboard (:3457) rebuilt honest (REQ-43~46): all 7 pages 200, task-health
  board, intent funnel, live home feed; Admin (:3456) honest actuators + ops
  depth + security trio (REQ-47/48).

### Model
- Pinned to **Opus 4.8** end-to-end (`main_model: opus`, never inherit a
  possibly-banned account default; Fable severed).

### Tests
- 779 passing. Every REQ shipped with regression tests; each wave
  adversarially red-teamed before release.

[1.0.0]: https://github.com/phronesis-io/pascal-jarvis/releases/tag/v1.0.0
