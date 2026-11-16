# sherlock-kit

[![CI](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml)

`shk` is a small command-line interface for agents (and people) that submit and
track Slurm jobs, move data and read partition occupancy on Stanford's Sherlock
cluster, from a workstation over OpenSSH or on Sherlock itself. It is one
standard-library Python package (3.11+) plus fixed POSIX `sh` programs that run
on the login node. Slurm is the only source of truth: a job's name is derived
from its logical task, and `shk` keeps no local state, cache or ledger.

`shk` does not authorize resources, infer borrowed access, police what a job
script does or certify scientific results. The operating rules for agents ship
with the package (`shk rules workstation`, `shk rules cluster`); Sherlock's own
`/etc/agents/AGENTS.md` takes precedence over them.

Submission can opt in to explicit host/container numerical thread defaults with
`--cpu-thread-budget [N]` or project `cpu_thread_budget = 1`; the default when the
flag has no value is 1. This does not cap OS thread creation or replace Slurm CPU
isolation. See [CPU thread budgets](docs/cpu-threads.md) for provenance, limits and
controller compatibility.

For multi-model or multi-stage GPU work, see [Running a parallel GPU campaign](docs/campaign-throughput.md) for independent pilot admission, explicit work-state counts, productive local capacity and safe rebalancing.

## Install

Install from a release tag, as below; `main` can be ahead of the latest release.
Older installs should follow their own `shk <command> --help`.

`shk --version` prints `sherlock-kit 0.7.1 (FORM)`, where FORM names the install:

| Form | How | FORM |
|---|---|---|
| Project environment (recommended) | `pip install 'sherlock-kit @ git+https://github.com/FridrichMethod/sherlock-kit@v0.7.1'`, or a uv source with `tag = "v0.7.1"` | `git <commit>` |
| Submodule | a consumer checks out this repository as a Git submodule and invokes its stdlib-only `bin/shk` entry point | `submodule <path>` |
| Editable | `pip install -e <path>` into an environment you choose; it follows that tree | `editable <path>` |

A wheel or path install reports `unknown`; a plain checkout run through `bin/shk`
reports `source <path>` and is for development only. Invoke the project
environment's executable directly, or activate that environment before using `shk`.
Installation and updates belong to the project; no global launcher is required.

```toml
[tool.uv.sources]
sherlock-kit = { git = "https://github.com/FridrichMethod/sherlock-kit", tag = "v0.7.1" }
```

On Sherlock, create the project-pinned environment under `$GROUP_HOME`, inside a
Slurm allocation (for example an `sh_dev` session), with Python 3.11 or newer.
Fetch sources or build artifacts beforehand if compute nodes cannot reach their
source. Do not create the environment on a login node or under scratch.
For example, inside the allocation, using a release wheel fetched beforehand:

```sh
python3 -m venv "$GROUP_HOME/demo/.venv-shk"
"$GROUP_HOME/demo/.venv-shk/bin/python" -m pip install --no-index --no-deps /path/to/sherlock_kit-0.7.1-py3-none-any.whl
"$GROUP_HOME/demo/.venv-shk/bin/shk" --version
```

Use the environment's absolute executable path in batch scripts and controllers;
do not assume the intended environment is active in every job. The project records
the pinned version and environment path. For example, inside the appropriate job:

```sh
"$GROUP_HOME/demo/.venv-shk/bin/shk" manifest /path/to/run/out
"$GROUP_HOME/demo/.venv-shk/bin/shk" --executor local --project-dir /path/to/project status --all
"$GROUP_HOME/demo/.venv-shk/bin/shk" --executor local allocation --pid "$WORKER_PID" --job-id "$SLURM_JOB_ID"
"$GROUP_HOME/demo/.venv-shk/bin/shk" --executor local requeue --self --expect-name "$SHK_NAME"
```

A local controller uses that same absolute path for its authorized `submit` and
status calls. Allocation evidence requires the actual worker PID; self-requeue
requires the job identity, partition and checkpoint/retry conditions documented
below. These examples do not allocate resources or authorize a workload.

## Quick start

`shk` never logs in: it rides OpenSSH control masters that you open yourself,
one on a Sherlock login node and one on a data transfer node (DTN). A minimal
`~/.ssh/config` for both, with your SUNet ID in place of `SUNETID`:

```sshconfig
Host sherlock-plain
  HostName login.sherlock.stanford.edu
  User SUNETID
  ControlMaster auto
  ControlPersist yes
  ControlPath ~/.ssh/%C

Host sherlock-dtn
  HostName dtn.sherlock.stanford.edu
  User SUNETID
  ControlMaster auto
  ControlPersist yes
  ControlPath ~/.ssh/%C
```

These two names are only the defaults of `control_host` and `data_host`; any
other OpenSSH aliases work when set in `[tool.sherlock-kit]`, as long as the two
differ and each sets `ControlMaster`, `ControlPersist` and `ControlPath` as
above. Open both masters yourself first (`ssh sherlock-plain`,
`ssh -fN sherlock-dtn`), then add the project configuration and work from the
project directory:

```toml
[tool.sherlock-kit]
project = "demo"
remote_root = "/scratch/users/USER/demo"

[tool.sherlock-kit.validators]
bundle = ["python", "-m", "demo.validate_bundle"]
```

```sh
shk doctor                  # offline: Python, ssh, rsync, config, both ssh masters
shk submit --task smoke --script job.sh --partition normal --cpus 1 --mem 2G --time 20m --dry-run
shk submit --task smoke --script job.sh --partition normal --cpus 1 --mem 2G --time 20m
shk status --task smoke     # at least 60 s apart; the reply says when (next_check_after)
mkdir -p results            # pull creates LOCAL, never its parent
shk pull /scratch/users/USER/demo/runs/NAME/out results/smoke --validate bundle
```

Codex may need per-command approval to reach the existing SSH control socket,
even for offline `doctor`. A sandbox startup failure or a local socket permission
denial does not mean the user needs a new login. Approve the bounded command;
do not disable the sandbox or grant write access to all of HOME.
`submit --dry-run` needs no SSH socket or network access.

The job writes `out/SHA256SUMS` as its last step (`shk manifest out`, or the
`sha256sum` one-liner in the skill), over its output directory, never the run
directory itself; `shk pull` verifies every file against it before the copy
appears under `results/`.

## Commands

```text
shk [--project-dir DIR] [--executor auto|ssh|local] <command>
shk --version
shk doctor [--remote]
shk rules {workstation,cluster}
shk install-skill --project DIR [--client claude|codex|all] [--rules workstation|cluster] [--check] [--force]
shk occupancy --partition P [--partition P ...]
shk submit --task T [--campaign C] --script FILE --partition P[,P...] --cpus N [--cpu-thread-budget [N]] --mem SIZE --time DUR
           [--gpus N] [--tasks N] [--array N[%M]] [--constraint X] [--prefer X] [--signal S] [--requeue|--no-requeue]
           [--after-ok JOBID ...] [--run-dir PATH] [--courtesy-ack] [--resubmit REASON] [--submission-token TOKEN] [--hold] [--dry-run|--preflight]
shk resources --partition P [--partition P ...]
              [--summary [--by sku|class ...] [--shape NAME:cpus=N,mem=SIZE[,gpus=N] ...]]
shk --executor local allocation --pid PID --job-id N_OR_N_TASK [--step-id NUMERIC_STEP] [--hostlist EXPRESSION [--hostlist EXPRESSION ...]]
shk release --job-id ID --expect-name NAME --expect-token TOKEN
shk requeue --self --expect-name NAME [--expect-token TOKEN]
shk status (--task T [--task T ...] [--campaign C [--campaign C ...]] [--run-dir PATH [--run-dir PATH ...]] | --name NAME [--name NAME ...] | --all | --job-id ID [--job-id ID ...])
           [--since DAYS] [--details]
           (--campaign once for every --task, or once per --task; --run-dir once per --task, or not at all)
shk cancel (--task T [--campaign C] | --name NAME)
shk push LOCAL REMOTE [--anywhere]
shk pull REMOTE LOCAL [--validate NAME] [--replace]
shk manifest DIR
```

`submit --partition` takes one partition, or compatible partitions as one value
`P,P`; a repeated `--partition` flag is not a list (the last one wins).

For GPU selection, use a broad compatible `--constraint` and a `--prefer` subset
for L40/A/H and discovered compatible future B/newer GPUs. The optional
`--prefer` tier is purely a compute-capability ordering, so it applies across
projects. `--prefer` is soft: Slurm tries it first, then the fallback
`--constraint`, and it tries the preference independently of the fallback, so
repeat all hard requirements in both. These are explicit arguments, not TOML defaults; see
[GPU scheduling and bundles](docs/campaign-throughput.md).

- Operational results print one JSON document with `schema_version: 1` on stdout; `rules`, `--version`
  and help print text, and usage errors print text on stderr (exit 2).
  `shk <command> --help` is the authoritative flag list. `submit`,
  `status`, `cancel`, `occupancy` and `resources` report `next_check_after`, 60 s after they
  ran.
- `submit`: the job name is `shk.<project>.<task slug>.<hash>` of project,
  campaign and task. One read call (`squeue` and the last 30 days of `sacct`)
  decides `new`, `live`, `done`, `failed` or `stale` (spec files in the run
  directory but no Slurm rows, submitted like `new`); a live name is always
  refused, a finished one unless `--resubmit REASON`. Then one mutation call
  stages the script under `<run-dir>/.shk/` and runs `sbatch`. The submission
  is accepted only when the stdout of `sbatch` is, trailing newlines aside, one
  line holding a strict job id, `N` or `N;CLUSTER` with N a positive decimal
  integer without a leading zero, matched as ASCII digits whatever the login
  locale: exit 0 `submitted`, and the staged files are renamed to the job id,
  whatever exit code `sbatch` returned (`sbatch --wait` returns the job's).
  Every other result once `sbatch` has started is outcome unknown, never a
  refusal: exit 5 with `action: unknown`, `detail` holding `sbatch exit RC` and
  Slurm's stderr text when there is any, the staged `NAME.STAMP.sh`, `.json`
  and `.err` kept under `<run-dir>/.shk/`, and `next` naming the `shk status`
  command and when to run it. A lost reply is exit 5 too. A name whose
  submission never landed then reads `stale` and submits like `new` when it
  has no earlier job in that 30-day window; when it has one, it keeps that
  job's `done` or `failed` state and `current.job_id` and still needs
  `--resubmit`. The attempt landed only if `current.job_id` is newer than the
  one seen before the submit.
  Defaults: `--campaign default`, `--run-dir <remote_root>/runs/<name>`,
  `--array N[%M]` with 2 to 1000 tasks. `--after-ok` is repeatable and takes
  several job ids (`--after-ok 111 222`), each a single `N` or `N_TASK`: `shk`
  writes the `--dependency` string itself, so a `,` or `?` list is refused.
  `--constraint`, `--prefer` and `--signal` reach `sbatch` unchanged; one that
  is given but empty or whitespace-only (an unset shell variable, for example)
  is refused offline, exit 1 `spec_invalid`.
  `dev` is refused offline (exit 3 `batch_not_allowed`): Sherlock rejects every
  batch job there and keeps it for interactive `sh_dev` or `salloc` sessions;
  `occupancy` and `resources` still read it.
  `--partition` takes one partition, or compatible partitions as one
  comma-separated value, Slurm's own syntax (`--partition gpu,bioe,stat`); a
  repeated `--partition` flag is not a list (the last one wins). Only `submit`
  takes a list: `resources` and `occupancy` take one name per `--partition`
  flag and refuse a value with a comma (exit 3 `unknown_partition`). The
  members of a list are checked where a single partition's profile is looked
  up, before the resource checks, in this order: an unlisted member, the empty
  one included, is exit 3 `unknown_partition` with the usual message for the
  first such member; a repeated member is exit 1 `spec_invalid`; a member
  without batch jobs is exit 3 `batch_not_allowed` with the usual message for
  the first such member; an incompatible set is exit 3
  `partitions_incompatible`, whose hint says to submit incompatible partitions
  separately. A set is compatible when no member is borrowed and all members
  share one `requeue` and one `preemptible` value: the message names every
  borrowed member, or else lists every member with both values, for example
  `gpu (preemptible=no, requeue=no), owners (preemptible=yes, requeue=yes)`.
  Then the resource checks apply to the set: `--gpus` above 0 needs every
  member to allow GPUs (exit 3 `gpus_not_allowed`, naming every member without
  them), requeue defaults to the members' common value, and `--requeue` on a
  set that does not allow it is exit 3 `requeue_not_allowed`, naming the set
  as given. So `gpu,bioe,stat` combines, while `owners`, `btrippe` and `possu`
  stay alone; by the same profile fields `normal`, `bigmem` and `service` are
  in the class too. A set states that the job may run on any member, and every
  member's courtesy text applies wherever it starts. The argv holds
  `--partition=<value as given>` in the usual position and is otherwise that
  of a single partition: the job name does not depend on the partition, and the read, the
  live and duplicate guards, `singleton`, the staging and the exit 5 handling
  are those of a single partition (no occupancy is read: no member is
  borrowed). The frozen spec keeps `resources.partition` as the value as given
  and adds `resources.partitions`, the list, only for more than one member; in
  the `spec` that `--dry-run` and `--preflight` print it is the last key of
  `resources` (after `prefer` when both are present), and the staged spec file
  has sorted keys. A list is given at submission only; it is not a TOML key.
  `--dry-run` is offline. `--preflight` is mutually exclusive and sends the
  same script/resources to `sbatch --test-only` as a read: no job, run directory
  or provenance is created. Its `preflight` object retains returncode, stdout
  and stderr; rejection is exit 3, read failure is never unknown. It is the way
  to get a definitive Slurm verdict on a request without creating a job.
  The `--preflight` reply adds `estimate` directly after `preflight`:
  `{status, start, start_zone, nodes, partition, line, observed_at}`, read from
  the lines of `stderr`, then `stdout`, each split on LF only. An estimate line
  is a whole line of Slurm's form
  `Job N to start at TIME [on cluster NAME] [a] using N processors on nodes NODES in partition PARTITION`,
  with or without the `sbatch: ` prefix. `status` is `parsed` for exactly one
  estimate line whose TIME is `YYYY-MM-DDTHH:MM:SS`, and then `start`, `nodes`,
  `partition` and `line` are set (other lines that merely resemble an estimate,
  a debug line for example, do not change this). It is `absent` when no line is
  an estimate line and none holds the words `to start at` between spaces, and
  then those four are null; this covers an accepted call without an estimate
  and a rejected call, which `ok` and the exit code tell apart. It is
  `unparsed` in every other case: several estimate lines, an estimate line
  whose TIME is not an ISO time (`Unknown`, `None`, a custom or relative
  format), or no estimate line while some line holds the words (an unknown
  shape, an empty node list). Then `start`, `nodes` and `partition` are null
  and `line` is the first estimate line, else the first line holding the
  words: nothing is guessed. `start` is the time as Slurm printed it, without a
  zone; `start_zone` is the constant `sbatch-local`, because Slurm formats the
  time in the local zone of the `sbatch` process on the executor host, which is
  the site's zone unless `TZ` is set in that session, and prints no offset.
  `observed_at` is UTC and equals `queried_at`; the kit converts nothing.
  `nodes` is the node list as printed, a Slurm hostlist expression, not
  expanded. The job id that Slurm prints in the line belongs to a temporary
  record: no key of `estimate` has it as its value (it shows only inside
  `line` and `preflight.stderr`). The estimate is advisory, not a reservation
  and not a promise. For a request with several partitions Slurm names the
  first partition, in its own priority order, that can run the job, and
  estimates the start there: it is not the earliest start over the set.
  Comparing partitions takes one preflight per partition.
  `--submission-token` accepts 1-128 public characters `[A-Za-z0-9_.:-]` (first
  alphanumeric), freezes it in the spec, sets Slurm Comment and exports it to
  the job as `SHK_SUBMISSION_TOKEN`. `--hold` freezes a user hold and needs a
  token, which `release` checks. Tokens correlate attempts and contain no secrets.
- The script's leading `#SBATCH` lines may set anything except what `shk` owns
  (`-A`/`--account`, `-x`/`--exclude`, `-G`/`--gpus*`/`--gres`,
  `-p`/`--partition`, `-a`/`--array`, `-d`/`--dependency`, `-b`/`--begin`,
  `-J`/`--job-name`, `-H`/`--hold`, and `--comment` when `--submission-token`
  sets Comment) and a `hetjob` or `packjob` separator: heterogeneous jobs are
  refused. Matching follows sbatch's own parsing, so abbreviated long
  options (`--part=gpu`) and short flags bundled after value-less ones
  (`-Hp gpu`) count too.
- `status`: one `squeue` and one `sacct` for all selected names (`--details`
  adds one `scontrol show job` per queued base job id); `--since` defaults to
  30 days (7 with `--all`) and accepts 1 to 31, because Sherlock's slurmdbd
  refuses wider `sacct` windows. Give `--campaign` once for every
  `--task` or once per `--task`, and `--run-dir` once per `--task` or not at
  all. Repeatable `--job-id N` or `N_TASK` is mutually exclusive with task,
  name and all selectors; it includes legacy names and only current UID rows,
  and lists one entry per selected base job. Without a spec file (always with
  `--all`, `--name` and `--job-id`) an array whose seen tasks are not exactly 0
  to the largest index has null `array.count`, `tasks.count` and
  `tasks.missing`, and `done` or `failed` describe the seen tasks only. A
  preemption or restart is unexpected (exit 6) on the non-preemptible partition
  of the attempt it interrupted, and on an interrupted partition list whose
  members are all listed and none preemptible. `current.partition` is Slurm's
  partition text of the job's first row; for a job submitted with a partition
  list, `status --task` adds `current.ran_on`, the partitions in which it
  started. Both are described with the [detailed status contract](#detailed-status-contract).
  `--details` returns the evidence contract described below. `cancel` cancels
  the live jobs of one name; a nonzero `scancel` exit is exit 5 with
  `scancel exit RC` and Slurm's stderr text in `detail`, and a lost reply is
  exit 5 too; both are settled by the status read that `next` names.
- `resources` reads `scontrol show nodes` once; Sherlock ships only the nodes
  whose `Partitions=` list names a requested partition, each once. A node keeps
  its state, partitions, active features, configured/allocated CPU (CPUEfctv,
  else CPUTot), memory and GPU, CfgTRES/AllocTRES and its raw line (the
  evidence, so `owners` is megabytes: filter the JSON rather than read it
  whole). `complete`, `malformed`, `next_check_after` and the `provenance` of
  the derived fields come before `nodes`. `free_*` is configured minus
  allocated whatever the state (`free_memory_mb` from CfgTRES minus AllocTRES;
  OS FreeMem never substitutes). `schedulable` is false when the `+`-joined
  state has any of `DOWN`, `DRAIN`, `FAIL`, `MAINTENANCE`, `NOT_RESPONDING`,
  `RESERVED`, `INVALID_REG`, `FUTURE`, `UNKNOWN`, `REBOOT_ISSUED`,
  `REBOOT_REQUESTED`, `POWER_DOWN`, `POWERED_DOWN` or `POWERING_DOWN` (or their
  short sinfo spellings, listed in `provenance`), true otherwise (`PLANNED` and
  `COMPLETING` included) and null without a state: count only schedulable
  nodes. Missing facts are null and `complete: false`; a failed `scontrol` read
  is exit 1 `malformed_read` with its rc and stderr. Partition and QOS limits,
  reservations and owners preemption are not reflected; availability is a
  snapshot, not a resource reservation. `--summary` reports the same single
  read as counts instead of `nodes`: per node class, for the union of the
  shipped nodes and for each requested partition, optionally split `--by sku`
  or `--by class` and with slot counts for each `--shape`. It ranks nothing;
  see [resource summary](#resource-summary).
- `allocation` verifies an explicit local worker PID, own UID, physical job/array
  identity, current host and optional numeric step membership. It preserves actual
  resources, node records, raw records and hashes; missing counts remain null and
  no project resource policy is inferred. It refuses SSH, exited/reused PIDs and
  ambiguous or contradictory evidence; a scheduler that does not answer is a
  retryable read failure, never non-membership. Its `gpu_sku_candidates` list
  the `GPU_SKU:` features of each node record as candidates for the GPU model,
  with an `ambiguous` flag and the source fields; it never states which device
  the job got. See [the allocation contract](docs/allocation.md).
- `release` checks one exact ID, current UID, expected name/token and user hold
  immediately before `scontrol release`. `requeue --self` has no arbitrary ID
  option: it requires the local allocation (`SLURM_JOB_ID`), actual Slurm PID
  membership in that physical job, current UID and name, the token only when
  `--expect-token` is given, RUNNING state, a partition whose profile allows
  requeue (today `owners`) and Requeue=1. These are read from Slurm's record of
  that physical job, which also names the target: an array element is requeued
  as its `ArrayJobId_ArrayTaskId`, never the bare array ID, which would requeue
  every element; inherited `SLURM_ARRAY_*` variables are not trusted. A refusal
  is exit 3 and keeps Slurm's message when a query failed; a nonzero `scontrol`
  mutation is exit 5 with `scontrol ACTION exit RC` and Slurm's message in
  `detail`, and a lost reply is exit 5 too. Slurm ownership
  checks still apply, and concurrent user changes can race the identity read.
  Checkpoint durability and retry limits belong to the caller.
- `push` rsyncs LOCAL/ through the DTN into an absolute REMOTE under
  `remote_root` (`--anywhere` lifts that) and never deletes. Pushed files keep
  their local modification times (`rsync -t`, so a re-push skips unchanged
  files instead of rewriting them); inputs that are old locally can therefore be
  purge-eligible on scratch soon after a push.
  REMOTE's parent must already exist. For a new remote root, push a staging
  directory containing `inputs/` to the root itself, for example
  `shk push staging/ /scratch/users/USER/demo` when `/scratch/users/USER` exists.
  Check for exit 0 before submitting a job that needs those inputs: a failed or
  timed-out push can leave a truncated file under its final name, also where a
  complete older copy was, so rerun it to exit 0 before any job reads REMOTE.
- `pull` is payload-only: the stage, and so LOCAL, holds exactly the regular
  files that `REMOTE/SHA256SUMS` lists, and the directories they imply.
  `SHA256SUMS` itself is not staged and not promoted; the receipt keeps the
  sha256 of the `SHA256SUMS` text that pull read (`manifest_sha256`). A
  manifest lists files only, so an empty remote directory is not
  created locally: a validator must not require one, and must not create one.
  `pull` copies exactly the listed files into a private stage
  beside LOCAL, verifies them, runs the optional validator and renames the stage
  onto LOCAL. With NAME the last component of LOCAL, the stage is
  `.NAME.shk-stage` (a rerun resumes it), `.NAME.shk-lock` serializes concurrent
  pulls, the receipt is `.NAME.shk-receipt.json`, and `--replace` keeps the
  previous copy as `.NAME.shk-old-<UTC>`; projects can gitignore `.*.shk-*`. The
  validator runs from the project root (the directory of the configuration file)
  with the stage directory as its last argument, passes with exit 0 and must
  treat the stage as read-only: nothing re-verifies the stage after the
  validator, and whatever it writes is promoted with the data. The kit does not
  enforce this obligation.
  An error while the remote side enumerates REMOTE (an unreadable subdirectory,
  for example) is exit 1 `remote_failed` with `find`'s message, and nothing is
  promoted. `already_present` means that the receipt beside LOCAL names this
  manifest; LOCAL itself is not verified again. The `pulled` and
  `already_present` replies end with the constant `payload_contract`:
  `{"name": "payload-only", "version": 1, "verified_against": "SHA256SUMS", "manifest_in_payload": false, "empty_directories_preserved": false, "validator_must_not_write_stage": true}`.
  It describes what a pull promotes, and `validator_must_not_write_stage`
  states the validator's obligation. On `already_present` LOCAL is not
  verified again: the pull reads `SHA256SUMS` once more, compares its digest
  with the receipt, reads no file of LOCAL and runs no validator, so that
  reply rests on the receipt of the earlier pull.
- `occupancy` lists current use of partitions. Borrowed partitions (`btrippe`,
  `possu`) need it and `--courtesy-ack` before a submission, and such a
  submission reports `occupancy`, the partition's `courtesy` text,
  `request = {tasks, throttle, minutes}` and `warnings`. `tasks` is 1 for a
  single job and the array count otherwise, `throttle` is the `%M` limit or
  null, and `minutes` is the requested walltime. Two advisories can appear in
  `warnings`, and neither refuses anything: the budget warning, when others
  are running there and you already have 2 jobs there or ask for more than
  2 h; and a total-work advisory, when others are running there and `tasks` is
  greater than 2, which states the number of array tasks, the throttle (or
  that there is none) and the minutes without calling the request within or
  over any budget. The courtesy terms are the profile texts alone: `btrippe`
  has no blanket daytime prohibition, and `possu` keeps its 00:00-07:00 Pacific
  window. Releasing a job held there counts as submitting it: `release` checks
  neither occupancy nor the clock, so check both first.
- `install-skill` copies the agent skill into a project (`.claude/skills/` and
  `.agents/skills/`) and, with `--rules`, the rules into `.claude/rules/` (Codex
  gets a hint instead); `--check` exits 1 when a copy is missing or stale.

## Detailed status contract

`status --details` adds `details = {queue, accounting, jobs, complete, queried_at, history_window_days, missing_job_ids, missing_control_job_ids, selection, allocation_evidence_complete, history_complete, control_identities_complete, returncodes, malformed}`. Queue and
accounting are lists of full records, before summary deduplication; every
record includes the original `raw` line, whose fields are separated by the
ASCII unit separator (0x1F) so that `|` in comments and job names stays data.
Their normalized fields are
`job_id`, `job_id_raw`, `cluster`, `db_index`, `state`, `partition`,
`elapsed_seconds`, `allocated_cpus`, `allocated_tres`, `submitted_at`,
`started_at`, `ended_at`, `restarts`, `exit_code`, `nodes`, `name`, `comment`
and `uid`. Missing fields are null, numeric facts are integers, timestamps
are UTC strings, IDs (including DBIndex) remain strings. Queue CPUs and start
are null for a job without nodes (pending, or cancelled before any
allocation), and a live job's end is null, because squeue prints requests,
placeholders or predictions there. Sherlock's accounting `Comment` is empty,
so `details` shows a submission token only in queue and control records while
the controller holds the job; the frozen spec keeps it. Slurm also records the
sbatch command line, which carries the token, as `SubmitLine` (in control
records, and in `sacct` where slurmdbd stores it).

`jobs` holds current-UID `scontrol` records of queued physical IDs as `{job_id, name, comment, uid, fields, raw}`, read with one `show job` per base job id: an
array ID prints every element the controller still holds, records outside the
selection are ignored, and each queued physical ID needs its own record.
`fields` retains original Slurm names and string values (times in UTC),
including request TRES, Features, TimeLimit, Requeue and array identity. All
records are restricted to the current UID. `returncodes` holds each tool's
exit status. A failed `squeue` or `sacct` read, or a selected name none of
whose rows parse, exits 1 `malformed_read` like plain status, with `details` and
without `names`. A failed control read, missing requested IDs, malformed rows or
missing accounting cost identity make `complete` false and retain available
facts; a failed control read alone still exits 0.
Accounting rows of jobs that never had an allocation (AllocCPUS 0, ElapsedRaw
0, no Start and no AllocTRES, in any state such as PENDING or
`CANCELLED by UID`) are structurally complete, and a terminal one has a final
zero cost. Missing allocation TRES after a job started and restart-history
gaps are incomplete.

`--job-id` lists one entry per selected base job, in job id order; legacy jobs
often share a script name, so `duplicate_live` (across the selected jobs) and
`unknown_partition` apply to `shk.` names only. Each entry's
`current.evidence_complete` covers its own job (accounting identity and
allocation, control records, restart history, partial scope) and malformed
lines that no job can be told apart from; `cost.final` requires it, while
`complete` is the conjunction over the whole query. A requested ID without
readable rows (a hetjob's `N+k`, for example) has no entry and is listed only in
`missing_job_ids`, so a batch's cost is final only when that list is empty and
every entry has `cost.final`. By task or name, an unreadable row withdraws only
its own name's final cost.
An exact array-element selection reports `selection.partial_array_ids`, entry
`scope: partial_array`, unknown whole-array state/count/missing values, and
nonfinal cost; sibling states cannot be inferred from a selected element.
Without a spec file an array's task count is inferred only when the seen tasks
are exactly 0 to the largest index; otherwise (1-based or stepped legacy
arrays, early tasks gone from the window) `array.count`, `tasks.count` and
`tasks.missing` are null, the state follows the seen tasks and the cost is not
final. A cancelled array that never split is one squeue row without a task
suffix, and names-mode `sacct` returned no rows on Sherlock for jobs held until
cancelled, so such a names-mode summary can count one task; `--job-id` reads
the accounting aggregate.
A complete query does not establish unlimited historical coverage: it is bounded by
`history_window_days`, and array cardinality without provenance is inferred.

`current.partition` is the partition text of the first row Slurm printed for
the job, queue rows before accounting rows; the order is Slurm's and `shk` does
not fix it. For a single job submitted with a partition list this is the list
while pending (in an order Slurm may change) and the run partition once
started; for a job that ended without starting it is one member in `squeue`
and the list in `sacct`. For an array, or a requeued job read from accounting
only, it is one row's text, can change between reads and says nothing about the
other tasks or attempts. `current.ran_on` directly follows `partition` and is
present only when status read the frozen spec of that job (the newest spec file
of the name, for that job id, as for the task count) and its
`resources.partitions` lists several partitions: that is `status --task` with a
run directory, plain or with `--details`. Its value is the sorted distinct
partitions of the job's started rows, that is every partition a task or attempt
started in, and `[]` until a task has started. A started row is an explicit row
(not an `N_[...]` aggregate) whose partition text is one name and which shows a
start: an accounting row with AllocCPUS above 0, or a queue row in a state that
status counts as running. Without a spec (`--all`, `--name`, `--job-id`) there
is no `ran_on`: `current.partition` is the first row's text, which for an array
whose tasks started on different members names only one of them or the pending
list. The per-row partition of `status --details`, or `--task`, tells where
each ran. Every queue and accounting record of `details` keeps its partition
text as Slurm printed it, and the cost arithmetic does not depend on it.
`unknown_partition` (`shk.` names only) means that some row of the job has a
partition text, split on `,`, with a member that is not listed; the verdict
does not depend on the row order. `unexpected_preemption` also counts an
interrupted partition text that is a comma list when every member is listed and
none is preemptible (a compatible set shares one value).

A bounded deterministic workflow manager may run inside its allocated Slurm
job and call `shk --executor local` to submit/poll authorized later compute
stages at intervals of at least 60 seconds, with no workstation dependency.
This does not authorize scheduling future agent or LLM sessions. Controllers
own their bounded retries, checkpointing, scientific validation and accounting.

## Resource summary

`resources --summary` is a separate output mode of the same single
`resources.sh` read. Without `--summary` the reply is the per-node reply
described above. In summary mode the reply holds, in this order, `complete`,
`malformed`, `executor`, `queried_at`, `next_check_after`, `provenance` (the
same object) and `summary`; there is no `nodes` key. `complete` and
`malformed` are computed as without `--summary`, and a failed read is exit 1
`malformed_read`.

- `--by` takes one value per flag, `sku` or `class` (`--by sku --by class`);
  another value is a usage error (exit 2), and repeats collapse.
- `--shape NAME:cpus=N,mem=SIZE[,gpus=N]` takes one shape per flag. NAME
  matches `[A-Za-z0-9][A-Za-z0-9_.-]{0,31}` and is unique; the keys may come in
  any order; `cpus` matches `[1-9][0-9]*`; `mem` uses the `submit --mem`
  grammar (`16G`, `16000M`; G = 1024 M); `gpus` matches `0|[1-9][0-9]*` and
  defaults to 0. A shape is one slot on one node. A bad NAME, a repeated NAME,
  an unknown, repeated or missing key or a bad value is exit 1 `spec_invalid`,
  offline.
- `--by` or `--shape` without `--summary` is exit 1 `spec_invalid`, offline,
  before any remote call. The offline checks run in this order: the
  partitions (exit 3 `unknown_partition`), then `--by` or `--shape` without
  `--summary`, then each shape in the order given.

Every node line the read ships is one node, in exactly one class. A known node
has a non-null `name` and `state` and non-null configured and allocated CPUs,
memory and GPUs (so its three `free_*` values are non-null). A countable node
is known and `schedulable`; an unschedulable node is known and not
`schedulable`; every other node is unknown. A line listed in `malformed` is an
unknown node too: its `partitions` and `features` are read from the raw line
as for any record, and every other fact, `schedulable` included, is null. It
counts in `union`, in the group of every requested partition it names, in the
breakdown entry for its feature key, and in `overlapping_nodes` when it names
more than one requested partition. The `malformed` list is the same as
without `--summary`.

The union, each partition and each breakdown entry are a group object:

```text
{"nodes": N, "schedulable_nodes": N, "countable_nodes": N,
 "unschedulable_nodes": N, "unknown_nodes": N,
 "cpus":      {"configured": N|null, "allocated": N|null, "free": N|null},
 "memory_mb": {"configured": N|null, "allocated": N|null, "free": N|null},
 "gpus":      {"configured": N|null, "allocated": N|null, "free": N|null},
 "slots": {"<shape NAME>": N|null, ...}}
```

- `nodes` is countable plus unschedulable plus unknown nodes.
  `schedulable_nodes` counts every node whose `schedulable` is true, known or
  not.
- `configured` and `allocated` are sums over known nodes (countable and
  unschedulable); `free` is the sum of `free_*` over countable nodes. Memory is
  in the unit of the node fields (`*_memory_mb`).
- `slots[NAME]` is, over countable nodes, the sum of
  `min(free_cpus // cpus, free_memory_mb // memory_mb, free_gpus // gpus)`, the
  GPU term only when the shape asks for GPUs. `slots` keys follow the shape
  order, and `slots` is `{}` without a shape.
- Unknown stays unknown: a value with nothing known behind it is null,
  never 0. `configured` and `allocated` are null in a group without a known node.
  `free` and every slot are null in a group without a countable node, unless
  every node of the group is a known unschedulable node (then they are 0, a
  known fact). So a group without any node (a requested partition that no
  shipped line names, or the union of an empty read) has `nodes` 0 and null
  sums and slots: the read does not show its capacity. With some known nodes
  and `unknown_nodes` above 0 the numbers are lower bounds, and `complete` is
  false.

The `summary` object holds these keys in this order:

```text
"summary": {
  "rules": {"snapshot": TEXT, "countable": TEXT, "slots": TEXT, "overlap": TEXT},
  "shapes": {"<NAME>": {"cpus": N, "memory_mb": N, "gpus": N}, ...},
  "by": ["sku", "class"],
  "overlapping_nodes": N,
  "union": GROUP + breakdowns,
  "partitions": {"<partition>": GROUP + breakdowns, ...}
}
```

- `shapes` and `by` are in the order given, `by` deduplicated and `[]` without
  `--by`. `overlapping_nodes` counts the nodes that list more than one
  requested partition. `union` counts every shipped node once. `partitions`
  is in request order, deduplicated.
- A partition's group holds the nodes whose `partitions` list names it, so a
  node in two requested partitions is in both groups and once in `union`. No
  total over partition groups is printed.
- Breakdowns appear on `union` and on every partition, only for the requested
  kinds, after the group fields in the order of `by`:
  `"by_sku": [{"sku": [VALUE, ...], GROUP fields}, ...]` and
  `"by_class": [{"class": [VALUE, ...], GROUP fields}, ...]`. The key of a node
  is the sorted list of its active feature values after the prefix `GPU_SKU:`
  (`sku`) or `CLASS:` (`class`); a node without such a feature has the key
  `[]`. A node is in exactly one entry of a breakdown, the one for its exact
  key, so a node with several SKU features is in the entry for that exact set
  and gives its GPUs to no single-SKU entry. Entries are sorted by key. The
  entries of one breakdown split their group: the five node counts add up to
  the group's; for each capacity value and slot, when the group's value is a
  number the non-null entry values add up to it, and when it is null every
  entry's value is null or 0 (0 only for `free` and slots, in an entry that
  holds only unschedulable known nodes). Without `--by` there is no `by_sku` or
  `by_class` key.
- `rules` holds four fixed texts:
  - `snapshot`: "Snapshot heuristic at queried_at: configured minus allocated
    capacity. It does not model fairshare, QOS or partition limits,
    reservations, preemption or pending work, reserves nothing and predicts no
    start time."
  - `countable`: "A node is countable when its State has none of the flags
    listed in provenance.schedulable and its configured and allocated CPUs,
    memory and GPUs are all known. free and slots count countable nodes only;
    configured and allocated also count unschedulable nodes. A node with a
    missing fact is in unknown_nodes and in no sum: beside a nonzero
    unknown_nodes the numbers are lower bounds. A value with nothing known
    behind it is null, never 0: configured and allocated in a group without a
    known node, free and slots in a group without a countable node unless all
    its nodes are known unschedulable nodes."
  - `slots`: "Per countable node: the smallest of free CPUs, free memory and,
    when the shape asks for GPUs, free GPUs, each divided by the shape's amount
    and rounded down; summed over the group."
  - `overlap`: "A node that lists several partitions is in each of their
    groups, and by_sku and by_class split the same nodes in two ways: never add
    partition groups or the two breakdowns together. union counts every node
    once. Within one breakdown a node is in exactly one entry, the one for its
    exact set of feature values."
- The summary ranks nothing: no recommended partition, shape or GPU model, no
  sorting by capacity, no lowered floor.

## Configuration

`[tool.sherlock-kit]` in `pyproject.toml`, else the top level of
`sherlock-kit.toml`, found with `--project-dir DIR` (exactly that directory) or
in the nearest directory at or above the working directory that has one.
Unknown keys are errors; nothing secret belongs here.

| Key | Default | Used by |
|---|---|---|
| `project` | none; `^[a-z0-9][a-z0-9-]{0,23}$` | `submit`, `status --task`, `cancel --task`; `status --all` lists only this project's names |
| `remote_root` | none; absolute, no `..` | `submit` (default run directories, also for `status`), `push` (unless `--anywhere`) |
| `control_host` | `sherlock-plain` | every remote call |
| `data_host` | `sherlock-dtn` | `push`, `pull` |
| `connect_timeout`, `command_timeout`, `transfer_timeout` | 15, 60, 3600 s | ssh and rsync |
| `cpu_thread_budget` | none (off); positive integer, at most `--cpus` | `submit` numerical thread defaults per task, overridden by `--cpu-thread-budget`; see [CPU thread budgets](docs/cpu-threads.md) |
| `[tool.sherlock-kit.validators]` | none; `NAME = ["argv", ...]` | `pull --validate NAME` runs argv plus the stage directory, only from a configuration file you own |

`doctor`, `rules` and `occupancy` work without a configuration. On Sherlock
(`sh<N>-ln<N>` login nodes, `SLURM_CLUSTER_NAME=sherlock`, a compute node inside a
job, or `SHK_EXECUTOR=local`) the same programs run locally and `push` and `pull`
are refused.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | local error: configuration, spec, verification, validator, malformed reply, failed remote read |
| 2 | usage error |
| 3 | refused, and nothing was submitted, cancelled, released or requeued: live duplicate, finished task without `--resubmit`, missing `--courtesy-ack`, an `#SBATCH` directive `shk` owns, an incompatible partition list (`partitions_incompatible`), a remote guard that stopped before `sbatch` or `scancel` ran, a `--preflight` request that Slurm rejects, push or pull on Sherlock |
| 4 | SSH transport or master check unavailable: `auth_required` (ask the user to open the master); `unavailable` with a local permission denial (check the sandbox; request per-command approval in Codex); `unavailable` with a `~/.ssh/config` hint (fix the client config); other `unavailable` or `timeout`, hint `run shk doctor` (run it, retry once after a minute, then stop) |
| 5 | outcome unknown after a dispatched mutation: `sbatch` ran without printing a strict job id (its exit code and stderr are in `detail`, and the staged script, spec and stderr are kept), `scancel` exited nonzero, `scontrol` exited nonzero in `release` or `requeue`, or the reply was lost. Run the `shk status` command in the reply's `next`, not before the time it names (at least 60 s) |
| 6 | status ok, but the newest job of a requested name has an anomaly |

Errors print `{"ok": false, "error": TOKEN, "message": ..., "hint": ...}`.

Do not wrap `shk` in an outer timeout shorter than its own (`command_timeout`,
`transfer_timeout`): if `shk` itself is killed, a mutation in flight is outcome
unknown without an exit 5 reply, and a transfer may still be running.

## Agents

- Rules: `src/sherlock_kit/data/rules/workstation.md` (agents on a workstation)
  and `cluster.md` (agents on Sherlock), at most 7 KiB each, each ending with the
  partition profiles generated from `data/partitions.json`.
- Skill: `src/sherlock_kit/data/skills/sherlock-kit/SKILL.md`, for Claude Code and
  Codex alike.
- Per-project delivery: use the pinned environment's `shk install-skill --project .`
  (optionally `--rules workstation` or `--rules cluster`). Read rules directly with
  `shk rules workstation` or `shk rules cluster` when they are not already loaded.
  When `~/.claude/rules/sherlock-kit.md` exists, `install-skill --rules` skips the
  project copy, because Claude Code already loads that file; `--force` copies it
  anyway.

## Development

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pre-commit install
.venv/bin/pre-commit run --all-files    # ruff, mypy, shellcheck, shfmt, mdformat
python3 -m venv .venv-build
.venv-build/bin/python -m pip install 'build>=1.2.2,<2' 'setuptools>=80'
.venv-build/bin/python -B tests/prepare_wheelhouse.py --project . --dest .state/test-wheelhouse
SHK_BUILD_PYTHON="$PWD/.venv-build/bin/python" \
SHK_TEST_WHEELHOUSE="$PWD/.state/test-wheelhouse" \
SHK_REQUIRE_ALL=1 .venv/bin/python -B tests/run_suite.py
```

The suite is offline: fake `ssh` and Slurm binaries, real local `rsync`, `sh` and
`dash`, and the `en_US.UTF-8` locale for one bash test. A skipped test fails the
run when `CI` or `SHK_REQUIRE_ALL` is set.
`SHK_BUILD_PYTHON` (an absolute interpreter path with `build` and the backend
requirements) and `SHK_TEST_WHEELHOUSE` (the prepared absolute wheelhouse path)
enable the sdist, wheel, editable and source-launcher checks. They build committed
`HEAD`, so commit changes before using them as release evidence. Prepare a fresh
wheelhouse for each Python/platform and whenever dependencies change. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and the
[documentation index](docs/README.md) for operating guides, design contracts,
compatibility evidence and release history.

Licensed under [MIT](LICENSE).
