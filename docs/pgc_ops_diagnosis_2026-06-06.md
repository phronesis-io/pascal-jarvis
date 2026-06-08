# PGC 信源系统 — 异常根因诊断（2026-06-06 早报）

> 排查方式：**完全只读**。本地仓库 `repos/eigenflux-pgc`（= `origin/main`），`git fetch` 后基于 git log / git show / 配置文件 / 源码定位代码层根因。本地 `pgc_items.db` 是空快照，非 prod 数据 —— 涉及"实际入库/丢弃记录"的判断已明确标注"需周一在 aliap 验证"。
> 排查人：Jarvis（运维 agent）

---

## TL;DR（30 秒读完，决定周一动哪几个）

1. **DEV.to 静默 + "开源与开发者"塌方 = 同源，且是预期内的人为操作**，不是故障。`6bd69f0`（6/4 15:56）按老板/人类反馈把 4 个 DEV.to feed 加了 `"disabled": true`。DEV.to 实跑 ~870 条/天，砍掉它，类目 24h 从 1026→188（-82%）数学上对得上。**要不要恢复是产品决策，不是修 bug。**
2. **🔑 "6/4 夹带了统一调高 polling cadence"的核心假设 —— 证伪。** 6/4 那批 commit 里**没有任何全局轮询/调度/cadence 改动**；唯一的 NewsAPI 间隔改动是 `20d96e5` 把 Cluster C 从 90→**120 分钟（调慢）**。Twitter 11 个簇全部仍是 `min_interval_hours=4`，没被改。C + Twitter 两个"超速"各有独立、更朴素的解释（见下），**不是同一个改动**。
3. **NewsAPI Cluster C "超频"高度怀疑是测量假象**，不是真超频。`20d96e5` commit 自己写明：晨间 monitor 的尖峰是"按 'Cluster C' 名字前缀 grep 日志、把 C+C2 合并、且把被 `_too_soon` 节流的尝试也算成真实 call"造成的。配置上 C/C2 都是 120min（各 12 call/天，合计 24），70/24h 这个数对不上真实 call、却正好对得上"被节流的尝试也计数"。
4. **Twitter credit burn +21% = 6/3 新增 2 个 X 簇（+22 账号，9 簇→11 簇）**，cadence 没动。+22% 账号量 × 不变的 4h 间隔 ≈ +21% burn，几乎完全吻合。signal_gate total 70→115 = 这 2 个新簇启用了二元 LLM 门（`58dc1c4`）后多出来的投票量。**预期内，非异常。**
5. **SEC 6-K 广播塌方（91 抓 / 0 发）—— 高度怀疑被 6/4 新上的 freshness 门误杀。** `fd45741`（6/4 21:18）给 `财经新闻` 类目加了 48h 时效上限；"SEC EDGAR 6-K"这个 legacy feed 正好挂在 `财经新闻` 下。若 6-K 的 `published_at` 落到 48h 以外（外国发行人定期报告常带旧的 period/updated 时间戳），会在 publish 阶段被 100% 丢成 `discarded`。**需周一在 aliap 用一条只读 SQL 确认 discard_reason。**

**周一建议动作排序**：① 先验 #5（一条 SQL，5 分钟，若坐实 6-K 一行 config 即可救）→ ② 修晨间 monitor 的 grep 口径（#3，否则每天都会误报）→ ③ DEV.to 恢复/不恢复走产品决策 → ④#4 无需动作，记一笔"预期增长"。

---

## 异常 1 — DEV.to 完全静默 + "开源与开发者"塌方（标 critical）

**根因：确认 —— 人为 disable，非故障。同源。**

- 证据：`6bd69f0 chore(feeds): disable DEV.to blog feeds`（2026-06-04 15:56 +0800）。在 `data/feeds/开源与开发者.json` 给 4 个 DEV.to 条目（`DEV.to` / `#mcp` / `#agents` / `#llm`）加了：
  ```json
  "disabled": true,
  "_disabled_reason": "boss/human feedback disable(2026-06-04): agent 消费全网第一(27%) 但人类终端不满意…可逆,移除本字段即恢复。"
  ```
