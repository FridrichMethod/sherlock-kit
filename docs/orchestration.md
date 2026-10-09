# Typed consumer operations

This implementation supports one allocation per attempt on a packaged partition
profile (`normal`, `owners`, `btrippe`), without arrays. Slurm requeue is emitted
only where the profile allows it (`owners`), and a requeued script must itself
checkpoint and resume; the toolkit does not certify general DDP or checkpoint
recovery. A workload supplies its source/runtime/input identities, immutable
script, resources, output manifest and scientific validator. The rasraser adapter
is a separate local research commit; no research source or data is distributed
here. The live CPU provenance pilot on `normal` passed shared submission,
reconciliation, DTN fetch and offline recovery; exact resources, identities and
limitations are recorded in [acceptance evidence](validation/acceptance.md). The
GPU, requeue, `--all`, occupancy and DTN fetch paths added in 0.2.0 were exercised
by a live owners/btrippe GPU pilot on 2026-10-09 (one operator-issued requeue with
restart-aware accounting, two fetched bundles); natural preemption and the
`unexpected_preemption` path remain offline-tested only.

The authoritative controller and artifact promotion currently require POSIX
(`fcntl`, Unix ownership and no-follow filesystem checks). Windows instruction,
guard and installer adapters do not imply Windows controller support. Native
PowerShell execution is covered by dotfiles' native Windows CI, while it remains
unavailable on the implementation workstation. That does not validate a Windows controller.

## Partition profiles

`shk policy` lists the packaged profiles; `shk policy --identity` reports their
hash as `partitions_sha256`. A profile carries only the flags `preemptible`,
`requeue`, `borrowed`, `gpus_allowed` and a courtesy sentence; it never carries
caps. A spec naming a partition outside the table is refused at spec check,
offline, before any remote call. GPUs are accepted only where `gpus_allowed`.
`requeue` defaults to the profile value and `true` is refused where the profile
forbids it. Admission freezes the profile and `partitions_sha256` into the attempt,
and dispatch reads only that frozen copy, so a later table change never alters a
recorded attempt.

Borrowed profiles (`btrippe`) additionally need an identity-bound `grant` in the
private configuration: grantee equal to the principal, an evidence reference, a
validity interval, the partition, the resource scope and the exact shared limits.
The grant is consulted only for borrowed profiles; a configured `btrippe` grant
does not affect `normal` or `owners` submissions, whose frozen attempt records
`grant: null`. Before a borrowed submission run `shk occupancy` and apply the
profile's courtesy text; the toolkit reports occupancy but never gates on it.
Historical visibility or a configurable grant field is not authorization.

## Private configuration

Configuration is a private owned JSON file (0600). `state_root` is an absolute,
private 0700 durable directory shared by all consumers on one authoritative
workstation, outside worktrees and scratch. SQLite transactions coordinate admission
and dispatch; no database lock is held across SSH. A different workstation/schema
is refused. This is not a distributed coordinator.

Managed CLI transport uses `state_root/auth-backoff.json` when no explicit
`transport.backoff_file` or `SHERLOCK_KIT_STATE_ROOT` override is supplied. Keep
private configuration, state and research results outside this checkout. See
[state lifecycle](maintenance.md) before moving or retiring a controller.

```json
{
  "schema_version": 1,
  "state_root": "/synthetic/private/coordinator",
  "principal": "fixture",
  "cluster": "sherlock",
  "limits": {"cpus": 1, "gpus": 0, "tasks": 1, "cpu_seconds": 1200},
  "transport": {"control_host": "sherlock-plain", "data_host": "sherlock-dtn"},
  "fetch_root": "/synthetic/private/results",
  "validator_roots": ["/synthetic/immutable/release"],
  "remote_roots": {
    "control": "/synthetic/authorized/pilot",
    "data": "/synthetic/authorized/pilot",
    "namespace_verified": true
  }
}
```

The key set is exhaustive. Required: `schema_version` (1), `state_root`,
`principal`, `cluster`, `limits` and `transport`. Optional: `fetch_root`,
`validator_roots`, `remote_roots` and `grant`. Any other key is refused before
the state root is touched (`unknown private config field(s): ...; filesystem and
partition access are not configurable`), so a misspelled key cannot silently
become inert. These paths and permissions are illustrative. Resolve and verify
real authorized roots on the intended host first. `namespace_verified` records
the consumer's explicit control/data namespace check; the toolkit also verifies
canonical remote paths and refuses symlinks. Source roots must remain immutable
during fetch. No configuration field grants filesystem or partition access.

