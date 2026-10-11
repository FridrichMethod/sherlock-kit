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
  `--itemize-changes` and `--chmod` failed with `invalid characters in scp command! here:%i ...`.
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

## Not observed live

Lost ssh replies, duplicate submissions racing each other, an array task
requeue, natural preemption, `possu`, and anything on compute-node hostnames
beyond what `SLURM_JOB_ID` implies. Those rest on offline tests with fake
`ssh` and Slurm binaries.
