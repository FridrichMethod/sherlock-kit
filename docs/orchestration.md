# Typed consumer operations

This implementation supports one Slurm allocation per attempt, either a single
job or a job array of 2 to 1000 tasks, on a packaged partition profile (ten
partitions; `shk policy` lists them). Slurm requeue is emitted only where the
profile allows it (`owners`), and a requeued script must itself checkpoint and
resume; the toolkit does not certify general DDP or checkpoint recovery. A
workload supplies its source/runtime/input identities, immutable script,
resources, output manifest and scientific validator. The rasraser adapter is a
separate local research commit; no research source or data is distributed here.

Durable attempt state lives in an attempt registry on Sherlock (`registry_root`)
and in Slurm accounting; the workstation keeps only the shared authentication
cooldown and a 60-second query cache, so any POSIX workstation with the frozen
toolkit and the same private configuration can operate. The 0.1.0 CPU
provenance pilot on `normal` and the 0.2.0 owners/btrippe GPU pilot (one
operator-issued requeue, `--all`, occupancy, two DTN fetches) are recorded in
[acceptance evidence](validation/acceptance.md). The live pilot of the registry
and array paths introduced in 0.3.0 is pending; until it has run, those paths
rest on offline tests that execute the real registry program against fake Slurm
binaries. Natural preemption and the `unexpected_preemption` path remain
offline-tested only.

The CLI and artifact promotion require POSIX (`fcntl`, Unix ownership and
no-follow filesystem checks); the registry program needs `python3` 3.11+ and
the same mechanisms on the login node. Windows instruction, guard and installer
adapters do not imply Windows CLI support. Native PowerShell execution is
covered by dotfiles' native Windows CI, while it remains unavailable on the
implementation workstation. That does not validate a Windows controller.

## Partition profiles

`shk policy` lists the packaged profiles; `shk policy --identity` reports their
hash as `partitions_sha256`. A profile carries only the flags `preemptible`,
`requeue`, `borrowed`, `gpus_allowed` and a courtesy sentence; it never carries
caps. A spec naming a partition outside the table is refused at spec check,
offline, before any remote call. GPUs are accepted only where `gpus_allowed`.
`requeue` defaults to the profile value and `true` is refused where the profile
forbids it. Admission freezes the profile and `partitions_sha256` into the
attempt record, and the runner reads only that frozen copy, so a later table
change never alters a recorded attempt.

Borrowed profiles (`btrippe`, `possu`) additionally need an identity-bound
`grant` in the private configuration: grantee equal to the principal, an
evidence reference, a validity interval, the partition and the resource scope;
any other member, including a 0.2.0 grant's limits, is ignored. The grant is
consulted only for borrowed profiles; a configured borrowed grant does not
affect submissions to any other profile, whose frozen attempt records
`grant: null`. Before a borrowed submission run `shk occupancy` and apply the
profile's courtesy text; the toolkit reports occupancy but never gates on it. A
courtesy sentence may confine submission to a time window (`possu`: 00:00-07:00
Pacific); the toolkit does not consult the clock, so the operator or agent must
honour the window before `--apply`. Historical visibility or a configurable
grant field is not authorization.

## Private configuration

Configuration is a private owned JSON file (0600) naming the controller
identity, the registry on Sherlock and the transport. `registry_root` is an
absolute canonical POSIX path on the control host: no `.` or `..` parts, no
repeated or trailing slash, no control characters. The registry runner creates
it 0700 with `attempts/` and `tasks/` on the first submission and refuses a root
that is not an owned private directory free of symlinks. One registry is shared
by every campaign of the principal; logical-task deduplication and `--all` are
global to it. Nothing about the workstation is recorded in it.

Typed commands keep two local files, the shared authentication cooldown
`auth-backoff.json` and the query cache `query-cache.json`, side by side under
one explicit private state location: `transport.backoff_file` or the
`SHERLOCK_KIT_STATE_ROOT` environment root. With neither set, every typed
command refuses to run (`typed commands need an explicit local state location:
set transport.backoff_file or SHERLOCK_KIT_STATE_ROOT`) rather than fall back
to `$HOME`. Keep private configuration, local state and research results outside
this checkout. See [maintenance](maintenance.md) for the runtime layout.

