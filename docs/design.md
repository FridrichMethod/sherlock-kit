# Design (0.8.0)

`shk` is a thin, stateless interface between an agent and Sherlock's Slurm and
filesystems. It answers three questions with as little machinery as possible:
is this logical task already in Slurm, what is its state, and did this data
arrive intact. Everything else (authorization, scientific validity, what a job
script does) stays with the user, the site rules and the project.

## Pieces

| Module | Role |
|---|---|
| `cli.py` | argparse frame, `Context` (lazy config, executors, clock), JSON output, exit codes |
| `errors.py` | exit codes, `ShkError(code, token, message, hint)`, `Outcome(payload, code)` |
| `config.py` | `[tool.sherlock-kit]` discovery and validation |
| `executor.py` | `SshExecutor` and `LocalExecutor`, the master preflight, detection, `run_program` |
| `partitions.py` | the packaged partition table and the generated rules block |
| `doctor.py` | read-only checks |
| `spec.py`, `cpu_threads.py`, `slurm.py`, `details.py`, `node_resources.py`, `control.py` | resources (one partition or a compatible partition list), job name, sbatch argv and its optional thread-budget `env` prefix; `submit` (with the `--preflight` estimate), `status`, `cancel`, `occupancy`, `resources` (per node or `--summary`), `release`, `requeue` |
| `allocation.py` | local worker PID and allocation/step evidence, strict field parsing, actual resources and GPU SKU candidates of the node records |
| `transfer.py` | `push`, payload-only `pull`, `manifest` |
| `install.py` | `rules`, `install-skill` |
| `data/remote/*.sh` | the fixed POSIX `sh` programs run on the login node |
| `data/rules/*.md`, `data/skills/sherlock-kit/SKILL.md` | agent content, delivered by `install-skill` or read with `shk rules` |

`bin/shk` is a stdlib-only entry point: it follows symlinks to the repository,
puts its `src/` first on `sys.path`, disables bytecode to keep the source checkout
clean and refuses Python older than 3.11. The version is the literal
`sherlock_kit.__version__`, which `pyproject.toml` reads as its dynamic version, so
the running code always reports its own version; `shk --version` names the
provenance (`git`, `editable`, `submodule`, `source` or `unknown`).

## No state

`shk` writes nothing outside the paths a command names: no state root, no cache,
no cooldown file, no ledger, nothing under `HOME` (a test runs `--version`,
`rules`, `doctor` and `submit --dry-run` with an empty `HOME` and checks it stays
empty). The facts live where they already are:

- Slurm (`squeue -t all`, `sacct`) is the only record of what was submitted.
- The run directory on Sherlock keeps `.shk/<name>.<jobid>.sh` and `.json` (the
  script as submitted and its frozen spec) as provenance; a submission whose
  `sbatch` printed no strict job id leaves its staged
  `.shk/<name>.<stamp>.sh`, `.json` and `.err` there instead, one set per such
  submission, and each `.json` under `.shk/`, staged or renamed, counts toward
  the name's `spec_files` in `status`. On scratch the directory is subject to
  the 90-day purge like everything else there.
- `pull` keeps its stage, lock and receipt beside the project's own destination.
- The project configuration is a project file.

## Configuration

`--project-dir DIR` reads exactly DIR. Otherwise discovery walks up from the
working directory to the first directory with a configuration; in each directory
`pyproject.toml` with `[tool.sherlock-kit]` wins over `sherlock-kit.toml`.
Unknown keys, bad values and unreadable files are `config_invalid`. Remote paths
are absolute and contain no `..`; the remote user comes from `id -un` inside each
remote program.

## Executors and the master preflight

`detect()` picks the executor: `--executor` or `SHK_EXECUTOR` first; else local
on a Sherlock login node (`^sh\d+-ln\d+$`), with `SLURM_CLUSTER_NAME=sherlock`,
or on an `sh<N>-` compute node inside a job; else ssh. It never probes system
configuration and never infers Sherlock from `sbatch` being on `PATH`.

Every ssh dial is preceded by `ssh -O check -oBatchMode=yes HOST`, which only
asks the local control socket:

- no master: exit 4 `auth_required`, hint `ssh sherlock-plain` (or
  `ssh -fN sherlock-dtn` for the data host). Only the `-O check` ran;