- 链路确认：DEV.to 实跑 ~870 条/天（见 `CHANGES_SUMMARY_2026-06-04.md` §2）。砍掉后"开源与开发者"24h 从 1026→188（-82%）数学上自洽 —— **不是抓取/抽取链路断了，是源被关了。**
- 排除 freshness 门：`开源与开发者` 类目**不在** `config.FRESHNESS_MAX_AGE_HOURS` 名单里（已用 `python3 -c "from rsspipe import config; ..."` 实测 cap=None），所以塌方 100% 由 disable 解释，与异常 #5 的 freshness 门无关。
- HTTP 状态：disable 后该 feed 根本不发请求，无需查 HTTP。

**建议修复（描述到 diff 级，不要真改）**：纯产品决策，二选一：
- 维持现状：DEV.to 确属"高 agent 消费 / 低人类满意"的 clickbait，砍它是对的，留 188 条更干净的"开源与开发者"是预期结果。
- 若 6/6 类目体量塌太狠想回血：从 4 个 DEV.to 条目移除 `"disabled"` + `"_disabled_reason"` 两字段即恢复（commit 里写明"可逆"）。建议**只恢复 tag 流（#mcp/#agents/#llm）不恢复 general**，或加 `min_likes`/质量门，别整源全开。

**风险/优先级**：低（系统行为符合设计）。优先级：仅当 Pascal 认为类目体量掉太多影响 feed 观感时再议。

---

## 异常 5（核心假设，最高优先）— "6/4 夹带统一调高 cadence" 假设

**根因：证伪 —— 6/4 那批没有任何全局 cadence / polling / schedule 改动。**

排查方法与证据：
- `git log -S "min_interval" --since=2026-06-02`：只命中 2 个 commit，且都是 6/3 的 Twitter 簇新增（`a00966a` / `4bbe8b3`），**6/4 当天零命中**。
- `git log -p` 扫 6/4 全天 diff 里 `min_interval|interval|cadence|poll|CRAWL_INTERVAL|schedule|sleep` 的新增行：唯一实质命中是 `newsapi_cluster_c.json` 的 `90→120`（调**慢**）。
- 全局抓取节奏 `config.CRAWL_INTERVAL` = 300s（5min），6/4 未被触碰。
- 各 NewsAPI 簇当前间隔：A/B/D=180min，C/C2=120min —— 没有任何簇被调快。
- Twitter 11 个簇 `min_interval_hours` 全部 = 4，未变。

**结论**：C 的"超速"和 Twitter 的"+21%"**不是同一个改动引起的**，需分开看（见异常 3、4）。这条假设若写进周一结论会误导排查方向，建议明确划掉。

**风险/优先级**：高优先级"先证伪"已完成 —— 把团队从"找一个不存在的全局开关"的方向上拉回来本身就是价值。

---

## 异常 3 — NewsAPI Cluster C "超频"（标 warn）

**根因：高度怀疑是监控测量假象，非真实超频。**

