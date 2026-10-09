---
name: sherlock-kit-operate
description: Operate an explicitly authorized Sherlock workload through the installed sherlock-kit policy, diagnostics, and identity-bound orchestration commands.
---

Use the existing frozen `shk` installation. Read `shk policy`, including its
packaged partition profiles (`normal`, `owners`, `btrippe`), then inspect
`shk policy --identity` and local `shk doctor` against the advertised pin.
Report a missing executable or identity mismatch before a new managed mutation.
Only request `shk doctor --remote` when remote diagnosis is needed.

For the user's authorized task, inspect the installed command help, then call
`shk submit --help`, `shk reconcile --help`, `shk occupancy --help`, or
`shk fetch --help` and supply the consumer's explicit contract/state. Commands
available in the installed release define their arguments; do not invent flags or
replace them with raw SSH wrappers. If an orchestration command is unavailable,
report that capability gate and continue independent diagnosis. This skill does
not authorize a job, budget increase, access grant, cancellation, or a historical
campaign restart.

Take resources from the partition profile, never from scheduler visibility. Before
a submission to a borrowed partition, run `shk occupancy --partition <borrowed>`
and apply the profile's courtesy text; the toolkit reports occupancy but does not
gate on it. Set `requeue` only for a script that resumes from its own checkpoints
(the `owners` default); `--requeue` is emitted only where the profile allows it.
With many open attempts, use `shk status --all` or `shk reconcile --all` rather
than one query per attempt. Treat an `unexpected_preemption` result (exit 2) as
an investigation, not a retry; `shk reconcile --attempt ID
--acknowledge-preemption` is the explicit release once it is understood.

Preserve unresolved mutation evidence and reservations; reconcile the recorded
attempt identity before another submission. A fetched artifact requires the
consumer's scientific validator. Report revision, attempt, and validation outcome.
Keep durable state outside worktrees and scratch. Do not implement a scheduler,
transfer engine, retry loop, or ledger in this adapter.

Install only this namespaced skill in the client's owned skill directory. Global
policy projections deliver instructions separately; the owner's opt-in dotfiles
configuration is the single hook registration owner.
