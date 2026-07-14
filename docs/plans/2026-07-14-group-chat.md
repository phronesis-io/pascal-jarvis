# PRD — 群聊模式（REQ-100~102，2026-07-14）

Pascal：「可以给 pascal-jarvis 搞上群聊功能吗？我想把他拉进一些飞书群里面。」
三个产品决策（Pascal 已拍板）：群里带**工作上下文、无隐私**；**所有人可问答、
动作只认主人**；群内容**不进记忆**（先跑通，之后要学习再配 perception lark_chat 源）。

## 现状盘点

骨架已存在：@提及门控、群会话按 chat_id 隔离、回复走 reply-to-message（落回群里）。
但直接拉群有三个洞：

1. **隐私**：每个会话（含群）注入全量个人记忆（~190k：健康/日程/联系人/邮件摘要）。
2. **执行权**：回复里的 `[ACTION:...]`（写日历/发广播/跑任务）对任何发送者都执行；
   且主会话 `--dangerously-skip-permissions` 带全工具（Bash/文件读写）跑在主人机器上
   ——群成员消息可驱动 = 对全群开放的 RCE。
3. **提及匹配 bug**：门控用 APP_ID（cli_...）匹配 mentions，而飞书 @机器人 引用的是
   bot 的 open_id（ou_...）——所有群 @ 都会被误判为未提及而忽略。

## 需求

**REQ-100 群知识边界**
- 群会话**不加载分层记忆**，只加载人工策展的 `hot/group_context.md`
  （`core.memory.load_group_context`；缺省用「一无所知」兜底行）。
- prompt 构建失败的 fallback 也绝不回退到 `load_memory`（群分支用无记忆兜底文案）。
- 多用户项目：主人称呼来自 `jarvis.yaml: owner_name`（默认「主人」），不硬编码。

**REQ-101 群会话形态**
- `build_system_prompt(chat_type=...)`：群分支 = 群聊行为准则（隐私「不掌握/不透露/
  不确认/不否认」+ 动作请私聊 + 简洁 1-3 句）+ group_context + 会话摘要 + 近轮
  ——无 ACTIONS_DOC、无 EigenFlux 技能段、无个人记忆。
- 发言人标注：消息体前缀 `[发言人: X]`（主人 = `$OWNER_NAME（主人）`，
  其他 = 群成员(open_id 尾 6 位)）。
- claude 工具面：`--allowedTools WebSearch` 且显式 disallow Bash/Edit/Write/Read/
  Glob/Grep/Agent/Skill/**WebFetch**——WebFetch 被排除因为它能访问 localhost
  （admin :3456 / dashboard :3457），是群成员的记忆渗出通道。

**REQ-102 动作权限**
- `ActionProcessor.process(reply, execute=False)`：剥掉全部 marker、零执行、
  附一句「动作类指令仅限主人触发」——非主人的群消息走此路径（bash 层动作因
  marker 已被剥而自然失效）。
- 内联命令（发/不发 广播确认、stop/cancel）在群里仅主人有效，其他人视为普通聊天。
- 判定：`sender_id == USER_ID`。

**REQ-100 附带修复：@提及门控**
- 启动时经 `lark-cli api get /open-apis/bot/v3/info` 解析 `BOT_OPEN_ID` 一次；
  mentions 匹配 APP_ID **或** BOT_OPEN_ID；忽略时日志附 mentions 头 120 字符便于排查。

## 非目标
- 群消息不进记忆/感知层（Pascal 拍板；要学习时配 sources.yaml lark_chat 源即可）。
- 不做群内主动推送/奏折（proactive 面仍只对主人 P2P）。
- 不做群成员实名解析（尾 6 位足够区分；contacts 富化留待需要时）。

## 验收
1. 单测：群 prompt 不含任何 hot/warm/system 内容标记、含隐私准则、无 ACTIONS_DOC；
   group_context 加载三态；owner_name 环境注入非硬编码；execute=False 全剥零执行。
2. 真群实测：@ 它问答正常且落回群里；非主人要求「帮我把明天 3 点写进日历」被
   礼貌拒绝且日历无新条目；主人私聊功能全部不回归（1440+ 套件绿）。
3. 若拉群后收不到事件：控制台确认应用具备群聊消息权限（scope）并重新发布版本
   （🧑 NEEDS HUMAN，见 INSTALL Phase 2 模式）。
