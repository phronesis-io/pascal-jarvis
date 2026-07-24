# PRD: Verified Delegation - 从一句话到可信完成

- Date: 2026-07-24
- Status: Active design; EigenFlux message Phase 1 shipped
- Owner: Pascal
- Priority: P0
- Product principle: 完成是一种有证据的状态，不是一句模型生成的话。

## 1. 决策摘要

Jarvis 下一阶段最重要的产品对象不是新的聊天入口、任务列表或 Agent，
而是一个一等公民的 **Delegation（委托）**：

> 用户把一件事交给 Jarvis 后，系统必须持续保存这次委托的目标、对象、
> 权限、执行过程、权威验证和最终结果，直到它被可信地完成、明确地阻塞，
> 或由用户取消。

委托把现有对象连接成一条确定的责任链：

```text
自然语言请求
  -> 解析并绑定目标、对象和授权
  -> 生成可执行的结果契约
  -> 幂等执行
  -> 从权威来源回读验证
  -> 以证据生成完成状态
  -> 在正确的终端展示下一步
```

核心规则：

1. 大模型可以理解、规划和解释，但不能直接把委托标记为完成。
2. 每个完成状态必须由与动作类型匹配的权威证据推导。
3. API 返回成功但尚未回读验证时，状态只能是“已执行，待核验”。
4. 对象、收件人、时间、金额或公开范围存在实质歧义时，先确认再执行。
5. 同一委托的重试必须复用幂等身份，不得产生第二次外部动作。
6. Jarvis 只在用户需要决策时打断；其余进度在手机和桌面端按需可见。

本 PRD 不再增加一个顶层收件箱。Delegation 是控制平面对象，用户仍通过
Item、Matter、飞书对话和跨端 Handoff 看到与自己有关的那一部分。

### 1.1 当前实施边界

已上线的是 connector-first Phase 1：

- EigenFlux 好友按实时好友列表分页解析，不接受模型手写数字 ID；
- 外部动作先写幂等预留，再执行发送；
- 返回回执后按 conversation、message ID、recipient 和正文哈希回读；
- 服务端已提交但本地返回失败时进入核验，不盲目重发；
- 精确 message ID 是权威身份，不受本机与服务端时钟偏差影响。

尚未上线的是本 PRD 后续的通用 Delegation schema、自动捕获、跨连接器
evaluator、Dashboard 控制面和 Reconciler。它们仍是设计，不得从文档
存在推断为运行能力。只有第二个独立连接器证明相同契约后，才提炼公共
状态机，避免为了抽象而再造一套与 Item/Matter/Intent 重叠的系统。

## 2. 为什么这是现在最重要的角度

Jarvis 已经具备多个入口和执行面：

- 飞书负责及时对话、原生审批和紧急提醒；
- 手机 PWA 负责随手查看、批量处理和跨网络访问；
- 桌面 Dashboard 负责全局检查、追踪和深度操作；
- Claude Code、Codex 和其他 Agent 负责复杂执行；
- Matter、Item、Intent、Job 和 Delivery 分别保存上下文、可见决策、
  定时闭环、计算过程和消息投递。

这些能力解决了“在哪里说”和“在哪里看”，但尚未完整解决：

> 我交代的那件事，Jarvis 到底有没有对正确的对象完成正确的动作？

继续增加模型能力和工具权限，会同时提高行动速度与错误半径。如果系统
没有委托契约和权威核验，能力越强，越可能更快地：

- 对错误对象执行正确动作；
- 把“已尝试”描述成“已完成”；
- 在重试或多 Agent 协作时重复执行；
- 让聊天、Intent、Matter 和真实外部状态长期分叉；
- 迫使用户反复追问“都好了吗”，重新承担本应由系统承担的记忆工作。

因此，Verified Delegation 是 Jarvis 从“会做事的聊天机器人”进入
“可以放心托付的个人系统”的控制平面。

## 3. 问题证据

以下问题来自历史交互和当前系统状态，均经过匿名化，不在公共仓库保存
私人姓名、消息正文、账号或凭据。

### 3.1 对象解析错误

一次转发请求中，用户指的是家庭关系中的某位代理人，系统却报告发送给
另一个工作关系中的联系人。这里动作本身并不复杂，失败发生在
“自然语言称谓 -> 稳定身份”绑定阶段。

现有系统没有强制保存：

- 用户原始称谓；
- 解析到的稳定联系人 ID；
- 选择该联系人的依据和置信度；
- 执行前是否需要确认；
- 最终消息返回的真实 recipient ID。

### 3.2 重复动作与重复打扰

同一个 Agent 好友申请曾被用户多次批准，但系统仍持续推送相同申请。
这说明按钮响应、远端关系状态和本地 Item 关闭之间没有共同的幂等键与
权威回读。

### 3.3 真实日程与内部 Intent 分叉

日历中的真实事件没有重复，但内部一度为同一事件创建多个准备 Intent。
外部事实正确，不代表内部责任链正确；反过来也一样。系统需要明确哪一层
是权威来源，并在动作后主动收敛派生状态。

### 3.4 执行已结束，Matter 仍停留在旧下一步

一个跨设备访问项目已经部署并通过运行验证，但 Matter 仍保持 active，
下一步仍是早已完成的配对操作。这是“运行事实、项目上下文和用户界面”
没有自动收敛的直接证据。

