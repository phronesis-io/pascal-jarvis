# Relationship Memory And Action Resolution

Status: implemented

## Incident

Jarvis had previously learned a close person's name and provider identities,
but the facts lived in topical notes and hourly archives. When the owner later
said "invite my spouse", the model asked who that meant and prepared a calendar
action without a deterministic attendee binding. The system had memory as
prose, but no executable person identity.

## Product Contract

1. Stable relationship references resolve from one owner-private registry.
2. The owner prompt receives an ID-free projection of known people before
   ordinary memory budgets are applied. Shared/group prompts receive none of it
   and cannot execute model-authored actions, including owner-authored turns.
3. Calendar creation and attendee updates accept names or relationship aliases.
   A requested attendee must resolve to a verified Lark identity before any
   external write; otherwise the whole action fails closed.
4. EigenFlux direct messages use the same relationship aliases, then verify the
   private binding against the current server-side friend record. Once the
   registry exists, removed aliases cannot revive through the legacy file.
5. Provider IDs, personal names, and relationship facts remain in gitignored
   local data, are mode 0600, and are covered by the verified private backup.
6. Historical prose and old IDs are context only and never execution authority.

## Storage

`data/person_registry.json` is the local source of truth. The tracked example
contains placeholders only. Each person has exact aliases, relationship labels,
channel bindings, verification dates, and optional behavioral boundaries.

## Acceptance

- "spouse" or another exact configured alias appears in the owner-private
  prompt without provider IDs and does not appear in a group prompt.
- Creating a calendar event with a configured relationship adds the verified
  attendee ID.
- Missing, ambiguous, malformed, or channel-less bindings cause no calendar or
  message write.
- Existing calendar actions without attendees remain backward compatible.
- EigenFlux resolves the same relationship alias and still cross-checks the
  live friend record before sending.
