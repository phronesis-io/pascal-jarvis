# Audit R20: T50-T53 Closure

Date: 2026-08-28

This round reconciles the final findings from `AUDIT_TODO_2026-08-28.md`
before the product-boundary audit continues. A finding is closed only by code
and a regression, or by an explicit product/configuration boundary.

## T50: Earlier Mechanism Debt

| Item | Verdict | Evidence |
|---|---|---|
| T3 model fallback | Already closed | `core.model_runtime` retries the provider-selected `next_model`; `tests/test_model_runtime.py` pins replayable failover. |
| T14 fast wedge verdict | Closed in R20 | Fresh consecutive failures now remain a candidate for 30 minutes. Stale failing success watermarks still trip immediately. The external dead-man waits for the verified recovery verdict. |
| T6 daily self-improve drift | Closed in R20 | Successful rounds use a local-calendar-day gate with an 18-hour minimum gap; failures retry after six hours; empty output remains failure evidence. |
| T6/T7 self-diagnostic silence | Intentional product rule | Automatically owned warnings stay in internal evidence. Only an expired owner OAuth token creates an action card. Reintroducing generic health cards would violate the owner's request that self-diagnosis repair itself. |
| T7 raw diagnostic copy | Already closed | `tasks/self_diagnostic_post.py` discards model prose and sends only deterministic plain-language owner action. |
| T22 generic recovery | Bounded legacy receipt | Successful self-heal is silent. The generic recovery line exists only to close a previously owner-visible restart-breaker incident; it cannot open a new incident. |
| T23 memory Git growth | Closed in R20 | Runtime hygiene inspects loose-object count/size and performs a safe full `git gc` above 1,000 objects or 50 MiB; it never uses immediate pruning. |
| T23 perception seen growth | Already closed | `core.perception` caps the seen ledger at 10,000 lines. |
| T21 residual polling | Product boundary | Intentions already exit early when no work is due. The EigenFlux friend poll remains the durable fallback to a best-effort real-time stream. Personal-site checks are configuration-gated and internal. |

## T51: False External Dead-Man Outage

Closed in R20. `brain_dead` is now an observation; `deadman_withhold` is the
separate verified authority. Post-wake grace, network-offline deferral, and the
20-minute recovery window cannot suppress the external receipt. Recovery
clears the authority immediately. Tests replay both verified withholding and
an unverified candidate that continues pinging.

## T52: EigenFlux Stream Zero Output

Closed in R20. After six long-lived zero-output connections the stream:

- persists `status=degraded`;
- slows retries to once per 15 minutes;
- relies on the existing five-minute polling ingress for no-loss delivery;
- appears as `△ degraded` in component reports while remaining available;
- does not trigger Guardian process recovery while polling is verified.

## T53: Cleanup and Configuration Boundaries

- `TODO.md` now matches `VERSION` 1.15.0.
- Runtime-only retired state keys are data migration, not tracked source; they
  are not deleted from an integration worktree.
- `sources.yaml`, mobile private keys, temporary logs, and local permissions
  are production-host assets. This branch must not copy or mutate them.
- Dependency locking, example-config parity, shellcheck cleanup, and dead-code
  removal remain named engineering work for later audit rounds; they are not
  falsely declared complete here.
- Existing stacked PRs are superseded by this one integration branch. Merge
  and deployment still require an exact-SHA Owner release receipt.

## Verification

- Focused R20 suite: brain health, Guardian dead-man, wake grace, runtime
  hygiene, components, self-improve, and EigenFlux stream.
- Full local quality gate and capability inventory are required before this
  round is committed.
