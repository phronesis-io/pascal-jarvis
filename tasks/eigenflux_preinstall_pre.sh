#!/usr/bin/env bash
# Pre-hook for the `eigenflux-preinstall` heartbeat task — the EigenFlux
# feature-PARITY TRACKER for pascal-jarvis.
#
# Jarvis is NOT an OpenClaw/Claude-Code plugin host; it re-implements the
# EigenFlux client contract natively (core/ef_stream*.py, tasks/eigenflux_*,
# plugins/eigenflux/client.sh, core/prompt.py + HEARTBEAT prompts). Skill text
# is sourced from the MAIN repo (eigenflux/skills) — the canonical upstream both
# plugins copy from; the claude-plugin/openclaw copies lag it. The OpenClaw
# plugin's notification-routing runtime is deliberately NOT tracked
# (multi-session/multi-channel plumbing that a single-user Lark bot does not need).
#
# Every run (idempotent, designed to finish in <60s — the heartbeat hard cap):
#   1. Freshen the two source repos (bounded git fetch+ff; repos-sync owns the
#      full pull, this is only a top-up — failure is tolerated).
#   2. SKILL SYNC: mirror eigenflux/skills (main) -> plugins/eigenflux/skills
#      (jarvis-owned real files), add+update, then apply reviewed local overlays.
#      Preserves jarvis-local skills (frontmatter `jarvis-local: true`, e.g.
#      ef-localdev). Upstream-removed paths are retired only when ownership is
#      proven by the previously verified upstream SHA; unknown local additions
#      are preserved and flagged for review.
#   3. CLI: compare installed vs latest; if behind, launch a detached
#      test-before-swap upgrade (scripts/eigenflux_cli_upgrade.sh).
#   4. PARITY DRIFT: diff watched upstream paths since the last stored commit —
#      eigenflux/cli/cmd, cli/internal/client/meta.go, the skill text, and the
#      claude-plugin shared-core constants. New CLI subcommands / stream event
#      types / changed flags are surfaced and appended to a durable review backlog
#      (eigenflux/parity_todo.md). openclaw-eigenflux/src is intentionally excluded.
#   5. VERIFY ("测通"): eigenflux pytest suite, live load_ef_skills(), CLI smoke,
#      auth probe, skill-integrity (upstream + Jarvis overlays), live feed-shape, and
#      bash -n on every eigenflux script.
#   6. Emit a report + one sentinel: PREINSTALL_OK / PREINSTALL_CHANGES /
#      PREINSTALL_FAIL. Stored commit SHAs advance only when verification is green.
#
# All skill writes land in the pascal-jarvis git repo (recoverable via git).
# Machine-readable state -> eigenflux/preinstall_state.json.

set -uo pipefail
export LC_ALL=C
export PATH="$HOME/.local/bin:$PATH"

# ── Paths (self-contained; does not rely on WORK_DIR) ─────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JARVIS_DIR="${JARVIS_DIR:-$(dirname "$SCRIPT_DIR")}"
REPOS_DIR="${JARVIS_REPOS_DIR:-$(dirname "$JARVIS_DIR")}"

PLUGIN_DIR="${EIGENFLUX_PLUGIN_DIR:-$REPOS_DIR/eigenflux-claude-plugin}"   # downstream copy; watched for runtime-constant drift
MAIN_DIR="${EIGENFLUX_MAIN_DIR:-$REPOS_DIR/eigenflux}"                   # TRUE upstream: CLI contract + canonical skill text
# Skills source of truth = the MAIN repo, not a plugin: both plugins are
# `copy-skills` snapshots of eigenflux/skills and LAG it — the claude-plugin
# copy was verified to be missing the messaging privacy boundary and the
# verify-only-once auth guidance that main (and jarvis's live auto-reply path)
# rely on. Sourcing from main keeps jarvis on the freshest, most protective text.
SRC_SKILLS="$MAIN_DIR/skills"
DST_SKILLS="$JARVIS_DIR/plugins/eigenflux/skills"
SKILL_OVERLAYS="$JARVIS_DIR/plugins/eigenflux/overlays"
CLIENT_SH="$JARVIS_DIR/plugins/eigenflux/client.sh"
STATE_FILE="$JARVIS_DIR/eigenflux/preinstall_state.json"
BACKLOG="$JARVIS_DIR/eigenflux/parity_todo.md"
UPGRADE_HELPER="$JARVIS_DIR/scripts/eigenflux_cli_upgrade.sh"
UPGRADE_RESULT="$JARVIS_DIR/eigenflux/.cli_upgrade_result"
LOG_FILE="${LOG_FILE:-/dev/null}"
CDN_URL="${EIGENFLUX_CDN_URL:-https://cdn.eigenflux.ai}"

