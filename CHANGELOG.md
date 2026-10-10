# Changelog

## 0.3.0

Slurm as the source of truth, job arrays and the full partition table. The local
SQLite ledger with its reservations, budgets and cost caps is gone: the only
durable attempt state is an attempt registry on Sherlock (`registry_root`) plus
Slurm accounting, so any POSIX workstation with the frozen toolkit and the
private configuration can operate. Every new path is tested offline by executing
the real registry program against fake `sbatch`/`sacct`/`squeue` binaries. The
live CPU pilot of the registry and array paths on `normal` is pending; its
evidence will be added to the acceptance document and this sentence updated when
it has run. The partition-table change has no live pilot of its own.

- Registry (`sherlock_registry`, standard library, Python 3.11-3.14): the CLI
  ships the module's own source to the login node as a self-decoding
  `python3 -c` program with `submit`, `read`, `event` and `fetch-manifest`
  subcommands, so runner, reader and event writer resolve attempts with the same
  code against a query taken on the login node under the registry lock. Layout
  under `registry_root` (owned, 0700, no symlinks): `.lock`, `tasks/<key>` (the
  latest attempt of a logical task), `attempts/<id>/record.json`,
  `submitted.json`, `not_sent.json`, `ack-<epoch>.json`, `abandoned.json` and
  `resolved.json`. Every file is create-once (private temporary file plus
  `link`), nothing is read-modify-written and timestamps are stamped on the
  login node.
- Private configuration: new required key `registry_root` (absolute canonical
  POSIX path on the control host); `state_root` and `limits` are refused by the
  unknown-field message as the migration signal. Typed commands keep only
  `auth-backoff.json` and the new 60 s query cache `query-cache.json` (0600,
  its own lock, sentinel semantics, expired entries pruned on write, malformed
  or public files refused) under an explicit `transport.backoff_file` or
  `SHERLOCK_KIT_STATE_ROOT`; with neither they refuse to run instead of using
  `$HOME`. Grants keep grantee, evidence reference, validity, partitions and
  scope; a legacy limits member is ignored.
- Submission: `submit --apply` makes one runner call per attempt with a fresh
  id, records `record.json` before `sbatch` and maps the single reply line to
  `submitted` (exit 0), `not_sent` (exit 1, recorded; resubmit naming the id as
  `parent_attempt`), a translated refusal (exit 2, nothing recorded) or `unknown`
  (exit 2, `shk status --attempt ID`). Logical-task deduplication and the retry
  rule live in the registry: a retry names `parent_attempt`, and the parent must
  be `not_sent`, `abandoned` or `terminal` without a `COMPLETED` task.
- Job arrays: `resources.array = {"count": 2..1000, "throttle": 1..count}`
  emits `--array=0-<count-1>[%throttle]` and `slurm-%A_%a` logs; sacct `N_k`
  task rows and `N_[a-b%t]` pending or cancelled aggregates are reconciled per
  task and restart, reported as `tasks {count, terminal, running, pending,
  missing, by_state, incomplete_history}`, with cost summed over complete task
  and restart groups and `known` only when every task is terminal with complete
  accounting.
- Reconciliation: `status`/`reconcile --attempt ID` and `--all` make one cached
  reader call (one `sacct --name=shk-A,shk-B,...` on the login node; 500 open
  attempts, 50,000 rows); resolutions are `not_sent`, `abandoned`,
  `inconclusive`, `abandonable`, `identified`, `terminal`,
  `unexpected_preemption` and `error`. New `reconcile --attempt ID --abandon
  [--note T]` closes an attempt that stayed absent from accounting for 900 s
  without a known job id after a fresh `squeue` check on the login node;
  `--acknowledge-preemption [--note T]` now writes a per-task waiver under the
  registry lock from a fresh query and needs the network. `reconcile` writes
  `resolved.json` for fresh terminal attempts, which bounds `--all`; a
  `submitted.json` job id that differs from accounting is an `error`
  (`job_id_conflict`) and never releases. `status --local` and
  `reconcile --local` are removed.
- Fetch: the control-side manifest read is the registry's `fetch-manifest`, and
  an attempt sidecar `.NAME.shk-attempt.json` (attempt, producer, validator
  identity, manifest and its SHA256) is written durably beside the destination
  before the transaction record; `fetch --local` recovers from that sidecar
  alone, so a destination promoted by 0.2.0 needs one remote fetch first.
- Removed: `Coordinator`, SQLite, budgets, reservations, cost floor, quarantine,
  `recover`, `scheduler_cost`, legacy-profile dispatch, evidence helpers,
  `REMOTE_MANIFEST`, `state_root`, `limits`. 0.2.0 ledger attempts are not
  migrated: resolve them with the old installation, then archive
  `coordinator.sqlite3`.
- Full partition table: `sherlock_kit_data/partitions.json` adds the public
  `bigmem`, `dev`, `service` and `gpu` profiles (not preemptible, not borrowed;
  GPUs allowed on `gpu` and `dev`), the department GPU profiles `bioe` and `stat`
  (not borrowed, fairshare courtesy) and the borrowed GPU profile `possu` (grant
  required; its courtesy text confines submission to 00:00-07:00 Pacific).
  `partitions_sha256` changes accordingly; frozen attempts keep the profile they
  were admitted with. Documentation names every profile and the borrowed set
  (`btrippe`, `possu`) and states that a courtesy time window is honoured by the
  operator or agent, not checked by the toolkit.
- Policy: two projection bullets change (a lost mutation reply is resolved by
  `shk status --attempt ID` against the registry and accounting; "budgets" is
  dropped from the explicit-inputs sentence) and the decisions section describes
  the registry model. `policy_sha256` changes, so the dotfiles pin and both
  delivered projections must be regenerated before new managed admissions.
- Tests: `tests/test_registry.py` executes the runner, reader and event writer
  in-process against a temporary registry with fake Slurm binaries, including
  three concurrent runners on one logical task; `tests/fake_remote.py` drives the
  CLI the same way; `tests/test_release_metadata.py` ties the version strings,
  the projection, the documented config keys and the duplicated constants.

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
