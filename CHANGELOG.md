# Changelog

## 0.3.0

Full partition table for the account. The packaged profile table now covers every
partition `sh_part` lists for the owner, so a spec may name any of them. Profile
shape, grant rule, admission and reconciliation code are unchanged; this release
is data and documentation only, with no new live pilot.

- `sherlock_kit_data/partitions.json` adds the public `bigmem`, `dev`, `service`
  and `gpu` profiles (not preemptible, not borrowed; GPUs allowed on `gpu` and
  `dev`), the department GPU profiles `bioe` and `stat` (not borrowed, fairshare
  courtesy) and the borrowed GPU profile `possu` (grant required; its courtesy text
  confines submission to 00:00-07:00 Pacific). `partitions_sha256` and the policy
  projection change accordingly; frozen attempts keep the profile they were
  admitted with.
- Documentation names every profile and the borrowed set (`btrippe`, `possu`) and
  states that a courtesy time window is honoured by the operator or agent, not
  checked by the toolkit.

## 0.2.0

Partition profiles, requeue-aware reconciliation, batch reconciliation and
partition occupancy. Every new path is tested offline against synthetic scheduler
responses, and a live owners/btrippe GPU pilot on 2026-10-09 exercised GPU
admission, one operator-issued requeue with restart-aware accounting, `--all`,
occupancy and DTN fetch (see acceptance evidence); natural preemption remains
offline evidence. All of it is engineering evidence, not scientific acceptance.

- Packaged partition profile table (`sherlock_kit_data/partitions.json`, module
  `sherlock_partitions`): `normal`, `owners` and `btrippe` with boolean
  `preemptible`/`requeue`/`borrowed`/`gpus_allowed` flags and a courtesy sentence,
  no caps. Unknown partitions are refused offline at spec check, GPUs only where
  allowed, `requeue` defaults to the profile and is refused where it forbids it.
- Admission freezes the profile and `partitions_sha256` into the attempt, consults
  a grant only for borrowed profiles (a configured `btrippe` grant no longer blocks
  `normal` submissions) and charges open or uncertified attempts at the larger of
  their estimate and stored lower bound. Dispatch emits `--requeue --open-mode=append`
  or `--no-requeue` from the frozen profile and refuses specs frozen by 0.1.0, so
  such `not_sent` attempts can no longer be dispatched and keep their reservation
  until explicitly resolved.
- Reconciliation groups accounting rows by restart: the highest restart is
  authoritative, `BOOT_FAIL` and `DEADLINE` are terminal, `PREEMPTED` ends only a
  non-requeued preemptible job, costs sum over restarts as a never-decreasing lower
  bound and are certified only with a complete restart history. Preemption or a
  restart on a non-preemptible profile is `unexpected_preemption` (exit 2,
  reservation kept) until `reconcile --attempt ID --acknowledge-preemption`.
  The submit-time tolerance is 300 seconds and the sacct cache key is per attempt.
- `status --all`/`reconcile --all` reconcile every reserved submitting/submitted/unknown
  attempt through one cached `sacct --name` list; a single attempt's contract
  violation is reported as an `error` entry. `occupancy --partition NAME` reports
  per-user running/pending jobs and running GPUs from fixed-width `squeue -O`
  output and gates nothing.
- Transport: the shared authentication cooldown arms only when OpenSSH exits 255
  with an authentication message; `auth_failure`, `record_auth_failure` and
  `cooldown_active` are public. rsync failures and deadlines raise `TransferError`
  with a bounded stderr tail instead of a traceback; `data_transfer` prepends the
  data host, refuses during a cooldown and arms it on a DTN authentication failure.
- Identity: `policy_identity()` reports `partitions_sha256` in both modes, frozen
  installs validate it, doctor and admission compare it only when a pin advertises
  it. `private_config` refuses unknown keys.
- The policy projection lists the packaged profiles verbatim. This changes
  `policy_sha256`, so the dotfiles pin and both delivered projections must be
  regenerated before new managed admissions. The package, Claude plugin and
  changelog versions are tied by `tests/test_release_metadata.py`.
- Offline in-process tests (`tests/test_cli_flows.py`) drive `status`, `--all`,
  `--acknowledge-preemption`, `submit --apply` gate ordering, remote fetch wiring
  and `occupancy` through the CLI against a faked transport.

## 0.1.0

Initial release prepared from the accepted foundation and CPU pilot.

- Canonical source-backed Sherlock policy, provenance identity and verified
  instruction projections for Claude Code and Codex.
- Bounded noninteractive OpenSSH with shared authentication cooldown and
  read-only local/explicit remote doctor.
- Typed single-allocation CPU submission, shared SQLite admission/budgets,
  durable uncertainty and identity-bound reconciliation with cached queries.
- Manifest-bound rsync transfer, frozen workload validators, atomic artifact
  promotion and durable offline recovery across toolkit updates.
- Narrow opt-in guard and namespaced first-party skills, tested with both
  actual clients. Production registration/trust is explicit.
- Explicit runtime state placement, release documentation and full POSIX CI.

Supported limits and real acceptance are in
[acceptance evidence](docs/validation/acceptance.md). No research code/data,
borrowed-partition authorization or generic GPU recovery is distributed.