```json
{
  "schema_version": 1,
  "registry_root": "/synthetic/group-home/sherlock-kit/registry",
  "principal": "fixture",
  "cluster": "sherlock",
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

The key set is exhaustive. Required: `schema_version` (1), `registry_root`,
`principal`, `cluster` and `transport`. Optional: `fetch_root`,
`validator_roots`, `remote_roots` and `grant`. Any other key is refused before
anything is read or written (`unknown private config field(s): ...; filesystem
and partition access are not configurable`), so a misspelled key cannot silently
become inert; a 0.2.0 configuration still carrying state_root or limits is
refused by that same message (see the migration note under Submission). These
paths and permissions are illustrative. Resolve and verify real authorized roots
on the intended host first. `namespace_verified` records the consumer's explicit
control/data namespace check; the toolkit also verifies canonical remote paths
and refuses symlinks. Source roots must remain immutable during fetch. No
configuration field grants filesystem or partition access.

## Attempt specification

An attempt JSON contains `project`, `campaign`, `task`, `cluster`, `principal`,
`resource_scope`, SHA256 `code_digest`, `input_digest`, `runtime_digest`,
`policy_digest`, `script_digest`, an absolute `remote_script`, and `resources`.
Resources include explicit `partition`, `cpus`, `tasks`, `gpus`, `memory_mb` and
`walltime_seconds`. Walltime is rounded up to Slurm's minute resolution. Optional
`constraint` and `signal` requests have a strict grammar; optional boolean
`requeue` follows the profile rule above and the checked resources always carry
it. `account` and node exclusion are unsupported.

A job array is requested with `resources.array = {"count": N, "throttle": M}`:
`count` is an integer from 2 to 1000 (Sherlock's `max_array_tasks`), the
optional `throttle` an integer from 1 to `count`, and no other member is
accepted. Task indices run from 0 to `count - 1`. The script lays its tasks out
under `remote_run_directory` from `SLURM_ARRAY_TASK_ID`, and the Slurm logs are
named `slurm-%A_%a.out`/`.err` instead of `slurm-%j`. `cpus`, `tasks`, `gpus`
and `memory_mb` describe one array task; accounting is reconciled per task and
summed. A spec without `array` is a single job and never emits an array option.
`parent_attempt` (32 hex characters) names the attempt a retry replaces; the
registry rule is under Submission.

For fetch, freeze `validator_path`, `validator_digest` and `validator_function`
at admission. The standalone Python validator is trusted workload code, not a
sandbox. It receives a bundle directory and must return exactly `True`; its
verified source snapshot executes without loading or writing a `.pyc`. Imported
dependencies belong to the consumer's frozen runtime contract. Production
submission also requires `remote_run_directory`: an existing canonical directory
in the consumer's authorized new namespace. The Slurm working directory and the
job-id-named stdout/stderr are explicitly placed there, separately from
immutable source. The runner validates it on the login node before `sbatch`.

```console
shk submit --config private.json --spec attempt.json
shk submit --config private.json --spec attempt.json --apply
shk status --config private.json --attempt ATTEMPT_ID
shk status --config private.json --all
shk reconcile --config private.json --attempt ATTEMPT_ID
shk reconcile --config private.json --all
shk reconcile --config private.json --attempt ATTEMPT_ID --acknowledge-preemption --note "why"
shk reconcile --config private.json --attempt ATTEMPT_ID --abandon --note "why"
shk occupancy --config private.json --partition btrippe
shk fetch --config private.json --attempt ATTEMPT_ID --manifest /authorized/manifest.json --source-root /authorized/bundle --destination /local/results/new-bundle
shk fetch --local --config private.json --attempt ATTEMPT_ID --manifest /authorized/manifest.json --source-root /authorized/bundle --destination /local/results/new-bundle
```

`--acknowledge-preemption` and `--abandon` exist on `reconcile` only, exclude
each other and require `--attempt`; `--note TEXT` is stored with either and is
refused without one of them. `status --local` and `reconcile --local` no longer
exist: every resolution reads the registry. Every command prints one JSON
document on stdout; operator notes go to stderr with the `shk:` prefix.

## Submission

Preview (`submit` without `--apply`) checks the spec offline and prints
`spec_digest` with the checked `resources` (walltime rounded, `requeue`
resolved, `array` normalised); it needs no remote access. Apply additionally
requires `remote_run_directory`, a frozen installed identity whose policy equals
the spec's `policy_digest`, a matching advertised pin when `SHERLOCK_KIT_PIN` is
set (`partitions_sha256` is compared only when the pin advertises it), and the
authenticated principal: `id -un` over the control connection must print the
spec principal. The CLI then generates the attempt id (32 lowercase hex
characters), freezes the record (the spec with checked resources,
`toolkit_revision`, `partition_profile`, `partitions_sha256` and `grant` only for
a borrowed profile) together with the exact option list, `sbatch --parsable
--job-name=shk-ID --comment=shk:ID --partition=NAME [--array=0-N-1[%M]]
--cpus-per-task=C --requeue --open-mode=append | --no-requeue --ntasks=T
--mem=MM --time=MIN --chdir=RUN --output=RUN/slurm-%j.out --error=... [-G G]
[--constraint=...] [--signal=...]`, and makes exactly one remote call: the
registry program's `submit`. The program is the `sherlock_registry` module's own
source shipped as a self-decoding `python3 -c` argument, so runner, reader and
event writer always run the toolkit's exact code; the CLI refuses to send when
the quoted argv exceeds 100,000 characters.

On the login node the runner validates, without writing, the record (shape, id,
key recomputed from `project`/`campaign`/`task`, job name `shk-ID`, an array
option exactly when `array` is frozen), the principal (`pwd` name of the uid
equals the spec principal), the registry root and the workload: the run
directory must be an existing canonical directory and the script bytes must hash
to `script_digest` and contain no `#SBATCH` line; those bytes are what sbatch
reads on stdin. It then holds the registry lock (`.lock`, `flock`, polled up to
20 s) through the end: applies the deduplication rule, creates `attempts/ID/`
and `record.json` (stamped `created`, `created_on`, `principal_uid`,
`program_sha256`) before sbatch, claims `tasks/<key>`, runs sbatch with
`SBATCH_*` removed from the environment, writes `submitted.json` (`job_id`,
`cluster`, `submitted_at`, sbatch return code and bounded stdout/stderr) and
prints exactly one line, last. Submission outcomes:

| Runner line | `resolution` | `recorded` | exit | meaning |
|---|---|---|---|---|
| `SHK_SUBMITTED:ID:JOB` | `submitted` | `true` | 0 | `job_id` is JOB; `record.json`, the task marker and `submitted.json` exist. |
| `SHK_NOT_SENT:ID:REASON` | `not_sent` | `true` | 1 | sbatch never ran (`sbatch_unavailable`, `claim_failed`); `not_sent.json` closes the attempt. Resubmit naming ID as `parent_attempt`. |
| `SHK_REFUSED:ID:TOKEN` | error `shk: ...` | - | 2 | Nothing a reader can see was written; the id is discarded. The token is translated (table below). |
| `SHK_UNKNOWN:ID:RC` | `unknown` | `true` | 2 | sbatch ran but returned no usable job id (non-zero exit, unparsable output or a foreign cluster suffix); `submitted.json` has `job_id: null`. |
| malformed or lost reply | `unknown` | `null` | 2 | Timeout, disconnect or an unparsable reply after dispatch; the record may exist. |
| transport not started | `not_sent` | `false` | 1 | Cooldown active or ssh never started; no record exists, a new submit gets a fresh id. |

Every `unknown` prints the hint `shk status --attempt ID`: the attempt is
identified by its job name, so a lost reply is resolved by the next read, never
by a retry. The output is `{"operation": "submit", "attempt": {"id", "spec",
"registry_root"}, "resolution", "job_id", "reason", "recorded", "transport"}`
plus `hint` when unknown.

Refusal tokens and their operator text: `record_invalid`, `principal_mismatch`,
`registry_unsafe`, `run_directory_invalid`, `digest_mismatch`,
`script_unreadable`, `registry_busy` (lock held for 20 s), `attempt_exists`,
`record_write_failed`, and the deduplication family `duplicate_logical_task:ID`,
`parent_unknown`, `parent_mismatch:ID`, `parent_not_released:RESOLUTION`,
`parent_conflict:REASON` and `parent_unverifiable`. An unknown token is printed
verbatim.

Deduplication is a registry rule, evaluated under the lock with a fresh `sacct`
query on the login node. The logical task key is the digest of
`[project, campaign, task]`, and `tasks/<key>` holds the id of its latest
attempt. A first attempt needs no marker and no `parent_attempt`; a retry must
name the current marker as `parent_attempt`, and the parent is released only
when it is `not_sent`, `abandoned`, or `terminal` with no task in `COMPLETED`.
Absence from accounting is never release, an acknowledgement alone is not
release, and any `COMPLETED` top restart blocks a retry under the same key: a
partial redo is a new logical task. A released retry replaces the marker
atomically, so the newest attempt is always the one a later retry must name.

