# Observed Sherlock and Slurm behavior

Facts seen live on Sherlock from one user's account, grouped by topic and,
where one exists, with the `shk` behavior that depends on it. They are
observations, not site guarantees; re-check a fact before building on it.
"Verified live with 0.N.0" means that a pre-release tree of 0.N.0 ran the
behavior on Sherlock; changes made between that tree and the release, and later
releases, have not been run live. 0.5.0 has two such trees: the 0.5.0
candidate and a later pre-release tree with fixes, the 0.5.0 fix tree. Sample
records that show job ids come from Slurm 25.11.8. User names, UIDs, private
configuration, paths, specs, receipts and bundles are not reproduced here;
`<token>`, `<uid>`, `<dbindex>` and `<name>` stand for the values seen.

## How the observations were made

- The runs with 0.1.0, 0.3.0, 0.4.0 and 0.5.0 were CPU only; the 0.2.0 runs
  used GPUs on `owners` and `btrippe`. With 0.4.0 and 0.5.0, `shk` ran from a
  workstation through the user's existing control and data masters and, with
  0.5.0, also inside array jobs on `owners` with the local executor.
- Workloads were synthetic (fuzzing the toolkit's own state resolver and its
  `squeue`/`sacct` row parser, fp16 matrix products with checkpoints), and no
  result here is a scientific result.
- Two 4-task arrays on `normal`, both submitted with `--array=0-3%2`, recur
  below: the 0.3.0 array ran 12-minute tasks, and the 0.4.0 array (1 CPU, 2 GB,
  20 minutes) ran the row-parser and name-resolution fuzzer for 690 CPU seconds
  per task.
- With the 0.5.0 candidate, a 2-task `--no-requeue` array on `owners`
  (48350361) was submitted held and released and ran `shk` inside its elements,
  and a single job (48350370) and a 2-task array (48350374) were submitted held
  and cancelled while held. With the 0.5.0 fix tree, a second 2-task array on
  `owners`, submitted with `--requeue` and a token, ran `shk` inside its
  elements.
