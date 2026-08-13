# Jarvis 安装指南（为 AI Agent 设计 / Agent-Oriented Install Guide)

> **读者设定**：你大概率是一个正在替人类安装 Jarvis 的 AI 助手（Claude Code / Cursor）。
> 本指南按这个前提写：每个阶段给出**可执行命令 + 验证方法**；凡是必须人类亲手做的
> 步骤（浏览器授权、控制台点击、复制密钥）都标注 **🧑 NEEDS HUMAN** 并给出精确的
> 点击路径——请把那段话原样转述给你的人类。
>
> **你的工作循环**：执行一个阶段 → 跑 `./scripts/doctor.sh` → 按 FAIL 项的 fix 命令
> 修复 → 重跑 doctor → 进入下一阶段。doctor 退出码 0（无 FAIL）即可继续。

> **English TL;DR**: This guide is written for the AI agent performing the install.
> Work phase by phase; after each phase run `./scripts/doctor.sh` and fix FAILs
> (each prints its fix command). Steps marked 🧑 NEEDS HUMAN require the human
> (browser auth / console clicks / secrets) — relay them verbatim. "飞书 (Feishu)"
> is the Chinese edition of Lark; the dev console is open.feishu.cn (CN accounts)
> or open.larksuite.com (international). A Feishu/Lark account is a prerequisite
> for Phase 2.

## 总览：装什么、哪些可选

| 组件 | 必需？ | 作用 | 人类要做的事 |
|---|---|---|---|
| Claude Code CLI | ✅ 必需 | Jarvis 的大脑（每次心跳/对话都是一次 claude 调用） | 登录一次（订阅或 API 计费） |
| Codex CLI | 可选但推荐 | Claude 限额时接管私聊；也可手动切为首选 | `codex login` 登录 ChatGPT |
| python3.10+ / jq / requirements-dev.txt | ✅ 必需 | 运行时与安装验收 | 无 |
| GitHub CLI (`gh`) | 仅生产发布 | PR/CI/review 发布门禁 | 首次 `gh auth login` |
| Lark/飞书插件 | 可选 | 手机上和 bot 双向聊天 | 浏览器创建应用 + 授权（约 5 分钟） |
| 卡片按钮（sidecar） | 可选 | 卡片上的交互按钮 | 控制台开回调 + 复制 App Secret（约 5 分钟） |
| EigenFlux 插件 | 可选 | Agent 广播网络 | 邮箱收一次 OTP |
| 感知层信源（含邮箱） | 可选 | 把群聊/邮件/文件变更灌进记忆 | 编辑 sources.yaml |

**不配任何可选项也能跑**：headless 模式（心跳 + 记忆 + 感知照常工作，没有 IM）。

---

## Phase 0 — 前置条件

```bash
# 必需三件套（doctor 会逐项检查并给出安装命令）
python3 --version   # 需要 3.10+
jq --version
claude --version    # Claude Code CLI: https://claude.com/claude-code
node --version      # 安装 claude CLI 和 lark-cli 都需要 node/npm
```

**🧑 NEEDS HUMAN — Claude Code 登录**：如果 `claude` 未登录，请人类在终端运行
`claude`，然后输入 `/login` 按浏览器流程完成。需要 Claude 订阅（Pro/Max）或
Console API 计费账户。验证：`printf 'Say OK' | claude -p` 能返回即成。

> 常见疑问：**不需要单独申请 API key** —— Jarvis 直接复用 Claude Code CLI 的登录态。
>
> **费用预期**（请如实转述给人类）：Jarvis 是常驻系统——心跳调度会按各任务的间隔
> 持续调用 Claude（每周期最多批量 4 个任务），这是主要消耗来源。新装默认
> `heartbeat_model: sonnet`（便宜档）；追求最高质量可改 `opus`（成本显著上升）。
> 订阅制（Pro/Max）下消耗计入订阅额度；API 计费下请先用 sonnet 观察一两天账单。

**🧑 NEEDS HUMAN — Codex 登录（推荐）**：运行 `codex login` 并按浏览器流程
登录 ChatGPT，验证 `codex login status` 显示已登录。macOS 的 ChatGPT 应用若已
包含 Codex，Jarvis 会自动发现其内置二进制；也可在 `jarvis.yaml` 设置
`codex.binary`。这个 fallback 不需要 `OPENAI_API_KEY`。

