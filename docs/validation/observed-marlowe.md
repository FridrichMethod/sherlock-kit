# Observed Marlowe and Slurm behavior

Facts seen live on Marlowe from one user's account on 2026-11-21, on Slurm
25.05.2, grouped by topic and, where one exists, with the `shk` behavior that
depends on it. They are observations of one day, not site guarantees: the site
changes its limits with demand, so re-check a fact before building on it. Site
policy is not taken from here; the [Marlowe policy guide](../marlowe-policy.md)
names its sources.

Identifiers are synthetic: `m000000` stands for the project id,
`marlowe-m000000` for its Slurm account and `USER` for the user name. Job ids,
user names, UIDs, network addresses and private configuration are not
reproduced; other users' jobs appear only as counts and percentages.

## How the observations were made

- `shk` itself has not run on Marlowe. Every observation used the user's
  existing `marlowe-plain` OpenSSH master with
  `ssh -oBatchMode=yes -oProxyCommand=false marlowe-plain ...`, which cannot
  log in.
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
  2.2 h with 1 to 4 h and 5.9 h with 4 to 8 h.
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
- Separate REQUEUED rows (21% of runs without a GPU, 92% of them shorter than
  60 s) look like launch failures, not preemptions.
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

## Not observed

- The signal sequence of a real preemption: whether and when a preempted job
  receives SIGTERM, and which grace time applies. The rules require periodic
  checkpoints and never rely on a signal.
- The requeue-hold path: how a job reaches REQUEUE_HOLD or a launch-failure
  hold, what `squeue`, `scontrol` and `sacct` show for it, and the recovery the
  rules prescribe (cancel, then resubmit with the user's consent).
- `shk`'s own path on Marlowe: no `shk` command has run there, so
  `doctor --remote`, `status`, `resources`, the test before submit, `submit`,
  `update`, `cancel`, `push` and `pull` through the prelude are not observed,
  nor the full text of a test-only estimate line that `shk`'s preflight
  parses.
- A GPU job: what a job sees with `--gpus` (`CUDA_VISIBLE_DEVICES`, CUDA MPS).
- An agent session on a login node.
- The source of `PROJECT_ID`, `SCRATCH` and `PROJECTS` in non-login shells, the
  lua job-submit plugin, the sshd configuration and the transfer node.
