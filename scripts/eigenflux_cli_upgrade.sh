#!/usr/bin/env bash
# Detached EigenFlux CLI upgrader — invoked in the background by
# tasks/eigenflux_preinstall_pre.sh when the installed CLI is behind latest.
#
# Why a separate, detached script: the heartbeat pre-hook is killed at 60s, but
# a CLI download can take longer and must not block it. This runs detached
# (nohup) so it survives the hook's exit.
#
# Strategy: download ONLY the binary from the CDN (NOT the full install.sh,
# which also touches the OpenClaw plugin + global skills) and test-before-swap:
# the new binary must run `version --short` and report the expected version
# before it replaces the live one. The swap is a true same-filesystem atomic
# rename (temp file created INSIDE the install dir). The old binary is backed up
# to .bak; on any failure the live binary is restored (or, on a first-ever
# install with no prior binary, the broken download is removed). A single-flight
# mkdir lock prevents two concurrent upgraders from racing the live path.
#
# Usage: eigenflux_cli_upgrade.sh <expected_latest_version>
# Writes a one-line outcome to eigenflux/.cli_upgrade_result for the next
# pre-hook run to surface in its report.

set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RESULT_FILE="$JARVIS_DIR/eigenflux/.cli_upgrade_result"
LOG_FILE="$JARVIS_DIR/eigenflux/preinstall_cli_upgrade.log"
CDN_URL="${EIGENFLUX_CDN_URL:-https://cdn.eigenflux.ai}"
INSTALL_DIR="${EIGENFLUX_INSTALL_DIR:-$HOME/.local/bin}"
LOCK_DIR="$INSTALL_DIR/.eigenflux.upgrade.lock"

mkdir -p "$(dirname "$RESULT_FILE")" "$INSTALL_DIR"
log()    { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG_FILE"; }
finish() { echo "$1" >"$RESULT_FILE"; log "$1"; exit 0; }

# ── Single-flight lock (mkdir is atomic; macOS has no flock) ──────────
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "skipped: another upgrade already in progress"; exit 0
fi
TMP=""
cleanup() { [ -n "$TMP" ] && rm -f "$TMP" 2>/dev/null; rmdir "$LOCK_DIR" 2>/dev/null; }
trap cleanup EXIT

EXPECTED="${1:-}"

# Resolve target platform the same way install.sh does.
case "$(uname -s)" in
  Linux*)  OS="linux" ;;
  Darwin*) OS="darwin" ;;
  *) finish "FAILED: unsupported OS $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  ARCH="amd64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *) finish "FAILED: unsupported arch $(uname -m)" ;;
esac
BIN_NAME="eigenflux-${OS}-${ARCH}"

LATEST="$EXPECTED"
[ -z "$LATEST" ] && LATEST="$(curl -fsSL "$CDN_URL/cli/latest/version.txt" 2>/dev/null || echo "")"
[ -z "$LATEST" ] && finish "FAILED: could not resolve latest version"

CURRENT="$(eigenflux version --short 2>/dev/null || echo "")"
[ "$CURRENT" = "$LATEST" ] && finish "already up to date (v$CURRENT)"

log "upgrading eigenflux v${CURRENT:-none} -> v$LATEST"
# Temp file INSIDE the install dir → mv is a same-filesystem atomic rename.
TMP="$(mktemp "$INSTALL_DIR/.eigenflux.upgrade.XXXXXX")" || finish "FAILED: mktemp in $INSTALL_DIR"

if ! curl -fsSL "$CDN_URL/cli/$LATEST/$BIN_NAME" -o "$TMP" 2>>"$LOG_FILE"; then
  finish "FAILED: download error for v$LATEST"
fi
chmod +x "$TMP"

# Test-before-swap: the new binary must run and report the expected version.
NEW_VER="$("$TMP" version --short 2>/dev/null || echo "")"
if [ "$NEW_VER" != "$LATEST" ]; then
  finish "FAILED: downloaded binary reports '$NEW_VER', expected '$LATEST' — kept v${CURRENT:-none}"
fi

# Back up the current binary (if any). A backup failure aborts the upgrade.
had_prev=false
if [ -f "$INSTALL_DIR/eigenflux" ]; then
  cp -p "$INSTALL_DIR/eigenflux" "$INSTALL_DIR/eigenflux.bak" \
    || finish "FAILED: could not back up current binary — kept v$CURRENT"
  had_prev=true
fi

if ! mv "$TMP" "$INSTALL_DIR/eigenflux"; then
  finish "FAILED: could not install to $INSTALL_DIR — kept v${CURRENT:-none}"
fi
TMP=""  # consumed by mv; nothing for cleanup() to remove

# Post-swap sanity check; restore (or remove) if the installed binary misbehaves.
if [ "$("$INSTALL_DIR/eigenflux" version --short 2>/dev/null)" = "$LATEST" ]; then
  finish "upgraded v${CURRENT:-none} -> v$LATEST ✓"
elif [ "$had_prev" = true ] && [ -f "$INSTALL_DIR/eigenflux.bak" ]; then
  mv "$INSTALL_DIR/eigenflux.bak" "$INSTALL_DIR/eigenflux"
  finish "FAILED: post-swap check failed — rolled back to v$CURRENT"
else
  rm -f "$INSTALL_DIR/eigenflux"
  finish "FAILED: post-swap check failed, removed broken binary (no prior version to restore)"
fi
