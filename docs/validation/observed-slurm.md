# Observed Sherlock and Slurm behavior

Facts seen live on Sherlock while piloting 0.1.0 to 0.3.0 (accepted 2026-10-08,
2026-10-09 and 2026-10-10 UTC), which the 0.4.0 design relies on, followed by the
0.4.0 smoke and the 0.5.0 verification. They are observations from
one user's account on specific days, not site guarantees; re-check a fact
before building on it. Private configuration, paths, specs, receipts and
bundles from those pilots are not public and are not reproduced here.

## Hosts and shells

- Login nodes are named like `sh02-ln04.stanford.edu`: the domain is
  `stanford.edu`, not `sherlock.stanford.edu`, so detection uses the short
  name (`^sh\d+-ln\d+$`).
- Every ssh session prints one banner line on stderr: "Automated tools: read
  and follow /etc/agents/AGENTS.md before doing anything else on Sherlock."
  Remote output parsing must ignore it.
- A non-login ssh shell (what `ssh sherlock-plain CMD` gets) has no Lmod:
  `module` and `ml` are undefined and `MODULEPATH` is empty, and `sh_part` and
  `sh_node_feat` are not on `PATH`. Use `bash -lc '...'` interactively and
  `#!/bin/bash -l` in job scripts. `module load math devel python/3.14.2` gave a
  working Python 3.14.
- Slurm 25.11.8: `MaxArraySize` 1,000,000, `max_array_tasks` 1000 per array,
  1000 job submissions per user per hour. Each argv element is limited to
  128 KiB (`MAX_ARG_STRLEN`), which is why long remote programs ship as fixed
  `sh` files with plain quoting instead of large quoted strings.
- `$HOME` and `$GROUP_HOME` are NFSv4, `$SCRATCH` is Lustre. Group directories
  carry the setgid bit (`drwx--S---`), so a privacy check must test
  `mode & 0o077`, never `mode == 0o700`.
- `sh_part` in a login shell listed exactly these usable partitions on
  2026-10-10: public `normal`, `bigmem`, `gpu`, `dev`, `service`; group `stat`,
  `bioe`, `btrippe`, `possu`; and `owners`.

## sbatch, squeue and sacct

- `sbatch --parsable` prints `N` or `N;cluster`.
- An array submitted with `--array=0-3%2` (0.3.0 pilot, job 47245971, 4 tasks of
  12 minutes on `normal`): while two tasks ran, the two tasks held by the
  throttle had **no sacct row at all** (no `N_[2-3]` aggregate) and appeared in
  `squeue` only as `N_[2-3%2] PENDING JobArrayTaskLimit`. 0.4.0 counts them as
  pending from the `squeue` aggregate.
- Started array tasks are sacct rows `N_k` with distinct `JobIDRaw`; the **last
  task to leave the array keeps the array id as its raw id** (raw ids 47245981,
  47245982, 47246202 and 47245971 for tasks 0-3). Group by the base id before
  `_`, never by `JobIDRaw`.
- `sacct --duplicates` gives one row per requeue: the same `JobID`, distinct
  `DBIndex`, `Restarts` 0, 1, ... (0.2.0 owners job 47099011: `REQUEUED`
  `Restarts=0` `ElapsedRaw=914`, then `COMPLETED` `Restarts=1` `ElapsedRaw=363`).
- sacct ANDs its filters: never combine `--jobs` and `--name` in one query.
- Array task CPU cost summed per task: 738 + 738 + 728 + 728 = 2,932 CPU-seconds
  for the 4-task pilot, complete only once every task was terminal.
- Queue waits: `normal` CPU array tasks started within about 2 minutes; `owners`
  GPU jobs waited 29 and 69 minutes in the 0.2.0 pilot.

## Requeue and preemption

- `scontrol requeue` (0.2.0 owners drill, issued once after fifteen 60 s
  checkpoints) reported `CANCELLED ... DUE TO JOB REQUEUE` two seconds later.
  Neither the batch shell nor the Python workload saw a SIGTERM window, and the
  final checkpoint write never happened: the restart resumed from the last
  periodic checkpoint and recomputed under a minute of work. Jobs that may be
  requeued must checkpoint periodically, not rely on signal handlers.
- Natural preemption by a node owner and an unexpected preemption on a
  non-preemptible partition were never observed live.

## Occupancy