## Phase 1 — 克隆与基础安装

```bash
git clone https://github.com/phronesis-io/pascal-jarvis
cd pascal-jarvis
./setup.sh            # 幂等：完整依赖、Python 验证、配置模板、记忆、全量测试
./scripts/doctor.sh   # ← 你的主反馈回路，从现在起每个阶段后都跑一次
```

然后编辑 `jarvis.yaml`（setup.sh 已从 example 生成）：
- `data_dir`: Jarvis 数据目录（会话/记忆），建议保持默认生成值
- `work_dir`: Claude 的工作目录（它能读写这里的文件）

**验证**：`./scripts/doctor.sh` 的 1/6 和 2/6 段**无 FAIL**（WARN 可以有——比如 macOS 默认没有 coreutils timeout，属可选项）。
此时已可启动 headless 模式：`./bot.sh`（Ctrl-C 退出；正式运行见 Phase 6）。

> **headless 模式有什么用？** 没有 IM 时它是一台后台「记忆+感知引擎」：持续整理
> 记忆、采集 sources.yaml 信源、跑各类周期任务。和它交互的方式：① 在 `work_dir`
> 里直接开 Claude Code 会话（共享同一套记忆）；② 把 `jarvis.yaml` 的
> `admin.enabled` 设为 `true`，浏览器开 http://localhost:3456 看记忆与会话。

## Phase 2 — Lark/飞书（可选：手机 IM 桥）

```bash
npm install -g @larksuite/cli
npx skills add larksuite/cli -y -g
```

**🧑 NEEDS HUMAN — 创建飞书应用并授权**（两条命令各弹一次浏览器）：
1. `lark-cli config init --new` —— 浏览器里创建一个企业自建应用（名字随意，如 "Jarvis"）
2. `lark-cli auth login --recommend` —— 授权推荐权限集（机器人收发消息、日历等）

拿到自己的 open_id 并写入配置：
```bash
lark-cli auth list        # 输出里有 "YourName (ou_xxxxxxxx)" — 那个 ou_ 开头的就是
# 写入 jarvis.yaml:
#   lark:
#     user_id: "ou_xxxxxxxx"
```

**验证**：doctor 3/6 段——`lark-cli bot auth works (API probe succeeded)` PASS。
完整流程见 [plugins/lark/README.md](../plugins/lark/README.md)。

## Phase 3 — EigenFlux（可选：Agent 广播网络）

```bash
curl -fsSL https://www.eigenflux.ai/install.sh | sh
```

**🧑 NEEDS HUMAN**：`eigenflux auth login --email 人类的邮箱` → 邮箱收 OTP 输入即成。

**验证**：doctor 4/6 段 `eigenflux authenticated` PASS。

## Phase 4 — 卡片按钮（可选进阶：sidecar）

