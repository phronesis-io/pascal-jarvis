# 无人值守编码退役与发布权限边界

**状态：** 退役决定已在本地候选实施，待独立审查与发布
**日期：** 2026-08-29

## 决策

Jarvis 不再启动任何无人值守、可修改代码的 Codex 或 Claude Code 进程。

四个平面仍然相互独立：

| 平面 | 负责什么 | 不负责什么 |
|---|---|---|
| 产品控制面 | Matter、记忆、Intent、Item、权限、收据与收口 | 不因当前模型或 IDE 改变真相 |
| 本人在场的执行器 | 在 Pascal 启动的 Codex 或 Claude Code 任务中分析、改码和验证 | 不拥有产品真相或发布权 |
| 模型运行面 | provider/model 选择、健康、额度、fallback 与调用收据 | 不决定权限、完成或发布 |
| 发布面 | exact SHA 独立审查、Owner 授权、CI、合并、部署和运行验证 | 不接受 Agent 自评替代证据 |

## 为什么退役

连续对抗审查逐步补上了 PID 复用、double-fork、cwd 逃逸、文件描述符
丢失、launchd job 身份和 resource coalition 清理等问题。但最后一轮发现：
一个已在 controller coalition 内运行的进程，仍可请求 launchd 创建第二个
job。新 job 不属于原边界，controller 因而不能证明所有 mutation 都在候选
检查前结束。

2026-08-29 在本机用 Codex 的 `workspace-write` Seatbelt、无审批升级配置做了
无害探针：沙箱内执行 `launchctl submit` 创建一次性 `/usr/bin/true` job 成功。
job 随后自动消失，没有留下服务；但实验已经足以推翻“workspace sandbox 能
阻止新 launchd job”这一安全假设。继续堆叠用户态清理逻辑只会制造虚假的
安全感。

## 替代流程

```text
Jarvis 后台：观察 -> 聚合 -> 去重 -> Proposal
Pascal 启动 Codex/Claude Code：spec -> dev -> test -> review
Git/GitHub：PR -> CI -> merge
Jarvis 发布控制：exact-SHA Owner authority -> deploy -> runtime verify -> L3 observe
```

- `core.iteration_loop` 与 `iteration-observe` 继续发现真实反馈、整理证据和形成提案。
- 后台任务到 Proposal 为止，不创建代码工作树、不运行可变更 coding agent、不提交代码。
- Pascal 在电脑或手机 Codex 中开启一个新任务后，Codex/Claude Code 才能执行修改。
- Matter 保存长期责任和跨任务上下文；Session 只是一段短执行窗口。
- 既有独立审查、exact-SHA Owner 授权、CI、合并、部署和运行验证门禁不变。

## 删除范围

- 删除 `self-improve-cycle` heartbeat 与自诊断入口。
- 删除无人值守 harness、coalition、process supervisor、prompt、pre-hook 和对应测试。
- 删除 capability policy 和生成清单中的相关 CLI/heartbeat 能力。
- 保留 Codex、Claude Code 和模型 fallback 的正常在场执行能力；本决定只退役后台
  mutation，不退役多模型系统。

## 验收

- 仓库不存在活动的 self-improve heartbeat、可变更 harness CLI、脚本或 import。
- governed deploy 与 same-revision restart 都先只读检查旧 state、进程和
  `com.pascal.jarvis.harness.*` launchd job；任何未收尾或无法检查的状态均阻断，
  不能靠删除 state 冒充清理完成。
- 能力清单没有无人值守编码能力，且 policy 覆盖与生成文档无漂移。
- iteration-observe 仍能形成证据和 Proposal，但不能启动编码进程。
- 全量测试、shell、维护性、能力清单和覆盖预算通过。
- 发布仍要求独立非作者审查、exact-SHA Owner 授权、CI、部署和运行证据。

本地候选验证结果：`3674 passed, 6 skipped`；statement 82.2%、branch 74.1%；
shell、shellcheck、维护性、能力清单与全部模块覆盖预算通过。

这不是一次实现失败后的降级，而是产品边界的澄清：如果一件事可以由本人启动的
Codex 任务完成，Jarvis 不应在后台偷偷再做一个 Codex。Jarvis 的不可替代价值是让
责任在你没盯着时仍然存在，并在真正需要你的时刻回来。