- `squeue -O` with fixed-width fields parsed reliably for per-user use of a
  partition (0.2.0, `btrippe`): one other user with 2 GPUs running, nothing
  pending.

## Transfers through the DTN

- `sherlock-dtn` accepts plain `rsync -rt --exclude=...` over the control
  master. Options that change the remote command line were rejected:
  `--itemize-changes` failed with `invalid characters in scp command! here:%i ...`
  (the `%i` is the `--log-format=%i` that rsync adds to the remote server
  command for it on a push, where the DTN is the receiver). The pilot tried
  `--chmod` in the same run and the note listed it too, but rsync 3.2.7
  `--debug=CMD2` shows the same server command and protected args with and
  without `--chmod` in both directions, and with and without
  `--itemize-changes` on a pull; 0.5.0 pulls with `--chmod=Du+rwx` worked
  through the DTN on 2026-10-18.
- The DTN has no interactive shell; fix permissions afterwards from a login
  node.
- Manifest-checked pulls of 4 to 6 files (1.5 KB to 25 MB) through the DTN into a
  private stage, with a validator and an atomic rename, worked end to end in all
  three pilots.

## Workload pattern that worked

`#!/bin/bash -l`, no `#SBATCH` lines, `module load ...`, the toolkit wheel
unzipped into `$L_SCRATCH_JOB` and put on `PYTHONPATH`, thread variables set
to 1, results written into the run directory and a manifest written beside the
results as the last step. Pilot workloads were synthetic (fuzzing the toolkit's
own state resolver, fp16 matrix products with checkpoints); no research code or
data was used, and no result here is a scientific result.

## 0.4.0 live smoke (2026-10-11)

CPU only on `normal`, from lab-ubuntu through the existing control and data
masters, with `bin/shk` from the release candidate and a scratch project
(`remote_root` on `$SCRATCH`, removed afterwards). The workload was a 4-task array
with throttle 2, 1 CPU, 2 GB and a 20-minute limit; each task fuzzed the toolkit's
own `squeue`/`sacct` row parser and name resolution for 690 CPU seconds
(about 3.97 million cases in total, no failures) and wrote `SHA256SUMS` with
`python3 -m sherlock_kit manifest`.

- `shk doctor --remote` dialled once over the running master with the
  `-oProxyCommand=false` prefix and found `sbatch`, `squeue`, `sacct`,
  `sha256sum` and a readable `/etc/agents/AGENTS.md` on `sh02-ln04`.
- `shk push` of the code snapshot through `sherlock-dtn` with
  `rsync -rlt --protect-args --partial` worked.
- `shk submit` printed the `--dry-run` argv offline, then submitted once
  (`--array=0-3%2`, `--no-requeue`, `--time=20`, `--mem=2048M`, no singleton for
  an array). A second submit 100 s later and a `--resubmit REASON` 85 s after that
  were both refused with exit 3 `live`.
- `shk status` at 10:35, 10:44 and 10:58 UTC (minutes apart): first 2 running and
  the 2 throttle-held tasks counted as pending with none missing; then 2 completed
  and 2 pending; then `done` with 4 `COMPLETED`, exit codes `0:0` and
  `cpu_seconds` 2833 marked final. No anomaly, no restart.
- Array tasks ran on compute nodes named `sh02-02n42.int` and `sh02-02n48.int`
  with the `python/3.14.2` module loaded by `#!/bin/bash -l`.
- `shk pull --validate` of each task's output directory read `SHA256SUMS` through
  the control host, fetched with `--files-from=-` through `sherlock-dtn` (accepted),
  verified, ran the project validator from the project root and promoted with a
  receipt. Pulling task 0 again returned `already_present`.
- After `done`, a plain `shk submit` was refused with exit 3 `done` and the hint
  to add `--resubmit REASON` once the cause is known.

## 0.5.0 live verification (2026-10-18)

Slurm 25.11.8, `shk` at the 0.5.0 candidate (commit 949f43b), CPU only: from
lab-ubuntu through the existing masters, and inside a 2-task array on `owners`
with `--no-requeue` whose tasks shared one node (local executor). Job 48350361
was that array, submitted held and released; 48350370 (single) and 48350374
(2-task array) were submitted held and cancelled while held. User names, UIDs
and paths are omitted below; `<token>` and `<uid>` stand for the values seen.

### Submission, holds and the dev partition

