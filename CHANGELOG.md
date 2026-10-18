# Changelog

## 0.5.0

- Operational JSON adds `schema_version: 1`; existing result keys and exits,
  help, rules and version text remain compatible.

- `status --details` retains queue, accounting and queued `scontrol` identities,
  including physical IDs, DBIndex, clusters, times, comments, UID and raw lines.
  Repeatable `--job-id` queries only the current UID, supports legacy names and
  lists one entry per selected base job in job id order; detailed ID queries read
  the queue without expired-ID filters while accounting keeps exact ID filters.
  Distinct accounting identities no longer collapse at `(JobID, Restarts)`.

  - Control records come from one `scontrol show job` per queued base job (array
    elements appear once); records outside the selection (siblings, other own
    jobs, other projects under `--all`) are ignored instead of marking the query
    incomplete, and `scontrol` times are UTC like `squeue` and `sacct`.
  - Raw queue and accounting fields are separated by the ASCII unit separator
    (0x1F), so a `|` in a comment or job name no longer poisons every name.
  - Accounting rows of jobs that never had an allocation (held or pending jobs
    cancelled before start, pending array remainders) are complete evidence, and
    a terminal one has a final zero cost; queue rows without nodes report null
    CPUs and start.
  - A failed `squeue` or `sacct` read is exit 1 `malformed_read` like plain
    status, keeping the evidence and `details.returncodes` but no name summaries;
    a failed control read alone exits 0 with `complete: false`. Each entry's
    `current.evidence_complete` covers its own job.
  - `duplicate_live` and `unknown_partition` apply to `shk.` names only, and
    `unexpected_preemption` is judged on the partition of each interrupted
    attempt, so legacy jobs sharing a script name or running on another group's
    partition no longer exit 6.

- Without a spec file, an array's task count is inferred only when its tasks run
  0 to max: 1-based, stepped or partly aged-out arrays have null count and missing
  tasks, a state that follows the observed tasks (no longer `failed`) and a
  nonfinal cost. Explicit task rows outrank pending accounting aggregates.
  `status --since` accepts 1 to 31 days, the widest window Sherlock's slurmdbd
  answers.

- `resources` reports Slurm configured minus allocated node capacity; OS FreeMem
  is never used. Nodes are filtered on Sherlock by their `Partitions=` list, each
  once, and keep their raw line; `complete`, `malformed`, the cadence and one
  top-level `provenance` precede `nodes`. Each node adds `schedulable` from its
  State flags (false for DOWN, DRAIN, FAIL, MAINTENANCE, NOT_RESPONDING, RESERVED
  and similar; PLANNED and COMPLETING stay true): count free capacity only there.
  A failed `scontrol` read is exit 1 `malformed_read`.

- `submit --preflight` runs `sbatch --test-only` without submitting or writing
  provenance; a Slurm refusal is exit 3 and an unreachable or timed-out
  controller is exit 1 `malformed_read` with a retry hint. `--hold` and the public
  `--submission-token` are CLI-owned options frozen with the spec; `--hold` needs
  a token, which the job sees as `SHK_SUBMISSION_TOKEN` (an inherited one is
  unset otherwise). A leading `#SBATCH --comment` is refused only when a token
  owns Comment. Submit, `--preflight` and `--dry-run` refuse `dev` offline (exit 3
  `batch_not_allowed`): Sherlock keeps it for interactive sessions; partition
  profiles gain a `batch` flag.

- Exact `release` checks UID, name, token and user hold and keeps Slurm's error
  text. `requeue --self` requires a local current allocation, Slurm PID membership
  in the physical `SLURM_JOB_ID`, the job's own `scontrol` record (the array meta
  element also returns its siblings), the current UID and name, RUNNING, a
  partition whose profile allows requeue and Requeue=1; `--expect-token` is
  optional and checked when given. An array element is requeued as
  `ArrayJobId_ArrayTaskId`, never as the bare array id; identity and policy fields
  come from the record's leading fields, never from free text such as Comment.
  Lost mutation replies
  remain unknown (exit 5) with Slurm's message; checkpoint/retry decisions stay
  outside the kit.

- `shk --executor local allocation --pid PID --job-id N_OR_N_TASK [--step-id S]`
  verifies a local worker's UID, PID membership (`pidinfo`, `listpids`), host and
  allocation and reports actual resources, topology and hashed raw records. The
  array meta element selects its own record among siblings, an array step is
  queried as `ArrayJobId_ArrayTaskId.STEP` as Slurm prints it, and timeouts or
  controller errors are retryable reads distinct from non-membership. No
  admission policy (hostlist caps, mandatory topology, affinity equality) is
  applied.