### 3.5 用户重复询问完成状态

历史交互中多次出现“检查”“都做完了吗”“现在呢”。这不是用户缺少耐心，
而是系统没有给出一种足够可信、可检查、跨终端一致的完成证明。

已有
[Interaction V4](prd_interaction_v4.md) 对持久化写入提出了回读要求，
[Unified Delivery](prd_unified_delivery_items.md) 统一了可见对象与投递，
但两者都没有为所有外部动作定义通用的“委托到结果”契约。

## 4. 产品目标

### 4.1 用户结果

用户应该能够：

1. 用自然语言交代事情，不必自己维护执行清单；
2. 在执行前看见真正有风险的对象歧义，而不是确认每一个微小步骤；
3. 随时知道一件事正在做、等待谁、需要自己做什么或为何失败；
4. 相信“已完成”意味着权威来源已经证明预期结果成立；
5. 在手机、桌面、飞书和代码 Agent 之间切换，而不会制造多个事实版本；
6. 只在需要判断时被打断，把工作记忆和追问成本交还给系统。

### 4.2 系统结果

- 外部动作拥有稳定、可重试的幂等身份；
- 每类动作拥有明确的权威来源与核验策略；
- 多 Agent 可以协作执行，但共享同一个结果契约；
- Item、Matter、Intent 和 Delivery 从 Delegation 状态派生或关联，
  不各自猜测“是否完成”；
- 所有完成声明都可追溯到结构化证据；
- 系统能识别陈旧、冲突和被新请求取代的委托。

### 4.3 北极星指标

**Verified Delegation Completion Rate（可核验委托完成率）**

```text
在承诺时间内以合格证据进入 completed 的委托数
------------------------------------------------
同期进入执行态且已到承诺时间的委托数
```

这个指标必须与“错误完成率”和“重复动作率”一起阅读。系统不能通过减少
委托创建、隐瞒失败或降低核验强度来提高完成率。

### 4.4 P0 需求追踪矩阵

| ID | 必须满足的能力 | 对应验收 |
|---|---|---|
| VD-01 | 只捕获系统明确接受的结果责任 | AT-16 |
| VD-02 | 外部动作前绑定稳定目标与授权主体 | AT-01、AT-02 |
| VD-03 | 按动作、对象和影响范围执行风险门禁 | AT-01、AT-13 |
| VD-04 | 重试、回调和 Agent 接力共享幂等身份 | AT-03、AT-06、AT-07 |
| VD-05 | 每类动作从指定权威来源回读 | AT-02、AT-04、AT-05 |
| VD-06 | 只有确定性 Evaluator 能生成完成终态 | AT-05、AT-10、AT-17 |
| VD-07 | 多步骤工作保留部分结果但不虚报整体完成 | AT-09、AT-10 |
| VD-08 | Item、Matter、Intent 和各终端收敛到同一状态 | AT-03、AT-12、AT-15 |
| VD-09 | 外部等待是独立、诚实且可恢复的状态 | AT-11 |
| VD-10 | 契约修改、冲突和取代有版本边界 | AT-08、AT-18 |
| VD-11 | 证据、日志和 API 遵守最小数据原则 | AT-14 |
| VD-12 | 系统只在需要人判断时主动打断 | AT-03、AT-11、AT-16 |

## 5. 非目标

本项目不：

- 替代飞书任务或 Jarvis Task System；
- 把每一句聊天都变成任务；
- 把运动、休息、关系维护等人生实践强制做成完成率；
- 替代 Matter、Item、Intent、Job、Delivery 或 Handoff；
- 默认授权高风险、公开、金融、法律或不可逆动作；
- 重建全部历史对话为委托；
- 用不可读的审计技术制造“可信”的表象；
- 要求用户观看 Agent 的逐步思考或工具调用日志；
- 将模型自述、截图文案或“命令退出码为 0”单独视为充分证据。

## 6. 核心概念与边界

| 对象 | 回答的问题 | 是否用户顶层入口 |
|---|---|---|
| Delegation | 用户交代的结果是否被可信完成 | 否，控制平面 |
| Outcome Contract | 什么结果才算完成 | 否 |
| Matter | 这件事属于什么长期上下文 | 否，主题详情 |
| Item / Memorial | 现在有什么需要用户看或决定 | 是 |
| Intent | 什么时候应该跟进或关闭 | 否 |
| Job / Session | 哪个执行器正在计算或操作 | 否 |
| Delivery | 输出通过什么渠道到达 | 否 |
| Handoff | 下一次交互应该在哪台设备继续 | 否 |
| Task | 用户愿意在有限时间中亲自承担什么 | 是，独立产品 |

### 6.1 Delegation

一个明确被 Jarvis 接受的结果责任。它必须来自：

- 用户明确提出的动作或结果请求；或
- Jarvis 提出建议，用户明确授权执行；或
- 已授权自动化规则产生、且规则本身可追溯的请求。

普通讨论、灵感、反问、情绪表达和未被接受的主动建议都不是 Delegation。

### 6.2 Outcome Contract

委托可执行、可验证的结果定义：

- `operation`：要发生的动作；
- `target`：动作作用的稳定对象；
- `expected_postcondition`：完成后权威来源应满足的条件；
- `authority`：哪个系统有资格证明该条件；
- `verification_policy`：如何、何时以及用什么强度核验；
- `risk_tier`：执行前需要什么授权；
- `completion_deadline`：如有，何时应完成或升级。

