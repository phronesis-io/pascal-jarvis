# Unified Model Runtime Usage

**Status:** deployed with PR #129--#131; provider-neutral execution
orchestration remains a separate Phase 3 objective
**Date:** 2026-08-27

## Problem

Pascal should not need to open several billing pages on a computer to learn
whether the current model package is about to run out. Provider health, login,
route order, and remaining subscription allowance are different facts. Jarvis
previously knew some of them but had no trustworthy single product surface.

## Product Contract

The Model Runtime returns one report containing:

1. the route that would execute now and its fallback order;
2. exact known usage percentage and reset time;
3. an exhaustion forecast when history supports one;
4. account type and real-request limit failures;
5. explicit `unknown` values where no provider-defined balance interface exists.

The normal query surface is Codex desktop/mobile through
`jarvis_model_status`. Owner Lark supports `/usage` and direct quota questions.
An hourly model-free refresh is silent unless a new route-risk episode needs
attention.

## Evidence Levels

| Level | Meaning | Numeric percentage allowed |
|---|---|---:|
| `exact` | Supported provider surface returned a named window and reset | yes |
| `account_only` | Login/account type is known, allowance is not | no |
| `unknown` | No provider-defined allowance surface exists or the read failed | no |

A successful canary proves only that one bounded request answered. It is never
quota evidence and cannot overwrite a production-size timeout or account-limit
failure.

## Implementation

- `core.model_usage` reads Codex `account/rateLimits/read`, sanitizes reset
  credit data, joins catalog/health/route state, stores numeric observations,
  forecasts exhaustion, and writes a mode-0600 latest snapshot.
- `core.codex_frontstage` and `core.codex_mcp` expose the joined report.
- `core.matter_bridge` handles `/usage` and natural owner quota questions on a
  deterministic path, so answering does not depend on the provider being
  measured.
- `tasks/model_usage_pre.py` refreshes without an LLM;
  `tasks/model_usage_post.py` keeps one mode-0600 episode fingerprint for
  low-noise alerts.
- SQLite stores route, limit, window, used percentage, reset epoch, observation
  epoch, and source for 45 days. It stores no credentials or provider prose.

## Risk And Attention Policy

- `warning`: 80-89%; visible in an explicit status read, no interruption.
- `critical`: 90-99% or predicted exhaustion before reset; alert once.
- `exhausted`: 100%, reached flag, or spend-control flag; alert once.
- real production request reports `account_limit`; alert once.
- unchanged episode; silent.
- all issues recover; clear fingerprint and rearm.

## Acceptance

1. Fake app-server tests prove initialize-before-read and bounded termination.
2. Exact, account-only, and unknown evidence never collapse into one state.
3. Opaque credit IDs, descriptions, credentials, and raw provider errors do
   not enter the report or alert state.
4. Forecasting requires the same reset window, positive use, and at least five
   minutes of elapsed evidence.
5. `/usage`, natural Lark wording, and Codex MCP return the joined report.
6. Healthy refreshes are silent; one risk episode alerts once; recovery rearms.
7. Full local tests, capability inventory, repo hygiene, protected CI, review,
   release gate, deploy verification, and a post-release real read all pass.

## Non-Goals

- Scraping provider billing webpages or storing browser sessions.
- Guessing subscription percentage from tokens, spend, login, or canaries.
- Automatically buying capacity, changing plans, or consuming reset credits.
- Claiming MICU/relay allowance until it exposes a defined read interface.