Migration from 0.2.0: a configuration with state_root or limits is refused by
the unknown-field message; drop both, add `registry_root`, and keep the
launcher's `SHERLOCK_KIT_STATE_ROOT` (or `transport.backoff_file`), which now
names only the local cache root. The 0.2.0 SQLite ledger is not migrated and
its attempts are invisible to 0.3.0 (they have no registry record): resolve them
with the old installation first, then archive `coordinator.sqlite3`. A grant may
keep its limits member; it is ignored. Attempts frozen before partition profiles
existed (0.1.0) cannot be submitted by this toolkit.

## Reconciliation

`status` and `reconcile` resolve attempts from three inputs, all taken on the
login node: the attempt's `record.json`, its event files and Slurm accounting.
`--attempt ID` makes one reader call (`read REGISTRY --attempt ID`) cached for
60 seconds under the key `[cluster, principal, control_host, "read", ID]`;
`--all` makes one reader call (`read REGISTRY --open`) under `"read-all"`. The
reader is read-only and takes no lock. `--open` selects every `attempts/ID/`
holding `record.json` and none of `not_sent.json`, `abandoned.json` or
`resolved.json` (acknowledgements do not close an attempt), sorted by `created`
and capped at 500 (`open attempts truncated at 500; resolve some and run again`
on stderr). For all selected records it runs one bounded accounting query,
`env TZ=UTC LC_ALL=C SLURM_TIME_FORMAT=standard sacct -n -P --local
--allocations --user=PRINCIPAL --duplicates --starttime=<min created - 300 s>
--endtime=now --format=JobID,JobIDRaw,User%128,JobName%128,State,ElapsedRaw,
AllocCPUS,AllocTRES,Submit,Start,End,Restarts,ExitCode,Cluster%128,DBIndex
--name=shk-A,shk-B,...` (15 s timeout, 50,000 rows). The selector is always
`--name`; `--jobs` is never used, and nothing is queried when no attempt is
open. The same `resolve()` that the login node runs for admission and events
then produces, per attempt, `attempt` (the record), `resolution`, `job_id`,
`tasks {count, terminal, running, pending, missing, by_state,
incomplete_history}`, `cost {cpu_seconds, gpu_seconds, known}`, `anomalies`,
`events` (the kinds present) and `scientific_validation: "unverified"`, plus
`reason` where one applies.

Resolutions: `not_sent` and `abandoned` are decided by their event file (rows
for such an attempt are an `error`); `inconclusive` means no accounting rows yet
while the job id is known or the attempt is younger than 900 s, or that the
reader or `sacct` failed (the result then carries `transport`, exit 0);
`abandonable` means no rows after 900 s and no known job id (exit 0, with a
stderr hint naming `--abandon`); `identified` means rows exist but not every
task is terminal; `terminal` means every task is terminal with a complete
restart history; `unexpected_preemption` exits 2; `error` is an isolated
contract violation carrying a reason token (exit 2 after the whole list is
printed). Absence from accounting is never proof of non-submission.

Rows are checked before they count: steps and other job names are skipped;
`JobID` is a plain id (task 0 of a single job), `N_k` (array task k) or an
aggregate `N_[a-b,c%t]` whose index set is expanded (a pending aggregate is
pending tasks, a `CANCELLED` aggregate is terminal tasks that cost nothing);
`User` and `Cluster` must equal the frozen spec; a `Submit` more than 300 s
before `created` is `stale_submit`; `AllocCPUS` must equal `cpus x tasks` once
started; GPUs come from `AllocTRES` (generic `gres/gpu=N` or the sum of typed
counts, a disagreement is an error); duplicates collapse by `DBIndex`. All rows
must name one job (`multiple_scheduler_identities`), and a `submitted.json` job
id that differs from the rows is `job_id_conflict`, which never releases.

Per task, rows are grouped by Slurm restart and the highest restart is
authoritative: the task is terminal when that restart's state is terminal
(`COMPLETED`, `FAILED`, `CANCELLED`, `TIMEOUT`, `NODE_FAIL`, `OUT_OF_MEMORY`,
`BOOT_FAIL`, `DEADLINE`; `PREEMPTED` only on a preemptible profile admitted with
`requeue: false`, or when waived) and every restart from 0 to the highest is
present; pending when the state is `PENDING`, `REQUEUED`, `REQUEUE_HOLD`,
`REQUEUE_FED`, `RESV_DEL_HOLD` or an unwaived `PREEMPTED`; running otherwise
(gaps are listed in `incomplete_history` with the `restart_gap` flag); missing
when it has no rows. Cost sums `ElapsedRaw x AllocCPUS` and `ElapsedRaw x GPUs`
over the complete task-and-restart groups; `known` is true only when the attempt
is terminal, every observed group is complete and no task is missing. Anomaly
flags are `aggregate_overlap`, `restart_gap`, `acknowledged_preemption`,
`submitted_unrecorded` (rows but no `submitted.json`), `cluster_suffix_mismatch`,
`reopened_after_resolved`, `requeued_without_requeue` (an `owners` attempt with
`requeue: false` that Slurm requeued anyway; informational) and the two below.