- 配置事实：`data/feeds/newsapi_cluster_c.json` 现为 `min_interval_min: 120`；C2 也是 120。两簇都走 `rsspipe/adapters/newsapi_ai.py` 的 `_too_soon()` 节流（基于 `feed_meta` 里 `newsapi:last_run:{cluster}` 时间戳）。120min 间隔 + 5min 抓取 tick → 每簇最多 12 call/天，C+C2 合计 ≤24/天。
- 报告里"70 call/24h、~20min 一次"与"间隔 120min（12/天）"对不上，也与"门完全失效（5min/次 = 288/天）"对不上 —— **介于两者之间，正是'把被节流的尝试也算成 call'的特征。**
- 直接证据：`20d96e5` 的 commit message 自己写明："the spike was an analysis artifact: grepping logs by the 'Cluster C' name prefix merged C + C2 and counted throttled (`_too_soon`) attempts as real calls. No code bug — per-cluster token/metrics counting already keys on cluster_id (newsapi-C vs -C2)。" 也就是说：真实 token 计费按 `cluster_id` 分开记（`token_budget.record(conn, cluster_id, n)`，只在 2xx 后调用），是准的；出问题的是**晨间 monitor 用日志 grep 估算 call 数**那条旁路。
- "4 天连涨 1→16→32→47→62→70"：与 C2 在 5/29 就已存在（`git log --diff-filter=A` 确认 `da7b4c7 2026-05-29`）不矛盾 —— 更像是 grep 口径随日志量累积越数越多的伪趋势。

**未完全坐实的部分**：我没法在本地复现晨间 monitor 的 grep（那段逻辑在 ops 监控/日志侧，且依赖 prod 日志）。**需周一在 aliap 验证**真实 call 数 vs token 计费数。

**建议修复（到 diff 级，不要真改）**：
- 不动 feed config（120min 已是合理值）。
- 改晨间 monitor 的 call 计数口径：① 不要用 `grep "Cluster C"` 前缀匹配（会吞 C2），改成按 `cluster_id`（`newsapi-C` / `newsapi-C2`）精确匹配；② 真实 call 数应直接读 `token_usage` 表（`token_budget.usage_by_cluster`）而不是数日志行；③ 把被 `_too_soon` 节流的 `return []` 早退**不要**计入 call。
- 顺带：`fetch_newsapi` 里 `last_run` 时间戳只在**调用成功后**写（`newsapi_ai.py:125`）。若 `get_articles` 抛错（quota/auth/网络重试耗尽），`last_run` 不更新 → 下个 5min tick 会立刻重试、不再受 120min 约束，是一个潜在的 error-driven 重试风暴口子。**建议**把 `last_run` 改成在"决定发起调用"时就 stamp（与 Twitter 适配器一致，见下），失败也算用掉一个窗口。这条是次要加固，**不是当前异常的主因**。

**风险/优先级**：中。真实 token 没失控（按 cluster_id 计费是对的），主要是监控误报噪音 + 上面那个 error 重试隐患。周一先用只读 SQL 自证。

---

## 异常 4 — Twitter credit burn +21%（373/h vs 308），signal_gate 70→115

**根因：确认 —— 6/3 新增 2 个 X 簇（账号 +22%），cadence 未变。预期内增长，非异常。**

- 证据：`4bbe8b3 feat(twitter): add 政策与地缘 + 科技与商业领袖 X clusters`（6/3）。新增"政策与地缘"(10 账号) + "科技与商业领袖"(12 账号)。
- 账号量：`推特.json` 现为 **11 个 X 簇、111 账号**（脚本实测）；新增前是 9 簇、89 账号（`a00966a` "89-account curated allowlist"）。89→111 = **+24.7% 账号**，9→11 簇 = **+22% 簇**。在 `min_interval_hours=4`（6 poll/天/簇）不变的前提下，poll 量 +22% ⇒ credit burn +21% 几乎完美吻合。
- cadence 未被改：所有 11 个簇 `min_interval_hours` 仍 = 4（`推特.json` 实测）。`twitter.py:_effective_interval_min()` 逻辑未动，记忆里的默认 4h 成立。
- signal_gate total 70→115：`58dc1c4 feat(twitter): binary LLM broadcast gate` 在这 2 个新簇上 `signal_gate=true`，每条 item 走一次二元 keep/drop LLM 投票（不改字、fail-open）。多出来的 ~45 就是这 2 个噪音簇（Musk/Altman/Trump 等大V）的投票流量。AI/research 老簇仍是零-LLM 直通。
- 加固确认：Twitter 适配器的节流 stamp 比 NewsAPI 稳 —— `twitter.py` 注释明确"stamp the attempt the moment the gate opens so even a crash can't"绕过节流，**没有 NewsAPI 那种 error 重试风暴口子**。