## Attempt specification

An attempt JSON contains `project`, `campaign`, `task`, `cluster`, `principal`,
`resource_scope`, SHA256 `code_digest`, `input_digest`, `runtime_digest`,
`policy_digest`, `script_digest`, an absolute `remote_script`, and `resources`.
Resources include explicit `partition`, `cpus`, `tasks`, `gpus`, `memory_mb` and
`walltime_seconds`. Walltime is rounded up to Slurm's minute resolution. Optional
`constraint` and `signal` requests have a strict grammar; optional boolean
`requeue` follows the profile rule above and the checked resources always carry
it. `account`, node exclusion and arrays are unsupported. For fetch, freeze
`validator_path`, `validator_digest` and `validator_function` at admission. The
standalone Python validator is trusted workload code, not a sandbox. It receives
a bundle directory and must return exactly `True`; its verified source snapshot
executes without loading or writing a `.pyc`. Imported dependencies belong to the
consumer's frozen runtime contract.
Production submission also requires `remote_run_directory`: an existing canonical
directory in the consumer's authorized new namespace. Slurm working directory and
job-ID-named stdout/stderr are explicitly placed there, separately from immutable
source. The remote runner validates it before calling sbatch.

```console
shk submit --config private.json --spec attempt.json
shk submit --config private.json --spec attempt.json --apply
shk status --config private.json --attempt ATTEMPT_ID
shk status --config private.json --all
shk reconcile --config private.json --attempt ATTEMPT_ID --local
shk reconcile --config private.json --all
shk reconcile --config private.json --attempt ATTEMPT_ID --acknowledge-preemption
shk occupancy --config private.json --partition btrippe
shk fetch --config private.json --attempt ATTEMPT_ID --manifest /authorized/manifest.json --source-root /authorized/bundle --destination /local/results/new-bundle
shk fetch --local --config private.json --attempt ATTEMPT_ID --manifest /authorized/manifest.json --source-root /authorized/bundle --destination /local/results/new-bundle
```

## Submission

Submission preview never launches a job and needs no remote access. Apply requires
frozen installed identity, matching advertised policy/revision when a pin is
configured (`partitions_sha256` is compared only when the pin advertises it), and
authenticated principal verification. Admission durably reserves shared scope
resources before dispatch. The sbatch options are built from the frozen profile:
`--partition=NAME`, then `--requeue --open-mode=append` when `requeue` is true or
`--no-requeue` otherwise, and `-G N` when GPUs were admitted. The helper snapshots
and hashes script bytes before `sbatch --parsable`, rejects embedded `#SBATCH`, and
removes `SBATCH_*` environment overrides. The script must launch only its immutable
release/runtime; a script digest alone cannot prove the contents of referenced paths.

Attempts admitted by 0.1.0 have no frozen profile. Dispatch builds the sbatch
options before taking its claim and refuses such a spec, so a 0.1.0 attempt still
in `not_sent` stays exactly as admitted, keeps its reservation and cannot be
dispatched by this toolkit; resolve or retire it explicitly. Already submitted
0.1.0 attempts reconcile as `normal` without requeue.

## Reconciliation

Any dispatched nonzero exit, timeout, disconnect or ambiguous response remains
unknown and retains reservation. Never retry an unknown attempt. An abandoned
`submitting` claim (dispatcher died after its durable claim) is treated exactly
like `unknown`: never re-dispatched, reservation kept, resolved only by
identity-bound scheduler evidence via `shk reconcile`; there is no separate
conversion step. Strict scheduler token/user/cluster/time and job identity resolve
attempts; absence or retention is inconclusive. A row whose submit time precedes
admission by more than 300 seconds (`SUBMIT_TIME_TOLERANCE_SECONDS`, also the
`sacct --starttime` margin) is stale. Equivalent queries share a 60-second cadence
even after failure; the cache key of a single-attempt query is the attempt, so
identifying a lost acknowledgement does not trigger a second query within the
cadence. Arrays still require an explicit accounting adapter and remain
inconclusive in this CLI.