### 6.3 Evidence

Evidence 是对权威来源一次结构化观察，不是自然语言结论。它至少包含：

- 权威系统和资源定位符；
- 观察时间；
- 关键字段摘要或摘要哈希；
- 预期条件与实际观察的比较结果；
- 证据强度；
- 隐私等级和保留策略。

证据不得保存访问令牌、密钥、完整私人消息或无必要的个人数据。

## 7. 状态模型

### 7.1 用户可见状态

| 状态 | 含义 | 用户是否需要动作 |
|---|---|---|
| 需要确认 | 对象、范围或风险需要用户决定 | 是 |
| 正在做 | 已接受，系统正在执行或核验 | 否 |
| 等外部 | Jarvis 已完成自己的动作，等待外部事件 | 通常否 |
| 需要你 | 出现只有用户能解决的授权或选择 | 是 |
| 已完成 | 权威证据证明结果成立 | 否 |
| 未完成 | 已失败、取消或被新请求取代 | 视情况 |

### 7.2 内部状态

```text
captured -> bound -> executing -> verifying -> completed
captured -> needs_clarification -> bound
executing/verifying -> awaiting_external -> executing
captured/.../awaiting_external -> needs_user -> bound/executing
any non-terminal state -> blocked / failed / cancelled / superseded
```

### 7.3 硬性转换规则

- `captured -> bound`：目标、对象、授权和核验策略均已确定。
- `bound -> executing`：执行器领取同一个委托版本和幂等键。
- `executing -> verifying`：至少一次动作尝试返回，但尚未证明后置条件。
- `verifying -> completed`：确定性策略判定所有 required evidence 通过。
- `executing -> awaiting_external`：本方动作已核验，但最终结果依赖他人。
- `needs_user`：只有用户能够补充授权、身份或业务选择，系统不会自行重试。
- `blocked`：当前依赖或系统条件不满足，但不需要用户立即做判断。
- 任意终态转换必须写入不可变事件记录。
- 模型输出不能直接触发 `completed`。
- 修改 Outcome Contract 后递增版本；旧版本的证据不能自动证明新版本。
- 新请求与旧请求冲突时，旧请求进入 `superseded`，不能并行执行。

## 8. 委托执行协议

### 8.1 第一步：捕获

对话层识别明确的结果请求，保存：

- 原始消息引用，而非默认复制全部对话；
- 一句可读标题；
- 请求者身份与对话范围；
- 初步动作类型；
- 关联 Matter；
- 时间约束；
- 是否需要异步继续。

捕获失败不得向用户声称“已经记住”或“会继续处理”。

### 8.2 第二步：绑定

执行任何外部副作用前，系统必须把自然语言对象绑定到稳定 ID。

绑定优先级：

1. 当前消息中的明确引用或原生对象 ID；
2. 可信通讯录、日历、文档或 Agent Profile；
3. 最近且唯一的对话上下文；
4. 用户确认。

禁止仅凭相似姓名、模糊关系称谓或模型常识执行外部动作。

### 8.3 第三步：风险分级

| 级别 | 示例 | 默认行为 |
|---|---|---|
| R0 | 本地只读、状态查询 | 自动执行 |
| R1 | 可逆的个人内部写入 | 自动执行并核验 |
| R2 | 给已确认对象发消息、改日历、改共享文档 | 低歧义自动，高歧义确认 |
| R3 | 公开发布、删除、权限、费用、正式承诺 | 执行前明确确认 |
| R4 | 法律、重大金融、不可逆或超出授权 | 拒绝自动执行，要求人工操作 |

风险不是只由工具决定。同一个“发送消息”对自己、熟悉同事、陌生人和公开群
可能属于不同级别。

### 8.4 第四步：幂等执行

每个副作用步骤必须拥有 `idempotency_key`：

```text
hash(principal + operation + stable_target + normalized_payload +
     contract_version)
```

规则：

- 网络重试、进程重启和 Agent 接力均复用同一 key；
- 已存在成功且契约相同的动作时，不再次调用外部 mutation；
- 外部 API 不支持幂等键时，本地先占用 action lease，并在重试前回读；
- lease 必须有 owner、版本、超时和续租，不能只依赖内存锁；
- 用户主动要求“再发一次”时创建新契约版本或新的 Delegation。

### 8.5 第五步：权威核验

动作返回成功后，Verifier 根据 `verification_policy` 从权威来源回读。

```text
attempt result != completion evidence
```

如果 mutation 成功但回读暂时不可用：

- 状态进入 `verifying`；
- 用户可见文案为“已执行，待核验”；
- 使用有上限的退避重试；
- 超出时间预算后进入 `needs_user` 或 `failed`；
- 不得用模型生成的安慰性文案掩盖不确定性。

### 8.6 第六步：完成与报告

Completion Evaluator 是确定性代码，只读取：

- 当前契约版本；
- required steps；
- 结构化 evidence；
- 外部等待条件；
- 取消或取代事件。

模型可以把结果翻译成自然语言，但完成报告必须包含：

- 做了什么；
- 对哪个稳定对象；
- 何时被核验；
- 一项最有用的证据；
- 如有，仍在等待什么。

默认报告要短。完整证据放在桌面详情中，不把飞书变成审计日志。