- Pull keeps incoming directories owner-writable across large file-list batches
  with the receiver-local `--chmod=Du+rwx`; the restricted DTN rsync server
  command, file bytes and file permissions are unchanged. Before resuming, a stage
  directory lacking owner access gets it back (an ordinary pull issues no chmod;
  a refused repair is exit 1 `stage_unwritable`), a stage directory another user
  owns or a symlinked stage root is refused, and child symlink targets are never
  followed or chmodded.

- Rules distinguish bounded deterministic compute controllers running in a
  Slurm allocation with `--executor local` from prohibited future agent
  sessions. Releasing a job held on a borrowed partition counts as submitting it;
  the reason for a cancellation goes to the user. Array grammar is `N[%M]`
  throughout. The skill documents allocation, release, requeue and resources.

- The offline suite stays hermetic inside Slurm jobs: importing `tests/fakes`
  removes inherited `SLURM_*`, `SBATCH_*`, `SQUEUE_*`, `SACCT_*`, `SCANCEL_*`,
  `SHK_*` and `SHERLOCK_KIT_*` variables and exported shell functions, puts
  blocking stubs for every Slurm client on `PATH` behind each test's fakes, and
  refuses a `TMPDIR` that cannot execute files; `tests/run_suite.py` fails a run
  that reached a stub. A run inside a Sherlock job had reached the real `squeue`
  and `scontrol`.

- `docs/validation/observed-slurm.md` records the 2026-10-18 live verification on
  Slurm 25.11.8 and corrects the old DTN note.

## 0.4.1

- Recovery commands after an unknown submit or cancel outcome preserve the
  original project directory and executor, so the status read still selects
  the right task after the caller changes directories.
- SSH master checks distinguish local permission failures, timeouts and other
  check failures from a missing master. Codex permission failures now point to
  per-command approval instead of asking the user to reopen the master.
- The skill and workstation rules explain sandbox failures, the parent
  directory needed for a first push, checking the UTC clock before status,
  malformed-read recovery and the text output of help and usage errors.
- Submission guidance requires status reconciliation after an unknown outcome
  before considering another submit.

## 0.4.0

**Breaking.** A lean rewrite: one small standard-library package, `shk`, with
Slurm as the only source of truth and no local or remote state of its own. Job
names derive from the logical task (`shk.<project>.<task slug>.<hash>`), so a
retry reuses the name and deduplication reads `squeue` and `sacct`. Remote work
runs as fixed POSIX `sh` programs over the existing OpenSSH control masters, and
`shk` never logs in.

- Removed: the attempt registry on Sherlock and its `registry_root`, attempt ids,
  `reconcile` with `--acknowledge-preemption` and `--abandon`, the private
  configuration file, borrowed-partition grants, the guard hooks and their
  adapters, the pin, projection and identity machinery (`shk policy`,
  `build_identity.json`, `SHERLOCK.md`), the frozen launcher and its state root
  (`SHERLOCK_KIT_STATE_ROOT`, `~/.local/share/sherlock-kit`), the authentication
  cooldown, the query cache and the JSON fetch manifest with producer digests.
  0.3.0 names (`shk-<hex>`) cannot collide with 0.4.0 names; 0.4.0 never reads
  the old registry.
- New: `[tool.sherlock-kit]` project configuration in `pyproject.toml` (or
  `sherlock-kit.toml`), found in the nearest directory at or above the working
  directory; `shk submit` with `--task`, `--resubmit REASON` and `--dry-run`;
  `shk status` by task, name or `--all` with `next_check_after`; `shk cancel`
  for one name; `shk push` and `shk pull` with a `SHA256SUMS` manifest
  (`shk manifest DIR`), an optional project validator and a receipt beside the
  destination; `shk rules {workstation,cluster}`; `shk install-skill`; `bin/shk`
  (mode 100755), a stdlib-only entry point that dotfiles links from its
  submodule.
- Transport: `ssh -O check` before every dial; without a usable master, exit 4
  `auth_required` (open the master) or `unavailable` (fix `~/.ssh/config`, or
  install OpenSSH). Dials are mux-only with `-oProxyCommand=false`; a transport
  failure after the check is exit 4 with the hint `run shk doctor`. On Sherlock
  the local executor runs the same programs and `push`/`pull` are refused.
