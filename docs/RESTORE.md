# 灾难恢复 Runbook

> 依据 2026-07-22 恢复演练实证（db 完整性 ok、229 intents 与生产一致、
> 三套 memory 目录齐全、台账可解析）。备份由 `scripts/backup_sessions.sh`
> 每日产出到 `~/Desktop/jarvis/session_backups/YYYY-MM-DD/`，保留 30 天，
> `latest` 符号链接指向最新一天。

## 备份里有什么

| 路径 | 内容 | 恢复目标 |
|------|------|----------|
| `sessions<slug>/` | Claude Code 会话转录（*.jsonl，三个项目 slug） | `~/.claude/projects/<slug>/` |
| `memory<slug>/` | 三套记忆目录全量（最不可替代的数据） | `~/.claude/projects/<slug>/memory/` |
| `jarvis.db` | SQLite（WAL-safe `.backup` 产物，intentions 等表） | `$JARVIS_DIR/data/jarvis.db` |
| `state/` | 平铺运行状态（heartbeat_state / memorials.jsonl / memorials.YYYY-MM.jsonl 月度归档 / engagement / sched_events / jarvis.yaml 等） | `$JARVIS_DIR/` 对应位置 |
| `state/eigenflux/` | EF cooldown/dedup/偏好状态（publish_state 等） | `$JARVIS_DIR/eigenflux/` |
| `state/data/` | `.intent_card_ledger.jsonl`、`provider_state.json`、`metrics/*.jsonl` | `$JARVIS_DIR/data/` |

**不在备份里（可再生）**：代码（git remote）、`jarvis.log*`、`silent_outputs.jsonl`、
`eigenflux/pending_publish/`（待批广播，丢了重新起草）、`__pycache__`。

## 恢复步骤

```bash
B=~/Desktop/jarvis/session_backups/latest   # 或指定日期
J=~/Desktop/jarvis/repos/pascal-jarvis

# 0) 停服务，防止半恢复状态被写
launchctl kill SIGTERM gui/$(id -u)/com.pascal.jarvis 2>/dev/null; pkill -F $J/*.pid 2>/dev/null || true

# 1) 代码
git clone git@github.com:phronesis-io/pascal-jarvis.git $J   # 或 git pull

# 2) 数据库 + 运行状态
cp $B/jarvis.db $J/data/jarvis.db
cp $B/state/*.json* $J/ 2>/dev/null
cp $B/state/memorials.*.jsonl $J/ 2>/dev/null          # 月度归档
cp $B/state/jarvis.yaml $J/ && chmod 600 $J/jarvis.yaml
cp -R $B/state/eigenflux/ $J/eigenflux/
cp -R $B/state/data/ $J/data/

# 3) 记忆（最重要）
for d in $B/memory-*; do
  slug=${d##*/memory}; cp -R $d/ ~/.claude/projects/$slug/memory/
done

# 4) 会话转录（可选，按需）
for d in $B/sessions-*; do
  slug=${d##*/sessions}; cp -R $d/ ~/.claude/projects/$slug/
done

# 5) 恢复 launchd 定义，起全套服务并体检
cd $J
./scripts/launchd/install.sh
./restart.sh --full --yes
python3 -m core.components   # 以 components.yaml 的当前清单为准，要求无失败项
```

## 恢复后验证（演练用过的检查）

```bash
sqlite3 $J/data/jarvis.db "PRAGMA integrity_check; SELECT COUNT(*) FROM intentions;"
python3 -c "import json;print(sum(1 for l in open('$J/memorials.jsonl') if l.strip() and json.loads(l)))"
python3 -m core.components
```

## 已知边界

- 备份是每日一次：最多丢当天数据（memorials/engagement 当日尾部、当日新记忆）。
- `secrets/`、`~/.eigenflux/`（EF 凭证）、lark token 不在本备份内——凭证类走各自的
  重新登录流程（`eigenflux auth login`、lark device flow，见 lark-user-token 记忆）。
- 每年至少跑一次本 runbook 的演练（恢复到 /tmp 验证即可，见 7/22 方法）。