**建议修复（到 diff 级，不要真改）**：无需修复。若要压 burn：把 2 个新簇的 `min_interval_hours` 从 4 调到 6（policy/leader 类信号本就不需要 4h 分辨率），或缩账号 allowlist。属调参，非修 bug。

**风险/优先级**：低。credit 监控 + burn-rate 续航已在（`8e108c1`），$10≈100万 credit、15 credit/tweet，余额每日 ops-digest 报。优先级：仅记一笔"+21% 是 6/3 扩簇的预期结果"。

---

## 异常 2 — SEC EDGAR 6-K 广播塌方（91 抓 / 0 发，baseline 11.3/天，标 warn）

**根因：高度怀疑被 6/4 新上的 per-category freshness 门误杀；非 dedup、非 LLM 门、非规则被踩坏。**

定位过程：
- "6-K"不是结构化 `美股申报` 那批（那批只有 8-K/10-K/S-1，type=`sec_edgar`）。真正的 6-K 是 `财经新闻.json` 里的 **legacy RSS feed**：
  ```
  name: "SEC EDGAR 6-K"  category: "财经新闻"
  url:  https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=6-K&...&output=atom
  ```
- **关键时间线吻合**：`fd45741 feat(publish): per-category freshness gate`（6/4 21:18）给 `财经新闻` 类目加了 **48h** 时效上限（`config._FRESHNESS_DEFAULTS` 里 `"财经新闻": 48`，已实测 `FRESHNESS_MAX_AGE_HOURS` 含此键）。该门在 `scripts/publish.py:86` 对**所有** `status='done'` item 生效（无论结构化还是 LLM 管线），按 `item.category` + `item.published_at` 判定，过期即 `status='discarded'`、`discard_reason="stale event {age}h > 48h cap [财经新闻]"`。
- **机制**：6-K 是外国私人发行人的报告，常封装定期/期间报告；EDGAR atom 的 `published`/`updated`（feedparser 取这两个，见 `crawler.py:158-165`）可能落在发行人报告期而非提交时刻。`_clamp_future_pub` 只把**未来**时间戳夹到 now（`crawler.py:301/533`），**不会**把旧时间戳拉新 —— 所以一个 published_at 偏旧的 6-K 会原样撞上 48h 门、被 100% 丢弃。"仍在抓(91) / 0 发布"正是"抓取与抽取正常、卡在 publish 时效门"的特征。
- 已排除：① dedup —— processor 的 `dedup_of`/`event_uri` 是按内容/URI 去重，不会把 91 条全干掉且与 6/4 无关；② LLM 门 —— 6-K 走结构化直通或 RSS-LLM，都不存在"全劫"逻辑，且时间点对不上；③ 规则被踩坏 —— 6-K feed config 本身 6/4 未被改（`财经新闻.json` 那条只有 name/category/url，无 forms/disabled 变更）。

**未坐实的部分（必须 prod 验证）**：本地 `pgc_items.db` 是空快照，我无法读到 6-K item 真实的 `published_at` 与 `discard_reason`。**需周一在 aliap 上跑只读 SQL 确认是不是 freshness 门干的。**

**周一只读验证命令（在 aliap 生产机，不写库）**：
```sql
-- 1) 6-K 最近 24h 各 status 分布 + 丢弃原因
SELECT status, discard_reason, COUNT(*)
FROM items
WHERE source LIKE '%6-K%' AND indexed_at > datetime('now','-1 day')
GROUP BY status, discard_reason
ORDER BY COUNT(*) DESC;

-- 2) 看 6-K 的 published_at 实际有多旧（确认是否普遍 > 48h）
SELECT id, published_at, indexed_at,
       round((julianday(indexed_at)-julianday(published_at))*24,1) AS age_h
FROM items
WHERE source LIKE '%6-K%'
ORDER BY indexed_at DESC LIMIT 20;
```
（psql 版按需把 `datetime('now','-1 day')` 换成 `now() - interval '1 day'`、`julianday` 换成 `extract(epoch …)`。）
- 若 status 全是 `discarded` 且 reason 带 `stale event … 48h cap [财经新闻]` → **坐实是 freshness 门**。