# Maintainer-machine gate (2026-07-13 fresh-install audit): parity tracking
# only makes sense where the upstream EigenFlux clones live next to this repo.
# On any other install, empty output = heartbeat skips (no Claude call, no
# "NEVER run" watermark noise).
if ! command -v eigenflux >/dev/null 2>&1 \
    || [ ! -d "$MAIN_DIR/.git" ] || [ ! -d "$PLUGIN_DIR/.git" ]; then
  exit 0
fi

# Runtime state is intentionally untracked, so a clean clone or isolated
# worktree does not contain this directory. Create it before the parity backlog
# and verified-SHA state are written; otherwise verification can pass while the
# durable review record is silently lost.
mkdir -p "$(dirname "$STATE_FILE")"

# ── Bounded runner (portable across macOS/Linux) ──────────────────────
bounded() {
  local s="$1"; shift
  python3 "$SCRIPT_DIR/../scripts/run_with_timeout.py" "$s" "$@"
}

state_get() { python3 - "$STATE_FILE" "$1" 2>/dev/null <<'PY'
import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2],""))
except Exception: print("")
PY
}

added=(); updated=(); removed_files=(); retired_skills=()
orphan_files=(); orphan_skills=(); local_skills=()
fail=(); notes=(); review=()

if [ ! -f "$SKILL_OVERLAYS/ef-communication/SKILL.md" ]; then
  echo "  FATAL: required Jarvis communication overlay is missing"
  echo ""
  echo "PREINSTALL_FAIL"
  exit 0
fi

mirror_one() {
  local src="$1" dst="$2" rel="$3"
  local candidate="$src" rendered=""
  if [ -f "$SKILL_OVERLAYS/$rel" ]; then
    rendered="$(mktemp /tmp/jarvis-ef-skill.XXXXXX)"
    if ! python3 "$JARVIS_DIR/core/eigenflux_skill_overlay.py" \
        --base "$src" --overlay "$SKILL_OVERLAYS/$rel" \
        --output "$rendered"; then
      rm -f "$rendered"
      fail+=("skill overlay failed: $rel")
      return
    fi
    candidate="$rendered"
  fi
  if [ ! -f "$dst" ]; then
    if mkdir -p "$(dirname "$dst")" && cp -p "$candidate" "$dst"; then
      added+=("$rel")
    else
      fail+=("skill copy failed: $rel")
    fi
  elif ! cmp -s "$candidate" "$dst"; then
    if cp -p "$candidate" "$dst"; then
      updated+=("$rel")
    else
      fail+=("skill copy failed: $rel")
    fi
  fi
  [ -n "$rendered" ] && rm -f "$rendered"
}

echo "EigenFlux parity tracker:"
echo ""

# ── 0. Surface a completed background CLI upgrade from a prior cycle ───
if [ -f "$UPGRADE_RESULT" ]; then
  notes+=("CLI upgrade (background): $(cat "$UPGRADE_RESULT" 2>/dev/null)")
  rm -f "$UPGRADE_RESULT" 2>/dev/null
fi

