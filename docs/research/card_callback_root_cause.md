# 卡片按钮回调失效 — 根因（2026-06-11 调研，已实锤）

**根因：lark-cli 1.0.44 根本不支持 `card.action.trigger`。** 订阅串里的该 event key 被静默忽略（`--event-types` 不校验）→ 飞书平台 3 秒内收不到回调应答 → 判定失败 → 客户端弹「出错了请稍后重试」。**所有端的回传按钮都从未成功过**；"桌面偶尔可以"是把 url 跳转按钮（纯客户端、无需回调）误认成回调成功。

实测证据：
- `lark-cli event consume card.action.trigger` → `unknown EventKey`；`event list` 无任何 card 类 key
- engagement_log 中 `"type":"feedback"` 条目数 = 0；日志四代归档无 `[card-action]` 行
- 上游 issue：[larksuite/cli#1051](https://github.com/larksuite/cli/issues/1051)（2026-05-22 提出，官方确认 "No ETA"）
- 本地卡片 JSON（core/card.py 1.0 结构、对象型 value）**合法无误**，问题全在消费端

## 已执行（2026-06-11）
- content_recommend_post.py 停发「收藏」回传按钮（url 按钮 + "回复'收藏'"文字兜底保留——这条路是通的）
- self-diagnostic 加 watch：`lark-cli event list | grep card` 一旦出现即提醒重新启用
- bot.sh 的 card-action 处理分支保留，将来恢复即用

## 将来启用回调按钮的完整清单
1. lark-cli 升级支持 card.action.trigger（盯 #1051），或自建 oapi-sdk-python ws sidecar——**警告**：飞书把事件随机分发到同 app 的多条长连接，加第二条连接会把 im.message 分流走、主 bot 随机丢消息；走 sidecar 必须整体迁移订阅
2. 开发者后台「事件与回调 → **回调配置** tab」（与事件订阅是两个 tab）→ 订阅方式选长连接 → 添加 card.action.trigger → 发布新版本
3. 重新加回 value 按钮（value 必须是对象，字符串触发 200671）

来源：[卡片回调通信](https://open.feishu.cn/document/feishu-cards/card-callback-communication?lang=zh-CN) · [配置卡片交互](https://open.feishu.cn/document/feishu-cards/configuring-card-interactions?lang=zh-CN) · [事件卡片 FAQ](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/event-card-faq?lang=zh-CN)