**建议修复（到 diff 级，不要真改；待验证后选）**：
- 若坐实：6-K 的"发布即信号"语义 ≠ "市场实时 tick"，48h 太紧。三选一：
  - (a) 给该 feed 加 per-item override：`"notes": {"freshness_max_age_hours": 0}`（0 = 无 cap，见 `freshness.max_age_hours`），或把它的 `category` 从 `财经新闻` 挪到一个无 cap 类目（如把申报类统一归到 `美股申报`，该类无 cap）。**最干净，一行 config。**
  - (b) 放宽 `财经新闻` 整类 cap（48→168h）—— 但会顺带放松真新闻，副作用大，不推荐。
  - (c) 在 freshness 门里对 `source_type in ('curated','forwarded')` 的结构化申报豁免 —— 改的是 `freshness.py`/`publish.py` 逻辑，范围最大，最后考虑。
- **顺带提醒**：`财经新闻` / `新闻` 同为 48h cap，6/4 后可能还有别的 legacy 财经/新闻 RSS 被同一道门压低产出（与异常 #6 的 pub_rate ~45% 可能部分同源）。验证 SQL 可去掉 `source LIKE '%6-K%'`、改 `GROUP BY source, discard_reason` 看全貌。

**风险/优先级**：**周一最高优先级**。若坐实，是 6/4 部署的真实回归（虽设计意图正当，但误伤了申报流），且一行 config 可救。先跑上面的 SQL。

---

## 异常 6（次要）— 新闻类 pub_rate ~45% + error_extract 翻倍

**根因：部分确认（The Independent 等被禁解释 volume 掉），第二个烂源未在本地坐实，需 prod 数据。**

- volume 掉：6/4 那批做了大量 feed 禁用 —— `a575bac`(禁 53 个零分发源)、`d11d074`(禁 8 个地理 newswire)、`78e92b2`/`5b35756`(禁 6 个低价值 Reddit)、`4919608`(禁 The Independent 等假阳性告警源)。新闻/财经类被裁是这些的合计效果。
- error_extract 翻倍**不能**用"禁源减 volume"解释（禁掉的源不再抓，不会贡献 error）。真正抬高 error_extract 比例的是：**仍在抓、但全文抽取结构性失败**的源。6/4 上了 `67f952b autoblock_dead_extractors`（>=95% error_extract / 3天 / >=30条 才永久封），prod preview 只会封 r/kpop、Reddit:science、r/newsokur —— 说明还有一批 error_extract 高但**没到 95% 阈值**的源在持续贡献 error（如 commit 里点名的 Investing.com 48k 条 100% error_extract 属另一类，已被规则盯上；阈值下方的中等烂源逃逸）。
- 本地 DB 空，无法直接点名"error_extract 最高的非 Reddit 源"。

**周一只读验证命令（aliap）**：
```sql
-- error_extract 比例最高的非 Reddit 源（近 3 天，>=30 条），找逃过 95% 阈值的烂源
SELECT source,
       COUNT(*) AS total,
       SUM(CASE WHEN status='error_extract' THEN 1 ELSE 0 END) AS ext,
       round(100.0*SUM(CASE WHEN status='error_extract' THEN 1 ELSE 0 END)/COUNT(*),1) AS ext_pct
FROM items
WHERE indexed_at > datetime('now','-3 day')
  AND source NOT LIKE '%Reddit%' AND source NOT LIKE 'r/%'
GROUP BY source
HAVING total >= 30
ORDER BY ext_pct DESC
LIMIT 20;
```
- 找 `ext_pct` 在 60~94% 区间的源 —— 它们在烂、但逃过了 autoblock 的 95% 门，就是"第二个静悄悄烂的源"。