Terminal states are `COMPLETED`, `FAILED`, `CANCELLED`, `TIMEOUT`, `NODE_FAIL`,
`OUT_OF_MEMORY`, `BOOT_FAIL` and `DEADLINE`; `PREEMPTED` is terminal only on a
preemptible profile whose attempt was admitted with `requeue: false`. Accounting
rows are grouped by Slurm restart number and the highest restart is authoritative.
An attempt is terminal only when that restart is in a terminal state and the
response shows every restart from 0 to the highest; cost is summed over restarts
and stored as a never-decreasing lower bound, `cost_known` is set only when every
restart has complete accounting, and budget admission charges reserved or
uncertified attempts at the larger of their frozen estimate and that lower bound.
Terminal allocation state releases concurrency but does not establish scientific
success. Conflicting successful job identities quarantine the scope, persist all
receipts, and block admission/dispatch until explicit operator recovery. There
is no automatic quarantine reset or controller restart.

On a profile that is not preemptible, any `PREEMPTED` or `REQUEUED` row or a
restart above zero is an anomaly, never expected: the evidence is recorded, the
job identity adopted, the reservation kept, and the command exits 2 with a
`shk: unexpected preemption on a non-preemptible partition` message that names
the release command. After investigation,
`shk reconcile --attempt ID --acknowledge-preemption` writes an `operator_ack`
evidence row and releases the reservation without a remote query; it requires
recorded preemption evidence, applies only to non-preemptible profiles and is the
only release path.

`status --all` and `reconcile --all` cover every reserved attempt still
`submitting`, `submitted` or `unknown` through one bounded `sacct` query whose selector is a
single `--name=shk-A,shk-B,...` list (sacct ANDs its filters, so job ids and names
are never mixed). The response is cached under one `sacct-all` key with the same
cadence, parsed once and reconciled per attempt; one attempt's contract violation
is reported as an entry with resolution `error` and the command exits 2 after
printing the whole list. Nothing is queried when no attempt is unresolved, and a
transport failure yields `inconclusive` for each attempt.

Known residual limits: a requeue attempt observed terminal before Slurm records
its next restart row is released on that observation (later rows are stale
observations; the restart-history check is the only lever); an `owners` attempt
admitted with `requeue: false` but observed `REQUEUED` (an operator requeued it
despite `--no-requeue`) stays identified and reserved and cannot be acknowledged
because its profile is preemptible; on an already terminal attempt the
terminal-conflict check inspects only the highest restart.

## Occupancy

`shk occupancy --partition NAME` is a read-only courtesy aid for one packaged
partition. It runs `squeue -h -p NAME -O UserName:64,State:24,tres-alloc:128,TimeUsed:24,TimeLimit:24`
with `LC_ALL=C`, parses the fixed-width columns, counts `RUNNING`/`COMPLETING`
jobs and their GPUs (the larger of the generic `gres/gpu=N` and typed
`gres/gpu:TYPE=N` counts, never their sum) and `PENDING` jobs per user, and prints
`partition`, `profile`, `users`, `totals` and `transport`. The query shares the
60-second cache under a per-partition key. A transport failure exits 1 with empty
users and the transport status. Occupancy gates nothing; the operator applies the
profile's courtesy text. The fixed-width column layout was confirmed once against
live `squeue` output in the 2026-10-09 pilot.

## Fetch

Fetch pins the full producer-bound manifest in durable state, limits compact bundles
to 10,000 items/64 MiB and manifest metadata to 4 MiB, and checks exact file and
directory inventory, regular-file types, size, SHA256, source stability and the
scientific validator. Private sibling staging, destination locks, recursive fsync,
atomic promotion and durable receipt support crash recovery on both sides of
promotion. Existing unrelated destinations are refused. `--local` can finish a
receipt for an already-promoted matching bundle without remote access. Recovery and
fetch remain available for recorded old attempts when a new advertised pin differs;
retain the old installation/runtime while its attempts need recovery.

Bytes move through `sherlock_kit.data_transfer`: `--source-root` is the host-less
remote directory and the configured `data_host` is prepended by the toolkit. An
rsync failure or deadline exits 2 with the `shk:` prefix and a bounded stderr tail,
leaving the destination absent and the transaction record retained for recovery.
A DTN authentication failure arms the shared control cooldown, and a later fetch is
refused before rsync is spawned while it holds; see
[installation](installation.md) for the transport contract.

This protocol provides durable at-most-one local dispatch claim, not exactly-once
scheduler execution. It cannot enforce arbitrary raw shell calls, prove scientific
validity without a workload validator, or survive unsupported state edits/downgrades.
