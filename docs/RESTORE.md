# Jarvis Disaster Recovery Runbook

- Reviewed: 2026-08-17
- Backup producer: `scripts/backup_sessions.sh`
- Verifier: `scripts/verify_backup.py`
- Default retention: 30 daily snapshots after a verified replacement exists

The backup is private system state, not a Git artifact. It is written under
`<work_dir>/session_backups/YYYY-MM-DD/`; `latest` changes atomically only
after class counts, checksums, permissions, SQLite integrity, and Git bundles
pass verification. Failed staging snapshots are never promoted and never
authorize retention deletion.

## Snapshot Contents

| Snapshot path | Contents | Restore target |
|---|---|---|
| `claude_sessions/` | All Claude Code project transcripts; unchanged files may be hard-linked to the prior verified snapshot | `~/.claude/projects/` |
| `codex_sessions/` | All Codex session transcripts | `~/.codex/sessions/` |
| `memory/claude/<slug>/` | Claude-project memory trees | `~/.claude/projects/<slug>/memory/` |
| `memory/codex/<slug>/` | Codex-project memory trees | `~/.Codex/projects/<slug>/memory/` |
| `databases/*.db` | Every repository/root and `data/` SQLite database, copied through SQLite's backup API | Original relative database path; `data__jarvis.db` means `data/jarvis.db` |
| `state/` | Private config, flat runtime ledgers, queues, archives, and `data/`/EigenFlux runtime state except live SQLite/WAL/lock files | Repository runtime paths |
| `code/bundles/*.bundle` | Complete Git refs plus every discovered worktree HEAD, including detached commits | A recovery Git repository |
| `code/worktrees/*/working.patch` | Binary dirty diff for each worktree | Apply only after reviewing its matching `assets.json` record |
| `code/worktrees/*/untracked/` | Non-ignored untracked drafts; symlinks are stored as inert `.symlink` text | Restore selectively after review |
| `backup_metadata.json` / `MANIFEST.sha256` | Class completeness and typed file/symlink checksums | Verification only |

Not included: Claude/Codex login state, Lark user OAuth in Keychain, or
`~/.eigenflux` access credentials. Reauthenticate those identities after
restore. Logs and caches are intentionally expendable.

## 1. Verify Before Touching Production

```bash
J=~/Desktop/jarvis/repos/pascal-jarvis
B=~/Desktop/jarvis/session_backups/latest

cd "$J"
./scripts/python.sh scripts/verify_backup.py "$B"
```

Stop if verification reports any checksum, class, permission, SQLite, symlink,
or Git-bundle error. Do not “repair” the only snapshot in place; select an
earlier dated snapshot and verify that instead.

For a rehearsal, restore into a temporary home/repository and never point a
running launchd service at it.

## 2. Stop Writers

Use the installed launchd definitions or the documented service controls to
stop daemon, bot, heartbeat, dashboard, and sidecars. Confirm no process can
write the source databases or ledgers before copying state. Do not delete PID,
lock, or state files merely to make the stop look clean.

## 3. Restore Code Without Losing Local Work

Clone the public repository at the intended trusted `main`. Then inspect
`$B/code/assets.json` before restoring local-only work.

```bash
git clone git@github.com:phronesis-io/pascal-jarvis.git "$J"
git -C "$J" status --short --branch
for bundle in "$B"/code/bundles/*.bundle; do
  git -C "$J" bundle verify "$bundle"
done
```

Use the bundle to recover a commit or branch that is absent from the remote.
Apply a matching `working.patch` and copy untracked drafts only after checking
the recorded original path, HEAD, branch, and worktree identity. Never bulk-copy
all worktree snapshots over a fresh checkout; that can combine unrelated agent
changes or re-create an unsafe symlink.

## 4. Restore Databases And Runtime State

Create the private runtime directories first. Restore each database according
to the `__` path encoding in `databases/`; the primary example is:

```bash
mkdir -p "$J/data"
cp "$B/databases/data__jarvis.db" "$J/data/jarvis.db"

rsync -a "$B/state/data/" "$J/data/"
rsync -a "$B/state/eigenflux/" "$J/eigenflux/"
find "$B/state" -maxdepth 1 -type f ! -name 'jarvis.yaml' -exec cp {} "$J/" \;
cp "$B/state/jarvis.yaml" "$J/jarvis.yaml"
chmod 600 "$J/jarvis.yaml"
```

If the snapshot contains additional databases, map each filename back to its
recorded repository-relative path and run `PRAGMA integrity_check` after copy.
Do not copy `*.db-wal`, `*.db-shm`, or lock files from another process epoch.

## 5. Restore Memory And Transcripts

```bash
rsync -a "$B/claude_sessions/" "$HOME/.claude/projects/"
rsync -a "$B/codex_sessions/" "$HOME/.codex/sessions/"

for src in "$B"/memory/claude/*; do
  [ -d "$src" ] || continue
  slug=${src##*/}
  mkdir -p "$HOME/.claude/projects/$slug/memory"
  rsync -a "$src/" "$HOME/.claude/projects/$slug/memory/"
done

for src in "$B"/memory/codex/*; do
  [ -d "$src" ] || continue
  slug=${src##*/}
  mkdir -p "$HOME/.Codex/projects/$slug/memory"
  rsync -a "$src/" "$HOME/.Codex/projects/$slug/memory/"
done
```

Restore memory before rebuilding the private cross-session index. The index is
rebuildable; source transcripts and curated memory are the durable evidence.

## 6. Reauthenticate And Validate Offline

- Claude Code: complete its normal login flow.
- Codex: `codex login`, then `codex login status`.
- EigenFlux: `eigenflux auth login --email ...` and OTP verification.
- Lark bot: restore `lark.app_id`/`lark.app_secret` from the protected config.
- Lark user APIs: redo `lark-cli auth login --recommend` if Keychain OAuth is
  absent; bot delivery and user OAuth are separate health domains.

Then validate without claiming deployment:

```bash
sqlite3 "$J/data/jarvis.db" 'PRAGMA integrity_check;'
cd "$J"
./scripts/doctor.sh
./scripts/python.sh scripts/capability_inventory.py --check-doc docs/capability_inventory.md
```

## 7. Governed Start And Proof

Install the current launchd definitions, satisfy release evidence for the
checked-out commit, and use the governed full release path:

```bash
cd "$J"
./scripts/python.sh -m core.release_gate
./scripts/launchd/install.sh
./restart.sh --yes
./scripts/localtest.sh --runtime
```

Completion requires the running bot and heartbeat revision, critical component
health, real Lark delivery, read-only provider canary, local Admin/Dashboard
smoke, and a post-release L3 observation. A successful restore copy or a
launchd “loaded” state is not completion evidence.

## Recovery Boundaries

- Daily cadence means up to one day of unbacked local work can exist.
- Hard links save space but each verified snapshot is read-only and complete
  from the verifier's perspective.
- The snapshot contains private personal and system data; keep every directory
  owner-only and never attach it to a public issue or PR.
- Run at least one temporary restore rehearsal each year. A backup that has
  never been restored is only a hypothesis.