- no usable `ControlPath`: exit 4 `unavailable` with a `~/.ssh/config` hint,
  because logging in again would not help;
- no `ssh`: exit 4 `unavailable`, hint `install OpenSSH`.
- local socket or process permission denial: exit 4 `unavailable`, state
  `inaccessible`; check local permissions and, in Codex, request per-command
  approval. This does not prove that the master is absent;
- a timed-out or otherwise failed check: exit 4 `unavailable`, state `timeout`
  or `unavailable`; inspect the failure before requesting a new login.

The dial itself is `ssh -T -oBatchMode=yes -oRemoteCommand=none -oConnectTimeout=N -oConnectionAttempts=1 -oStrictHostKeyChecking=yes -oUpdateHostKeys=no -oControlMaster=no -oPermitLocalCommand=no -oClearAllForwardings=yes -oForwardAgent=no -oProxyCommand=false HOST CMD`, where
CMD is one `shlex.join` of a literal argv. The check and the dial are separate
processes: if the master dies in between, or refuses the session, OpenSSH would
fall back to a fresh login. `-oProxyCommand=false` turns that fallback into an
immediate failure without changing the control path, so `shk` cannot cause an
authentication attempt, let alone a storm.

Results map to `complete`, `failed` (a read exited nonzero), `unknown` (a
mutation exited nonzero or timed out after dispatch: never retried),
`auth_required` or `unavailable` (ssh exit 255 with or without authentication
text, such as a dropped network) and `timeout` (a read); these exit 4 with the
hint `run shk doctor`. The Sherlock login banner leaves the parsed output and
goes to stderr once per process, since site rule 12 forbids filtering out
instruction reminders. A timeout or
interrupt kills the child's whole process group. The local executor runs the
same programs with `sh -c` and no preflight; the data executor on any Sherlock
host refuses (exit 3), because transfers start outside Sherlock and the site
forbids ssh to a DTN from Sherlock.

## Job names

```text
shk.<project>.<slug(task)[:20]>.<sha256(canonical([project, campaign, task]))[:10]>
```

At most 60 characters of `[a-z0-9.-]`. Retries reuse the name; the newest base
job id under a name is the current job and older ones are history. The `.`
separator makes `shk.<project>.` an unambiguous prefix for `status --all`.

## Submit