- Partitions: the ten profiles carry over; listing is the authorization fact,
  borrowed partitions need `--courtesy-ack`, and the possu window and btrippe
  budget are courtesy text. `#SBATCH` lines may set anything except the
  directives `shk` owns (account, exclude, GPUs, partition, array, dependency,
  begin, job name) or start a heterogeneous job.
- Rules and skill: `src/sherlock_kit/data/rules/workstation.md` and `cluster.md`,
  rewritten from the Sherlock site files read on 2026-10-10 (purge evasion is
  account termination on the first occurrence; no agent module is named), and
  one `sherlock-kit` skill for Claude Code and Codex.
- Documentation: `docs/design.md`, `docs/sherlock-policy.md` and
  `docs/validation/observed-slurm.md` replace the 0.3.0 guides.
- Development: ruff (lint and format), mypy and pre-commit, run in CI; the
  version is the literal `sherlock_kit.__version__`, read by `pyproject.toml` as
  its dynamic version (build with `setuptools>=80`); `pip install -e '.[dev]'`
  installs the tools.

| 0.3.0 | 0.4.0 |
|---|---|
| `shk policy`, `shk policy --projection` | `shk rules workstation`, `shk rules cluster` |
| `shk policy --identity` | `shk --version` |
| `shk submit --config private.json --spec attempt.json [--apply]` | `shk submit --task T --script FILE --partition P --cpus N --mem SIZE --time DUR [--dry-run]` |
| retry naming `parent_attempt` | `shk submit ... --resubmit "<reason>"` |
| `shk status --attempt ID`, `shk reconcile --attempt ID` | `shk status --task T` or `--name NAME` |
| `shk status --all`, `shk reconcile --all` | `shk status --all` |
| `reconcile --acknowledge-preemption`, `reconcile --abandon` | none: nothing is recorded; report the anomaly, then `--resubmit` |
| `shk occupancy --config private.json --partition P` | `shk occupancy --partition P` |
| `shk fetch ... --manifest M --destination D`, `fetch --local` | `shk pull REMOTE LOCAL [--validate NAME]` |
| JSON manifest with producer digests | `SHA256SUMS` (`shk manifest DIR` or `sha256sum`) |
| private configuration, grants | `[tool.sherlock-kit]`, `--courtesy-ack` |
| `shk guard`, plugin and `sherlock-kit-operate` skills | the `sherlock-kit` skill, `shk install-skill` |
| pinned frozen install and launcher | a pip install in the project environment, or the dotfiles submodule `bin/shk` |

## 0.3.0

Slurm as the source of truth, job arrays and the full partition table. The local
SQLite ledger with its reservations, budgets and cost caps is gone: the only
durable attempt state is an attempt registry on Sherlock (`registry_root`) plus
Slurm accounting, so any POSIX workstation with the frozen toolkit and the
private configuration can operate. Every new path is tested offline by executing
the real registry program against fake `sbatch`/`sacct`/`squeue` binaries. A
live CPU pilot on `normal` on 2026-10-10 (one 4-task array, throttle 2) exercised
the registry runner, `status` through the throttled, partial and terminal phases,
`reconcile --all`, DTN fetch with the attempt sidecar and `fetch --local`; see
the acceptance document. Lost replies, `--abandon` and preemption paths remain
offline evidence. The partition-table change has no live pilot of its own.

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
  task and restart, reported as `tasks {count, terminal, running, pending, missing, by_state, incomplete_history}`, with cost summed over complete task
  and restart groups and `known` only when every task is terminal with complete
  accounting.
- Reconciliation: `status`/`reconcile --attempt ID` and `--all` make one cached
  reader call (one `sacct --name=shk-A,shk-B,...` on the login node; 500 open
  attempts, 50,000 rows); resolutions are `not_sent`, `abandoned`,
  `inconclusive`, `abandonable`, `identified`, `terminal`,
  `unexpected_preemption` and `error`. New `reconcile --attempt ID --abandon [--note T]` closes an attempt that stayed absent from accounting for 900 s
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
  the projection, the documented config keys and the skill adapters, and
  `tests/test_orchestration.py` ties the constants duplicated from the registry.

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

Supported limits and real acceptance were in the acceptance document of the
0.1.0 to 0.3.0 trees; its live Slurm observations are now in
[observed Slurm behavior](docs/validation/observed-slurm.md). No research
code/data, borrowed-partition authorization or generic GPU recovery is
distributed.