# ── 1. Freshen source repos + capture current HEADs ───────────────────
for repo in "$PLUGIN_DIR" "$MAIN_DIR"; do
  [ -d "$repo/.git" ] || { notes+=("missing source repo: $(basename "$repo")"); continue; }
  bounded 8 git -C "$repo" fetch --quiet --prune origin 2>>"$LOG_FILE" || \
    notes+=("fetch top-up skipped for $(basename "$repo") (repos-sync covers it)")
  bounded 8 git -C "$repo" merge --ff-only --quiet 2>>"$LOG_FILE" || true
done
plugin_head="$(git -C "$PLUGIN_DIR" rev-parse HEAD 2>/dev/null || echo "")"
main_head="$(git -C "$MAIN_DIR" rev-parse HEAD 2>/dev/null || echo "")"
plugin_stored="$(state_get plugin_head)"
main_stored="$(state_get main_head)"

if [ ! -d "$SRC_SKILLS" ]; then
  echo "  FATAL: source skills dir not found: $SRC_SKILLS"; echo ""; echo "PREINSTALL_FAIL"; exit 0
fi
mkdir -p "$DST_SKILLS"

# ── 2. Mirror main-repo skills -> Jarvis with provenance-gated retirement ─
managed=()
while IFS= read -r d; do managed+=("$(basename "$d")"); done \
  < <(find "$SRC_SKILLS" -mindepth 1 -maxdepth 1 -type d | sort)

# An empty source means a partial checkout / upstream restructure. Bail with a
# sentinel rather than letting the bare "${managed[@]}" loops abort under set -u
# in bash 3.2 (which would emit NO sentinel and break the heartbeat contract).
if [ ${#managed[@]} -eq 0 ]; then
  echo "  FATAL: no ef-*/ skills found under $SRC_SKILLS (partial checkout?)"
  echo ""; echo "PREINSTALL_FAIL"; exit 0
fi

# Propagate intentional upstream deletions through a separately testable,
# provenance-gated retire step. Unknown local additions are left untouched.
if [ -n "$main_stored" ]; then
  retire_output="$(python3 "$SCRIPT_DIR/eigenflux_preinstall_retire.py" \
    --upstream-repo "$MAIN_DIR" --previous-sha "$main_stored" \
    --source-skills "$SRC_SKILLS" --destination-skills "$DST_SKILLS" 2>>"$LOG_FILE")"
  retire_rc=$?
  if [ "$retire_rc" -ne 0 ]; then
    fail+=("upstream skill retirement check failed")
  else
    while IFS=$'\t' read -r kind value; do
      [ -n "$value" ] || continue
      case "$kind" in
        REMOVED_FILE) removed_files+=("$value") ;;
        RETIRED_SKILL) retired_skills+=("$value") ;;
      esac
    done <<< "$retire_output"
  fi
fi

for skill in "${managed[@]}"; do
  while IFS= read -r f; do
    rel="${f#"$SRC_SKILLS"/}"; mirror_one "$f" "$DST_SKILLS/$rel" "$rel"
  done < <(find "$SRC_SKILLS/$skill" -type f | sort)
  if [ -d "$DST_SKILLS/$skill" ]; then
    while IFS= read -r f; do
      rel="${f#"$DST_SKILLS"/}"
      [ -f "$SRC_SKILLS/$rel" ] || orphan_files+=("$rel")
    done < <(find "$DST_SKILLS/$skill" -type f | sort)
  fi
done

while IFS= read -r d; do
  name="$(basename "$d")"; is_managed=false
  for m in "${managed[@]}"; do [ "$m" = "$name" ] && is_managed=true && break; done
  if [ "$is_managed" = false ]; then
    if grep -qi "jarvis-local" "$d/SKILL.md" 2>/dev/null; then local_skills+=("$name")
    else orphan_skills+=("$name"); fi
  fi
done < <(find "$DST_SKILLS" -mindepth 1 -maxdepth 1 -type d | sort)