## 9. 权威来源与证明强度

### 9.1 连接器核验矩阵

| 动作 | 权威来源 | 最低完成证据 |
|---|---|---|
| 飞书/A2A 发消息 | 消息服务 | 稳定 recipient ID、message ID、发送状态回读 |
| 好友申请通过 | EigenFlux 关系服务 | request ID 对应关系为 accepted/friend |
| 日历创建/修改 | 日历服务 | event ID 回读，时间、时区、参与人和地点匹配 |
| 文档更新 | 文档服务 | revision/block 回读，目标内容摘要匹配 |
| 本地文件修改 | 文件系统 | 目标路径回读与 digest/结构断言 |
| Git commit | Git object database | commit SHA 包含预期 scoped diff |
| Git push | 远端 ref | origin ref 与目标 commit SHA 一致 |
| 部署 | 运行环境 | runtime version、组件健康和 smoke 均通过 |
| Intent 关闭 | Intent store | 同一 intent ID 进入 terminal state |
| Delivery 到达 | 渠道回执 | delivery/message ID 进入 delivered 或更高状态 |
| 人工线下结果 | 用户或可信外部事件 | 明确确认；此前保持 awaiting_external |

### 9.2 证据等级

- **Strong**：从权威 API 或运行环境直接回读，字段满足后置条件。
- **Corroborated**：两个独立但非最终权威信号一致。
- **User-attested**：用户明确确认线下或不可观测结果。
- **Weak**：工具输出、日志文本、模型自述或未验证截图。

`completed` 默认要求 Strong；无法数字化观测的线下结果可使用
User-attested。Weak 只能用于排障，不能单独关闭委托。

## 10. 关键用户流程

### 10.1 向关系称谓指向的人发送资料

用户：“把这份资料发给家人的代理人。”

1. 捕获 Delegation；
2. 从可信关系映射解析稳定联系人 ID；
3. 若存在多个候选或映射低置信度，飞书只问一次“是 A 还是 B”；
4. 用户确认后锁定契约版本；
5. 发送时带幂等键；
6. 回读真实 recipient ID 与 message ID；
7. 证据一致才报告“已发送给 A”。

若实际 recipient 与契约不一致，立即进入 failed 并创建高优先级 Item，
不得生成成功文案。

### 10.2 创建或修改日程

1. 绑定日历、event ID 或唯一时间窗口；
2. 标准化时区、起止时间、参与人和地点；
3. 执行 create/update；
4. 回读 event；
5. 仅当关键字段一致时完成；
6. 使用 event ID 统一或关闭派生的准备 Intent。

同一 event 不得因标题略有变化生成多个 active 准备 Intent。

### 10.3 批准 Agent 好友申请

1. Item 持有稳定 request ID；
2. 按钮动作与远端 accept 使用相同幂等身份；
3. accept 后回读 relationship；
4. relationship 已为 friend 时视为幂等成功；
5. 关闭所有引用该 request ID 的重复 Item 和 Delivery；
6. 后续同一申请事件直接抑制，不再打扰用户。

### 10.4 “写 PRD、写代码、测试、提交、推送、部署”

这是一个 Delegation，包含有序 required steps：

| 步骤 | 最低证据 |
|---|---|
| PRD | 文件存在、结构校验通过、版本受 Git 跟踪 |
| 代码 | scoped diff 对应 Outcome Contract |
| 测试 | 规定测试集结果与退出状态 |
| 提交 | commit SHA 包含且只包含预期改动 |
| 推送 | origin ref 等于 commit SHA |
| 部署 | runtime version 等于 commit SHA |
| 验证 | 组件健康和关键 smoke 通过 |

某一步失败时，整体不能报告“全部完成”。已通过步骤保留证据，恢复时从
第一个未通过步骤继续。

### 10.5 等待外部回复

“把问题发给对方，收到回复后继续。”

- 消息发送并核验后，委托进入 `awaiting_external`；
- “已发出”是一个已完成步骤，不是整个委托的完成；
- 回复事件用 conversation/thread identity 关联原委托；
- 收到回复后恢复执行；
- 超时只产生一个聚合 Item，不重复催促用户。

### 10.6 健康、运动与生活实践

当用户说“下周尝试恢复运动”：

- 默认可创建温和提醒或建议，不自动创建完成率责任；
- 只有用户明确要求跟踪，才创建 Delegation 或 Task；
- 未反馈不等于失败；
- 不使用惩罚性、道德化或持续催促文案；
- 产品目标是支持实践与恢复，而不是最大化系统中的绿色勾。

## 11. 数据模型

