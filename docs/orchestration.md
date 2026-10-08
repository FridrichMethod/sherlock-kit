# Typed consumer operations

This implementation supports a single immutable CPU allocation without arrays or
requeue. It does not certify general DDP or checkpoint recovery. A workload supplies
its source/runtime/input identities, immutable script, resources, output manifest
and scientific validator. The rasraser adapter is a separate local research commit;
no research source or data is distributed here. The live CPU provenance pilot passed
shared submission, reconciliation, DTN fetch and offline recovery; exact resources,
identities and limitations are recorded in [acceptance evidence](validation/acceptance.md).

The authoritative controller and artifact promotion currently require POSIX
(`fcntl`, Unix ownership and no-follow filesystem checks). Windows instruction,
guard and installer adapters do not imply Windows controller support. Native
PowerShell execution is covered by dotfiles' native Windows CI, while it remains
unavailable on the implementation workstation. That does not validate a Windows controller.

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

These paths and permissions are illustrative. Resolve and verify real authorized
roots on the intended host first. `namespace_verified` records the consumer's
explicit control/data namespace check; the toolkit also verifies canonical remote
paths and refuses symlinks. Source roots must remain immutable during fetch.
No configuration field grants filesystem or partition access.

An attempt JSON contains `project`, `campaign`, `task`, `cluster`, `principal`,
`resource_scope`, SHA256 `code_digest`, `input_digest`, `runtime_digest`,
`policy_digest`, `script_digest`, an absolute `remote_script`, and `resources`.
Resources include explicit `partition`, `cpus`, `tasks`, `gpus`, `memory_mb` and
`walltime_seconds`. Walltime is rounded up to Slurm's minute resolution. Optional
`constraint` and signal requests have a strict grammar; `account`, exclusion,
arrays and requeue are unsupported. For fetch, freeze `validator_path`,
`validator_digest` and `validator_function` at admission. The standalone Python
validator is trusted workload code, not a sandbox. It receives a bundle directory
and must return exactly `True`; its verified source snapshot executes without
loading or writing a `.pyc`. Imported dependencies belong to the consumer's frozen
runtime contract.
Production submission also requires `remote_run_directory`: an existing canonical
directory in the consumer's authorized new namespace. Slurm working directory and
job-ID-named stdout/stderr are explicitly placed there, separately from immutable
source. The remote runner validates it before calling sbatch.

```console
shk submit --config private.json --spec attempt.json
shk submit --config private.json --spec attempt.json --apply
shk status --config private.json --attempt ATTEMPT_ID
shk reconcile --config private.json --attempt ATTEMPT_ID --local
shk fetch --config private.json --attempt ATTEMPT_ID --manifest /authorized/manifest.json --source-root /authorized/bundle --destination /local/results/new-bundle
shk fetch --local --config private.json --attempt ATTEMPT_ID --manifest /authorized/manifest.json --source-root /authorized/bundle --destination /local/results/new-bundle
```

Submission preview never launches a job. Apply requires frozen installed identity,
matching advertised policy/revision when a pin is configured, and authenticated
principal verification. Admission durably reserves shared scope resources before
dispatch. The helper snapshots and hashes script bytes before `sbatch --parsable`,
rejects embedded `#SBATCH`, and removes `SBATCH_*` environment overrides. The
script must launch only its immutable release/runtime; a script digest alone cannot
prove the contents of referenced paths.

Any dispatched nonzero exit, timeout, disconnect or ambiguous response remains
unknown and retains reservation. Never retry an unknown attempt. Recovery converts
an abandoned submitting claim to unknown. Strict scheduler token/user/cluster/time
and job identity resolve attempts; absence or retention is inconclusive. Equivalent
queries share a 60-second cadence even after failure. Arrays and restart histories
require explicit accounting adapters and remain inconclusive in this CLI. Terminal
allocation state releases concurrency but does not establish scientific success.
Missing final accounting retains the bounded cost reservation; confirmed charges
never decrease. Conflicting successful job identities quarantine the scope, persist
all receipts, and block admission/dispatch until explicit operator recovery. There
is no automatic quarantine reset or controller restart.

Borrowed admission is disabled unless the consumer supplies a real identity-bound
grant, evidence reference, validity interval, partitions, scope and exact shared
limits. Historical visibility or a configurable grant field is not authorization.

Fetch pins the full producer-bound manifest in durable state, limits compact bundles
to 10,000 items/64 MiB and manifest metadata to 4 MiB, and checks exact file and
directory inventory, regular-file types, size, SHA256, source stability and the
scientific validator. Private sibling staging, destination locks, recursive fsync,
atomic promotion and durable receipt support crash recovery on both sides of
promotion. Existing unrelated destinations are refused. `--local` can finish a
receipt for an already-promoted matching bundle without remote access. Recovery and
fetch remain available for recorded old attempts when a new advertised pin differs;
retain the old installation/runtime while its attempts need recovery.

This protocol provides durable at-most-one local dispatch claim, not exactly-once
scheduler execution. It cannot enforce arbitrary raw shell calls, prove scientific
validity without a workload validator, or survive unsupported state edits/downgrades.