- A job pattern that worked: `#!/bin/bash -l`, no `#SBATCH` lines,
  `module load ...`, the toolkit wheel unzipped into `$L_SCRATCH_JOB` and put
  on `PYTHONPATH`, thread variables set to 1, results written into the run
  directory and a manifest written beside the results as the last step. These
  runs used that pattern; the supported way to run the kit inside jobs is a
  project-pinned environment called by absolute path
  ([installation guide](../install.md#inside-jobs-and-controllers)).

## Hosts, shells and limits

- Login nodes are named like `sh02-ln04.stanford.edu`: the domain is
  `stanford.edu`, not `sherlock.stanford.edu`, so detection uses the short
  name (`^sh\d+-ln\d+$`). Compute nodes are named like `sh02-02n42.int`; inside
  a job, detection accepts the `sh<N>-` prefix.
- Every ssh session prints one banner line on stderr: "Automated tools: read
  and follow /etc/agents/AGENTS.md before doing anything else on Sherlock."
  A parser of remote output must ignore it without hiding it from the agent:
  `shk` takes it out of the output it parses and writes each banner line to
  stderr once per process.
- A non-login ssh shell (what `ssh sherlock-plain CMD` gets) has no Lmod:
  `module` and `ml` are undefined and `MODULEPATH` is empty, and `sh_part` and
  `sh_node_feat` are not on `PATH`. Use `bash -lc '...'` interactively and
  `#!/bin/bash -l` in job scripts. `module load math devel python/3.14.2` gave a
  working Python 3.14, on compute nodes too.
- Slurm 25.11.8: `MaxArraySize` 1,000,000, `max_array_tasks` 1000 per array,
  1000 job submissions per user per hour; `shk submit --array` accepts 2 to
  1000 tasks. Each argv element is limited to 128 KiB (`MAX_ARG_STRLEN`), which
  is why long remote programs ship as fixed `sh` files with plain quoting
  instead of large quoted strings.
- `$HOME` and `$GROUP_HOME` are NFSv4, `$SCRATCH` is Lustre. Group directories
  carry the setgid bit (`drwx--S---`), so a privacy check must test
  `mode & 0o077`, never `mode == 0o700`.
- When the 0.3.0 partition profiles were written, `sh_part` in a login shell
  listed exactly these usable partitions for the observing account: public
  `normal`, `bigmem`, `gpu`, `dev`, `service`; group `stat`, `bioe`, `btrippe`,
  `possu`; and `owners`. `partitions.json` profiles each of them.
- `shk doctor --remote` dialled once over the running master with the
  `-oProxyCommand=false` prefix and found `sbatch`, `squeue`, `sacct`,
  `sha256sum` and a readable `/etc/agents/AGENTS.md` on `sh02-ln04` (verified
  live with 0.4.0).

## Submission

- `sbatch --parsable` prints `N` or `N;cluster`.
- `submit --preflight` (`sbatch --test-only`) created no job and printed its
  estimate on stderr in site-local time (verified live with the 0.5.0
  candidate):
  `sbatch: Job 48350340 to start at 2026-10-22T13:24:32 a using 1 processors on nodes sh02-01n56 in partition normal`.
- `dev` rejects every batch job: `sbatch` exits 1 with
  `Batch jobs are not allowed in the 'dev' partition, which is reserved for interactive sessions (salloc/srun/sdev and OnDemand)`
  and `allocation failure: Invalid partition name specified`. `shk submit`
  refuses `dev` offline (exit 3 `batch_not_allowed`).
- `shk submit` printed the `--dry-run` argv offline, then submitted the 0.4.0
  array once (`--array=0-3%2`, `--no-requeue`, `--time=20`, `--mem=2048M`, no
  singleton for an array). While the array was live, a second submit and a
  `--resubmit REASON` were both refused with exit 3 `live`; after `done`, a
  plain submit was refused with exit 3 `done` and the hint to add
  `--resubmit REASON` once the cause is known (verified live with 0.4.0).
- `SHK_NAME` reaches the job through `--export` (verified live with the 0.5.0
  candidate), and so does `SHK_SUBMISSION_TOKEN` when a token is given
  (verified live with the 0.5.0 fix tree).
- Queue waits: `normal` CPU array tasks started within about 2 minutes, and a
  2-task CPU array on `owners` about 2 minutes after its hold was released;
  `owners` GPU jobs waited 29 and 69 minutes.

## Holds, release and submission tokens

- A held job reads `JobState=PENDING Reason=JobHeldUser Priority=0 Comment=<token>`
  in `scontrol`. `shk release` with a wrong token gave exit 3
  `control: SHK_REFUSED token`; with the right one exit 0 (verified live with
  the 0.5.0 candidate).
- The `sacct` Comment field was empty for pending, running and cancelled jobs
  while `scontrol` and `squeue` `%k` showed the token: Sherlock's accounting
  keeps no job comment. The token is visible as a Comment only while the job is
  in the controller, so `status --details` shows it only in queue and control
  records; the frozen spec in the run directory keeps it. Slurm also records
  the sbatch command line, which carries the token, as `SubmitLine` (in control
  records, and in `sacct` where slurmdbd stores it).

## Arrays

- In the 0.3.0 array, while two tasks ran, the two tasks held by the throttle
  had **no sacct row at all** (no `N_[2-3]` aggregate) and appeared in `squeue`
  only as `N_[2-3%2] PENDING JobArrayTaskLimit`. `shk status` counts them as
  pending from the `squeue` aggregate. Polled minutes apart during the 0.4.0
  array, it reported 2 running and the 2 throttle-held tasks pending with none
  missing, then 2 completed and 2 pending, then `done` with 4 `COMPLETED`, exit
  codes `0:0` and `cpu_seconds` 2833 marked final, with no anomaly and no
  restart (verified live with 0.4.0).
- Started array tasks are sacct rows `N_k`, each with its own physical id
  (`JobIDRaw`); the **last task to leave the array keeps the array id as its
  physical id**. In the 0.3.0 array, tasks 0 to 2 got new physical ids and
  task 3 kept the array id. A 2-task array released from a hold split at once:
  task 0 got a new physical id and task 1 kept the array id, the meta record.
  Group by the base id before `_`, never by `JobIDRaw`.
- A held pending array is one `sacct` aggregate row `48350374_[0-1]`, while
  `squeue` lists its tasks `48350374_0` and `48350374_1`.
- A cancelled array that never split is one `squeue` row without a task suffix
  (`48350374`), and `scontrol` shows `ArrayJobId=48350374 ArrayTaskId=4294967294`.
- Array task CPU cost summed per task: 738 + 738 + 728 + 728 = 2,932
  CPU-seconds for the 0.3.0 array, complete only once every task was terminal.
  `shk status` marks the cost final only when `sacct` holds every task in its
  terminal state.
- The account's history also holds legacy, non-`shk` arrays whose tasks ran on
  several partitions (`stat`, `gpu`, `bioe`) with non-contiguous observed
  indices; no legacy name contained `|`. Without a spec file, `shk status`
  infers an array's task count only when the seen tasks are exactly 0 to the
  largest index.

## Accounting

- sacct ANDs its filters: never combine `--jobs` and `--name` in one query.
- `sacct --duplicates` gives one row per requeue: the same `JobID`, distinct
  `DBIndex`, `Restarts` 0, 1, ... (an `owners` job: `REQUEUED` `Restarts=0`
  `ElapsedRaw=914`, then `COMPLETED` `Restarts=1` `ElapsedRaw=363`).
- A held pending job in `sacct --jobs`, with the `status --details` fields:
  `48350370|48350370|sherlock|<dbindex>|PENDING|normal|0|0||2026-10-18T17:34:05|Unknown|Unknown|0|0:0|None assigned|<uid>||<name>`.
- Cancelled while held, `sacct` about three minutes later:
  `48350370|48350370|sherlock|<dbindex>|CANCELLED by <uid>|normal|0|0||2026-10-18T17:34:05|None|2026-10-18T17:36:26|0|0:0|None assigned|<uid>||<name>`:
  AllocCPUS 0, ElapsedRaw 0, empty AllocTRES, Start `None`, NodeList
  `None assigned`, End set. `squeue` for the same held-then-cancelled jobs
  while the controller still held them:
  `48350374|48350374|CANCELLED|normal|0:00|1|2026-10-18T17:34:14|2026-10-18T17:36:33|2026-10-18T17:36:33||<uid>|<token>|<name>`:
  `%C` echoes the requested CPUs, `%S` and `%e` are both the cancel time and
  `%N` is empty, although nothing was allocated. `status --details` therefore
  reports null queue CPUs and start for a job without nodes, and treats such
  accounting rows as complete, with a final zero cost once terminal.
- A windowed name query (`sacct --name=NAME -S now-30days -E now`) returned no
  rows for the held-then-cancelled jobs, while `--jobs=ID` with the same
  window returned them. Inference: windowed `sacct` selects by eligible time,
  and a job held until it was cancelled was never eligible. Once the controller
  purges such a job, a lookup by name sees neither `squeue` nor `sacct` rows, so
  the name reads as `new` (or `stale` with spec files).
- `sacct -S now-31days` worked; `now-45days` and `now-60days` failed with
  `sacct: error: Too wide of a date range in query` (exit 1), so a status window
  much beyond 30 days fails on Sherlock; `shk status --since` accepts 1 to 31
  days.
- `scontrol` prints site-local times (`SubmitTime=2026-10-18T10:33:37`), while
  `squeue` and `sacct` under `TZ=UTC` print `2026-10-18T17:33:37`.

## Requeue and preemption

- `scontrol requeue` on `owners`, issued once after fifteen 60 s checkpoints,
  reported `CANCELLED ... DUE TO JOB REQUEUE` two seconds later. Neither the
  batch shell nor the Python workload saw a SIGTERM window, and the final
  checkpoint write never happened: the restart resumed from the last periodic
  checkpoint and recomputed under a minute of work. Jobs that may be requeued
  must checkpoint periodically, not rely on signal handlers.
- `shk requeue --self` in an element of a `--no-requeue` array refused without
  mutating at the Requeue=0 policy check (verified live with the 0.5.0
  candidate). In a 2-task `--requeue` array on `owners` with a token, a wrong
  token refused at the token check, and the right one requeued the element
  `ARRAYJOBID_1`: accounting then held a `REQUEUED` attempt (Restarts 0) and a
  `COMPLETED` attempt (Restarts 1) for task 1 and one `COMPLETED` attempt for
  task 0, and `shk status` reported no anomaly on `owners` (verified live with
  the 0.5.0 fix tree).

## Array and step identity inside a job

The Slurm records below come from the 0.5.0 candidate's 2-task array on
`owners`, whose task 1 kept the array id 48350361 (the meta record) and whose
task 0 ran as 48350477.

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

`shk allocation` and `shk requeue --self` depend on these records: both keep
only the job's own record among its siblings and give `listpids` the physical
id; `allocation` queries an element's step as `ArrayJobId_ArrayTaskId.STEP`,
and `requeue --self` requeues an element as `ArrayJobId_ArrayTaskId` (see
[the allocation contract](../allocation.md)). In the second 2-task array,
`allocation --job-id` with the physical id and with `ArrayJobId_ArrayTaskId`
succeeded in both elements, the meta element included, and `--step-id 0`
proofs reported the steps as `ARRAYJOBID_0.0` and `ARRAYJOBID_1.0` (verified
live with the 0.5.0 fix tree).

## Node resources and timing

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

## Occupancy

- `squeue -O` with fixed-width fields parsed reliably for per-user use of a
  partition, including another user's running GPUs (verified live with 0.2.0 on
  `btrippe`). `shk occupancy` reads this layout.

## Transfers through the DTN

- `sherlock-dtn` accepts plain `rsync -rt --exclude=...` over the control
  master. An option that changes the remote command line was rejected:
  `--itemize-changes` failed with `invalid characters in scp command! here:%i ...`
  (the `%i` is the `--log-format=%i` that rsync adds to the remote server
  command for it on a push, where the DTN is the receiver). `--chmod` is not
  such an option: rsync 3.2.7 `--debug=CMD2` shows the same server command and
  protected args with and without `--chmod` in both directions, and with and
  without `--itemize-changes` on a pull.
- The DTN has no interactive shell; fix permissions afterwards from a login
  node.
- `shk push` of a code snapshot through `sherlock-dtn` with
  `rsync -rlt --protect-args --partial` worked (verified live with 0.4.0).
- `shk pull --validate` read `SHA256SUMS` through the control host, fetched the
  listed files with `--files-from=-` through `sherlock-dtn` (accepted),
  verified them, ran the project validator from the project root and promoted
  the stage with a receipt; pulling the same output again returned
  `already_present` (verified live with 0.4.0). Pulls of 4 files per task
  (111 KB and 100 KB) with the receiver-local `--chmod=Du+rwx` verified and
  promoted through the DTN (verified live with the 0.5.0 candidate).
- Manifest-checked pulls of 4 to 6 files (1.5 KB to 25 MB) through the DTN into
  a private stage, with a validator and an atomic rename, worked end to end
  (verified live with 0.1.0, 0.2.0 and 0.3.0).

## The test suite inside a job

- Inside a job `SLURM_*` is set and the real Slurm clients are on `PATH`; an
  offline suite that does not isolate itself reaches the real `squeue` and
  `scontrol` there. The suite must never fall through to the host's Slurm
  clients or inherit its job environment;
  [CONTRIBUTING.md](../../CONTRIBUTING.md#checks) describes how it stays
  hermetic.
- On compute nodes, with `TMPDIR` on node-local scratch or on `$SCRATCH`, the
  hermetic suite reached no Slurm client, and node-local scratch allowed
  execution (verified live with the 0.5.0 fix tree). Two test-side errors
  appeared there: the zombie-worker test found its killed worker already
  reaped, and a CLI started from `$SCRATCH` took longer than 20 s to answer.
  The tests' allowances for them, a skip when the worker is already reaped and
  60 s for a cold CLI start, have not run live.

## Not observed live

Lost ssh replies, duplicate submissions racing each other, natural preemption
by a node owner, an unexpected preemption on a non-preemptible partition, and
jobs on `possu`. Also not observed: `sbatch --prefer` (`shk submit --prefer`),
OR feature expressions in `--constraint` or `--prefer`, and the `env`-prefixed
`sbatch` of the CPU thread budget (`shk submit --cpu-thread-budget`). Those
rest on offline tests with fake `ssh` and Slurm binaries.

Also not observed live, and resting on the Slurm source below and on offline
tests:

- the will-run test (`shk submit --preflight`) of a comma-separated partition
  list on Sherlock;
- a real `sbatch` with a partition list, and the usability of each member;
- what `squeue` and `sacct` print as the partition of a multi-partition job
  while pending, running, finished, requeued and cancelled while pending,
  their row order for an array whose tasks run in different partitions and
  for the attempts of a requeued job, and AllocCPUS 0 before a start;
- the estimate line for a partition list or with a cluster clause;
- an accepted `--test-only` without an estimate;
- `shk resources --summary` on live node records, and the `CLASS:` feature
  prefix;
- GPU types and MIG types in the `Gres` and `CfgTRES` of a live GPU node.

Sherlock's `EnforcePartLimits` and `AccountingStorageEnforce` values, its
job_submit plugin, its partition priority tiers and its associations are not
determinable from the source. The will-run test of a partition list is
therefore an attended, read-only check:

```sh
shk submit --task multi-partition-probe --script job.sh --partition gpu,bioe,stat --cpus 1 --mem 2G --time 20m --gpus 1 --preflight
```

It runs `sbatch --test-only`: no job, no run directory, no GPU work. Exit 0
means that the request passed Slurm's creation-time checks with the list; with
a parsed estimate, the partition named in `estimate.partition` can run it, and
that is a member or the Alternate of a drained or inactive member; exit 3
keeps Slurm's reason in `preflight.stderr`. Exit 0 does not show that a
real multi-partition `sbatch` is accepted, nor that each member is usable: the
will-run branch returns before the per-partition node-selection test of a real
submission
([`src/slurmctld/job_mgr.c`, lines 4298-4306](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L4298)
and
[lines 4389-4391](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L4389)),
so both stay unverified-live after this check. A real submission that Slurm
rejects is exit 5 (outcome unknown) in `shk submit`, as for any `sbatch`
error.

The following are confirmed in the official Slurm source at tag
`slurm-25-11-9-1` and are likewise not observed live:

- `scontrol show job` prints `ArrayTaskThrottle=N` directly after a numeric
  `ArrayTaskId` when the array has a run limit
  ([`src/scontrol/info_job.c`, lines 186-192](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/scontrol/info_job.c#L186)).
  `shk requeue --self` and `shk release` therefore read `ArrayTaskId` as the
  first word after the key. Whether a split element on Sherlock carries the
  limit has not been seen.
- A batch job can survive a failed submission reply. The client returns an
  error when the controller send or receive fails
  ([`src/api/submit.c`, lines 81-84](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/submit.c#L81)),
  `sbatch` turns an unhandled submission error into a nonzero exit
  ([`src/sbatch/sbatch.c`, lines 313-315](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/sbatch/sbatch.c#L313)),
  and the controller sends the success response of a batch submission,
  discards that send's result and has no rollback on that path
  ([`src/slurmctld/proc_req.c`, lines 3451-3464](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/proc_req.c#L3451)).
  `shk submit` therefore reports a `sbatch` that ran without printing a strict
  job id as outcome unknown (exit 5), never as a refusal.
- `sbatch --wait` returns the submitted job's exit code after printing the job
  id ([`src/sbatch/sbatch.c`, lines 355-356](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/sbatch/sbatch.c#L355);
  [sbatch reference](https://slurm.schedmd.com/sbatch.html#OPT_wait)). A
  script may carry `#SBATCH --wait`, so `shk submit` reads a strict job id on
  stdout as an accepted submission whatever the exit code.

### Test-only estimates

`shk submit --preflight` reads its `estimate` from the line that
`sbatch --test-only` prints. Confirmed in the source at the same tag, or
inferred from it where a statement says so, and not observed live apart from
the one line recorded under [Submission](#submission):

- One `info()` call prints it, with the format
  `"Job %u to start at %s%s%s a using %u processors on nodes %s in partition %s"`
  and the arguments job id, time text, `" on cluster "` or `""`, the cluster
  name or `""`, processor count, node list and partition name
  ([`src/api/allocate.c`, lines 641-647](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/allocate.c#L641)).
  The `a` before `using` is a literal of the format string from 25.05.0 on;
  24.11 and older print the same line without it and without the cluster
  clause
  ([`src/api/allocate.c` at `slurm-24-11-0-1`, line 568](https://github.com/SchedMD/slurm/blob/slurm-24-11-0-1/src/api/allocate.c#L568)).
  The cluster clause appears only with `--clusters`, `SBATCH_CLUSTERS`,
  `SLURM_CLUSTERS` or a federation
  ([lines 632-639](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/allocate.c#L632)).
- The line goes to stderr only, as `sbatch: <text>`
  ([`src/common/log.c`, lines 1413-1428](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/common/log.c#L1413));
  `sbatch` writes nothing to stdout with `--test-only`
  ([`src/sbatch/sbatch.c`, lines 282-293](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/sbatch/sbatch.c#L282)).
  `--quiet` on the command line suppresses it
  ([lines 125-131](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/sbatch/sbatch.c#L125)).
- The time text comes from `slurm_make_time_str`: `localtime_r` in the
  `sbatch` process and the format `%FT%T`, with no zone
  ([`src/common/parse_time.c`, lines 657-672 and 713-717](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/common/parse_time.c#L657)).
  It is the local time of the process that ran `sbatch`, the login node; the
  controller sends a raw time value (inferred). Other texts are `Unknown` (time 0 or
  infinite), `None` and a run of `#`
  ([lines 666-669 and 690-697](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/common/parse_time.c#L666)),
  and whatever `SLURM_TIME_FORMAT` selects in the environment of `sbatch`
  ([lines 674-688](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/common/parse_time.c#L674)).
  `preflight.sh` unsets `SLURM_*`, so the default applies, and `#SBATCH`
  lines cannot set it; `preflight.sh` pins no `TZ`, so the zone is that of
  the session.
- The node list is a ranged hostlist built by the controller, without spaces
  ([`src/slurmctld/job_scheduler.c`, line 4595](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L4595)),
  and it can be empty
  ([`src/common/node_conf.c`, lines 219-232](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/common/node_conf.c#L219)).
- The partition field is always one name, the partition under test
  ([`src/slurmctld/job_scheduler.c`, line 4596](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L4596)).
  With several partitions the controller walks them in priority-tier order and
  stops at the first one for which the test succeeds
  ([lines 4613-4616](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L4613)
  and
  [4645-4657](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L4645));
  the list is sorted by PriorityTier, with ties in an order the source does
  not specify
  ([`src/slurmctld/partition_mgr.c`, lines 736-741](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/partition_mgr.c#L736)).
  The estimate is that partition's, not the earliest start over the set. A
  drained or inactive listed partition can be replaced by its Alternate
  ([`src/slurmctld/job_mgr.c`, lines 6661-6691](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L6661)),
  so the name can be one the caller did not list.
- The start is the select plugin's estimate from running jobs plus a rough
  queue-delay term for pending jobs of no lower priority in the same
  partition, never earlier than the controller's current time (inferred)
  ([`src/slurmctld/job_scheduler.c`, lines 4537-4541](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L4537)
  and
  [4590-4593](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L4590)).
  It is not the backfill plan.
- The job id in the line belongs to a temporary record that the controller
  creates and purges
  ([`src/slurmctld/job_mgr.c`, lines 4202-4205](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L4202)
  and
  [4301-4304](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L4301)):
  it is not a submitted job.
- A rejected or failed request prints `allocation failure: <text>` and exits 1
  ([`src/sbatch/sbatch.c`, lines 288-291](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/sbatch/sbatch.c#L288)),
  after any `sbatch: error: <message>` lines from the controller's reply
  ([`src/slurmctld/proc_req.c`, lines 2516-2521](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/proc_req.c#L2516)).
- An exit 0 without an estimate line is possible in the client code
  ([`src/api/allocate.c`, line 631](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/allocate.c#L631);
  [`src/slurmctld/proc_req.c`, lines 2528-2532](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/proc_req.c#L2528)),
  though the 25.11 controller does not appear to produce it for an ordinary
  single job
  ([`src/slurmctld/job_mgr.c`, lines 4298-4306](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L4298)).
- Look-alikes: debug lines `JobId=N to start at ... on cluster ... using ...`
  with the prefix `sbatch: debug:`, printed only at `-vv`
  ([`src/api/allocate.c`, lines 294-297 and 317-323](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/allocate.c#L294));
  a following `Preempts: 1,2` line, indented by two spaces
  ([lines 648-662](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/allocate.c#L648));
  job_submit plugin messages, free text with the same `sbatch: ` prefix,
  printed before the estimate
  ([lines 626-629](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/allocate.c#L626));
  and the heterogeneous form `Job N to start at TIME using N processors on NODES`
  ([lines 740-749](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/allocate.c#L740)),
  which `shk` never meets, because it refuses heterogeneous jobs.

### Jobs submitted to several partitions

`shk submit --partition P,P...` and the partition rules of `shk status`
depend on these. Confirmed in the source at the same tag, or inferred from it
where a statement says so, and not observed live:

- `sbatch` documents a comma-separated partition list: the job starts in the
  partition offering the earliest initiation
  ([`doc/man/man1/sbatch.1`, lines 2111-2119](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/doc/man/man1/sbatch.1#L2111)).
- The controller keeps the list for the life of the job and writes the run
  partition first in its own record
  ([`src/slurmctld/job_scheduler.c`, lines 5682-5708](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L5682)).
  What clients get is different: the job record sent to `squeue` and
  `scontrol` holds the single partition the job runs in for every job that is
  not pending, and the list only for a pending job
  ([`src/slurmctld/job_mgr.c`, lines 10611-10614](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L10611)).
  So `squeue %P`
  ([`src/squeue/print.c`, lines 125-134](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/squeue/print.c#L125))
  and `scontrol show job`
  ([`src/scontrol/info_job.c`, lines 406-407](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/scontrol/info_job.c#L406))
  print the list while pending and one partition once started.
- The order of a pending job's list is not the typed order: the parsed list
  is sorted by priority tier
  ([`src/slurmctld/partition_mgr.c`, lines 728-742](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/partition_mgr.c#L728)),
  and the string is often rebuilt in that order at submission
  ([`src/slurmctld/job_mgr.c`, lines 4460-4464](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L4460))
  and again on later events.
- Accounting: the start message carries the list while the job is pending and
  the run partition once it is not
  ([`src/plugins/accounting_storage/slurmdbd/accounting_storage_slurmdbd.c`, lines 2663-2666](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/plugins/accounting_storage/slurmdbd/accounting_storage_slurmdbd.c#L2663)).
  The row is overwritten at start
  ([`src/plugins/accounting_storage/mysql/as_mysql_job.c`, lines 738-740 and 809-811](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/plugins/accounting_storage/mysql/as_mysql_job.c#L738);
  sent from
  [`src/slurmctld/node_scheduler.c`, lines 3037-3043](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/node_scheduler.c#L3037)),
  asynchronously (inferred), and `sacct` prints the stored value
  ([`src/sacct/print.c`, lines 1554-1558](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/sacct/print.c#L1554)).
  After a requeue the new row holds the list again, and the row of the
  earlier run keeps its run partition (inferred from
  [`src/slurmctld/job_mgr.c`, lines 16916-16921](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L16916)).
- A job that leaves pending without ever running (cancelled while pending)
  shows one partition name in `squeue` and `scontrol`, whichever the
  controller's pointer last named, not where anything ran (inferred from the
  same lines 10611-10614 of `job_mgr.c`), while its accounting row keeps the
  list: the completion message sends no partition
  ([`accounting_storage_slurmdbd.c`, from line 2763](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/plugins/accounting_storage/slurmdbd/accounting_storage_slurmdbd.c#L2763)).
- Array tasks are independent records, and different tasks of one array can
  start in different partitions
  ([`src/slurmctld/job_mgr.c`, lines 3596-3597](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L3596);
  [`src/slurmctld/job_scheduler.c`, lines 1561-1575](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L1561)).
  The pending remainder keeps the list (inferred: the split happens before
  the started task's string is rebuilt,
  [`src/slurmctld/node_scheduler.c`, lines 2929-2935](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/node_scheduler.c#L2929)).
- `--dependency=singleton` compares user id and job name only
  ([`src/slurmctld/job_scheduler.c`, lines 258-279](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_scheduler.c#L258)).
  Requeue, job name and array handling have no partition-list rule
  (inferred: none was found on the multi-partition paths that were read).
- Submission checks: one nonexistent name rejects the whole submission
  (`Invalid partition name specified`,
  [`src/slurmctld/job_mgr.c`, lines 6611-6626](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L6611)).
  A listed partition that takes no submissions is dropped from the list or
  replaced by its Alternate
  ([lines 6661-6691](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L6661)).
  Limits of one listed partition (MaxTime, node counts, memory, AllowGroups,
  AllowAccounts, AllowQos) reject the whole submission under
  `EnforcePartLimits=ALL` and only exclude that partition under `ANY` or `NO`
  ([lines 6740-6814](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L6740)
  and
  [3868-3902](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L3868);
  [`doc/man/man5/slurm.conf.5`, lines 1443-1462](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/doc/man/man5/slurm.conf.5#L1443));
  the AllowAccounts test at submission also needs enforced associations.
  Partition-specific associations that differ between the listed partitions
  reject it
  ([`src/slurmctld/job_mgr.c`, lines 1198-1213](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/job_mgr.c#L1198)),
  and association or QOS limits of any listed partition can reject it
  ([`src/slurmctld/acct_policy.c`, lines 3664-3665](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/acct_policy.c#L3664)).

### Node records and GPU models

`shk resources --summary` reads the `GPU_SKU:` and `CLASS:` features of node
records, and `shk allocation` reports `gpu_sku_candidates` from them. Confirmed
in the source at the same tag, or inferred from it where a statement says so,
and not observed live:

- `scontrol show node NAME --oneliner` prints, in this order,
  `AvailableFeatures`, `ActiveFeatures`, `Gres`, later `CfgTRES` and
  `AllocTRES`
  ([`src/api/node_info.c`, lines 236-258 and 404-410](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/api/node_info.c#L236));
  `GresUsed` only with details. With glibc an unset string among the first
  four prints as `(null)`, an inference from the C library rather than from
  Slurm's source.
- `Gres` is one comma-separated element per GPU type, `gpu:TYPE:COUNT` with an
  optional `(S:...)` socket suffix, or `gpu:COUNT` when untyped
  ([`src/interfaces/gres.c`, lines 4825-4960](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/interfaces/gres.c#L4825)).
  `CfgTRES` has `gres/gpu=N` for all GPUs and `gres/gpu:TYPE=N` only for the
  typed TRES the site tracks
  ([`src/slurmctld/controller.c`, lines 3198-3223](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/slurmctld/controller.c#L3198)),
  so it need not list every type (inferred).
- Node features are free administrator strings with no link to GRES types in
  the source (inferred from the files that were read)
  ([`doc/man/man5/slurm.conf.5`, lines 7118-7130](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/doc/man/man5/slurm.conf.5#L7118)).
  `GPU_SKU:<model>` is a Sherlock convention, and `CLASS:` is the prefix that
  `--by class` assumes.
- A MIG device is an ordinary `gpu` GRES whose type is optional and
  site-chosen; the documented example type is `a100_3g.20gb`
  ([`doc/html/gres.shtml`, lines 654-669](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/doc/html/gres.shtml#L654)).
  The node record has no MIG field. Sherlock's feature inventory in
  [the policy guide](../sherlock-policy.md) lists a MIG slice as
  `A30_MIG-1g.6gb`.
- A job record without details has `NodeList` and a job-wide `AllocTRES`
  only: no per-node GPU split, no device index and no UUID
  ([`src/scontrol/info_job.c`, lines 508-515 and 556-561](https://github.com/SchedMD/slurm/blob/slurm-25-11-9-1/src/scontrol/info_job.c#L508)).
  The index printed with details is a position in a bitmap, not a device
  UUID.