### 11.1 `delegations`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | `dlg_` 前缀的稳定 ID |
| `principal_id` | TEXT | 授权主体 |
| `source` | TEXT | lark/web/codex/claude/rule |
| `source_ref` | TEXT | 原消息或规则的受控引用 |
| `title` | TEXT | 用户可读的一句话结果 |
| `request_summary` | TEXT | 最小必要的请求摘要 |
| `matter_id` | TEXT NULL | 关联主题 |
| `status` | TEXT | 内部状态 |
| `risk_tier` | INTEGER | R0-R4 |
| `contract_version` | INTEGER | 契约乐观锁版本 |
| `operation` | TEXT | 规范动作类型 |
| `target_type` | TEXT | contact/event/doc/repo/runtime/... |
| `target_id` | TEXT NULL | 稳定目标 ID |
| `target_label` | TEXT NULL | 可读且可脱敏标签 |
| `expected_postcondition` | JSON | 结构化完成条件 |
| `authority` | TEXT | 权威系统 |
| `verification_policy` | JSON | verifier 和重试预算 |
| `idempotency_key` | TEXT | 当前契约的动作身份 |
| `waiting_on` | TEXT NULL | external/user/system |
| `deadline_at` | DATETIME NULL | 结果承诺时间 |
| `created_at` | DATETIME | 创建时间 |
| `started_at` | DATETIME NULL | 开始执行 |
| `verified_at` | DATETIME NULL | 最后核验 |
| `completed_at` | DATETIME NULL | 可信完成 |
| `updated_at` | DATETIME | 最后状态更新 |
| `privacy_class` | TEXT | public/internal/private/restricted |

### 11.2 `delegation_steps`

- `id`, `delegation_id`, `contract_version`, `sequence`;
- `kind`, `executor`, `status`, `required`;
- `idempotency_key`, `lease_owner`, `lease_expires_at`;
- `attempt_count`, `last_error_code`;
- `started_at`, `finished_at`.

步骤只保存执行摘要和 artifact reference，不保存完整思维链。

### 11.3 `delegation_evidence`

- `id`, `delegation_id`, `step_id`, `contract_version`;
- `evidence_type`, `strength`, `authority`;
- `resource_locator`, `observed_digest`;
- `expected_summary`, `observed_summary`, `matched`;
- `observed_at`, `expires_at`, `privacy_class`;
- `metadata_json`.

### 11.4 `delegation_events`

append-only 事件流：

- `event_id`, `delegation_id`, `contract_version`;
- `event_type`, `actor_type`, `actor_id`;
- `from_status`, `to_status`;
- `reason_code`, `created_at`, `metadata_json`.

状态表提供快速读取，事件表提供追溯和重建。所有终态更新与事件写入必须在
同一 SQLite 事务中完成。

### 11.5 约束与索引

- 当前契约的 `idempotency_key` 唯一；
- 一个外部 source event 只能捕获一个 active Delegation；
- evidence 必须引用存在的 step 和 contract version；
- completed 必须至少有一条满足 policy 的 evidence；
- `(delegation_id, contract_version, sequence)` 唯一；
- active 状态按 `deadline_at`、`waiting_on` 和 `updated_at` 建索引；
- 所有 JSON 字段进入数据库前做 schema validation。

## 12. 服务边界与接口

### 12.1 核心模块

建议新增：

- `core/delegations.py`：存储、事务和状态机；
- `core/delegation_policy.py`：风险、授权和完成策略；
- `core/delegation_executor.py`：步骤 lease 与执行编排；
- `core/delegation_verify.py`：证据写入与完成评估；
- `core/verifiers/`：按连接器实现权威回读；
- `core/delegation_reconcile.py`：有限范围的陈旧状态收敛。

连接器只实现 mutation 和 readback，不自行决定终态。

### 12.2 HTTP API

- `POST /api/delegations`
- `GET /api/delegations`
- `GET /api/delegations/{id}`
- `POST /api/delegations/{id}/confirm`
- `POST /api/delegations/{id}/cancel`
- `POST /api/delegations/{id}/retry`
- `POST /api/delegations/{id}/handoff`
- `GET /api/delegations/{id}/evidence`

内部执行接口：

- `bind_contract`
- `claim_step`
- `record_attempt`
- `record_evidence`
- `mark_waiting`
- `evaluate_completion`
- `supersede`

所有 mutation 接口要求 principal、expected version 和 idempotency key。

### 12.3 领域事件

- `delegation.captured`
- `delegation.clarification_requested`
- `delegation.bound`
- `delegation.started`
- `delegation.attempted`
- `delegation.evidence_recorded`
- `delegation.verification_deferred`
- `delegation.awaiting_external`
- `delegation.completed`
- `delegation.failed`
- `delegation.cancelled`
- `delegation.superseded`

Item、Intent、Matter 和 Delivery 订阅这些事件，但不反向猜测 Delegation
终态。

## 13. 各终端的产品体验

### 13.1 飞书

飞书只承担：

- 新委托的自然语言入口；
- 关键歧义和高风险确认；
- 当前对话内的简短进度；
- 紧急失败和需要用户处理的阻塞；
- 一句话完成报告。

普通后台进度、详细 evidence 和历史步骤不逐条推送。

### 13.2 手机 PWA

手机首页优先显示：

- “需要你”：确认、授权、选择；
- “正在做”：可折叠的少量 active 委托；
- “刚完成”：最近结果及一项关键证据。

支持单手完成确认、取消、稍后处理和继续到飞书。不得出现与桌面不同的
平行状态。

### 13.3 桌面 Dashboard

桌面提供完整工作台：

- Delegation 时间线；
- Outcome Contract 与版本；
- Step、attempt 和 evidence；
- 当前 executor / Job / Session；
- 关联 Matter、Item、Intent、Delivery 和 Handoff；
- 重试、取消、取代和诊断；
- 按错误类型、连接器和证据强度筛选。

桌面是检查和修复面，不要求用户常驻。

### 13.4 Claude Code、Codex 与其他 Agent

代码 Agent 是可替换的 worker，不是事实来源。它们：