- `submit --preflight` (`sbatch --test-only`) created no job and printed its
  estimate on stderr in site-local time:
  `sbatch: Job 48350340 to start at 2026-10-22T13:24:32 a using 1 processors on nodes sh02-01n56 in partition normal`.
- `dev` rejects every batch job: `sbatch` exits 1 with
  `Batch jobs are not allowed in the 'dev' partition, which is reserved for interactive sessions (salloc/srun/sdev and OnDemand)`
  and `allocation failure: Invalid partition name specified`. 0.5.0 refuses
  `dev` offline (exit 3 `batch_not_allowed`).
- A held job reads `JobState=PENDING Reason=JobHeldUser Priority=0 Comment=<token>`
  in `scontrol`. `release` with a wrong token gave exit 3
  `control: SHK_REFUSED token`; with the right one exit 0.
- After the release the pending array split at once: task 0 became job
  48350477 and task 1 kept 48350361, the meta record (compare the 0.3.0 note
  that the last task to leave the array keeps the array id). Both started
  about two minutes after the release.
- `SHK_NAME` reached the job; no token variable did (0.5.0 now exports
  `SHK_SUBMISSION_TOKEN` when a token is given).

### Never-allocated jobs and accounting

- A held pending job in `sacct --jobs`, with the `status --details` fields:
  `48350370|48350370|sherlock|<dbindex>|PENDING|normal|0|0||2026-10-18T17:34:05|Unknown|Unknown|0|0:0|None assigned|<uid>||<name>`.
  A held pending array is one aggregate row `48350374_[0-1]`, while `squeue`
  lists its tasks `48350374_0` and `48350374_1`.
- Cancelled while held, `sacct` about three minutes later:
  `48350370|48350370|sherlock|<dbindex>|CANCELLED by <uid>|normal|0|0||2026-10-18T17:34:05|None|2026-10-18T17:36:26|0|0:0|None assigned|<uid>||<name>`:
  AllocCPUS 0, ElapsedRaw 0, empty AllocTRES, Start `None`, NodeList
  `None assigned`, End set. `squeue` for the same jobs while the controller
  still held them:
  `48350374|48350374|CANCELLED|normal|0:00|1|2026-10-18T17:34:14|2026-10-18T17:36:33|2026-10-18T17:36:33||<uid>|<token>|<name>`:
  `%C` echoes the requested CPUs, `%S` and `%e` are both the cancel time and
  `%N` is empty, although nothing was allocated.
- A cancelled array that never split is one `squeue` row without a task suffix
  (`48350374`), and `scontrol` shows `ArrayJobId=48350374 ArrayTaskId=4294967294`.
- The `sacct` Comment field was empty for pending, running and cancelled jobs
  while `scontrol` and `squeue` `%k` showed the token: Sherlock's accounting
  keeps no job comment. A submission token is visible only while the job is in
  the controller, and in the frozen spec in the run directory.
- A windowed name query (`sacct --name=NAME -S now-30days -E now`) returned no
  rows for the held-then-cancelled jobs, while `--jobs=ID` with the same
  window returned them. Inference: windowed `sacct` selects by eligible time,
  and a job held until it was cancelled was never eligible. Once the controller
  purges such a job, a lookup by name sees neither `squeue` nor `sacct` rows, so
  the name reads as `new` (or `stale` with spec files).
- `sacct -S now-31days` worked; `now-45days` and `now-60days` failed with
  `sacct: error: Too wide of a date range in query` (exit 1), so a status window
  much beyond 30 days fails on Sherlock.
- `scontrol` prints site-local times (`SubmitTime=2026-10-18T10:33:37`), while
  `squeue` and `sacct` under `TZ=UTC` print `2026-10-18T17:33:37`.
- The account's history also holds legacy, non-`shk` arrays whose tasks ran on
  several partitions (`stat`, `gpu`, `bioe`) with non-contiguous observed
  indices; no legacy name contained `|`.

### Array and step identity inside the job

- Task 0 saw `SLURM_JOB_ID=48350477 SLURM_ARRAY_JOB_ID=48350361 SLURM_ARRAY_TASK_ID=0`,
  task 1 `SLURM_JOB_ID=48350361 SLURM_ARRAY_JOB_ID=48350361 SLURM_ARRAY_TASK_ID=1`.