On a profile that is not preemptible, a task whose highest restart is above zero
or that shows `PREEMPTED`/`REQUEUED` is an anomaly, never expected
(`restart_on_non_preemptible`, `preempted_on_non_preemptible`): the attempt is
`unexpected_preemption` and the command exits 2 with `unexpected preemption on
a non-preemptible partition; investigate, then reconcile
--acknowledge-preemption`. After investigation, `shk reconcile --attempt ID
--acknowledge-preemption [--note TEXT]` checks the last read (non-preemptible
profile, resolution `unexpected_preemption`) and sends one `event ack` call: the
writer takes the registry lock, resolves the attempt again from a fresh `sacct`,
and writes `ack-<epoch>.json` waiving every currently anomalous task at its
current restart (`waived {"<task>": {restart, state}}`, the note and the
observation's row digest, task counts and cost). A waiver covers restarts up to
the recorded one, so a later restart is a new anomaly, and the acknowledgement
now needs the network. The event writer refuses with `preemptible_profile`,
`not_applicable:RESOLUTION`, `already_present`, `unverifiable` (sacct or squeue
failed), `registry_busy` or `unknown_attempt`.

`shk reconcile --attempt ID --abandon [--note TEXT]` closes an `abandonable`
attempt. The CLI first requires a complete read of the attempt (the cached one
within the cadence counts; a failed read refuses with `--abandon needs a fresh
registry read of the attempt before it writes`) and the resolution
`abandonable`; the writer then re-resolves under the lock with a fresh `sacct`,
requires `squeue -h --name=shk-ID -o %i` to print nothing (`squeue_shows_job`
otherwise) and writes `abandoned.json` (`at`, `age_seconds`, `note`, the exact
`sacct`/`squeue` argv and their zero row counts). Both event commands print
`{"operation": "ack"|"abandon", "attempt", "note", "event", "outcome",
"transport", "scientific_validation"}`: `written` exits 0 with the file name in
`event`, `not_sent` (cooldown or ssh never started) exits 1, a lost or unparsable
reply exits 2 as `unknown` with the `shk status --attempt ID` hint, and a
refusal exits 2 as `shk: registry refused --abandon: REASON`.

`reconcile` (never `status`) also sends one `event resolved ID...` call for the
attempts it just found `terminal` without a `resolved.json`; the writer verifies
each one again and writes `resolved.json` (`at`, `job_id`, `tasks`, `cost`,
`anomalies`, `rows_sha256`), which removes the attempt from `--open` and so
bounds `--all`. A refusal is not fatal: the entry is annotated
`resolved_event: "refused:REASON"`, `"unknown:DETAIL"` or
`"transport:STATUS"`, while a written closure appends `resolved` to `events`.
After any written event the CLI drops its cached `read-all` and `read ID`
scopes so the next read within the cadence is fresh; if that fails, a stderr
note says the next read may be stale.

`status --all` and `reconcile --all` resolve every open attempt from one reader
document: a record of another principal/cluster is an `inconclusive` entry with
a reason, each attempt is resolved on its own so one contract violation hides
nothing, and the command exits 2 after printing the whole list when any entry is
`error` or `unexpected_preemption`. A reader transport failure yields one
`inconclusive` entry with `transport`; a `sacct` failure yields `inconclusive`
per attempt with `transport: "sacct:STATUS"`, except that an attempt closed by
`not_sent.json` or `abandoned.json` still resolves from its event.

Equivalent queries share a 60-second cadence even after failure. The cache is
the file `query-cache.json` beside `auth-backoff.json`
(`{"schema_version": 1, "entries": {KEY: {"observed", "body"}}}`, mode 0600,
its own `query-cache.json.lock`, never the backoff lock that `run_remote` holds
for the whole ssh call). A slot is reserved with the sentinel
`{"status": "query_pending_or_failed"}` under the lock, the query runs unlocked,
and the result replaces the sentinel only when the slot is still ours; a failed
query leaves the sentinel, so a retry within the cadence gets it instead of a
new call. Expired entries are pruned on every write, a body above 8 MiB or a
file above 64 MiB is refused, and a malformed, symlinked or group/world-readable
file fails closed with `query cache malformed or unsafe; remove query-cache.json
explicitly`. Occupancy shares the file under `[..., "squeue", PARTITION]`.

Known residual limits: `--all` may miss an attempt directory created seconds
earlier on another login node (NFS attribute cache), while `--attempt ID` is
exact; an array task that never appears in accounting keeps the attempt
`identified` with `missing > 0` (only an attempt with no rows at all becomes
`abandonable`); `--acknowledge-preemption` needs the network; an `owners`
attempt admitted with `requeue: false` but requeued by an operator stays
`identified` and cannot be acknowledged because its profile is preemptible; a
second acknowledgement within the same second is refused as `already_present`;
an attempt resolved `terminal` before Slurm records a further restart row is
flagged `reopened_after_resolved` on a later read but its `resolved.json` stays.

## Occupancy

`shk occupancy --partition NAME` is a read-only courtesy aid for one packaged
partition. It runs `squeue -h -p NAME -O UserName:64,State:24,tres-alloc:128,TimeUsed:24,TimeLimit:24`
with `LC_ALL=C`, parses the fixed-width columns, counts `RUNNING`/`COMPLETING`
jobs and their GPUs (the larger of the generic `gres/gpu=N` and typed
`gres/gpu:TYPE=N` counts, never their sum) and `PENDING` jobs per user, and prints
`partition`, `profile`, `users`, `totals` and `transport`. The query shares the
60-second cache file above under a per-partition key. A transport failure exits
1 with empty users and the transport status. Occupancy gates nothing; the
operator applies the profile's courtesy text. The fixed-width column layout was
confirmed once against live `squeue` output in the 2026-10-09 pilot.

## Fetch

A remote fetch starts with the registry program's `fetch-manifest` on the login
node: it loads the attempt's `record.json`, checks that the manifest is a regular
file of at most 4 MiB and that the manifest, the source and every listed file
are canonical paths (no symlinks) under the verified remote root, and returns
`{record, manifest}`. The CLI requires the record to name the attempt and this
controller, loads the validator frozen in the record (`validator_path`,
`validator_digest`, `validator_function`, under an authorized `validator_roots`
entry), checks the manifest against the frozen identity, and builds the attempt
sidecar `.NAME.shk-attempt.json` beside the destination: `schema_version`,
`attempt`, `producer`, the validator triple, `manifest` and `manifest_sha256`.
Under the destination lock the sidecar is written durably (0600) immediately
before the transaction record, or verified equal to an existing one
(`existing attempt sidecar conflicts`). Compact bundles are limited to 10,000
items/64 MiB; exact file and directory inventory, regular-file types, size,
SHA256, source stability (the manifest is re-read through the control endpoint
before and after the transfer) and the scientific validator are checked before
private sibling staging, recursive fsync, atomic promotion and a durable receipt
support crash recovery on both sides of promotion. Existing unrelated
destinations are refused. Fetch remains available for recorded old attempts when
a new advertised pin differs; retain the old installation/runtime while its
attempts need recovery.

`fetch --local` is network-free recovery from the sidecar alone: it reads
`.NAME.shk-attempt.json` (a missing one is `no attempt sidecar for local
recovery; run a remote fetch first`; a sidecar of another attempt, another
principal/cluster, a corrupted `manifest_sha256` or a validator assertion that
differs from the frozen triple is refused), requires an already promoted
destination directory, verifies inventory and bytes against the embedded
manifest, runs the frozen validator and completes the receipt, printing
`{destination, receipt, recovered: true}`. A destination promoted by 0.2.0 has
no sidecar; its first 0.3.0 remote fetch takes the recovery branch and writes
one, after which `--local` works.

Bytes move through `sherlock_kit.data_transfer`: `--source-root` is the host-less
remote directory and the configured `data_host` is prepended by the toolkit. An
rsync failure or deadline exits 2 with the `shk:` prefix and a bounded stderr tail,
leaving the destination absent and the transaction record retained for recovery.
A DTN authentication failure arms the shared control cooldown, and a later fetch is
refused before rsync is spawned while it holds; see
[installation](installation.md) for the transport contract.

This protocol provides a durable at-most-one claim per logical task, taken under
the registry lock on the login node, not exactly-once scheduler execution. It
cannot enforce arbitrary raw shell calls, prove scientific validity without a
workload validator, or survive edits to the registry or downgrades.