- 领取一个明确版本的 Outcome Contract；
- 只操作授权范围内的资源；
- 为每个 step 回传 artifact locator 和机器可检查结果；
- 可以提出 contract change，但不能静默改变成功标准；
- 不能仅用自然语言声明完成；
- 会话结束不等于 Delegation 结束。

新 Session 通过 Delegation ID 恢复，不依赖复制完整聊天上下文。

### 13.5 与 Item、Matter 和 Intent 的呈现关系

- 需要用户动作时，Delegation 产生或更新唯一 Item；
- Matter 详情显示关联委托和唯一当前 next action；
- deadline 或外部等待需要跟进时，创建一个 Intent；
- Delegation 终态自动关闭关联的 active Intent 和 Handoff；
- 同一个问题不得因多个 surface 产生多个 Item。

## 14. 收敛、重试与防打扰

### 14.1 Reconciler

Reconciler 只扫描非终态 Delegation，并按 policy：

1. 回读权威来源；
2. 补写缺失 evidence；
3. 释放过期 lease；
4. 关闭已由外部事实完成的步骤；
5. 检测与新契约冲突的旧工作；
6. 为真正需要用户的异常维护一个聚合 Item。

它不扫描全部历史对象，也不通过不断重发通知“修复”状态。

### 14.2 陈旧策略

- `executing` 超过步骤预算：回读后重试或 failed；
- `verifying` 超过核验预算：needs_user 或 failed；
- `awaiting_external`：按业务期限提醒，不按 heartbeat 频率提醒；
- `needs_clarification`：提醒一次后静默保留；
- 无 deadline 的低风险委托 72 小时无进展：进入 review queue；
- Matter 的 next action 必须由最新非终态 Delegation 或明确人工输入生成。

### 14.3 失败文案

失败报告必须说明：

- 哪一步未完成；
- 已确认成功的部分；
- 当前外部事实；
- 系统下一步会不会自动重试；
- 用户是否需要做决定。

禁止只回复“遇到一点问题，请重试”或把供应商错误原样抛给用户。

## 15. 安全、隐私与权限

### 15.1 授权主体

每个 Delegation 绑定 principal。个人私聊、群聊、Agent-to-Agent 请求和
自动规则拥有不同权限边界，不能借用 Pascal 私聊中的工具权限。

### 15.2 最小权限

- Verifier 默认只读；
- Executor 按 step 获得短期、最小范围授权；
- 群聊和外部 Agent 默认不得调用本地 bash/file_write；
- R3/R4 动作不能因模型 fallback 而降低确认要求；
- worker 变更不改变契约、风险和证据策略。

### 15.3 数据最小化

- 数据库存 source reference，非必要不复制完整消息；
- evidence 保存摘要、稳定 ID 和 digest，不保存秘密正文；
- UI 根据 privacy class 脱敏；
- 日志禁止输出 token、Authorization、Cookie、私密附件内容；
- public PRD、测试 fixture 和 telemetry 使用合成数据；
- 默认保留 evidence metadata 180 天，源 artifact 由源系统保留。

### 15.4 可撤销性

支持撤销的连接器应在完成报告中提供有效期内的 undo 动作。Undo 是新的
Delegation，引用原契约与证据，不直接删除审计事件。

## 16. 指标与运营面板

### 16.1 结果指标

- verified delegation completion rate；
- completion claim without qualifying evidence：目标 0；
- wrong-target external action：目标 0；
- duplicate external mutation：目标低于 0.1%；
- completed 后 24 小时内用户重新询问同一结果的比例；
- active Delegation 超过 deadline 的比例；
- 从捕获到首次可信进度的中位时间；
- 等用户确认中，最终证明无需确认的比例。

### 16.2 人本护栏

- 每个委托产生的平均主动打扰次数；
- 同一异常重复 Item / Delivery 数：目标 0；
- 用户手动维护状态的次数；
- 生活实践被自动转成任务或失败状态的次数：目标 0；
- 飞书中后台进度消息占比；
- 手机“需要你”列表的陈旧项数量。

### 16.3 质量抽样

每周自动抽样：

- completed 的 evidence 是否真能证明 postcondition；
- target binding 是否来自可信来源；
- clarification 是否避免了实质风险；
- failed 是否保留已完成子步骤；
- Matter、Item、Intent 是否与 Delegation 收敛。

抽样结果只使用脱敏字段。

## 17. 分阶段上线

### 17.1 Phase 0：Shadow Contract

目标：验证“哪些请求构成委托”和“什么证据才算完成”，不改变外部动作。

- 对明确动作请求生成 shadow Delegation；
- 记录模型建议的 target、risk 和 verification policy；
- 与真实动作和用户后续追问对比；
- 不展示新状态，不接管执行；
- 至少覆盖两周和五类连接器。

进入 Phase 1 的门槛：

- 明确请求捕获 precision >= 95%；
- 高风险对象歧义 recall >= 95%；
- 不把普通讨论和生活表达批量转成委托；
- verifier policy 人工抽样准确率 >= 95%。

### 17.2 Phase 1：确定性强的连接器

首批接入：

1. 飞书/A2A 消息；
2. EigenFlux 好友关系；
3. 日历；
4. 飞书文档；
5. Git push 和现有部署验证。

只为这些连接器启用 evidence-backed completion。保留现有执行路径作为
feature flag 回退。

