# Observed Sherlock and Slurm behavior

Facts seen live on Sherlock while piloting 0.1.0 to 0.3.0 (accepted 2026-10-08,
2026-10-09 and 2026-10-10 UTC), which the 0.4.0 design relies on. They are observations from
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
  (the `%i` is the log format that rsync forwards to the sender for it). The
  pilot tried `--chmod` in the same run and the note listed it too, but rsync
  never forwards `--chmod` to the sender (rsync 3.2.7 `--debug=CMD2` shows the
  same server command and protected args with and without it), and 0.5.0 pulls
  with `--chmod=Du+rwx` worked through the DTN on 2026-10-18.
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

## Not observed live

Lost ssh replies, duplicate submissions racing each other, an array task
requeue, natural preemption, `possu`, a live `shk cancel`, and `shk` itself
running on Sherlock with the local executor. Those rest on offline tests with fake
`ssh` and Slurm binaries.
