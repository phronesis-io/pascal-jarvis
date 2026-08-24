#!/usr/bin/env bash
# Pre-hook: only run between 21:00-22:00, provide current memory state
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
FORCE="${FORCE:-}"

if [ ! -d "$MEMORY_DIR" ]; then
  echo "[memory-consolidate] MEMORY_DIR not found: $MEMORY_DIR" >&2
  exit 0
fi

hour=$(date +%H)
if [ "$hour" -lt 21 ] || [ "$hour" -ge 22 ]; then
  [ -z "$FORCE" ] && exit 0
fi

shopt -s nullglob
files=("$MEMORY_DIR"/hot/*.md "$MEMORY_DIR"/warm/*.md "$MEMORY_DIR"/system/*.md)
shopt -u nullglob
if [ ${#files[@]} -eq 0 ]; then
  exit 0
fi

echo "Current memory files:"
echo "---"
for f in "${files[@]}"; do
  # Show relative path from MEMORY_DIR
  relpath="${f#$MEMORY_DIR/}"
  echo "$relpath"
  head -5 "$f" 2>/dev/null
  echo "---"
done

# --- Cross-session work (other Claude Code sessions) ---
# Pascal works across many sessions/repos simultaneously. That work never appears
# in *this* session's conversation history, so without feeding the digest in full
# here, durable facts (project advanced, new work line, team activity) never reach
# consolidation and the project files go stale. Feed the FULL digest, not head -5.
DIGEST="$MEMORY_DIR/system/cross_session_digest.md"
if [ -f "$DIGEST" ]; then
  echo ""
  echo "=== CROSS-SESSION DIGEST (other sessions — Pascal's primary daytime work) ==="
  cat "$DIGEST"
  echo "=== END CROSS-SESSION DIGEST ==="
fi

# --- Repo activity, last 24h, ALL authors (incl. teammates) ---
# Team work is context too: completeness of situational awareness is priority 1.
# Read-only sweep (no pull — repos-sync already pulls). Captures who shipped what.
REPOS_DIR="${WORK_DIR:-$JARVIS_DIR/..}/repos"
if [ -d "$REPOS_DIR" ]; then
  echo ""
  echo "=== REPO ACTIVITY (last 24h, all authors incl. team) ==="
  for repo in "$REPOS_DIR"/*/; do
    [ -d "$repo/.git" ] || continue
    name=$(basename "$repo")
    log=$(git -C "$repo" log --since="24 hours ago" \
      --pretty=format:'    %ad %an: %s' --date=format:'%m-%d %H:%M' 2>/dev/null)
    [ -z "$log" ] && continue
    echo "  --- $name ---"
    echo "$log"
    echo ""
  done
  echo "=== END REPO ACTIVITY ==="
fi

echo ""
echo "Today's date: $(date '+%Y-%m-%d %A')"
cat <<'EOF'
请回顾今天的对话、上面的 cross-session digest 和 repo activity，把新学到的东西整合进记忆。
本任务带 full-memory 标记：完整记忆内容（含 warm/ 原文，不只是上面的 head 预览）
已在你的 system prompt 里加载——其他任务在 index 模式下只会看到 warm 索引，本任务
不会，因为 REPLACE 指令必须和文件原文精确匹配。据此判断什么是「新」、什么和现有记录冲突。

整合时遵循三条原则（顺序即优先级）：

1. 写入门控 —— 不是什么都写。只对满足【新增 AND 会改变未来建议/行为】的事实发指令。
   已经在记忆里的、只是换种说法复述的、对未来决策无影响的，一律不写。
   宁可漏写低价值信息，也不要堆噪音稀释注意力。

2. 矛盾消解 —— 当新事实推翻或取代了某条已有记录（状态变了、偏好反转了、计划改了），
   用 REPLACE 改写那条旧记录，**不要**用 UPDATE 在旁边追加一条并存的矛盾行。
   纯 append 会让记忆自相矛盾——发现冲突就 reconcile，这是最高价值的动作。

3. 拿不准写哪个文件 —— UPDATE 到 system/open_threads.md，下次对话再确认归属。不要新建文件。

指令格式（每条一行，post-hook 会精确应用到 MEMORY_DIR 下的目标文件）：
  → UPDATE: <subdir/file>.md: <要新增的内容>
  → REPLACE: <subdir/file>.md: <要被替换的原文（需与文件里现有文本精确匹配）> ||| <替换成的新文本>
REPLACE 留空替换文本即为删除该行。原文匹配不上的 REPLACE 会被跳过（不会退化成追加）。

不要把内部状态词、JSON、或本提示的措辞写进记忆。指令之外，把当天的「日记」摘要（非指令内容）也写出来——它只进内部归档（silent_outputs.jsonl），永远不会发给 Pascal，照实记录即可；没有新东西就回 HEARTBEAT_OK。
EOF