### 17.3 Phase 2：多步骤工程委托

- 接入 Claude Code、Codex、Job 和 Session；
- 支持 required step DAG；
- 支持 worker 接力与中断恢复；
- 将 runtime version、组件健康和 smoke 作为部署证据；
- Matter next action 从 active Delegation 派生。

### 17.4 Phase 3：外部等待与主动收敛

- 对话回复、审批、人工线下结果；
- bounded reconciler；
- 陈旧、冲突与 supersede；
- 跨设备的“需要你”聚合体验。

### 17.5 迁移策略

- 不回填全部历史聊天；
- 只迁移仍 active 且有明确结果责任的工作；
- 陈旧 Matter 先展示一次候选结论，确认后关闭或转 Delegation；
- 旧 Intent、Item 和 Delivery 保持原 ID，通过 link table 关联；
- 迁移脚本可重复运行且幂等；
- 任一阶段可关闭 capture/execution/completion 三个独立 feature flag。

## 18. 验收标准

### 18.1 功能验收

1. **AT-01 对象歧义**
   - Given 一个关系称谓解析到两个联系人；
   - When 用户要求发送外部消息；
   - Then 不得发送，创建唯一确认 Item。

2. **AT-02 对象核验**
   - Given 用户确认稳定 recipient ID；
   - When 消息发送成功；
   - Then 只有回读 recipient ID 与契约一致才 completed。

3. **AT-03 重复回调**
   - Given 同一好友申请按钮事件到达 100 次；
   - When 第一次已使关系成为 friend；
   - Then 外部 mutation 最多一次，Item 关闭且不再投递。

4. **AT-04 执行成功、核验失败**
   - Given mutation 返回 2xx；
   - When authority readback 超时；
   - Then 状态为 verifying，文案为“已执行，待核验”。

5. **AT-05 权威事实冲突**
   - Given 工具报告成功；
   - When 权威回读显示目标字段未变化；
   - Then 委托不得 completed，并记录 mismatch evidence。

6. **AT-06 相同请求重试**
   - Given 进程在发送后、记录前崩溃；
   - When watchdog 恢复执行；
   - Then 系统先回读并复用 idempotency key，不重复发送。

7. **AT-07 用户明确再执行**
   - Given 原委托已完成；
   - When 用户明确要求再发送一次；
   - Then 创建新契约版本或新 Delegation，并生成新幂等键。

8. **AT-08 冲突请求**
   - Given 一个尚未执行的旧时间安排；
   - When 用户给出新的替代时间；
   - Then 旧委托 superseded，只有新委托可执行。

9. **AT-09 多步骤工程工作**
   - Given 请求包含 commit、push、deploy；
   - When commit 和 push 完成但 smoke 失败；
   - Then 整体显示未完成，已完成步骤保留 evidence。

10. **AT-10 部署完成**
    - Given origin SHA、runtime version、组件健康和 smoke；
    - When 四项全部满足 policy；
    - Then 才允许生成“已部署完成”。

11. **AT-11 外部等待**
    - Given 消息已核验送达但尚未回复；
    - When 用户查看状态；
    - Then 显示“等外部”，不显示完成或失败。

12. **AT-12 跨端一致**
    - Given 同一 Delegation 在飞书、手机和桌面可见；
    - When 用户在手机确认；
    - Then 三端读取同一版本和状态，不产生第二个 Item。

13. **AT-13 权限隔离**
    - Given 群聊或外部 Agent 创建委托；
    - When worker 请求本地写工具；
    - Then 未有明确 principal 授权时拒绝执行。

14. **AT-14 秘密数据**
    - Given 连接器使用 token 和私人消息；
    - When 写入 event、evidence、日志和 API response；
    - Then 不包含 token 或完整私人正文。

15. **AT-15 陈旧收敛**
    - Given 外部事实已完成而 Matter 仍 active；
    - When Reconciler 回读到合格证据；
    - Then Delegation 完成，Matter next action 同步更新。

16. **AT-16 生活实践保护**
    - Given 用户表达“尝试恢复运动”但没有要求跟踪；
    - When capture 分类运行；
    - Then 不创建必须完成的 Delegation 或失败状态。

17. **AT-17 模型越权**
    - Given 模型输出“全部完成”；
    - When required evidence 不足；
    - Then Completion Evaluator 拒绝终态，UI 不展示完成。

18. **AT-18 契约版本**
    - Given 执行过程中 target 被用户修改；
    - When 旧版本 evidence 到达；
    - Then evidence 被保留但不能证明新版本完成。

### 18.2 非功能验收

- 单次状态事务 p95 < 100ms；
- active Delegation 列表 p95 < 300ms；
- Reconciler 不扫描终态历史；
- executor 崩溃后在 lease 到期内可恢复；
- SQLite WAL 并发下无重复 claim；
- feature flag 关闭后现有 Item/Intent/Delivery 路径继续工作；
- 全量日志与 fixture 通过 secret scanner；
- UI 在手机和桌面视口无状态文本重叠或动作错位。

## 19. 风险、对策与停止条件