if [ ${#added[@]} -gt 0 ]; then echo "  NEW skill files (${#added[@]}):"; printf '    + %s\n' "${added[@]}"; fi
if [ ${#updated[@]} -gt 0 ]; then echo "  UPDATED to match upstream (${#updated[@]}):"; printf '    ~ %s\n' "${updated[@]}"; fi
if [ ${#removed_files[@]} -gt 0 ]; then echo "  REMOVED upstream files (${#removed_files[@]}):"; printf '    - %s\n' "${removed_files[@]}"; fi
if [ ${#retired_skills[@]} -gt 0 ]; then echo "  RETIRED upstream skills (${#retired_skills[@]}):"; printf '    - %s\n' "${retired_skills[@]}"; fi
[ ${#added[@]} -eq 0 ] && [ ${#updated[@]} -eq 0 ] \
  && [ ${#removed_files[@]} -eq 0 ] && [ ${#retired_skills[@]} -eq 0 ] \
  && echo "  skills: current with eigenflux(main) (${#managed[@]} skills mirrored)"
[ ${#local_skills[@]} -gt 0 ] && echo "  jarvis-local skills preserved: ${local_skills[*]}"
[ ${#orphan_skills[@]} -gt 0 ] && echo "  ⚠ skills in jarvis but NOT in plugin (review): ${orphan_skills[*]}"
if [ ${#orphan_files[@]} -gt 0 ]; then echo "  ⚠ files removed upstream, kept in jarvis (review):"; printf '    ? %s\n' "${orphan_files[@]}"; fi
if [ ${#orphan_skills[@]} -gt 0 ]; then review+=("unknown local skill(s) outside upstream: ${orphan_skills[*]} — mark jarvis-local or retire"); fi
if [ ${#orphan_files[@]} -gt 0 ]; then review+=("unknown local file(s) outside upstream: ${orphan_files[*]} — move to an overlay or retire"); fi

# ── 3. CLI version check + detached upgrade if behind ─────────────────
echo ""
cli_current="$(bounded 5 eigenflux version --short 2>/dev/null || echo "")"
cli_latest="$(bounded 6 curl -fsSL "$CDN_URL/cli/latest/version.txt" 2>/dev/null || echo "")"
cli_upgrade_started=false
if [ -z "$cli_current" ]; then echo "  CLI: NOT INSTALLED"; fail+=("eigenflux CLI not on PATH")
elif [ -z "$cli_latest" ]; then echo "  CLI: v$cli_current (latest unknown — offline?)"
elif [ "$cli_current" = "$cli_latest" ]; then echo "  CLI: v$cli_current (up to date)"
else
  echo "  CLI: v$cli_current installed, v$cli_latest available → background upgrade"
  if [ -x "$UPGRADE_HELPER" ]; then
    # Detach so the download survives this hook's 60s cap. macOS has no setsid;
    # nohup + disown is enough (the heartbeat kills only the pre-script's pid,
    # not the whole process group).
    if command -v setsid >/dev/null 2>&1; then
      setsid nohup "$UPGRADE_HELPER" "$cli_latest" >>"$LOG_FILE" 2>&1 < /dev/null &
    else
      nohup "$UPGRADE_HELPER" "$cli_latest" >>"$LOG_FILE" 2>&1 < /dev/null &
    fi
    disown 2>/dev/null || true
    cli_upgrade_started=true
    notes+=("CLI upgrade v$cli_current→v$cli_latest started in background (test-before-swap)")
  else notes+=("upgrade helper missing: $UPGRADE_HELPER"); fail+=("CLI behind, helper unavailable"); fi
fi

# ── 4. Parity drift on watched upstream paths (since stored SHAs) ──────
echo ""
echo "  upstream drift (watched paths only; openclaw/src excluded):"
report_drift() {
  # report_drift <repo_dir> <label> <stored_sha> <current_sha> <watched-paths...>
  local repo="$1" label="$2" stored="$3" current="$4"; shift 4
  local paths=("$@")
  if [ -z "$stored" ]; then echo "    $label: baseline recorded ($current)"; return; fi
  if [ "$stored" = "$current" ]; then echo "    $label: no new commits"; return; fi
  local log
  log="$(git -C "$repo" log --oneline "$stored..$current" -- "${paths[@]}" 2>/dev/null)"
  if [ -z "$log" ]; then echo "    $label: changed, but nothing in watched paths"; return; fi
  echo "    $label: $(echo "$log" | wc -l | tr -d ' ') watched commit(s) ${stored:0:7}..${current:0:7}:"
  echo "$log" | sed 's/^/      /' | head -10
  # Specific high-signal flags
  if git -C "$repo" diff --name-only "$stored..$current" -- cli/cmd 2>/dev/null | grep -q .; then
    review+=("$label: eigenflux/cli/cmd changed — re-check client.sh wrappers & flags vs new CLI surface")
  fi
  if git -C "$repo" diff --name-only "$stored..$current" 2>/dev/null | grep -q "cli/cmd/stream.go"; then
    review+=("$label: cli/cmd/stream.go changed — verify core/ef_stream.py parses any new NDJSON event types")
  fi
}
# Main repo is authoritative for skills + CLI contract. The claude-plugin is
# watched only for runtime-constant drift (poll interval / backoff / windows).
report_drift "$MAIN_DIR" "eigenflux(main)" "$main_stored" "$main_head" \
  cli/cmd cli/internal/client/meta.go skills
report_drift "$PLUGIN_DIR" "claude-plugin" "$plugin_stored" "$plugin_head" \
  src/feed-poller.ts src/pm-stream.ts src/profile-refresher.ts src/config.ts

# Top-level CLI command-list drift (new subcommand groups)
cli_cmds="$(bounded 5 eigenflux help 2>/dev/null | awk '/Available Commands:/{f=1;next} /^Flags:/{f=0} f && NF{print $1}' | sort | tr '\n' ' ' | sed 's/ *$//')"
prev_cmds="$(state_get cli_commands)"
if [ -n "$prev_cmds" ] && [ -n "$cli_cmds" ] && [ "$prev_cmds" != "$cli_cmds" ]; then
  new_cmds=$(comm -13 <(echo "$prev_cmds" | tr ' ' '\n' | sort) <(echo "$cli_cmds" | tr ' ' '\n' | sort) | tr '\n' ' ')
  removed_cmds=$(comm -23 <(echo "$prev_cmds" | tr ' ' '\n' | sort) <(echo "$cli_cmds" | tr ' ' '\n' | sort) | tr '\n' ' ')
  [ -n "${new_cmds// }" ] && review+=("new top-level CLI command(s): ${new_cmds}— evaluate a client.sh wrapper")
  [ -n "${removed_cmds// }" ] && review+=("removed top-level CLI command(s): ${removed_cmds}— retire any remaining consumers")
fi

# ── 5. Verify ("测通") ────────────────────────────────────────────────
echo ""
echo "  verification:"
# 5a. eigenflux-related pytest suite. This suite measured 32.5s on the
# production Mac; the old 30s budget killed a healthy run before its summary.
if bounded 120 python3 -m pytest -q "$JARVIS_DIR/tests/test_prompt.py" \
     "$JARVIS_DIR/tests/test_eigenflux_feed_search.py" \
     "$JARVIS_DIR/tests/test_eigenflux_publish_post.py" \
     "$JARVIS_DIR/tests/test_ef_stream.py" \
     "$JARVIS_DIR/tests/test_ef_stream_loop.py" \
     "$JARVIS_DIR/tests/test_eigenflux_ingress.py" \
     "$JARVIS_DIR/tests/test_eigenflux_messages.py" >/tmp/ef_pi_pytest.out 2>&1; then
  echo "    ✓ pytest (prompt + feed + stream + ingress + messages)"
else
  if ! grep -Eq '(passed|failed|error)' /tmp/ef_pi_pytest.out; then
    echo "    ✗ pytest — timed out before finishing (no summary line); raise the bound, do not read this as a test failure"
  else
    echo "    ✗ pytest — $(tail -3 /tmp/ef_pi_pytest.out | tr '\n' ' ' | cut -c1-220)"
  fi
  fail+=("pytest failed")
fi
# 5b. Live load_ef_skills() against the real synced dir
if live=$(cd "$JARVIS_DIR" && python3 - <<'PY' 2>&1
import sys, pathlib
sys.path.insert(0, ".")
from core.prompt import load_ef_skills
out = load_ef_skills(".")
dirs = sorted(p.name for p in pathlib.Path("plugins/eigenflux/skills").glob("ef-*") if (p/"SKILL.md").exists())
assert out.strip(), "load_ef_skills empty"
assert not out.lstrip().startswith("---"), "frontmatter not stripped"
print(f"{len(dirs)} skills, {len(out)} chars: {','.join(dirs)}")
PY
); then echo "    ✓ load_ef_skills() — $live"
else echo "    ✗ load_ef_skills() — $(echo "$live" | tail -2 | tr '\n' ' ' | cut -c1-200)"; fail+=("load_ef_skills failed"); fi
# 5c. CLI smoke (read-only)
authed=false
if [ -n "$cli_current" ]; then
  if bounded 5 eigenflux version >/dev/null 2>&1 && bounded 8 eigenflux server list >/dev/null 2>&1; then
    echo "    ✓ CLI smoke (version + server list)"
  else echo "    ✗ CLI smoke"; fail+=("CLI smoke failed"); fi
  # 5c-bis. settings command present (CLI 0.0.8+) + client.sh wrapper exists.
  # Read-only: `settings --help` does not touch the backend. The wrapper check
  # closes the "new top-level command → evaluate a wrapper" parity gap.
  if bounded 5 eigenflux settings --help >/dev/null 2>&1; then
    if grep -q "eigenflux_settings_sync" "$CLIENT_SH" 2>/dev/null; then
      echo "    ✓ settings command + client.sh wrapper present"
    else echo "    ✗ settings command present but no client.sh wrapper"; fail+=("settings wrapper missing"); fi
  else echo "    • settings: not available (CLI < 0.0.8?)"; fi
  # 5d. auth probe — exit 4 means token expired (a real auth nudge, not a parity failure)
  if bounded 10 eigenflux profile show -f json >/dev/null 2>&1; then authed=true; echo "    ✓ auth probe (authed)"
  else
    rc=$?; if [ "$rc" -eq 4 ]; then echo "    • auth probe: AUTH_REQUIRED (token expired — run: eigenflux auth login)"
      notes+=("EigenFlux token expired — feed/messages/publish paused until 'eigenflux auth login'")
    else echo "    • auth probe: inconclusive (rc=$rc)"; fi
  fi
fi
# 5e. Skill integrity — managed files equal upstream plus reviewed overlays.
integrity_bad=()
for skill in "${managed[@]}"; do
  while IFS= read -r f; do
    rel="${f#"$SRC_SKILLS"/}"
    expected="$f"; rendered=""
    if [ -f "$SKILL_OVERLAYS/$rel" ]; then
      rendered="$(mktemp /tmp/jarvis-ef-integrity.XXXXXX)"
      if python3 "$JARVIS_DIR/core/eigenflux_skill_overlay.py" \
          --base "$f" --overlay "$SKILL_OVERLAYS/$rel" \
          --output "$rendered"; then
        expected="$rendered"
      else
        integrity_bad+=("$rel")
        rm -f "$rendered"
        continue
      fi
    fi
    cmp -s "$expected" "$DST_SKILLS/$rel" || integrity_bad+=("$rel")
    [ -n "$rendered" ] && rm -f "$rendered"
  done < <(find "$SRC_SKILLS/$skill" -type f | sort)
done
if [ ${#integrity_bad[@]} -eq 0 ]; then echo "    ✓ skill integrity (upstream + Jarvis overlays)"
else echo "    ✗ skill integrity drift: ${integrity_bad[*]}"; fail+=("skill integrity: ${integrity_bad[*]}"); fi
# 5f. Live feed-shape check — only when authed (read-only, small sample).
# Contract (ef-broadcast/references/feed.md): every item carries a STRING
# item_id (the feedback API 400s on numeric ones — see 5f-ter); `url` is the
# source link IF PROVIDED — `original` broadcasts have none by design, so only
# curated/forwarded items must carry one. Server text fields occasionally hold
# raw control chars (not strict JSON) — parse strict=False like a tolerant
# consumer would, and report a parse failure as itself, not as field drift.
if [ "$authed" = true ]; then
  feed_json="$(bounded 10 eigenflux feed poll --limit 3 --action refresh -f json 2>/dev/null || echo "")"
  if [ -z "$feed_json" ]; then
    # Transient: poll timed out / network blip / not yet primed — NOT a contract
    # regression, so don't fail the run on it.
    echo "    • live feed shape: skipped (poll returned no data)"
  else
    shape_msg="$(echo "$feed_json" | python3 -c "
import json,sys
try: d=json.loads(sys.stdin.read(), strict=False)
except Exception as e:
    print('unparseable even with strict=False: %s' % e); sys.exit(2)
items=d.get('items') or d.get('data',{}).get('items') or []
bad=[]
for it in items:  # empty feed is fine (loop is a no-op)
    iid=it.get('item_id')
    if not (isinstance(iid,str) and iid): bad.append('item_id missing/non-string')
    if it.get('source_type') in ('curated','forwarded') and not (it.get('url') or it.get('source_url')):
        bad.append('%s item %s missing url' % (it.get('source_type'), iid))
if bad: print('; '.join(sorted(set(bad)))); sys.exit(3)
" 2>&1)"
    shape_rc=$?
    if [ "$shape_rc" -eq 0 ]; then echo "    ✓ live feed shape (string item_id; url on curated/forwarded)"
    elif [ "$shape_rc" -eq 2 ]; then echo "    ✗ live feed JSON $shape_msg"; fail+=("feed JSON parse failure")
    else echo "    ✗ live feed shape drift — $shape_msg"; fail+=("feed shape drift"); fi
  fi
fi
# 5f-ter. Feedback WRITE round-trip — actually EXERCISE the submit path, which a
# read-only smoke can't. This is the bug that black-holed every feedback: the API
# requires item_id as a STRING and 400s on a numeric one ("Mismatch type string
# with value number"). We probe with a junk string id + neutral score (0): it
# never touches a real item's score, runs even when the feed is empty, and a
# present processed_count proves the string-typed contract holds end-to-end.
if [ "$authed" = true ]; then
  fb_resp="$(bounded 10 eigenflux feed feedback --items '[{"item_id":"1","score":0}]' -f json 2>&1)"
  # The CLI prints a human 'Feedback submitted...' line before the JSON; parse
  # from the first '{' so that leading message doesn't break the JSON check.
  if echo "$fb_resp" | python3 -c "import json,sys
raw=sys.stdin.read(); i=raw.find('{')
try: d=json.loads(raw[i:]) if i>=0 else {}
except Exception: d={}
sys.exit(0 if d.get('processed_count') is not None else 1)" 2>/dev/null; then
    echo "    ✓ feedback write round-trip (string item_id accepted, processed_count returned)"
  else
    echo "    ✗ feedback write REJECTED — $(echo "$fb_resp" | tr '\n' ' ' | cut -c1-160)"; fail+=("feedback write regression")
  fi
fi
# 5f-bis. Report runtime mode to the backend (CLI 0.0.8+ settings). Jarvis is a
# native skill-based integration (not the OpenClaw plugin host) → mode "skill".
# Idempotent: `settings push` no-ops when unchanged. Best-effort telemetry — a
# failure here never fails parity. The pull direction (console edits → local
# config, incl. auto_reply_pm) already rides the feed poll above.
if [ "$authed" = true ] && bounded 5 eigenflux settings --help >/dev/null 2>&1; then
  if bounded 10 eigenflux settings push --mode skill >/dev/null 2>&1; then
    echo "    ✓ settings push (--mode skill reported)"
  else
    echo "    • settings push: skipped (transient)"
  fi
fi
# 5g. bash -n on all eigenflux scripts
syntax_bad=()
for s in "$CLIENT_SH" "$UPGRADE_HELPER" "$SCRIPT_DIR"/eigenflux_*.sh; do
  [ -f "$s" ] || continue; bash -n "$s" 2>/dev/null || syntax_bad+=("$(basename "$s")")
done
if [ ${#syntax_bad[@]} -eq 0 ]; then echo "    ✓ bash -n (client.sh + eigenflux task scripts)"
else echo "    ✗ bash -n: ${syntax_bad[*]}"; fail+=("syntax: ${syntax_bad[*]}"); fi

# ── 6. Review backlog (durable) + notes + state + sentinel ────────────
verify_ok=$([ ${#fail[@]} -eq 0 ] && echo true || echo false)
if [ ${#review[@]} -gt 0 ]; then
  echo ""; echo "  review flags (also appended to parity_todo.md):"; printf '    ! %s\n' "${review[@]}"
  # Append-dedup to the durable backlog so flags survive SHA advance.
  touch "$BACKLOG"
  for r in "${review[@]}"; do
    grep -Fqx -- "- [ ] $r" "$BACKLOG" 2>/dev/null || echo "- [ ] $r" >> "$BACKLOG"
  done
fi
# Old releases called grep without `--`, so checkbox lines beginning with '-'
# were parsed as options and appended again on every run. Canonicalize exact
# duplicates before reporting the durable open count.
open_review_count=0
if [ -f "$BACKLOG" ]; then
  python3 - "$BACKLOG" <<'PY' || true
from pathlib import Path
import sys

path = Path(sys.argv[1])
original = path.read_text(encoding="utf-8")
seen = set()
deduped = []
for line in original.splitlines():
    if line in seen:
        continue
    seen.add(line)
    deduped.append(line)
content = "\n".join(deduped).rstrip() + "\n"
if content != original:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
PY
  open_review_count=$(grep -Ec '^- \[ \] ' "$BACKLOG" 2>/dev/null || true)
fi
if [ ${#notes[@]} -gt 0 ]; then echo ""; echo "  notes:"; printf '    - %s\n' "${notes[@]}"; fi

# Advance stored SHAs ONLY on green verification (so a regression keeps the
# drift visible next run); always refresh cli_commands snapshot.
adv_plugin="$plugin_stored"; adv_main="$main_stored"
if [ "$verify_ok" = true ]; then adv_plugin="$plugin_head"; adv_main="$main_head"; fi
python3 - "$STATE_FILE" "$cli_current" "$cli_latest" "$adv_plugin" "$adv_main" "$cli_cmds" \
  "${#added[@]}" "${#updated[@]}" \
  "$(( ${#removed_files[@]} + ${#retired_skills[@]} ))" \
  "$verify_ok" "$open_review_count" 2>/dev/null <<'PY' || true
import json, sys, datetime
path, cur, latest, ph, mh, cmds, na, nu, nd, ok, nr = sys.argv[1:12]
json.dump({
  "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
  "cli_version": cur, "cli_latest": latest,
  "plugin_head": ph, "main_head": mh, "cli_commands": cmds,
  "skills_added": int(na), "skills_updated": int(nu), "skills_removed": int(nd),
  "verify_ok": ok == "true", "open_review_flags": int(nr),
}, open(path, "w"), indent=2)
PY

echo ""
if [ ${#fail[@]} -gt 0 ]; then echo "PREINSTALL_FAIL"
elif [ ${#added[@]} -gt 0 ] || [ ${#updated[@]} -gt 0 ] \
    || [ ${#removed_files[@]} -gt 0 ] || [ ${#retired_skills[@]} -gt 0 ] \
    || [ "$cli_upgrade_started" = true ] || [ ${#review[@]} -gt 0 ] \
    || [ ${#notes[@]} -gt 0 ]; then echo "PREINSTALL_CHANGES"
else echo "PREINSTALL_OK"; fi
exit 0
