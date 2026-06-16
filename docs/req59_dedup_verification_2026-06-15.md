# REQ-59 卡片去重：两个红队 bug 的收尾核验（2026-06-15 夜）

> 夜间深工自主核验。结论：**今晨红队确认的 BUG1/BUG2 已于今天 13:19 的 `538db98` 修复并部署，当前不再 live。** cross-session 记忆里"仍在 live 待修"的措辞已过时（是 morning red-team 的 pre-fix 发现被记忆管道反复重发，未复核中午的修复）。

## 证据链（git + 生产 ledger + 部署代码三向核对）

1. **时间线**
   - `ae1a49c` 13:03 ship REQ-59：去重键 = `roots_now = {_root_id(c) for c in covered}`（**所有 covered id**）。这是 buggy 版本，05:08 红队复审草稿时正确地抓到了它。
   - `538db98` 13:19 `fix(redteam-v3)`：键改为 `nag_roots = {button 父 id}`（仅 closure-ask 按钮卡的 root）；ledger 改记 `card_roots` 字段；`_recent_card_roots` 改读 `card_roots` 而非 `intent_ids`。

2. **生产 ledger `data/.intent_card_ledger.jsonl`（23 行）**
   - ≤12:22 的行：`card_roots=None`（旧格式，修复前）——红队的"冒烟枪"行就是这些。
   - ≥13:31 的行：`card_roots=[]`（新字段，修复后已部署）。`int_6362ae1606`（小时报）只出现在 `intent_ids`，**从不进 `card_roots`**。

3. **当前 HEAD = 工作树**（`git status` 干净），即在跑的就是含修复的代码。

## 为什么两个 bug 都被关上

- **BUG1（silent/prompt 槽位毒化 root，压制后续真实通知）**：ledger 只记 `card_roots = sorted(nag_roots)`，而 `nag_roots` 严格等于按钮父 id。silent/prompt 型（如 `int_6362ae1606`）永远不是按钮父 → 不进 `card_roots` → 不可能压制任何东西。**关闭。**
- **BUG2（sub-30min interval/cron notify 第 2+ 次被丢）**：普通 recurring notify 无按钮 → 无 `nag_roots` → 该路径根本不去重，保持各自 cadence。**关闭。** （red-team 自己也判 live 暴露 LOW，因当前无 sub-30min notify intent。）
- **回归检查**：唯一会进 `card_roots` 的是 closure-ask 按钮卡；closure followup 按 `rel_hours` 单次触发，非 sub-30min 复发，无新回归。红队亦确认 subset 方向（point 1）SAFE。

## 残留 / 待办
- 无需修代码。**唯一动作**：下次 cross-session/记忆复盘时，把"REQ-59 两个 live bug 待修"标注为 **已于 538db98 关闭**，停止重发该 stale 信号。
- 长期：sub-30min notify intent 真出现时（番茄钟/喝水提醒），按本核验已知去重不会误伤——但值得到时补一条单测固化（非阻塞）。

*核验方式：纯只读（git log/show、读 ledger、读部署源码），未改任何生产代码。*