1. **Offline checks.** The partition is listed in `partitions.json` and takes
   batch jobs (`batch: false` for `dev`, where Sherlock rejects every sbatch
   job; `occupancy` and `resources` still read it); GPUs only
   where the profile allows them; requeue only where the profile allows it
   (today `owners`), else `--no-requeue`. A `--partition` value with a comma
   is a partition list, Slurm's own syntax (`gpu,bioe,stat`), on `submit` only:
   `resources` and `occupancy` take one name per flag and refuse a comma as
   exit 3 `unknown_partition`, and a repeated `--partition` flag is not a list
   (argparse keeps the last one). A value without a comma takes the
   single-partition path. The members of a list are checked where the
   single partition's profile is looked up and `batch` tested, before the
   resource checks, in this order: an unlisted member, the empty one included
   (exit 3 `unknown_partition`, the usual message for the first such member);
   a repeated member (exit 1 `spec_invalid`); a member without batch jobs
   (exit 3 `batch_not_allowed`, the usual message for the first such member);
   an incompatible set (exit 3 `partitions_incompatible`). Compatibility is
   derived from the existing profile fields alone: no member `borrowed`, one
   common `requeue` value and one common `preemptible` value. With a borrowed
   member the message names every borrowed member; otherwise it lists every
   member with both values, and the hint says to submit incompatible
   partitions separately. The resource checks then apply to the set: `--gpus`
   above 0 needs `gpus_allowed` on every member (`gpus_not_allowed` names
   every member without GPUs), requeue defaults to the common profile value,
   and `--requeue` on a set that does not allow it is `requeue_not_allowed`,
   naming the set as given. So `gpu,bioe,stat` combines and `owners`,
   `btrippe` and `possu` stay alone; `normal`, `bigmem` and `service` are in
   the class too. A set states that the job may run on any member, and every
   member's courtesy text applies wherever it starts. A list is explicit at
   submission only, never a TOML key. The other checks: the `--mem`, `--time` and `--array`
   grammar, at least one CPU and task; a script of at most 1 MiB whose leading
   `#SBATCH` lines do not set a directive `shk` owns (`-A`/`--account`,
   `-x`/`--exclude`, `-G`/`--gpus*`/`--gres`, `-p`/`--partition`,
   `-a`/`--array`, `-d`/`--dependency`, `-b`/`--begin`, `-J`/`--job-name`,
   `-H`/`--hold`, and `--comment` when a submission token sets Comment) or
   start a heterogeneous job (`hetjob`, `packjob`); a borrowed partition needs
   `--courtesy-ack`. The denylist follows sbatch's getopt parsing: abbreviated
   long options (`--part`) and short flags bundled after value-less ones
   (`-Hp`) count. `--constraint`, `--prefer` and `--signal` go to sbatch
   unchanged, which checks them itself; one that is given but empty or
   whitespace-only is refused here (exit 1 `spec_invalid`, for example
   `--constraint must not be empty`), while an omitted one leaves the frozen
   spec and argv as they are. Each `--after-ok` value (repeatable) must be one
   job id, `N` or `N_TASK`, because `shk` joins them into its own `--dependency`
   string, where a `,` or `?` would add clauses. Build the sbatch argv
   (explicit name, partition, CPUs, memory, minutes, run directory, log paths,
   `--export=ALL,SHK_NAME=<name>`, `singleton` for single jobs; a submission
   token adds `,SHK_SUBMISSION_TOKEN=<token>` to the export and
   `--comment=<token>`, and `--hold` is accepted only with a token) and the
   frozen spec. A partition list reaches the argv as
   `--partition=<value as given>` in the same position, and the argv is
   otherwise that of a single partition; the job name does not depend on the
   partition. The frozen
   spec keeps the value as given in `resources.partition` and adds the list as
   `resources.partitions`, only for more than one member; in the `spec` that
   `--dry-run` and `--preflight` print it is the last key of `resources` (after
   `prefer` when both are present), and the staged file has sorted keys. A
   single partition leaves the frozen spec and argv as they are. An explicit `--prefer` is also recorded in `resources.prefer`;
   omitting it leaves the resource document and argv unchanged. Slurm tries
   preferred features independently before the fallback constraint; callers must
   include every hard requirement in both. With `--cpu-thread-budget` or the
   project's `cpu_thread_budget`, `cpu_threads.py` puts an
   `env NAME=VALUE ...` prefix in front of the sbatch argv and records
   `cpu_thread_policy` in the spec; without either, both are unchanged (see
   [CPU thread budgets](cpu-threads.md)). `--dry-run` stops here, with no
   network at all.

2. **Read call** (`query.sh`, shared with `status`): one `squeue -t all` and one
   `sacct --duplicates` over the last 30 days for the user and name, plus the
   newest spec file; for a borrowed partition also its occupancy (never for a
   partition list, which has no borrowed member). The read, the live and
   duplicate guards, `singleton`, the staging and the exit 5 handling are the
   same for a list as for one partition. Only
   `SHK_`-prefixed lines count, and the read is complete only with `SHK_END`
   and every tool returning 0 (else exit 1 `malformed_read`). Every remote
   program that calls a Slurm client first unsets `SBATCH_*`, `SQUEUE_*`,
   `SACCT_*` and `SLURM_*` (except `SLURM_CONF`); `doctor.sh` and
   `manifest.sh` call none. A reply of any remote program is a sequence of
   records separated by LF only: the `SHK_SPEC` payload, the one file content
   embedded inside a single record, has its LF bytes flattened to spaces on the
   remote side, and every local parser splits a reply on LF alone, never with
   `str.splitlines()`, so no other separator inside a field (CR, VT, FF, FS,
   GS, RS or a Unicode line separator) can start a record or forge a marker.

