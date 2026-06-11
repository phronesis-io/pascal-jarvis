# Agent Discovery 赛道横评（2026-06）— Blog 03 §B 素材

调研日期：2026-06-11。方法：一手来源（spec / 官方 blog / GitHub）核实，自报口径已标注。
用途：Blog 03 steel-man 段 + EigenFlux 定位论证。

## A. 路线对比表

| 路线 | 发现机制 | demand 侧 | 推/拉 | 中心化 | 采用度（来源属性） |
|---|---|---|---|---|---|
| **Google A2A** (v1.0, LF) | `/.well-known/agent-card.json` 自描述；registry **spec 明确不标准化**（[Discussion #741](https://github.com/a2aproject/A2A/discussions/741) 仍是提案） | ❌ | 拉 | well-known 去中心、registry 各自为政 | 大厂背书，registry 标准未落地（[discovery 文档](https://a2a-protocol.org/dev/topics/agent-discovery/)） |
| **MCP Registry** | 官方中心库 + sub-registry 镜像，server 自 publish | ❌ | 拉 | 单一官方 source of truth，**仍标 preview** | ~9,652 server / 28,959 版本（2026-05，[registry](https://registry.modelcontextprotocol.io/)） |
| **ERC-8004** | 链上三 registry（Identity/Reputation/Validation），自注册 | ❌ | 拉 | 合约去中心、registration file 链下 | 主网 2026-01-29 上线，45,000+ 注册、30+ EVM 链（自报；注册≠交易）（[EIP](https://eips.ethereum.org/EIPS/eip-8004)） |
| **HF agents.md** (2026-04-17) | 每个 Gradio Space 自动 serve `/agents.md`；**Dynamic Spaces 允许 agent 运行时自主发现并当回合调用** | ❌ | 拉（运行时自动化） | 高度中心化（HF 单平台） | 全量 Gradio Spaces 覆盖（[changelog](https://huggingface.co/changelog/spaces-agents-md)） |

## B. 遗漏路线

**目录类（完备性点名，均无 demand 侧）**：
- **AGNTCY**（Cisco→LF，OASF schema + P2P 分布式目录——目录路线里去中心化最认真的）[agntcy.org](https://agntcy.org/)
- **MIT NANDA**（"agent DNS"，学术声量 > 生产采用）[projectnanda.org](https://projectnanda.org/)
- **企业三巨头 per-tenant 目录**：MSFT Entra Agent Registry（并入 Agent 365）、AWS Agent Registry（AgentCore preview，NL 搜索）、Google Gemini Enterprise Agent Gallery
- **OpenAI ChatGPT App Directory**（面向人，占消费侧分发）
- **W3C AI Agent Protocol CG**：章程明确写"交换 intent 与能力信息"——方向最接近 EigenFlux 论点的标准化努力，但 spec 预期 2026-27，白皮书阶段

**⚠️ 真竞品（真正做撮合，全部 crypto-native）**：
1. **Olas Mech Marketplace** — requester 链上发布带支付任务，mech 接单。**累计 1000 万+ a2a 交易**（官方口径）
2. **Virtuals ACP** — Request/Negotiation/Transaction/Evaluation + escrow；**2026-03 与 EF dAI 共同起草 ERC-8183（agent 雇佣/结算标准）**——撮合被标准化是最强赛道确认信号
3. **Coinbase x402 Bazaar / Agentic.Market** — "agent 搜索引擎"，发现→支付闭环最顺（16.5 万 tx / $50M / 48 万 agent，PR 口径）
4. **Fetch.ai Agentverse** — 自称 270 万 agent（自报，需折扣）

## C. Steel-man 三论据 + 回应

**1.「目录 + 调用端语义搜索 = 撮合」**（铁证：HF Dynamic Spaces 运行时自主发现+调用）
回应：只覆盖 demand→supply 单向、且 supply 须为无状态在线工具。三个不可消解缺口：① supply 永远看不到 demand（无报价/竞价/主动响应，价格发现为零）；② 拉模型丢失时效敏感信号；③ 调用方必须已知"该问什么"，预期外供给永不被发现。**目录解决 lookup，不解决 market。**

**2.「分发引力：目录已有流动性，broadcast 冷启动必死」**
回应：listing 流动性 ≠ 交易流动性——**有 demand 侧的 Olas 跑出 1000 万笔交易，纯目录的 MCP registry 无交易语义可言**。且目录可被复用为撮合网络的 supply 底料（见 D-2），冷启动只剩单侧。

**3.「demand 侧正被现有体系吸收（ERC-8183、AP2 intent mandate、x402 演化）」**
回应：细看全是**交易/结算/授权**层，没有一个做**信号分发**层——"把 demand 广播给相关 supply"的 pub-sub 原语在所有路线中仍空缺（W3C 想做但在白皮书）。企业侧反而在收敛成封闭 per-tenant 目录，跨组织开放撮合的真空在扩大。8183/x402 应当结算轨道用，非竞品。

## D. 战略含义

1. **真竞品坐标系 = Olas / Virtuals ACP / x402**，不是 A2A/MCP/8004（那是基础设施邻居）。差异化：现有撮合者绑定单链+撮合结算耦合；EigenFlux 占"协议中立的 demand-supply 信号广播层"，结算可插拔。
2. **吃掉目录而非对抗目录**：Agent Card / agents.md / OASF / 8004 registration file 是四种现成 supply 画像格式——直接消费它们，自己只补两个缺失原语：**demand 发布 + 按 intent 推送**。叙事从"第五种 discovery"升维成"四种目录之上缺失的撮合层"。
3. **Blog 03 主动收窄战线**：先让一步（工具型 lookup 场景目录确实够用，回避 Dynamic Spaces 显得不诚实），把命题钉死在**双边、时效敏感、跨组织**撮合上，用「Olas 1000 万交易 vs MCP registry 零交易语义」做实证锚点，以 ERC-8183 由 EF 官方共同起草做"撮合是共识方向"背书。

数据口径提示：Agentverse 270 万、x402 48 万、8004 4.5 万均为项目方自报，写入 blog 须标注。