| 风险 | 影响 | 早期信号 | 对策 |
|---|---|---|---|
| 把普通对话过度捕获为委托 | 系统制造压力和噪音 | capture precision 下降、用户频繁取消 | Phase 0 shadow；只接受明确结果责任 |
| 确认过多 | 用户重新成为流程管理员 | 每委托确认数上升 | 只确认实质风险；记住可信稳定身份 |
| 核验过重导致延迟 | 简单动作显得迟钝 | verifying p95 持续上升 | 按风险选择 verifier；并行 readback |
| 为追求速度降低证据门槛 | 出现虚假完成 | Weak evidence 关闭委托 | Evaluator 拒绝策略外证据，不能动态降级 |
| 连接器 API 或 schema 漂移 | 大量委托卡在核验 | 同一 verifier mismatch 激增 | 连接器契约测试、熔断和单一聚合告警 |
| 多个投影反向改状态 | Item/Matter/Intent 再次分叉 | 同一事件出现循环更新 | Delegation 单向发布终态；投影不得反推 |
| evidence 保存过多私人数据 | 隐私和公开仓库风险 | secret scanner 或抽样命中 | 摘要与引用分离、字段白名单、保留期限 |
| SQLite 或 Reconciler 成为单点 | 状态停滞或重复执行 | lease 积压、WAL 错误、队列增长 | 事务约束、备份恢复、有限扫描和故障注入 |
| 用户看不懂“待核验” | 信任没有真正改善 | 完成后仍重复追问 | 使用动作、对象、事实三段式短报告 |
| 旧工作迁移制造历史噪音 | 上线即出现巨大待办 | active 数量异常增长 | 不全量回填，只迁移明确且仍有效的责任 |

任一阶段触发以下条件，应停止扩大流量并回到 shadow 或现有路径：

- 出现一次 R3/R4 未确认外部动作；
- 出现一次 wrong-target action；
- 出现一次没有合格 evidence 的完成声明；
- 同一幂等键产生两次不可逆副作用；
- 私密正文、token 或凭据进入 telemetry、公共仓库或普通日志；
- 新路径让每个委托的主动打扰中位数连续一周高于旧路径。

## 20. 实施顺序与工作量

### 20.1 建议顺序

1. schema、状态机、contract validation 和 event log；
2. shadow capture 与离线评估；
3. Lark/A2A、好友关系和日历 verifier；
4. Item/Matter/Intent 投影；
5. Git/deploy 多步骤契约；
6. Dashboard 和手机状态；
7. Reconciler、指标和运营面板。

### 20.2 粗略工作量

| 工作包 | 预计工程量 |
|---|---|
| 核心 schema、状态机、事件与策略 | 4-6 人日 |
| Shadow capture 与评估工具 | 3-5 人日 |
| 首批五类 verifier | 8-12 人日 |
| Item/Matter/Intent 集成 | 4-6 人日 |
| 工程工作流与部署证据 | 5-8 人日 |
| 手机/桌面体验 | 5-8 人日 |
| Reconciler、指标、迁移和运维 | 5-8 人日 |
| 自动化、故障注入与跨端测试 | 7-10 人日 |

总计约 41-63 人日。建议按 Phase 独立上线，不进行一次性大爆炸替换。

## 21. 已锁定的产品决策

1. Delegation 是明确接受的结果责任，不是所有对话的自动任务化。
2. Completion 必须由确定性 evaluator 和合格 evidence 推导。
3. 模型、worker、Job 和 Session 均无权单独声明终态。
4. `awaiting_external` 是诚实的进度，不是失败，也不是完成。
5. 不新增用户顶层收件箱；需要决策时投影为唯一 Item。
6. Matter 表示上下文，不能继续保存与权威结果矛盾的 next action。
7. Retry、watchdog、自愈和多 Agent 接力必须共享幂等身份。
8. 高风险动作的确认要求不能因模型 fallback 而降低。
9. 私人正文和凭据不进入公共文档、evidence 摘要或 telemetry。
10. 产品优化目标是归还用户时间与注意力，而不是提高“已完成”数量。

## 22. 默认值与待验证假设

以下采用默认值启动，不阻塞 Phase 0：

- evidence metadata 默认保留 180 天；
- `needs_clarification` 主动提醒一次；
- 无 deadline 的低风险 active 委托 72 小时后进入 review；
- completed 报告默认展示一项关键证据；
- 首批自动执行上限为 R2 且必须低歧义；
- 强核验不可用时宁可停留在 verifying，不降级为模型自证。

Phase 0 应验证：

- 用户语言中明确委托的 precision 是否能稳定超过 95%；
- 哪些动作必须在执行前展示 target preview；
- “一项关键证据”是否足以减少用户重复追问；
- 180 天是否满足排障需要而不过度保留个人信息；
- Matter next action 自动派生是否覆盖现有人工写入场景。

## 23. 最终产品判断

跨端入口决定用户在哪里遇见 Jarvis，模型与工具决定 Jarvis 能做什么；
Verified Delegation 决定用户是否敢把事情真正交出去。

当这层成立后，飞书可以更安静，手机可以更清楚，桌面可以更可信，
Claude Code、Codex 和未来 Agent 也可以自由替换。用户不需要理解后台
使用了哪个模型、重启了几次、经过多少个 Session，只需要看到：

- Jarvis 正确理解了谁和什么；
- 它正在承担哪一段责任；
- 事实已经证明了什么；
- 现在是否真的需要我。

这才是人本的闭环：不是让人更勤奋地管理 AI，而是让 AI 可靠地替人承担
可委托的复杂性，把注意力还给更有价值的生活与创造。