- `scontrol show job 48350477 --oneliner` and `show job 48350361_0` both gave
  the one record `JobId=48350477 ArrayJobId=48350361 ArrayTaskId=0`.
  `show job 48350361` (the meta physical id) gave both records, task 1 first,
  as long as the sibling was in the controller; `show job 48350361_1` gave one.
  An element is therefore named `ARRAYJOBID_TASKID`; the meta physical id is
  not a unique selector.
- A 25.11.8 CPU job record carries, among others, `JobId ArrayJobId ArrayTaskId JobName UserId=name(uid) JobState Reason Dependency Requeue=0 Restarts BatchFlag SubmitTime EligibleTime AccrueTime StartTime EndTime Partition NodeList BatchHost StepMgrEnabled=Yes NumNodes NumCPUs NumTasks CPUs/Task ReqTRES=cpu=1,mem=2G,node=1,billing=1 AllocTRES=cpu=1,mem=2G,node=1,billing=1 Command SubmitLine Comment=<token>`,
  and no `Gres=` field.
- `scontrol show step 48350477.0` (physical id of the split element) prints
  `StepId=48350361_0.0`; `show step 48350361.0` prints the steps of both
  elements (`48350361_0.0` and `48350361_1.0`); `show step 48350361_1.0`
  prints exactly one. Step records carry `StepId UserId=<uid> StartTime TimeLimit State Partition NodeList Nodes=1 CPUs=1 Tasks=1 Name Network TRES=cpu=1,mem=2G,node=1 ResvPorts CPUFreqReq Dist SrunHost:Pid TresPerTask`
  (a numeric UserId), with no `Gres=`, `NumTasks=` or `NumNodes=`.
- `scontrol listpids` takes a physical id only: `listpids 48350361_0` prints
  `"48350361_0" does not look like a jobid` (exit 1), and so does an
  `A_T.S` step. With a physical id it prints the header
  `PID JOBID STEPID LOCALID GLOBALID` and rows such as `6741 48350477 batch 0 0`,
  `6957 48350477 batch - -` and `-1 48350477 extern 0 0`, or `7349 48350477 0 0 0`
  for step 0; JOBID is always the physical id. Without an argument it lists
  every job on the node, other users' jobs included.
- `scontrol pidinfo PID`, from the batch shell and from inside an `srun` step,
  prints `Slurm JobId=48350477 ends at Sun Oct 18 11:03:22 2026` and then
  `Job remaining time is 1496 seconds`: no step suffix, and the end time is
  site-local `ctime` text.
- `shk requeue --self` inside the job refused without mutating (task 0 at the
  Requeue=0 policy check). These records shaped the 0.5.0 handling of array
  elements, step proofs and duplicate `scontrol` records.

### Node resources and timing

- `resources --partition normal` read 268 nodes (454 KB of JSON),
  `--partition owners` 1665 nodes (2.9 MB). State strings seen: `MIXED`,
  `ALLOCATED`, `IDLE`, `DOWN+NOT_RESPONDING`, `DOWN+DRAIN+NOT_RESPONDING`,
  `IDLE+DRAIN`, `MIXED+DRAIN`, `ALLOCATED+NOT_RESPONDING`, `MIXED+COMPLETING`,
  `IDLE+COMPLETING+NOT_RESPONDING`, `MIXED+PLANNED`, `IDLE+PLANNED`,
  `IDLE+COMPLETING+DRAIN+NOT_RESPONDING` and `IDLE+DRAIN+NOT_RESPONDING`.
  Down, draining or unresponsive nodes (46 on `normal`, 252 on `owners`) still
  report configured-minus-allocated capacity, so free capacity counts only
  schedulable nodes.
- One `scontrol show job ID --oneliner` took 0.03 to 0.04 s on a login node.

### Transfers and the test suite

- `shk pull` of each task's output (4 files per task, 111 KB and 100 KB) through
  `sherlock-dtn` with the 0.5.0 argv, including `--chmod=Du+rwx`, verified and
  promoted.
- The offline test suite run inside the job (compute node, `SLURM_*` set,
  `TMPDIR` on node-local scratch, once per task) reached the real Slurm
  clients: 22 failures and 4 or 5 errors out of 200 tests, with no job created
  or cancelled. The suite must never fall through to the host's Slurm clients
  or inherit its job environment.

## Not observed live

Lost ssh replies, duplicate submissions racing each other, an array task
requeue, a successful `requeue --self`, natural preemption and `possu`. Those
rest on offline tests with fake `ssh` and Slurm binaries.
