# Observed Marlowe and Slurm behavior

Facts seen live on Marlowe from one user's account on 2026-11-21 and, for
[shk inside a batch job](#shk-inside-a-batch-job), on 2026-11-22, on Slurm
25.05.2, grouped by topic and, where one exists, with the `shk` behavior that
depends on it. They are observations of those days, not site guarantees: the
site changes its limits with demand, so re-check a fact before building on it.
Site policy is not taken from here; the
[Marlowe policy guide](../marlowe-policy.md) names its sources.

Identifiers are synthetic: `m000000` stands for the project id,
`marlowe-m000000` for its Slurm account and `USER` for the user name. Job ids,
user names, UIDs, network addresses and private configuration are not
reproduced; other users' jobs appear only as counts and percentages.

## How the observations were made

- The cluster facts were read over the user's existing `marlowe-plain`
  OpenSSH master with
  `ssh -oBatchMode=yes -oProxyCommand=false marlowe-plain ...`, which cannot
  log in. `shk`'s own commands ran afterwards, from a development tree
  ([shk on Marlowe](#shk-on-marlowe)).
- Read-only commands: `scontrol show config`, `partition`, `node` and `job`,
  `sacctmgr show qos`, `assoc` and `user`, `sinfo`, `squeue`, `sacct`,
  `sshare`, `module`, environment and filesystem reads.
- Submission verdicts came from `sbatch --test-only`, which validates a request
  and prints an estimate without creating a job.
- One CPU-only batch job ran: 1 CPU, 1 GB, a 10-minute limit, partition
  `preempt`, the base account, `--export=ALL` plus one marker variable,
  submitted by hand with `sbatch` from a shell that held the client environment
  below. Its fixed script printed what a job sees (variable names only, apart
  from a listed set of values) and completed in seconds.
- On 2026-11-22 a CPU-only job that `shk` submitted from the workstation ran
  `shk` inside itself and requeued itself once
  ([shk inside a batch job](#shk-inside-a-batch-job)).
- Queue waits and preemption come from 30 days of accounting for all users
  (`sacct -a -X --duplicates`, 74,325 rows), read without user names and
  summarized as counts.

## Hosts, shells and identity

- `login.marlowe.stanford.edu` round-robins over the login nodes
  `login-0N.marlowe.stanford.edu`; the OS is Ubuntu 22.04. On a login node
  `hostname` is `login-0N`, and `hostname -f` and Python's `socket.getfqdn()`
  give `login-0N.cm.cluster`. Compute nodes are `nNN` (`nNN.cm.cluster`), and
  the Slurm ClusterName is `slurm`. These are vendor defaults that do not
  identify Marlowe, so `shk` selects Marlowe by `--site` or by the
  configuration's `site`, never by host name.
- There is no `/etc/agents` directory and no login banner: a non-interactive
  `ssh marlowe-plain 'echo hi'` gives clean stdout and empty stderr, and
  `/etc/motd` is empty. `/opt/duo/sbin` is on PATH (Duo second factor).
- `/bin/sh` is bash, and `dash` exists. The login shell is `/bin/bash`, umask
  0022, `awk` GNU Awk 5.1.
- The system `python3` is 3.10.12, older than the Python 3.11 that `shk`
  needs. The `python3` module gives 3.9, `conda/24.3.0-0` gives 3.10, and a
  `uv/0.13.7` module exists; a micromamba environment with Python 3.14 ran on
  the login node and on a compute node. `rsync` 3.2.7, `sha256sum`, `flock`
  and `timeout` are in `/usr/bin`. `apptainer`, `singularity` and `enroot` are
  on the login node, and unprivileged user namespaces are enabled.
- Login shells print an Lmod note on stderr ("The system default contains no
  modules (env var: LMOD_SYSTEM_DEFAULT_MODULES is empty) No changes in loaded
  modules").
- A `dtn.marlowe.stanford.edu` DNS record exists; it was not contacted.

## Slurm client environment

- The non-interactive ssh shell that `shk` uses has the PATH
  `~/bin:~/.local/bin:/opt/duo/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin`,
  so `sbatch`, `squeue`, `sacct`, `scontrol`, `sinfo` and `scancel` are not
  found. `module` is not defined there (`MODULEPATH` is unset).
- The module `slurm/slurm/25.05.2` sets
  `SLURM_CONF=/cm/shared/apps/slurm/var/etc/slurm/slurm.conf` and
  `CMD_WLM_CLUSTER_NAME=slurm`, and prepends `/cm/shared/apps/slurm/current/bin`
  and `sbin` to PATH and the `lib64` directories to `LD_LIBRARY_PATH`.
  `/cm/shared/apps/slurm/current` links to the 25.05.2 directory. `module purge`
  removes Slurm.
- With only the bin directory on PATH the clients fail
  (`resolve_ctls_from_dns_srv: res_nsearch error: Unknown host ... fatal: Could not establish a configuration source`).
  With the bin directory and `SLURM_CONF` and nothing else
  (`env -i HOME=... PATH=/usr/bin:/bin SLURM_CONF=... squeue`) they work;
  `LD_LIBRARY_PATH` is not needed. There is no `/etc/slurm/slurm.conf`.
- `shk` therefore runs its remote programs for Marlowe after a fixed prelude
  that appends the bin directory to PATH and exports `SLURM_CONF`.
- No `SBATCH_*`, `SLURM_*`, `SQUEUE_*` or `SACCT_*` variable is exported in a
  login or a non-login shell on the login node. `PROJECT_ID`, `SCRATCH` and
  `PROJECTS` are set in non-login shells too; no startup file read showed
  where they come from.

## Partitions, QOS and accounts

Values observed 2026-11-21.

- Three partitions span the same 31 nodes. `preempt`: QoS normal, DenyQos
  `class,medium`, MaxTime 12:00:00, GraceTime 900, PriorityTier 1,
  DefMemPerCPU 13000, MaxMemPerNode 1950000. `batch`: AllowQos medium,
  MaxNodes 16, MaxTime 2-00:00:00, PriorityTier 100. `hero`: AllowQos large,
  MaxTime 30-00:00:00, PriorityTier 80. No partition is the default, and none
  has a default time.
- Scheduler: PreemptType `preempt/qos`, PreemptMode `REQUEUE`,
  PreemptExemptTime 00:14:59, PreemptParameters `youngest_first` (no
  `send_user_signal`), JobRequeue 1, MaxBatchRequeue 4, KillWait 30 s,
  ProctrackType `proctrack/cgroup`,
  MaxArraySize 1001, TmpFS `/tmp`, JobSubmitPlugins `lua`,
  AccountingStorageEnforce `associations,limits,qos,safe`, EnforcePartLimits
  `ALL`, SelectTypeParameters `CR_CORE_MEMORY`. Priority weights
  (`sprio -w`): age 10,000,000, QOS 2,000,000, `gres/gpu` 1,500,000,
  partition 1,000,000, fairshare 10,000, job size 100.
- QOS `normal`: PreemptMode requeue, flags `DenyOnLimit,OverPartQOS`, and
  limits per account, not per user: MaxSubmitPA 250, MaxJobsPA 100,
  MaxTRESPA `gres/gpu=96`, GraceTime 00:05:05, PreemptExemptTime 00:05:00.
  QOS `medium` (MaxSubmitPA 32, MaxJobsPA 32, `gres/gpu=128`) and `large`
  preempt `normal`.
- The user's associations are `marlowe-m000000` (QOS normal) and
  `marlowe-users` (QOS normal, MaxSubmit 0); the default account is
  `marlowe-m000000`. Every member of the observed project account holds QOS
  normal only, so `preempt` is the only usable partition. The account
  association sets no GrpTRES, GrpTRESMins or MaxWall. No partition sets
  TRESBillingWeights; billing TRES equals the CPU count for CPU-only jobs.
- Queue snapshot: in `preempt` (QOS normal) 266 jobs pending, 52 running and
  1 in REQUEUE_HOLD; in `batch` 65 pending and 61 running. Pending reasons in
  `preempt`, by count: Priority 123, Dependency 103,
  `user env retrieval failed requeued held` 10, BeginTime 10,
  DependencyNeverSatisfied 9, JobArrayTaskLimit 6, JobHeldAdmin 2. The
  REQUEUE_HOLD job showed reason Priority.

## Nodes

- 31 identical nodes, `n01` to `n31`, each in all three partitions: x86_64,
  2 sockets of 56 cores with 1 thread per core (112 CPUs), RealMemory
  1,950,000 MB, TmpDisk 0, `Gres=gpu:8(S:0-1)` without a GPU type, CfgTRES
  `cpu=112,mem=1950000M,billing=112,gres/gpu=8`: 248 GPUs in total.
- The only node feature is `location=local`. There is no GPU model, GPU memory
  or CPU generation feature, so Sherlock's GPU model constraints have no
  counterpart.
- `scontrol show node` prints CPUAlloc, CPUEfctv, CPUTot, RealMemory, AllocMem,
  CfgTRES, AllocTRES and Gres; it prints no CoreSpecCount, MemSpecLimit or
  GresUsed. One snapshot: 24 nodes MIXED+PLANNED, 4 MIXED, 2 ALLOCATED and
  1 MIXED+DRAIN, no reservation; a later one: 243 of 248 GPUs allocated and 27
  of 31 nodes with free CPUs.
- Running jobs in `preempt` were mostly `cpu=8,mem=64G,gres/gpu=1` or whole
  nodes `cpu=96,mem=1600G,gres/gpu=8`; 1 of 102 running jobs asked for
  OverSubscribe.

## Submission verdicts

`sbatch --test-only` with the base account and partition `preempt` unless the
row says otherwise:

| Request | Verdict |
|---|---|
| no `--account` (the user has a default account) | `sbatch: error: ACCOUNT ERROR: Did you remember to set your account?`, then a pointer to the older documentation site |
| `--account=marlowe-users` | the same ACCOUNT ERROR, then `Invalid account or account/partition combination specified` |
| no `--partition` | `allocation failure: No partition specified or system default partition` |
| `--partition=batch` or `--partition=hero` | `allocation failure: Invalid qos specification` |
| `--partition=preempt,batch` | `Invalid qos specification` |
| `--qos=medium` | `Invalid qos specification` (`--qos=normal` is accepted) |
| 1 CPU without a GPU | accepted |
| no `--time` | accepted; the partition's MaxTime applies |
| no `--mem` | accepted; DefMemPerCPU applies |
| `--gpus=1`, `--gres=gpu:1`, `-n 4 -G 4` | accepted |
| `--time=13:00:00` | `Requested time limit is invalid (missing or exceeds some limit)` |
| `--constraint=nonexistent_feature` | `allocation failure: Invalid feature specification` |
| `--gres=gpu:h100:1` | `Requested node configuration is not available` |
| `--tmp=1G` | `Requested node configuration is not available` |
| `-N 1 -G 9` | `Requested node configuration is not available` |
| `-G 97` | `sbatch: error: MaxGRESPerAccount`, then the accounting/QOS policy message |
| `--array=0-199` | accepted |
| `--array=0-499`, `--array=0-1000` | `sbatch: error: MaxSubmitJobsPerAccount`, `Job violates accounting/QOS policy (job submit limit, user's size and/or time limits)` |
| `--requeue --signal=B:TERM@120`, `--no-requeue` | accepted |
| `--exclusive` | accepted; the job takes all 112 CPUs of a node |
| `--prefer=location=local` | accepted |

Every deterministic refusal came at test time, so `shk submit` on Marlowe runs
the test after its duplicate read and before staging, and returns a refusal as
exit 3 with nothing staged. The submit plugin demands an explicit account even
from a user with a default account, so `shk` takes the account from the
configuration.

## Read formats

- The exact `sacct` query of `query.sh`
  (`-n -P --local --allocations --duplicates -u USER -S now-30days -E now --format=JobID,State,Partition,ElapsedRaw,AllocCPUS,AllocTRES,Submit,Restarts,ExitCode,JobName%128`)
  and the exact `squeue` and `sacct` invocations of `details.sh` (0x1f
  delimiter, Cluster, DBIndex, UID, Comment and the wide fields) returned exit
  code 0 and well-formed rows. A sample row:
  `<jobid>|FAILED|preempt|577|2|billing=2,cpu=2,mem=8G,node=1|<submit time>|0|0:9|bash`.
  The Cluster column reads `slurm`.
- A 400-day `sacct` window is accepted and returns year-old rows; `shk`
  bounds `--since` to 31 days on every site.

## Inside a batch job

What the observation job saw:

- It started 26 s after submission (`Scheduler=Backfill`), ran 7 s and ended
  COMPLETED 0:0 with `billing=1`.
- `hostname` is `nNN`, `hostname -f` is `nNN.cm.cluster`, `SLURMD_NODENAME`
  equals the host name and so does the node's NodeName in
  `scontrol show node`; `SLURM_CLUSTER_NAME=slurm`.
- `#!/bin/bash` runs as a non-login shell, yet `module` is a function there
  (`BASH_ENV=/usr/share/lmod/lmod/init/bash`) and
  `LOADEDMODULES=slurm/slurm/25.05.2`. `bash -l` has it too and prints a
  harmless Lmod note. `ENVIRONMENT=BATCH`.
- `SLURM_GET_USER_ENV=1`: Slurm builds the job environment from a capture of
  the user's login-shell environment plus the exported variables. This matches
  the pending reason `user env retrieval failed requeued held` seen in the
  queue: a slow or failing login-shell startup can leave a job requeued and
  held.
- The exported PATH arrived unchanged, with the Slurm bin directory last, and
  `SLURM_CONF` was set: `sbatch`, `squeue`, `sacct`, `scontrol`, `sinfo`,
  `scancel` and `srun` resolve, and `scontrol show job`, `squeue -j` and an
  `srun -n1` step work with the environment as given. `--export=ALL` also
  carried the ssh session's variables (`SSH_CLIENT`, `SSH_CONNECTION`,
  `KRB5CCNAME`, `XDG_*`).
- `SLURM_SUBMIT_DIR` is the submitting shell's working directory; `--chdir`
  sets the job's.
- `/local_scratch` links to `/raid`, a node-local array of tens of terabytes,
  and `/local_scratch/USER.JOBID` exists and is writable. `TMPDIR` is `/tmp` on
  the root filesystem, so it does not point at the job directory; `TMP` is
  unset.
- `nproc` is 1 with one allowed CPU, `ulimit -m` equals the requested memory,
  and 131072 files may be open.
- No GPU is visible without a GPU request: `nvidia-smi -L` prints
  `No devices found.` and `CUDA_VISIBLE_DEVICES` is unset.
- The compute node reaches the internet: `pypi.org` and `github.com` answered
  HTTP 200.
- `/usr/bin/python3` is 3.10.12; `uv` and `conda` are not on PATH (`uv` is a
  module).
- Many cache variables are set (`APPTAINER_CACHEDIR`, `HF_HOME`,
  `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `TORCH_HOME`, `XDG_CACHE_HOME` and others);
  only their names were recorded.
- `scontrol show job` shows `Requeue=1`, `Restarts=0`, `MinTmpDiskNode=0`,
  `Features=(null)` and a `PreemptEligibleTime` 5 minutes after `StartTime`.

## Test-only estimates and real starts

- `sbatch --test-only` of the observation job's request, seconds before the
  submission, estimated a start about 32 h later (`to start at <time>`); the
  job started 26 s after submission.
- The estimate did not change with `--time` (10 minutes, 1 hour and 12 hours
  gave the same second) and put larger GPU requests earlier: 8 GPUs about 18 h,
  4 GPUs about 19.5 h, 1 GPU about 30 h, 1 CPU about 33 h. Earlier the same day
  1 CPU was estimated at about 34.6 h and 1 GPU at about 31.5 h.
- Only a test's accept-or-refuse verdict is usable on Marlowe; its start time
  is not a queue-wait prediction, and the rules forbid relaying it as one.

## Queue waits

Wait = Start - Eligible, partition `preempt`, non-array jobs, in the 30 days of
accounting ending 2026-11-21:

| Request | Jobs | Median | p75 | p90 | p95 | Started within 24 h |
|---|---|---|---|---|---|---|
| 0 GPU | 8581 | seconds | under 1 min | under 1 min | 1 min | 100% |
| 1 GPU | 10129 | 1.07 h | 4.32 h | 11.2 h | 18.7 h | 97% |
| 2-4 GPU | 818 | 2.02 h | 5.16 h | 15.1 h | 56.3 h | 92% |
| 5-8 GPU | 326 | 52.8 h | 119.5 h | 196 h | 248 h | 33% |

- In the last 7 days of that window, 1-GPU jobs waited a median 0.11 h (p90
  4.2 h). The weekly median for GPU jobs lay between 0.12 h and 2.9 h in the
  four most recent weeks.
- By requested limit, GPU jobs waited a median 0.60 h with a limit up to 1 h,
  2.2 h with 1 to 4 h, 5.9 h with 4 to 8 h and 1.90 h with 8 to 12 h (137
  jobs, p90 87.8 h): the tendency of shorter limits to start sooner holds up
  to 8 h.
- For comparison, partition `batch` (QOS medium): 1 GPU median 0.19 h, p90
  4.4 h; 5 to 8 GPUs median 5.3 h.

## Preemption and requeue

Same window, partition `preempt`:

- Share of runs that ended PREEMPTED: 2.8% without a GPU, 24.4% with 1 GPU,
  38.5% with 2 to 4 GPUs, 40.3% with 5 to 8 GPUs.
- Median elapsed time at preemption: 11.6 minutes with 1 GPU (p10 7.9
  minutes), 14.7 minutes with 2 to 4 GPUs.
- GPU runs by requested limit: up to 1 h, 76% completed and 16% were
  preempted; 1 to 4 h, 24% completed and 48% were preempted; 4 to 12 h, 30%
  completed and 60% were preempted.
- One job id has 42 PREEMPTED rows and one has 327 accounting rows, so
  preemption requeues do not stop at MaxBatchRequeue (4).
- Without a GPU, separate REQUEUED rows (21% of runs, 92% of them shorter
  than 60 s) look like launch failures, not preemptions. With 1 GPU, REQUEUED
  rows are 5.3% of runs, with a median elapsed time of 23 minutes and none
  shorter than 60 s.
- The observation job's `PreemptEligibleTime` was its start plus 5 minutes:
  QOS normal's PreemptExemptTime (00:05:00) governs, not the cluster's
  00:14:59. Which grace time, the partition's 900 s or QOS normal's 00:05:05,
  applies to a preempted job is not established.
- The observed project's own 30-day accounting holds no preempted or requeued
  row.

## Login nodes

- The login node read has no GPU (`nvidia-smi` is absent), 96 CPUs and about
  503 GB of memory, and shows no per-user cgroup memory limit (`memory.max` is
  `max`). Sherlock login nodes, for comparison, enforce per-process CPU time and
  process limits.
- The documentation (Usage Violations) says that login nodes "are for editing,
  compiling, submitting jobs, and moving data. Short, light commands are fine;
  heavy or long-running work is not."

## Storage

- `HOME=/users/USER` on NFS. `SCRATCH=/scratch/m000000` on Lustre, group
  `marlowe-m000000`, mode `drwxrws---`, holding a per-user directory
  `/scratch/m000000/USER`. `PROJECTS=/projects/m000000`, an autofs NFS export
  per project, holding `/projects/m000000/USER`. `PROJECT_ID=m000000`.
- There is no `$GROUP_HOME`, `$GROUP_SCRATCH`, `$L_SCRATCH_JOB` or `$OAK`.
- The module `marlowe-scripts/1.0` provides `storage-quota [DIR]`: the Lustre
  project quota of `/scratch` (10.0 TiB and 6,000,000 inodes for the observed
  project), the export size of `/projects` (1.0 TiB) and "not queryable yet"
  for `/users`.

## Charging

- The site script `/usr/local/bin/current-cost` (POSIX sh, read but not run)
  builds a Bright `cmsh wlm chargeback` report for a project and a date range,
  at the basic access (preempt) prices or at the medium and large prices, each
  with a price per CPU-hour and per GPU-hour: Marlowe charges CPU time and GPU
  time, CPU-only jobs included. It needs `module load cmsh` and creates and
  removes a temporary chargeback object. Its prices are not reproduced here;
  the recharge page states the current ones.
- Billing TRES equals the CPU count for CPU-only jobs, and no partition sets
  TRESBillingWeights.

## shk on Marlowe

`shk` ran on 2026-11-21 from a development tree with a throwaway Marlowe
configuration, from the workstation over the `marlowe-plain` master unless a
bullet says otherwise. One job that `shk` submitted ran: one CPU, for a few
seconds.

- `shk --site marlowe doctor`: both masters `running` from one check, and
  `"site": "marlowe"` after `executor`. With `--remote` the host was a login
  node, `sbatch`, `squeue` and `sacct` resolved under
  `/cm/shared/apps/slurm/current/bin` through the prelude and `sha256sum` under
  `/usr/bin`, and the reply had no `site_instructions` key.
- `status --job-id N --details` of the finished observation job: state
  `done`, its accounting row parsed, a cost of 7 CPU seconds, times converted
  to UTC. `status --all` listed no names: the throwaway project had no jobs
  yet.
- `push` of a three-file tree into a REMOTE whose parent did not exist failed
  with rsync exit 11 (`transfer_failed`, rsync's `mkdir ... failed: No such file or directory`). Once the parent existed, `push` and a repeated `push`
  succeeded, and after `SHA256SUMS` was written with the skill's one-liner on
  the login node, `pull` fetched a tree equal to the pushed one.
- On a login node (the source tree pushed to scratch, Python 3.14 from a
  micromamba environment, a Marlowe configuration in the working directory,
  no flags): `doctor --remote` chose the local executor by itself
  (`"executor": "local"`, `"site": "marlowe"`, `masters` `{}`),
  `status --job-id N` worked, and `push` was refused with exit 3
  `transfer_on_marlowe`.
- `resources --partition preempt --summary`: complete, 31 nodes of which 30
  schedulable, with CPU, memory and GPU sums; without `--by` or `--shape`,
  `by` was `[]` and `slots` `{}`.
- `submit --dry-run` printed the argv
  `sbatch --parsable --job-name=... --partition=preempt --account=marlowe-m000000 --cpus-per-task=1 --requeue --open-mode=append --ntasks=1 --mem=1024M --time=10 --chdir=... --output=... --error=... --export=ALL,SHK_NAME=...,SHK_SUBMISSION_TOKEN=... --dependency=singleton --comment=... --hold`.
- `submit --preflight` for 1 CPU and for `--gpus 1`: exit 0, `job_submitted`
  false, and the line
  `sbatch: Job N to start at <time> a using 1 processors on nodes nNN in partition preempt`
  (Slurm 25.05.2 prints the word `a` after the time), parsed into `estimate`
  with `status` `parsed` and a `note` saying that it is not a wait prediction
  on Marlowe. With `--partition batch` it was exit 3 `unknown_partition`
  ("listed: preempt"), offline.
- A real `submit` with a `--constraint` that no node has, plus `--hold` and a
  token: exit 3 with the preflight reply (`"action": "preflight"`,
  `"ok": false`, `"job_submitted": false`, `preflight.stderr`
  `allocation failure: Invalid feature specification`), and no `runs`
  directory under `remote_root`: nothing was staged.
- A held `submit` (1 CPU, 1 GB, 10 minutes, a token) gave `submitted` with a
  job id and `"site": "marlowe"`. Then `status --task`: `live`, 1 PENDING, no
  anomaly. `update --time 5m`: `updated`, TimeLimit 00:10:00 to 00:05:00.
  `update --time 20m`: exit 3 `remote_refused`, `update: SHK_REFUSED time`.
  The same `submit` again: exit 3 `live`, with the hint
  `wait; check shk --site marlowe status at most once per 60 s`. `cancel`:
  `cancelled`. A later `status`: `failed`, 1 CANCELLED, zero CPU and GPU
  seconds. The job never ran.
- A plain `submit` of a one-CPU script (1 GB, 5 minutes, no token):
  `submitted` with a job id, the dry run's argv (`--requeue`, no `--hold`).
  The job started 23 s later and ran 2 s. `status --task`: `done`, 1
  COMPLETED, exit code `0:0`, `max_restart` 0, a final cost of 2 CPU seconds,
  no anomaly; `status --job-id N --details` showed the same from the queue row
  and the accounting row (`billing=1,cpu=1,mem=1G,node=1`). Inside, the working
  directory was the run directory `<remote_root>/runs/<name>` (not
  `SLURM_SUBMIT_DIR`), which held `.shk/<name>.<jobid>.sh` and `.json`;
  `SHK_NAME`, the only `SHK_` variable, equaled the Slurm job name; `module`
  was defined, `sbatch` was on PATH, one CPU and no GPU were visible, and
  `scontrol show job` showed `Requeue=1`. The script's last step wrote
  `out/SHA256SUMS` with the skill's one-liner, and `pull` of `out` gave
  `pulled` with the one listed file under the payload-only contract (the
  manifest is not copied).
- Locally: `shk --site marlowe rules workstation` and
  `shk --site marlowe rules cluster` printed the Marlowe files;
  `install-skill` for both sites into one project wrote each site's skill
  under `.claude/skills/` and `.agents/skills/` and each site's rules under
  `.claude/rules/`; `--site marlowe` in a Sherlock project was exit 1
  `site_mismatch`.

## shk inside a batch job

One CPU-only job on `preempt` (1 task, 1 CPU, 1 GB, a 10-minute limit,
`--signal B:USR1@300`), submitted with `shk` from the workstation on
2026-11-22, with the development tree of 0.11.0 and a Python 3.14 that `uv`
installed on scratch. Its script was the Marlowe skill's walltime template
with `max_restarts=2` around a stand-in workload (a counter that saved a
checkpoint every second, stopped with a checkpoint on TERM, and in a later
attempt ran ten more seconds and exited 0), plus read-only probes in the first
attempt; it set no TERM trap. The job ran two attempts, one self-requeue, and
completed. Everything below rests on this one job: a GPU job, an array
element, a preempted attempt, a real `submit` from inside a job and an in-job
`cancel`, `update` or `release` are not observed.

### First attempt

- `shk --site marlowe doctor --remote`, without `--executor`, on the compute
  node: `"executor": "local"`, `"site": "marlowe"`, and `sbatch`, `squeue` and
  `sacct` found under the Slurm bin directory. The on-cluster test holds on a
  compute node, and the executor is local there by itself.
- The batch script's environment: `SHK_NAME`, equal to the Slurm job name, was
  the only `SHK_` variable; `SLURM_RESTART_COUNT` was unset; `SLURM_CONF` named
  Marlowe's configuration file, a file on the node; `sbatch` was on PATH;
  `SLURM_JOB_END_TIME` minus `SLURM_JOB_START_TIME` was the 600 s limit.
- `/bin/sh` is bash, bash is 5.1.16, `/usr/bin/python3` is 3.10.12, and the
  job's processes sit in cgroup v1 hierarchies.
- `scontrol show config` in the job: `AuthInfo` `(null)`, `KillWait` 30 s,
  `MaxBatchRequeue` 4, `ProctrackType` `proctrack/cgroup`, `PrologFlags`
  `Alloc,Contain`. The site's `cgroup.conf` is readable and holds no
  `SignalChildrenProcesses` line.
- `scontrol pidinfo PID` printed two lines,
  `Slurm JobId=N ends at <weekday month day time year>` and
  `Job remaining time is N seconds`. `scontrol listpids JOBID` printed the
  header `PID JOBID STEPID LOCALID GLOBALID`, one row per process of the batch
  step (STEPID `batch`; LOCALID and GLOBALID `0` for the batch shell and `-`
  for its children) and one row `-1 JOBID extern 0 0`. Both ran as the
  unprivileged user.
- `shk --site marlowe --executor local allocation --pid <batch shell> --job-id $SLURM_JOB_ID`:
  exit 0, `complete` true, `"site": "marlowe"`, `job_membership_verified`
  true from `scontrol pidinfo`, state RUNNING, partition `preempt`, one node,
  1 allocated CPU and 0 GPUs (`AllocTRES=cpu=1,mem=1G,node=1,billing=1`), one
  CPU in the affinity; `gpu_sku_candidates`: `candidates` `[]`, `ambiguous`
  false, source `ActiveFeatures` `location=local`, `Gres` `gpu:8(S:0-1)`,
  `CfgTRES` `cpu=112,mem=1950000M,billing=112,gres/gpu=8`.
- Inside one `srun -n1` step of the job's own allocation (no new allocation):
  `scontrol show step JOB.0` printed
  `StepId=JOB.0 UserId=... StartTime=... TimeLimit=UNLIMITED State=RUNNING Partition=preempt NodeList=... Nodes=1 CPUs=1 Tasks=1 Name=bash Network=(null) TRES=cpu=1,mem=1G,node=1 ResvPorts=(null) CPUFreqReq=Default Dist=Cyclic SrunHost:Pid=...:... TresPerTask=cpu=1`;
  `scontrol listpids JOB.0` listed the step's two processes; and
  `shk --site marlowe --executor local allocation --pid <step process> --job-id $SLURM_JOB_ID --step-id 0`
  was exit 0 with `complete` true and `step_membership_verified` true
  (1 task, 1 CPU, 0 GPUs).
- `shk allocation --pid PID --job-id ID` without `--site`: exit 1
  `site_mismatch`, message "this machine belongs to Marlowe, but the site is
  Sherlock: --executor local (or SHK_EXECUTOR=local) would treat Marlowe's
  Slurm as Sherlock's", hint "pass --site marlowe, or --project-dir DIR where
  DIR holds the Marlowe configuration".
- `shk --site marlowe --executor local status --job-id $SLURM_JOB_ID --details`:
  exit 0, the name `live` with one running task.
- `shk --site marlowe --executor local --project-dir DIR submit --preflight ...`,
  DIR holding a Marlowe configuration on scratch: exit 0, `job_submitted`
  false, Slurm's will-run line parsed, `estimate.note` present. So
  `sbatch --test-only` is accepted from a compute node. No job was submitted
  from inside the job.
- With the 600 s limit and `--signal B:USR1@300`, USR1 reached the batch shell
  299 s after the attempt started, 301 s before the limit. The handler sent
  TERM to the workload, which saved its checkpoint and exited 0.
- `shk --site marlowe --executor local requeue --self --expect-name "$SHK_NAME"`:
  the complete reply reached its output file (`"action": "requeued"`,
  `"executor": "local"`, `"site": "marlowe"`). The script line after the call
  never ran: the batch step ended CANCELLED with exit code `0:15` (the shell
  died of SIGTERM), and the job's stderr log holds
  `*** JOB N ON nNN CANCELLED AT <time> DUE TO JOB REQUEUE ***`. The
  accounting row of the attempt is `REQUEUED`, exit code `0:0`, elapsed 4:59.

### Second attempt

- It started 130 s after the requeue, on another node: the job's submit time
  was reset to the second after the requeue, it became eligible 121 s after
  that (Slurm's default credential lifetime of 120 s plus one; `AuthInfo` is
  unset), and it started 8 s later.
- `SLURM_RESTART_COUNT` was 1 and `scontrol show job` showed `Restarts=1`;
  `BatchFlag` was 1 in both attempts; `SHK_NAME` was unchanged and still the
  only `SHK_` variable; the attempt had its own 600 s limit and a
  `PreemptEligibleTime` of its own start plus 5 minutes.
- The workload resumed from the first attempt's checkpoint, ran ten seconds
  and exited 0; `shk --site marlowe manifest out` wrote the manifest over 43
  files; the job ended COMPLETED `0:0`.

### From the workstation

- The submit ran Slurm's test and was `submitted`; the first attempt started
  21 s after the submit.
- One `shk --site marlowe wait --task T --campaign C --timeout 40m --interval 2m`:
  five reads, `wait.outcome` `settled`, exit 0, about nine minutes after the
  submit; the name was `done` with `current.max_restart` 1 and `by_state`
  `{"COMPLETED": 1}`.
- `status --job-id N --details` afterwards: the queue row COMPLETED and the
  accounting row of the first attempt REQUEUED; `cost.cpu_seconds` 312 for
  both attempts, final.
- `pull` of `out/` checked the hashes of the 43 listed files and copied them.

## Not observed

- Anything about a preemption: whether and when a preempted job receives
  SIGTERM, which grace time applies, the hold after exits during the grace
  window, and the inferred start of a job once more after its work is done.
  The rules require periodic checkpoints and never rely on a signal.
- `KillWait` for a batch shell that survives SIGTERM: the one observed
  self-requeue ended a shell without a TERM trap
  ([shk inside a batch job](#shk-inside-a-batch-job)).
- A self-requeue of an array element or of a GPU job, and whether the site
  ever limits repeated self-requeues: one self-requeue of one CPU-only job is
  observed.
- The requeue-hold path: how a job reaches REQUEUE_HOLD or a launch-failure
  hold, what `squeue`, `scontrol` and `sacct` show for it, and the recovery the
  rules prescribe (cancel, then resubmit with the user's consent).
- A GPU job, an array or a preempted job that `shk` submits and that then
  runs: every job on Marlowe is billed, and such a job needs the owner's
  authorization. The two jobs of `shk` that ran had one CPU each and were not
  preempted.
- `occupancy`, `release`, an `update` of the partition, the features or an
  array's throttle, and `status --details` of a pending job, on Marlowe.
- "Requested node configuration is not available" for a request that some
  node could hold while no node is schedulable: the message was seen only for
  requests that no node can satisfy, and the rules say only that it may also
  mean no schedulable node.
- A `submit` from inside a job that creates a job (only `submit --preflight`
  ran there), and an in-job `cancel`, `update` or `release`.
- A GPU job: what a job sees with `--gpus` (`CUDA_VISIBLE_DEVICES`, CUDA MPS).
- An agent session on a login node: `shk` ran there, but not inside an agent
  session on that node.
- The source of `PROJECT_ID`, `SCRATCH` and `PROJECTS` in non-login shells, the
  lua job-submit plugin, the sshd configuration and the transfer node.

### Signals and requeues in Slurm's source

The in-job guidance (the walltime template of the Marlowe skill, the rule
never to rely on a signal) rests on these. They are confirmed in the official
Slurm source at tag `slurm-25-05-2-1`, the release Marlowe runs, or inferred
from it where a statement says so, and are not observed live, except where an
item says what [shk inside a batch job](#shk-inside-a-batch-job) observed. The
Marlowe settings they use are those recorded above; a setting that was not
read is named as such.

- When a job is selected for preemption with a grace time, the controller
  sends SIGCONT and SIGTERM to the job's steps other than the batch step: the
  batch shell and the processes it started directly get nothing
  ([`src/interfaces/preempt.c`, lines 416-432](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/interfaces/preempt.c#L416);
  [`src/slurmctld/job_mgr.c`, lines 17062-17067](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L17062)).
  With PreemptType `preempt/qos` the grace time is the GraceTime of the job's
  QOS
  ([`src/plugins/preempt/qos/preempt_qos.c`, lines 107-113](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/plugins/preempt/qos/preempt_qos.c#L107)).
- A `--signal` is not sent during that window: the user signal goes out at
  selection only with the PreemptParameters option `send_user_signal`, which
  Marlowe does not set, or the older SlurmctldParameters option
  `preempt_send_user_signal` (Marlowe's SlurmctldParameters not read), and
  the time-limit check skips a job selected for preemption
  ([`src/slurmctld/node_scheduler.c`, lines 2130-2137](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/node_scheduler.c#L2130);
  [`src/interfaces/preempt.c`, lines 425-431](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/interfaces/preempt.c#L425);
  [`src/slurmctld/job_mgr.c`, lines 9248-9253](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L9248)
  and [line 9269](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L9269)).
- At the end of the window the job is requeued: every step, the batch step
  included, gets SIGCONT and SIGTERM, and whatever is left gets SIGKILL after
  KillWait. An accepted `scontrol requeue` and the time limit end an attempt
  the same way
  ([`src/interfaces/preempt.c`, lines 498-501](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/interfaces/preempt.c#L498);
  [`src/slurmd/slurmd/req.c`, lines 4173-4185](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmd/slurmd/req.c#L4173),
  [5428-5442](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmd/slurmd/req.c#L5428)
  and [5492-5505](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmd/slurmd/req.c#L5492)).
  For an accepted self-requeue the batch step's SIGTERM is
  [observed](#shk-inside-a-batch-job): the step ended CANCELLED with exit code
  `0:15`.
- A batch shell without a TERM trap dies of that SIGTERM (shell semantics),
  and slurmstepd then kills the rest of the step at once with SIGKILL, so
  KillWait applies only to a shell that survives SIGTERM
  ([`src/slurmd/slurmstepd/mgr.c`, lines 1751-1774](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmd/slurmstepd/mgr.c#L1751)).
  The shell's death is [observed](#shk-inside-a-batch-job) after an accepted
  self-requeue: the script line after the request never ran.
- With ProctrackType `proctrack/cgroup`, a signal to a step reaches only the
  processes that slurmstepd started, for the batch step the batch shell; their
  children get only SIGKILL, unless cgroup.conf sets `SignalChildrenProcesses`
  ([`src/plugins/proctrack/cgroup/proctrack_cgroup.c`, lines 220-239](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/plugins/proctrack/cgroup/proctrack_cgroup.c#L220);
  [`doc/man/man5/cgroup.conf.5`, lines 228-231](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/doc/man/man5/cgroup.conf.5#L228)).
  Marlowe's cgroup.conf, [read in a job](#shk-inside-a-batch-job), holds no
  `SignalChildrenProcesses` line, so the manual's default, `no`, applies.
- `srun` turns a SIGTERM it receives into SIGKILL of its tasks
  ([`src/srun/srun_job.c`, lines 2262-2268](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/srun/srun_job.c#L2262)).
- A batch script that exits by itself during the window is requeued through
  the completion path and recorded PREEMPTED, which raises the count that
  MaxBatchRequeue bounds: with MaxBatchRequeue 4 the fourth such exit holds
  the job (REQUEUE_HOLD) until an operator releases it. Failed launches raise
  the same count
  ([`src/slurmctld/proc_req.c`, lines 2128-2131](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/proc_req.c#L2128);
  [`src/slurmctld/job_mgr.c`, lines 6046-6101](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L6046),
  [5957-5972](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L5957)
  and [14024-14029](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L14024)).
- An owner's `scontrol requeue` of the own running job is accepted when the
  job's Requeue flag is set and does not count toward MaxBatchRequeue; the
  new attempt can still be held after a failed launch
  ([`src/slurmctld/job_mgr.c`, lines 17649-17654](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L17649)
  and [17835-17857](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L17835)).
  [Observed](#shk-inside-a-batch-job) once: the self-requeue was accepted and
  left `BatchFlag` unchanged, 1 in both attempts.
- `SLURM_RESTART_COUNT` is unset in the first attempt and counts every restart
  afterwards, preemptions included: a requeue by the owner, a preemption and a
  completion-path requeue all raise it
  ([`src/common/env.c`, lines 810-813](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/common/env.c#L810);
  [`src/slurmctld/job_mgr.c`, line 17757](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L17757)
  and [line 6080](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L6080)).
  [Observed](#shk-inside-a-batch-job) for a requeue by the owner: unset in the
  first attempt, 1 in the second, with `Restarts=1`.
- A requeued job waits again: its begin time is the end of the attempt's
  cleanup plus the credential lifetime and one second, a delay of about two
  minutes by Slurm's default credential lifetime, which applies on Marlowe
  (`AuthInfo` is unset), and its submit time and its age priority start over,
  age being Marlowe's largest priority weight
  ([`src/slurmctld/job_mgr.c`, lines 16600-16630](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L16600);
  [`src/interfaces/cred.h`, line 67](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/interfaces/cred.h#L67)).
  [Observed](#shk-inside-a-batch-job) after a self-requeue: the submit time
  was reset to the second after the requeue, the job became eligible 121 s
  later and started 8 s after that.
- With `--signal=B:SIG@T`, SIG goes to the batch shell alone, once per
  attempt, when about T to T+30 s remain before the time limit; the manual
  allows up to 60 s earlier, and a busy controller can send it later. The
  time limit ends the attempt as TIMEOUT, with no requeue, so a requeue for
  the walltime has to come before the limit
  ([`src/slurmctld/job_mgr.c`, lines 19237-19264](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L19237),
  [17808-17809](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L17808)
  and [9583-9598](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L9583);
  [`src/slurmd/slurmstepd/req.c`, lines 1220-1226](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmd/slurmstepd/req.c#L1220);
  [`doc/man/man1/sbatch.1`, lines 2303-2304](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/doc/man/man1/sbatch.1#L2303)).
  [Observed](#shk-inside-a-batch-job) with `--signal=B:USR1@300` and a 600 s
  limit: USR1 reached the batch shell 301 s before the limit.
- Inferred: after a preemption that ended at the end of its grace window, or a
  self-requeue during one, the job keeps a flag that only the completion path
  clears, so the next time the script exits by itself, with any exit code,
  the attempt can be recorded PREEMPTED and the script started once more
  ([`src/interfaces/preempt.c`, line 432](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/interfaces/preempt.c#L432);
  [`src/slurmctld/job_mgr.c`, lines 6053-6057](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/job_mgr.c#L6053);
  [`src/slurmctld/proc_req.c`, lines 2128-2131](https://github.com/SchedMD/slurm/blob/slurm-25-05-2-1/src/slurmctld/proc_req.c#L2128)).
