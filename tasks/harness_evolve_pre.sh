#!/usr/bin/env bash
# Pre-hook: daily harness self-evolution. Gathers the DELTA since the last run
# (new feedback memories, today's behavioral signals, recent commits, the pending
# proposal queue) and hands it to Claude. The full memory (behavioral_rules etc.)
# is already loaded in the system prompt, so we only feed what's NEW here.
#
# Window: 03:00–05:00 (quiet, after memory-consolidate@21:00 / daily-reflect).
# FORCE=1 bypasses the window for testing / manual runs.
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK_DIR="${WORK_DIR:-$JARVIS_DIR}"
FORCE="${FORCE:-}"

AUTO_MEMORY="$HOME/.claude/projects/-Users-pascal-Desktop-jarvis/memory"
STATE_FILE="$JARVIS_DIR/.harness_evolve_state"
PENDING="$JARVIS_DIR/harness_proposals_pending.jsonl"

if [ ! -d "$MEMORY_DIR" ]; then
  echo "[harness-evolve] MEMORY_DIR not found: $MEMORY_DIR" >&2
  exit 0
fi

hour=$(date +%H)
if [ -z "$FORCE" ]; then
  if [ "$hour" -lt 3 ] || [ "$hour" -ge 5 ]; then exit 0; fi
fi

last_run="(unknown)"
[ -f "$STATE_FILE" ] && last_run=$(cat "$STATE_FILE" 2>/dev/null)

echo "[HARNESS SELF-EVOLUTION] $(date '+%Y-%m-%d %A')  ·  上次运行: $last_run"
echo ""

# --- New / changed feedback memories in the last ~25h (both slugs) ---
echo "=== 近 25h 新增/改动的反馈记忆（full content）==="
found_any=""
for dir in "$AUTO_MEMORY" "$MEMORY_DIR" "$MEMORY_DIR/warm" "$AUTO_MEMORY/warm"; do
  [ -d "$dir" ] || continue
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    found_any="1"
    echo "--- ${f#$HOME/} ---"
    cat "$f" 2>/dev/null
    echo ""
  done < <(find "$dir" -maxdepth 1 -name 'feedback_*.md' -o -maxdepth 1 -name 'issue_*.md' 2>/dev/null | xargs -I{} find {} -mtime -2 2>/dev/null)
done
[ -z "$found_any" ] && echo "（无）"
echo ""

# --- Today's behavioral signals ---
echo "=== 行为信号（patterns / engagement / 近期 activity·checkin）==="
[ -f "$MEMORY_DIR/system/patterns.jsonl" ] && { echo "-- patterns.jsonl (tail) --"; tail -12 "$MEMORY_DIR/system/patterns.jsonl"; echo ""; }
[ -f "$MEMORY_DIR/system/engagement_insights.md" ] && { echo "-- engagement_insights.md --"; cat "$MEMORY_DIR/system/engagement_insights.md"; echo ""; }
[ -f "$MEMORY_DIR/system/activity_log.jsonl" ] && { echo "-- activity_log (tail) --"; tail -20 "$MEMORY_DIR/system/activity_log.jsonl"; echo ""; }
echo ""

# --- Repo commits last 24h (engineering signal) ---
echo "=== 近 24h 提交（所有作者）==="
REPOS_DIR="$WORK_DIR/repos"
if [ -d "$REPOS_DIR" ]; then
  for repo in "$REPOS_DIR"/*/; do
    [ -d "$repo/.git" ] || continue
    log=$(git -C "$repo" log --since="24 hours ago" --pretty=format:'    %ad %an: %s' --date=format:'%m-%d %H:%M' 2>/dev/null)
    [ -z "$log" ] && continue
    echo "  --- $(basename "$repo") ---"; echo "$log"; echo ""
  done
else
  git -C "$JARVIS_DIR" log --since="24 hours ago" --pretty=format:'    %ad %an: %s' --date=format:'%m-%d %H:%M' 2>/dev/null
fi
echo ""

# --- Already-pending proposals (don't re-propose these) ---
echo "=== 已在审批队列里的提案（不要重复提）==="
if [ -f "$PENDING" ] && [ -s "$PENDING" ]; then
  cat "$PENDING"
else
  echo "（空）"
fi
echo ""

cat <<'EOF'
====================================================================
你是 Jarvis 的自进化任务。基于上面的「增量」（新反馈、行为信号、提交），
判断 harness 是否需要演化。完整记忆（behavioral_rules / feedback_rules /
warm / system）已在你的 system prompt 里加载——据此判断什么是「新」、
什么已被现有契约覆盖。这是**自主内务**，不要把分析过程 triage 给 Pascal。

产出**两类**变更，分级处理：

【A 级 · 卫生】低风险、可逆、不改行为——直接自动落（post-hook 会应用）：
  归档过期 open_threads、退役被取代的记忆、同步索引、修过时日期等。
  **禁止**对 hot/behavioral_rules.md 或 hot/feedback_rules.md 用 A 级。

【B 级 · 提案】改 behavioral_rules / feedback_rules / 代码——**绝不自动改**，
  进审批队列、发飞书给 Pascal 批。

三道质量闸（不过闸就别提，宁缺毋滥）：
  1. 去重：现有契约/记忆已覆盖的，不提。
  2. 不 memorize 代码：repo/git 已记录的实现细节不写成规则（至多引用）。
  3. 重复信号阈值：一条行为规则要么是 Pascal 的**明确纠正/指令**，
     要么在多处/多次出现（≥2 个独立信号）才提；单次一时之言不进契约。
  调和冲突：若与现有规则张力，措辞要 reconcile，不制造矛盾。
  体积预算：behavioral_rules 每周期加载，提炼成精炼规则 + 指向详情记忆，别堆长文。

**只输出一个 JSON 对象**（无其他文字、无 fence 也可）：
{
  "hygiene": [
    {"op":"update","file":"system/open_threads.md","content":"要追加的一行"},
    {"op":"replace","file":"<subdir/x>.md","old":"<文件里现有的精确原文>","new":"<新文本，留空=删除>"}
  ],
  "proposals": [
    {"target":"hot/behavioral_rules.md","summary":"≤20字摘要","old":"<要替换的精确原文，多行 OK；新增则填邻近锚点>","new":"<替换/插入后的完整文本>","rationale":"为什么+对他的影响","signal":"证据来源+出现次数"}
  ],
  "digest": "<飞书 markdown：仅当有 proposals 时填；A 级卫生用一句话带过>"
}

无任何变更就回：HEARTBEAT_OK
EOF