3. **Local decision** (pure): `new` (no rows) submits; `live` (any task pending,
   running, suspended or completing, including throttle-held array tasks that
   `squeue` reports only as an aggregate) is refused even with `--resubmit`;
   `done` and `failed` are refused unless `--resubmit REASON`; `stale` (spec files
   but no Slurm rows) is informational and treated as `new`. On a borrowed
   partition (`btrippe`, `possu`) the reply carries `occupancy`, the
   partition's `courtesy` text, `request = {tasks, throttle, minutes}` (`tasks`
   is 1 for a single job and the array count otherwise, `throttle` is the `%M`
   limit or null) and `warnings`. The budget warning appears when others are
   running there and you already have 2 jobs there or ask for more than 2 h.
   It quotes "about 2 jobs x 2 h" as the courtesy budget only when the
   partition's own courtesy text contains that phrase; otherwise it names the
   advisory threshold and points at the `courtesy` text in the same reply. A
   total-work advisory appears when others are running there and `tasks` is
   greater than 2: it states the number of array tasks, the throttle (or that
   there is none) and the minutes, and does not call the request within or
   over any budget. Both are advisory and nothing is refused. The courtesy
   terms are the profile texts alone: `btrippe` has no blanket daytime
   prohibition, and `possu` keeps its 00:00-07:00 Pacific window.

