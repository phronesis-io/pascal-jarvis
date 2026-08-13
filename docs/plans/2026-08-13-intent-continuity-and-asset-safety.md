# Intent Continuity, Dead-Man, and Asset Safety PRD

## Problem

Several stale status reports mix already-shipped work with real reliability
gaps. Backup-memory priority allocation and cross-session collection are live,
and the unused X Ads intent is cancelled. The remaining defects are narrower:

1. A provider timeout, quota failure, or process shutdown is reconciled as a
   missing model envelope. That consumes an intent attempt even though the
   intent was never evaluated.
2. One-shot reminders use wall-clock age as retry grace. Sleeping or powering
   off the Mac can therefore exhaust the grace before Jarvis gets an awake
   chance to deliver.
3. Breach cards ignore the closure attention policy, so non-notifying context,
   healing, and autonomous intents can become user-facing apologies.
4. The on-machine guardian cannot detect a FileVault/pre-login outage from
   outside the Mac.
5. Claude Code and Codex ingestion have component tests but no scenario test
   proving that both providers reach durable memory.
6. Daily backup covers Claude sessions but not Codex sessions, and only one of
   several SQLite databases. Local branches, dirty patches, and untracked
   drafts also have no unified recovery image. Backup pruning is not currently
   gated on a fully verified replacement snapshot.

## Product Contract

- A provider/infrastructure failure returns every inflight intent to `pending`
  and restores the attempt consumed by the claim. It never creates a breach.
- A model that responded but omitted usable intent content still consumes a
  bounded content attempt. One-shot reminders receive up to three real content
  attempts regardless of asleep wall time; reminders older than the existing
  24-hour storm boundary remain retired.
- Only closure categories with `may_notify=true` (`hard`, `external`) may enter
  the breach notification queue. Other categories still close observably in
  lifecycle events without taking Pascal's attention.
- Cancelling an Intent locally resolves every still-pending Memorial whose
  closure action targets it, without sending or bulk-editing old Lark cards.
  User-decided cards and unrelated reminders remain untouched.
- An optional external dead-man endpoint receives a bounded, secret-safe ping
  from the independent guardian only while the local stack is healthy. Missing
  pings are interpreted by the external service; Jarvis never claims this
  protection is active until an endpoint is configured and a ping succeeds.
- A deterministic test covers Claude + Codex transcript discovery, digest
  projection, consolidation prompt inclusion, and silent durable memory write.
- A successful daily snapshot includes Claude sessions, Codex sessions, all
  local memory (provider-separated), WAL-safe copies of every SQLite database,
  private runtime data, configuration, Git history, dirty binary patches, and
  non-ignored untracked drafts. Snapshots are private by filesystem permission,
  carry a typed checksum manifest for files and links, and old snapshots are
  pruned only after the new snapshot verifies.

## Non-Goals

- Jarvis does not provision or own a third-party monitoring account.
- The cancelled X Ads collector belongs to its external data system; Jarvis
  only owns its intent and scheduling surfaces.
- Cross-session consolidation remains selective. It must not dump full chat
  transcripts into memory or send routine summaries to Lark.
- Cleanup never deletes an untracked file, unique local branch, database, key,
  or transcript merely because it looks stale. Destructive cleanup requires a
  verified backup and an independently reproducible source.
- Backup never changes permissions on live session sources. Snapshot
  permissions protect the copy; source immutability would block legitimate
  session recovery without preventing directory-level deletion.

## Acceptance

- `__CALL_FAILED__` is distinct from `__NO_ENVELOPE__` end to end.
- Infrastructure deferral preserves attempt budget for date and cron intents.
- Three contentless responses expire a date intent; elapsed sleep time alone
  does not.
- Non-notifying categories cannot append breach queue entries or aggregate
  skip-digest noise.
- Dead-man URL values never appear in logs, result details, or committed files.
- Disabled dead-man is an explicit skipped component; enabled-but-unhealthy is
  a failed component.
- The cross-session E2E test proves both provider sources and zero stdout from
  memory consolidation.
- Backup verification fails closed on a missing class, bad SQLite image,
  checksum/link mismatch, unsafe symlink, or group/world-readable snapshot.
- Every Git bundle verifies as a complete repository image and contains the
  recorded HEAD of every linked or detached worktree.