默认状态下卡片上的**回传按钮不可用**（上游 lark-cli ≤1.0.52 不消费
`card.action.trigger`，[larksuite/cli#1051](https://github.com/larksuite/cli/issues/1051)），
但有完整的替代手势：**对带链接的 bot 消息点任意表情 = 一键收藏**。想要真按钮，
启用单连接 sidecar：

```bash
./scripts/python.sh -m pip install lark-oapi
```

**🧑 NEEDS HUMAN — 飞书开发者后台两件事**（共约 5 分钟）：
1. 打开 [open.feishu.cn](https://open.feishu.cn) → 你的应用 → 左侧「**事件与回调**」→
   切到「**回调配置**」tab（注意：和"事件订阅"是两个独立 tab）→ 订阅方式选
   「**使用长连接接收回调**」→「添加回调」勾选 **card.action.trigger（卡片回传交互）**
   → **发布新版本**（不发版不生效——这是最常见的坑）
2. 同应用「**凭证与基础信息**」页 → 复制 **App Secret**

写入 `jarvis.yaml`（此文件被 gitignore，secret 不会进仓库）：
```yaml
lark:
  app_secret: "粘贴 App Secret"
  event_backend: sidecar
```

然后 `./restart.sh --runtime --yes`。它会重新校验发布授权，并且只允许重启当前
已经部署的同一份代码，不会绕过代码发布门禁。**回滚**：删掉 `event_backend`
行再以同样命令重启，
即回到 lark-cli 模式。

⚠️ **绝对不要**让 sidecar 和 `lark-cli event` 同时各开一条连接——飞书把事件随机分发
到同一应用的所有长连接，消息会被随机抢走。`event_backend` 开关保证只有一条连接。

**验证**：给自己发一张带按钮的卡片点一下，应弹出绿色 toast；或检查
`pgrep -f lark_event_sidecar` 存活 + 正常收发消息。

## Phase 5 — 感知层信源（可选：邮箱/群聊/文件灌入记忆）

编辑 `sources.yaml`（setup.sh 已生成模板，每个信源就是一段配置）：

- **飞书邮箱**（`lark_mail`）：把 `enabled: false` 改 `true`，`mailboxes` 填 `["me"]`
  或具体邮箱地址。只摄入元数据（发件人/主题），正文不进上下文（防注入+省 token）。
  `sensitivity: private` + `inbox_private_*` 缓冲名保证邮件信息永不进对外任务。
- **通用邮箱 IMAP**（`imap_mail`，163/Gmail/Outlook 等）：凭证放一个 0600 权限的
  JSON 文件（格式见 sources.example.yaml 注释），`secret_file` 指向它——**凭证
  绝不写进 sources.yaml**。🧑 NEEDS HUMAN：去邮箱设置里开 IMAP 并生成
  **应用专用密码/授权码**（不是登录密码）。
- **群聊**（`lark_chat`）：`lark-cli im +chat-search --query 群名` 查 chat_id 填入。
- **文件/报告变更**（`file_watch`）：globs 指向人类的工作目录。
- **本地仓库 commit**（`git_repo`）：repos_dir 指向代码目录的父目录。

改完无需重启等待下个心跳周期（≤15 分钟）自动生效；验证：信号会出现在
`$data_dir/memory/system/inbox_*.md`。

## Phase 5.5 — 个人化配置（per-user，全部 gitignored）

因人而异的内容（兴趣、日程、联系人、项目代号）**永远不进代码**，都放在
gitignored 的 `data/` 下。不配任何一项也能跑（用中性默认）；配了体验更贴身。

| 文件 | 作用 | 格式 |
|---|---|---|
| `data/checkin_personal.sh` | 周期性预约的 check-in 提醒（如每周固定课程） | bash 片段，可用 `$day`/`$hour` 设置 `$therapy_prep` |
| `data/checkin_topics_personal.txt` | check-in 话题去重的个人关键词 | 每行一个关键词 |
| `data/content_queries_personal.txt` | 内容推荐的兴趣搜索词（=用户兴趣画像） | 每行 `category\|platform\|query`，platform: `yt`/`bili`，`#` 开头为注释；缺省时用内置中性 starter 集 |
| `data/category_keywords_personal.json` | 意图自动分类的个人关键词扩展（项目代号等） | `{"external": ["代号"], "healing": [...]}`，键为分类名 |
| `data/person_registry.json` | 私人人物关系、称谓与各渠道已验证身份；用于“拉上我老婆”这类跨日历/私信动作 | 复制 `examples/person_registry.example.json` 后填写；真实姓名和 provider ID 不进 Git，文件权限应为 `0600` |
| `data/eigenflux_contact_bindings.json` | 旧版 EigenFlux 关系称谓绑定，仅在尚未配置 `person_registry.json` 时作为迁移期 fallback | `{"bindings":{"家人 agent":{"agent_name":"好友显示名"}}}`；新安装应统一写入人物登记册 |
| `data/heartbeat_overlay/<task>.md` | 单个心跳任务的 prompt 追加层 | markdown，追加到该任务 prompt 末尾 |

**纪律**：往任何 tracked 文件写用户个人信息（人名/机构/兴趣/日程）都是 bug，
`tests/test_public_repo_hygiene.py` 会挡；新增个人化维度时照上表模式加
gitignored 配置文件 + 中性默认。

---

## Phase 6 — 启动与日常运维

```bash
./bot.sh               # 首次前台启动；Ctrl-C 退出
./restart.sh --runtime # 日常配置/状态重启；要求已治理且 live code == clean HEAD
./restart.sh --status  # 三进程状态（daemon / bot / 事件监听器）
./restart.sh           # 代码发布：校验治理证据并同步所有已安装常驻组件
./restart.sh --full    # 同一条完整发布路径的显式别名
./scripts/doctor.sh    # 任何时候的全面体检
```

`restart.sh` 的默认和 `--full` 路径是同一条**完整生产代码发布**，不是普通安装器。
它需要 `gh` 已安装并登录，而且当前提交必须满足仓库的 PR、CI、审核和分支保护
规则。普通配置变化使用 `--runtime`；该路径仍会验证当前提交的发布授权，再证明
正在运行的 bot/heartbeat 已经是当前干净 `HEAD`；任何一项不满足都会拒绝。

**（可选，macOS）launchd 常驻监督** —— 让守护进程/看板/备份在重启和崩溃后
自动拉活。plist 是模板（`__JARVIS_DIR__` 等占位符），脚本安装时替换成本机
真实路径，**不要手动拷贝 plist**：

```bash
./scripts/launchd/install.sh   # 幂等；常驻服务需连续稳定 running，否则事务回滚
```

默认发布与 `restart.sh --full` 只同步这台机器上已经启用的 daemon 和 Dashboard
定义，不会替新安装擅自启用可选服务。定义切换失败时安装器恢复旧 plist
和原加载状态；launchd 状态无法可靠读取时发布会在停止 bot 前失败关闭。

**首装后的健康告警预期**（转述给人类，避免虚惊）：
- 没配置的可选功能（EigenFlux、sidecar、admin、launchd 服务）在体检里显示
  `○ skipped`，**不告警**。看到 `⚠️` 才需要处理。
- 刚装好时各任务还没轮到第一次运行，watermark 有按任务间隔计算的宽限期，
  不会在第一天刷 "has NEVER run"。
- 唯一预期中的首装 ⚠️：配了 Lark 但还没跑 `lark-cli auth login`（user 身份）
  时的日历 token 探针——按提示补授权即可。

**最终验证清单**（agent 逐项确认）：
1. `./scripts/python.sh -m core.components` 所有已配置组件全绿
2. `grep 'Beat sent' jarvis.log | tail -1` 时间戳在 10 分钟内（心跳线程首拍后按 ≤1 条/10 分钟节流）
3. （配了 Lark）人类从手机发一句话，收到回复
4. （配了 sidecar）点一个卡片按钮，弹 toast
5. （装了 Dashboard）`:3457` 本机可访问（手机网关 `:3458` 已退役，2026-08-11，REQ-120）

**值得告诉人类的日常命令**（在 IM 里直接发给 bot）：`jobs` 列出后台任务、
`cancel <id>` 取消、`stop` 中止当前回复；超过 ~2 分钟的请求会自动转后台，
转后台期间可以继续正常聊天。

## 故障对照表（真实踩过的坑）

| 症状 | 原因 | 解法 |
|---|---|---|
| Python 报 externally-managed | 新 macOS/Debian 的 PEP 668 | 重跑 `./setup.sh`；它会自动创建并统一使用 `~/.jarvis/runtime-venv` |
| 卡片按钮弹"出错了请稍后重试" | 回调配置没发版，或没启用 sidecar | Phase 4 完整走一遍，**别忘发版** |
| 消息收不到但进程都在 | 两条事件连接互抢 | 确认只有一个监听器：`pgrep -fl 'lark-cli event|lark_event_sidecar'` 只应有一行 |
| 改了 daemon.py 没生效/守护进程误报 | 常驻进程没重载 | `./restart.sh --full` |
| claude 调用超时/很慢 | API 高峰 | 等待即可；看门狗与重试都已内置 |
| 语音消息没转写 | 未配 OPENAI_API_KEY（Whisper 用） | 可选：export OPENAI_API_KEY=...；不配则提示用户打字 |
| `timeout: command not found` 类告警 | macOS 无 coreutils | 可忽略（内置 bash 后备），或 `brew install coreutils` |

## 给装机 Agent 的三条纪律

1. **每个阶段后跑 doctor**，按 fix 命令修，绿了再前进——不要凭感觉判断装没装好。
2. **🧑 标记的步骤原样转述给人类**，等人类确认完成再继续；不要尝试绕过浏览器授权。
3. **改配置用 `./restart.sh --runtime --yes`；代码变化必须走受治理的 `./restart.sh` 发布路径**。