4. **Mutation call** (`submit.sh`, the script on stdin): a live guard, staging of
   the script and spec as `<run-dir>/.shk/NAME.STAMP.sh` and `.json`, a digest
   check, `sbatch` on the staged script with its stderr in `NAME.STAMP.err`,
   then exactly one terminal marker:

   - `SHK_LIVE` (token `live`) or `SHK_REFUSED <why>` (token `remote_refused`):
     a guard stopped before `sbatch` ran, so nothing was dispatched; exit 3.
   - `SHK_SUBMITTED <jobid>` (exit 0, action `submitted`): the stdout of
     `sbatch` is, trailing newlines aside, one line holding a strict job id,
     `N` or `N;CLUSTER`, where N is a positive decimal integer without a
     leading zero and CLUSTER is a non-empty run of letters, digits,
     underscore, dot or dash. `submit.sh` sets `LC_ALL=C` once `sbatch` has
     returned, so both are matched as ASCII bytes whatever the login locale
     (digits or letters of another script are never an id). The id reaches
     the client only in the controller's success response, so it is positive
     evidence of acceptance whatever exit code `sbatch` returned
     (`sbatch --wait` returns the job's exit code). The staged files are
     renamed to the job id. Nothing else is ever read as a job id.
   - `SHK_UNPARSED`, with the `sbatch` exit code and Slurm's stderr as
     evidence: every other result once `sbatch` has started, whether it exited
     0 or not. The outcome is unknown, exit 5, with the reply
     `{ok: false, action: unknown, name, detail, next}`; `detail` holds
     `sbatch exit RC` followed by the stderr text when there is any, and the
     staged `.sh`, `.json` and `.err` are all kept as evidence.

   No stderr text is classified as a definitive refusal: a nonzero `sbatch`
   can follow an accepted submission
   ([source-confirmed](validation/observed-slurm.md#not-observed-live)), so a
   remembered error string proves nothing. `shk submit --preflight`
   (`sbatch --test-only`) is the way to get a definitive Slurm verdict
   (exit 3) without creating a job. A transport failure after dispatch or a
   missing or duplicate marker is exit 5 as well. In every exit 5 case the
   reply's `next` names the `shk status` command and the time before which not
   to run it (60 s later); settle the outcome with that status read before
   considering another submit. A name whose submission never landed then reads
   `stale` (spec files, no Slurm rows), which submits like `new`, when it has
   no earlier job in the 30-day window that submit reads. When it has one, status still reads
   that job's `done` or `failed` state and `current.job_id`, with `spec_files`
   one higher, and the name still needs `--resubmit`. The attempt landed only
   if `current.job_id` is newer than the one seen before the submit. A
   deliberate rerun must follow the duplicate and resubmission guards above;
   the check-then-submit race described below prevents an exactly-once
   guarantee.

## Status, cancel and occupancy

`status` batches every selected name into one read call. Per name it reports the
state, the current job (partition, array size, task counts by state, exit codes,
restarts, CPU and GPU seconds) and the history, plus `queried_at` and
`next_check_after` (now + 60 s). Anomalies are evaluated on the current job only:
`unexpected_preemption` (an interrupted attempt, that is a `PREEMPTED`/`REQUEUED`
row or an attempt replaced by a later restart, on a partition whose profile is not
preemptible, or on a comma list whose members are all listed and none
preemptible, since a compatible set shares one value), `duplicate_live`,
`unknown_partition` (some row of the job has a partition text, split on `,`,
with a member that is not listed; the verdict does not depend on the row
order), `malformed_rows`; any of them
gives exit 6. `duplicate_live` and `unknown_partition` apply to `shk.` names only,
and `--job-id` entries are per base job. Nothing is acknowledged or stored,
so nothing needs closing: a deliberate `--resubmit` makes a new current job.

Status reports what Slurm prints. `current.partition` is the partition text of
the first row Slurm printed for the job, queue rows before accounting rows, in
Slurm's order, which `shk` does not fix. The rows of a started job do not show
that it was submitted to a list: clients get the run partition for a job that
is not pending and the list only while it pends, and a job that
ended without starting shows one member in `squeue` and keeps the list in
`sacct` ([source-confirmed](validation/observed-slurm.md#jobs-submitted-to-several-partitions)).
For an array, or a requeued job read from accounting only, the text is one
row's, can change between reads and says nothing about the other tasks or
attempts. Whether a job used a list is therefore decided by its frozen spec:
`current.ran_on`, directly after `partition`, appears only when status read the
spec of that job (the newest spec file of the name, for that job id, as for the
task count) and its `resources.partitions` lists several partitions, that is
`status --task` with a run directory, plain or with `--details`. Its value is
the sorted distinct partitions of the job's started rows, `[]` while no row
shows a start. A started row is an explicit row (not an `N_[...]` aggregate) whose
partition text is one name and which shows a start: an accounting row that is
not `PENDING` and has AllocCPUS above 0, or a queue row whose state is in
`slurm.RUNNING`
(`RUNNING`, `COMPLETING`, `SUSPENDED`, `CONFIGURING`, `STAGE_OUT`,
`SIGNALING`, `RESIZING`, `STOPPED`). Without a spec (`--all`, `--name`,
`--job-id`) there is no `ran_on`: `current.partition` is the first row's text,
which for an array whose tasks started on different members names only one of
them or the pending list. The per-row partition of `status --details`, or
`--task`, tells where each ran. The cost arithmetic is the same for a list, and
every detail row keeps its partition text as Slurm printed it.

`cancel` resolves one name and cancels only its live jobs (`scancel` scoped by
user and name), reporting the job ids and `next_check_after`. A guard that
stops before `scancel` runs is exit 3; any nonzero `scancel` exit is outcome
unknown, exit 5 with `scancel exit RC` and the stderr text in `detail`, and so
is a lost reply, each with a `next` like submit's. `occupancy` reads the
partition's current use with the fixed-width `squeue -O` layout
([verified live with 0.2.0](validation/observed-slurm.md#occupancy)).

## Controller primitives

Every operational JSON document, errors included, carries `schema_version: 1`;
text output (help, `rules`, `--version` and usage errors) carries none.
`details.py` keeps full accounting identities (Cluster, DBIndex, physical
JobIDRaw, Start/End and Restarts) and keeps the raw rows alongside the summaries
derived from them. `status --job-id` selects current
UID jobs including legacy names and reports one entry per base job; full queued
`scontrol` request facts (read once per base job, times in UTC) support held job
verification. A failed queue or accounting read is exit 1; a failed control read
only marks the evidence incomplete. Partial evidence is explicit and cannot claim
final cost.

`node_resources.py` subtracts Slurm allocations from configured capacity; OS
FreeMem does not drive admission. `resources.sh` keeps only nodes whose
`Partitions=` list names a wanted partition, `schedulable` comes from the State
flags while `free_*` stays configured minus allocated, and a failed `scontrol`
read is exit 1. `resources --summary` is a separate output mode of that same
single read: the reply replaces `nodes` with `summary`, and without
`--summary` it is the per-node reply. The summary puts every shipped node in
exactly one class (countable: all facts known and schedulable; unschedulable: all
facts known and not schedulable; unknown: any fact missing, a malformed line
included) and sums groups for the union, for each requested partition and for
the `--by sku` and `--by class` breakdowns of the `GPU_SKU:` and `CLASS:`
features, with optional `--shape` slot counts
([README](../README.md#resource-summary)). Unknown stays unknown: a sum with
nothing known behind it is null, never 0. The reply carries its rules as four
fixed texts and ranks nothing. `--by` and `--shape` need `--summary`; they and
the shapes are checked offline, after the partitions and before any remote
call. `control.py` implements read-only `sbatch --test-only` (a
controller transport failure is a retryable exit 1, not a refusal), exact
identity-verified user-hold release and local allocation-only self requeue: PID
membership through `listpids` and `show job` both use the physical
`SLURM_JOB_ID`, the job's own record is kept out of any sibling records, and an
array element is requeued as that record's `ArrayJobId_ArrayTaskId` on a
partition whose profile allows requeue. After a mutation is
issued an ambiguous reply stays unknown, with no retry. Slurm identity reads
and mutations are not atomic; no exactly-once guarantee is claimed. The public
submission token is correlation data stored in Slurm Comment, the job's
`SHK_SUBMISSION_TOKEN` and the frozen spec, not a credential; a held job needs
one, because release verifies it. No project ledger, checkpoint or retry
policy enters the kit.

The `--preflight` reply adds `estimate` directly after `preflight`; the
`preflight` object, the exit codes (0 accepted, 3 rejected by Slurm, 1
`malformed_read`) and `preflight.sh` are those of a plain preflight.
`control.py` splits `stderr`, then `stdout`, on LF only, and an estimate line
is a line that matches this pattern from its first character to its last:

```text
(?:sbatch: )?Job [0-9]+ to start at (?P<start>\S+)(?: on cluster \S+)?(?: a)? using [0-9]+ processors on nodes (?P<nodes>\S+) in partition (?P<partition>\S+)
```

`status` is `parsed` when there is exactly one estimate line and its `start`
also matches `[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}`; `start`,
`nodes`, `partition` and `line` are then set, and lines that merely resemble an
estimate do not change this. `absent` means no estimate line and no line
holding the words `to start at` between spaces, with those four null; it
covers an accepted call without an estimate and a rejected call. `unparsed`
is every other case (several estimate lines, an estimate line whose time is
not an ISO time, or no estimate line while some line holds the words, as for
an unknown shape or an empty node list): `start`, `nodes` and `partition` are
null and `line` is the first estimate line, else the first line holding the
words. `start` is the text Slurm printed, the local time of the `sbatch`
process on the executor host without a zone, which the constant
`start_zone: "sbatch-local"` names; `observed_at` is UTC and equals
`queried_at`. The kit converts nothing and guesses nothing, and no key of
`estimate` holds the job id of the line, which belongs to a temporary record
([source-confirmed](validation/observed-slurm.md#test-only-estimates)).

## Transfer

`push` checks the DTN master, then runs `rsync -rlt --protect-args --partial`
into an absolute REMOTE under `remote_root` (unless `--anywhere`) and never
deletes. `-t` keeps the local modification times: a re-push compares size and
time and skips unchanged files, where rewriting them would refresh their purge
clock on scratch, which looks like purge evasion. The cost is stated plainly:
pushed files keep their local modification times, so inputs that are old
locally can be purge-eligible soon after a push. With plain `--partial`, a
push that fails or times out can leave a truncated file under its final name,
also where a complete older copy was: rerun the push to exit 0 before any job
reads REMOTE.

`pull` reads `REMOTE/SHA256SUMS` (GNU `sha256sum` format) through the control
host and needs it to list every regular file under REMOTE except `SHA256SUMS`
and `.shk*` entries, so the job writes it over its own output subdirectory, not
the run directory with its `.shk/` and logs. `manifest.sh` counts those files
with one `find` and keeps its exit status: an enumeration error (an unreadable
subdirectory, for example) makes the program exit 1, which `pull` reports as
exit 1 `remote_failed` with `find`'s message, and nothing is promoted. `pull`
refuses unsafe manifest entries
(absolute paths, empty, `.` or `..` components, `.shk` components, duplicates).
With NAME the last component of LOCAL, it takes a lock on `.NAME.shk-lock`,
rsyncs exactly the listed files into the private stage `.NAME.shk-stage`
(created 0700) beside LOCAL, pruning anything the manifest does not list so a
rerun resumes it, and uses receiver-local `--chmod=Du+rwx` to keep incoming
directories writable when a readonly source spans multiple file-list batches.
Before pruning it gives owner rwx back only to real stage directories that lack
it (left by an interrupted pull of a readonly source); an ordinary pull issues
no chmod, so filesystems that refuse chmod (CIFS) work, and a needed repair that
fails is exit 1 `stage_unwritable`. Because the stage is promoted onto LOCAL, a
stage that already exists and has a directory another user owns (planted in a
shared parent) is refused with exit 3 `stage_untrusted`; a stage shk has just
created is not checked, so on an id-mapped filesystem that shows another owner a
refused resume works again once the stage is removed. File permissions are unchanged, a symlinked
stage root is refused and a child symlink target is never followed or
chmodded. It verifies the inventory and every hash, re-reads the manifest, then
runs the validator named by `--validate`, if any: its argv from
`[tool.sherlock-kit.validators]` plus the stage directory as the last argument,
from the project root (the directory of the configuration file); exit 0 passes,
and the validator treats the stage as read-only because whatever it writes is
promoted. Validators are commands, so they run only from a configuration file the
current user owns (otherwise exit 3 `validator_untrusted`); discovery itself walks up
freely so group-owned project trees on Sherlock keep working. Then it renames the stage onto LOCAL and writes
`.NAME.shk-receipt.json`. An existing LOCAL with a receipt for the same manifest
is `already_present`: the receipt beside LOCAL names this manifest, and LOCAL
itself is not verified again. Any other existing LOCAL is kept unless
`--replace`, which renames it to `.NAME.shk-old-<UTC>` (never deletes). `manifest DIR`
writes `DIR/SHA256SUMS` inside jobs; the skill's `sha256sum` one-liner writes
the same.

The pull contract is payload-only:

- The stage, and so LOCAL, holds exactly the regular files that
  `REMOTE/SHA256SUMS` lists, and the directories they imply. `SHA256SUMS`
  itself is not staged and not promoted; the receipt keeps the sha256 of the
  `SHA256SUMS` text that pull read (`manifest_sha256`).
- A manifest lists files only, so an empty remote directory is not created
  locally. A validator must not require one, and must not create one.
- The validator gets the stage as its last argument and treats it as
  read-only: nothing re-verifies the stage after the validator, and whatever
  it writes is promoted with the data. The kit does not enforce this.
- The `pulled` and `already_present` replies end with the constant
  `payload_contract`:
  `{"name": "payload-only", "version": 1, "verified_against": "SHA256SUMS", "manifest_in_payload": false, "empty_directories_preserved": false, "validator_must_not_write_stage": true}`.
  It describes what a pull promotes; `validator_must_not_write_stage` states
  the validator's obligation. On `already_present` LOCAL is not verified
  again: the pull reads `SHA256SUMS` once more, compares its digest with the
  receipt, reads no file of LOCAL and runs no validator, so that reply rests on
  the receipt of the earlier pull.

## Weaker guarantees, accepted

- Deduplication uses only `squeue -t all` and `sacct` over the last 30 days
  (`status` takes `--since`, 1 to 31 days: Sherlock's slurmdbd refuses wider windows). Spec files are reported as `stale` but never block:
  a task with no Slurm rows in that window is submitted again as new, without
  `--resubmit`, even when its run directory still exists, and the new job runs
  in the same run directory.
- Sherlock's windowed `sacct` omits jobs that were never eligible
  ([observed live](validation/observed-slurm.md#accounting)): a job held until
  it was cancelled leaves no row for a name query, so once the controller
  purges it the name reads as `new` or `stale`.
  `sacct --jobs` still finds it. Sherlock also refuses `sacct` windows much
  wider than 30 days (`Too wide of a date range in query` at 45 days) and keeps
  no job Comment in accounting, so a submission token is visible as a Comment
  only while the job is in the controller; the frozen spec keeps it, and so does
  the control record's `SubmitLine` (the sbatch command line).
- Check-then-submit is a race: two submitters can both pass the guard. For a
  single job `singleton` keeps the two from running at once; arrays have no
  backstop; `status` flags `duplicate_live`. There is no exactly-once claim.
- `--all` lists names by prefix within a time window; there is no ledger of
  everything ever submitted.
- Cost is informational: no budget, cap or reservation.
- The 60 s cadence is advisory (`next_check_after`), enforced by the rules, not
  by a file.
- A master can die between the check and the dial; `-oProxyCommand=false` makes
  that dial fail instead of logging in, at most once per command.
- The remote side trusts `sh`, `sha256sum` and the Slurm clients (`squeue`,
  `sacct`, `sbatch`, `scancel`, `scontrol`) on the login shell's `PATH`.
- Provenance lives in the run directory and is purged with scratch.
- Reply records are separated by LF only: the `SHK_SPEC` payload is flattened
  remotely, but an LF inside a job name or comment that `squeue`, `sacct` or
  `scontrol` prints raw is Slurm's own output and still starts a new line.
- `shk` bounds its children with its own timeouts and kills their process
  group on a timeout or interrupt, but it installs no handler for SIGTERM or
  SIGHUP. Do not wrap it in an outer timeout shorter than its own: if `shk`
  itself is killed, a mutation in flight is outcome unknown without an exit 5
  reply, and a transfer may still be running.
- `already_present` trusts the pull receipt, and a failed push can leave a
  truncated remote file (see Transfer). Nothing re-verifies the pull stage
  after the validator, and the kit does not enforce that a validator leaves it
  unwritten.
- `resources --summary` is a snapshot heuristic at `queried_at`: configured
  minus allocated capacity. It does not model fairshare, QOS or partition
  limits, reservations, preemption or pending work, reserves nothing and
  predicts no start time. Beside a nonzero `unknown_nodes` its numbers are
  lower bounds.
- The `--preflight` estimate is advisory: not a reservation and not a promise,
  and its start is the local time of the `sbatch` process, which the kit does
  not convert. For a request with several partitions Slurm names the first
  partition, in its own priority order, that can run the job, and estimates
  the start there: it is not the earliest start over the set, and the named
  partition can be the Alternate of a listed one
  ([source-confirmed](validation/observed-slurm.md#test-only-estimates)).
  Comparing partitions takes one preflight per partition.
- The partition text of a job submitted with a partition list is what Slurm
  prints: the list while it pends, in an order Slurm may change, and the run
  partition once it has started; one member in `squeue` and the list in
  `sacct` for a job that ended without starting; one row's text for an array
  or a requeued job read from accounting only. `current.partition` therefore
  does not say where every task ran.
- `current.ran_on` needs the frozen spec: it appears only for `status --task`,
  for a job whose spec lists several partitions. `--all`, `--name` and
  `--job-id` read no spec and show Slurm's partition text alone, so they
  cannot tell that a started job was submitted to a list. A job that finished
  before accounting recorded its start reads `[]` on that read, with
  `cost.final` false: a finished queue row alone cannot be told from one
  cancelled while pending.
- Sherlock's acceptance of a partition list, and what `squeue` and `sacct`
  print for one, are source-confirmed and
  [not observed live](validation/observed-slurm.md#not-observed-live); a
  `--preflight` of the list is the first check.
- A job submitted with a partition list must be read with a kit version that
  knows lists: an older one reports `unknown_partition` (exit 6) while Slurm
  prints the list.

## Rules, skill and delivery

The rules files and the skill are package data at fixed paths (contracts C1-C3,
with `bin/shk` as C4). Package consumers may reference these paths directly.
The rules start with an H2 and carry no front matter, HTML comments or imports,
so consumers can load them as instructions. Projects copy the skill with
`shk install-skill`, which follows links inside the project (a shared
`.agents/skills -> ../.claude/skills`) and refuses one that leads out of it, so
it never writes through a link into `HOME` or another source checkout. When
`~/.claude/rules/sherlock-kit.md` exists, Claude Code already loads it, so
`install-skill --rules` skips the project rules copy unless `--force` is given;
the check needs no global `shk` executable.
Codex has no rules import: with `--rules` it gets a hint to append `shk rules KIND` to the
project's `AGENTS.md`. The partition block in both rules files is generated by
`partitions.markdown()` and a test keeps it byte-identical to the table.