**建议修复（到 diff 级，不要真改；待验证后）**：
- 若验证发现一批稳定在 70~94% error_extract 的源：把 `db.autoblock_dead_extractors` 的 `fail_rate=0.95` 调到 0.80（`rsspipe/db.py`），或对这些源人工 `db.set_feed_meta('<source>:blocked', ...)`。
- 注意别误伤 `extractor.py:143-145` 里有意保留的"日经/Japan Times 80 字摘要"那类 —— 它们是合法短摘要，不是 error。

**风险/优先级**：中。pub_rate 45% 本身被多重设计性因素（反幻觉硬化丢稿 19%、freshness 门、禁源）叠加压低，不一定是单一故障；error_extract 翻倍才是要查的真信号。优先级排在 #2 之后。

---

## 附：本次排查未触及生产机的声明

全部结论基于本地只读 git/配置/源码。三处明确需周一在 aliap 上用**只读 SQL**坐实：异常 #2（6-K 是否被 freshness 门误杀）、异常 #3（真实 call 数 vs token 计费、确认监控 grep 假象）、异常 #6（第二个 error_extract 烂源点名）。其余（#1 disable、#4 扩簇、#5 无全局 cadence 改动）已在代码层确认，无需上 prod。

*文档由 Jarvis 生成，逐条对照 git commit / feed config / 源码，非凭记忆。*

---

## 追加：2026-06-07 09:04 周日晨间 ops 告警 —— 周末 false-alarm 复核（Jarvis 只读分析，未打扰 Pascal）

晨间 monitor 又报"24h published 5696 = 7d median 的 44%（warn 底部）+ 中国市场 category 411→0 + SEC EDGAR 塌到 1 + DEV.to 死透"，并下了"三源同日塌掉、技术栈互不相干、概率低、查共用 ts/host/部署"的钩子。**这个钩子基本可以拆掉——三源各有独立解释，不是关联故障：**

1. **中国市场（Tushare A股结构化：限售解禁/股东户数/大宗交易）411→0 = 周末 A 股休市，预期内**。这些是按 trade_date 的日频数据，周六周日无新交易日 → 0 新增是正确行为，不是抓取链路断。报告自己也说该源仍在累积（限售解禁 17042、自 5/31）——只是周末没有"今天"可抓。
2. **SEC EDGAR 塌到 1 = 周末美股不申报，预期内**（叠加昨天诊断里 6-K 48h freshness 门的 publish 侧问题，那是另一回事，周一仍按原计划只读 SQL 验）。报告担心"是 1 不是 0 更可疑"——周末偶有零星 8-K/6-K 正常，1 条不反常。
3. **DEV.to 死透 = 6/4 commit `6bd69f0` 人为 disable，昨天已定性**，不是新故障。

→ **结论：今日 44% throughput drop 主要由"周日 = 全周最低量日"（市场休市 + 申报停摆 + 周末发帖下降）+ DEV.to 已停用 叠加造成。monitor 用含工作日的 7d median 作基线，周日必然显示成"drop"——这正是 weekly digest 早已记录的系统性短板：告警阈值缺 day-of-week / market-calendar 感知**（同 SEC 周末豁免、worker_stalled 周末误报一脉）。

**周一动作补一条（排在原 #1~#6 之后，低优先）**：给 throughput / category-collapse 告警加"周末·市场休市"基线感知——周末与交易日分开建 baseline，或对 market-data / filings 类源在非交易日豁免 throughput 告警。否则每个周末都会重复刷这条 false alarm。

**未触及生产**：本次纯基于报告数据 + 昨日诊断 + 市场日历常识推理，本地 pgc_items.db 为空快照无法验。无需周一额外 SQL（结论是"无需动作"）。Pascal 在天目山，未打扰。
