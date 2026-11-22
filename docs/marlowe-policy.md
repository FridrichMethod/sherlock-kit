# Marlowe policy: provenance and toolkit choices

The delivered Marlowe policy is two rules files,
`src/sherlock_kit/data/rules/marlowe-workstation.md` (agents that drive Marlowe
from a workstation over the user's `marlowe-plain` master) and
`src/sherlock_kit/data/rules/marlowe-cluster.md` (agents running on a Marlowe
login node), plus the skill
`src/sherlock_kit/data/skills/sherlock-kit-marlowe/SKILL.md`. They are short
operational summaries, not copies. Marlowe's own documentation binds every user
and takes precedence. Marlowe has no `/etc/agents` and no file written for
agents: the documentation is the only site policy source. Each rules file
stands alone; the Sherlock rules and their provenance
([Sherlock policy](sherlock-policy.md)) are separate.

## Source

The rules follow these pages of <https://marlowe-research.stanford.edu> and
one University IT page, all retrieved 2026-11-21. The documentation carries no
version or modification date of its own, so the retrieval date is the only
version mark.

| Page | Path | Used for |
|---|---|---|
| Usage Violations | `/documentation/violations/` | the authenticated-login-path restriction and its examples, login-node use, the quoted university provisions on unauthorized access and interference |
| SLURM on Marlowe | `/documentation/slurm/` | accounts, partitions and QOS, limits that change with demand, preemption and requeue |
| Saving Checkpoints | `/documentation/getting-started/saving-checkpoints/` | checkpoints on scratch, atomic writes, resuming from the newest checkpoint, restart from the top of the script, requeue as the default |
| Filesystems | `/documentation/getting-started/filesystems/` | home, `/projects` and `/scratch`, backups |
| Connecting | `/documentation/getting-started/connecting/` | SSH with SUNet ID, password and Duo, connection reuse, failed logins blocking the client address |
| FAQ | `/documentation/faq/` | usage accounting, preemption, the scratch purge, node-local scratch, GPUs only on request, code servers on login nodes, environments off home, transfer methods by size |
| Globus | `/documentation/getting-started/globus/` | the Globus transfer node |
| Open OnDemand | `/documentation/openondemand/` | self-run code servers only inside a job |
| Conda | `/documentation/software/conda/` | environments outside the home directory |
| Apply for Access | `/access/` | recharge: billing of GPU-hours, CPU-hours and project storage to the research account |
| Tech Specs | `/documentation/specs/` | identical DGX H100 compute nodes |
| About | `/about/` | the data classification, "Low & Moderate Risk" |
| Risk Classifications (University IT) | `https://uit.stanford.edu/guide/riskclassifications` | Marlowe approved for low and moderate risk data, not for general high risk data and not for PHI |

The older documentation site, `docs.marlowe.stanford.edu`, which the submit
plugin's account error still names, now points to the pages above; its
archived copies carry other partition limits and are not used.

Both rules files carry
`Reviewed 2026-11-21 against https://marlowe-research.stanford.edu/documentation/ .`,
the date of their last comparison with these pages. Before a rules change that
touches site policy, re-read every page in the table, compare it with both
rules files and the skill, then set the `Reviewed` date to the date of that
re-read in both rules files (and in any test that pins it), whether or not a
page changed, and update this guide.

## Kinds of statement

Each rules sentence is one of:

- **site**: stated by a documentation page above;
- **owner**: the user's choice, recorded under [owner choices](#owner-choices);
- **toolkit**: a `shk` rule, often stricter than the site;
- **observed**: a fact seen live on Marlowe on 2026-11-21, recorded in
  [observed Marlowe behavior](validation/observed-marlowe.md).

The rules files label site and owner statements (`Site:`, `Owner:`, or
"(site)" after a reason) and one observed statement (`Observed:`); unlabeled
sentences are toolkit rules or observed facts. "W" below is the workstation
file, "C" the cluster file.

## Where each rules sentence comes from

### Introduction

| Statement | File | Kind | Source |
|---|---|---|---|
| Marlowe only; Sherlock work follows the Sherlock rules; a task lives on one cluster | W, C | toolkit | one site per configuration and distinct `project` values per cluster |
| Marlowe has no `/etc/agents`; the documentation is the policy and takes precedence, above all Usage Violations | W, C | observed, site | [hosts](validation/observed-marlowe.md#hosts-shells-and-identity); Usage Violations names the university policy and Marlowe's operating restrictions |
| Do not carry over `/etc/agents`, `sh_*` helpers, `$GROUP_HOME`, `$OAK`, `$L_SCRATCH_JOB`, the 90-day purge and its sanction, `sherlock-dtn`, GPU model constraints, `--prefer` tiers, shk inside jobs | W, C | toolkit | each is Sherlock's: the variables are absent on Marlowe ([storage](validation/observed-marlowe.md#storage)), Marlowe documents no purge period, it has no GPU model features ([nodes](validation/observed-marlowe.md#nodes)), and this release exports no site into jobs |
| The labels; keep this in handoffs | W, C | toolkit | format of the rules files |
| Reviewed date | W, C | toolkit | this guide |

### Site rules

| Statement | File | Kind | Source |
|---|---|---|---|
| Run nothing that lets a person or a service reach Marlowe without SUNet ID, password and Duo; the examples (websocket tunnels and remote-control servers such as SSHX and Claude Remote Control/Server, userspace VPNs such as Tailscale and ZeroTier, editor web tunnels of VS Code, Cursor and Windsurf) | W, C | site | Usage Violations, "Do not provide access to Marlowe outside the authenticated login path" |
| The user's own SSH forwarding through that login is fine; open no tunnel or forward yourself | W, C | site, toolkit | Usage Violations: "Ordinary SSH tunnels and port forwarding are fine, because they still require your password and Duo"; that the agent opens none itself is a toolkit rule |
| Own code servers on login nodes are among them | W, C | site | FAQ, "What's the recommended way to use VSCode on a compute node?"; Open OnDemand, Code Server |
| W: the open master passed that gate; never share it, credentials or this session, including through a remote-control relay | W | site | Usage Violations, the quoted provisions against enabling unauthorized access and sharing an authenticator, read with the restriction above: a relay of a session that holds the master lets a service reach Marlowe without the gate |
| C: never start a remote-control server for this session; never share credentials or this session | C | site | as above |
| Login nodes are shared, for editing, compiling, submitting jobs and moving data, with short, light commands; sustained, server-like or CPU- or memory-heavy work runs on a compute node through Slurm | W, C | site | Usage Violations, "Do not degrade the shared nodes" (it names inference servers among its examples) |
| Never authenticate, retry a login or store passwords or Duo secrets | W, C | toolkit | `shk` never logs in; the reason, that repeated failed logins can block the client address on the login nodes, is from Connecting |
| Never contact Marlowe staff or post in their channels; draft the message for the user (C adds: stop affected work) | W, C | toolkit | the same rule as on Sherlock; the user decides what reaches the Marlowe team |

### Connection (W) and where you are (C)

| Statement | File | Kind | Source |
|---|---|---|---|
| Agent sessions on the workstation may drive Marlowe over the open master, as a session on Marlowe would | W | owner | owner decision 1 below |
| Every Marlowe command is `shk --site marlowe ...`, with `--project-dir DIR` when the Marlowe configuration is not the nearest; `site_mismatch` means the nearest one is another cluster's | W, C | toolkit | site selection by flag, then configuration, then Sherlock; a flag that names another site than the found configuration is refused |
| A reply that names its `executor` without `"site": "marlowe"` came from Sherlock | W, C | toolkit | replies of a non-Sherlock site carry `site` beside `executor` |
| Light reads may use `ssh -oBatchMode=yes -oProxyCommand=false marlowe-plain CMD`; that PATH has no Slurm clients | W | toolkit, observed | [Slurm client environment](validation/observed-marlowe.md#slurm-client-environment) |
| Exit 4 handling: `auth_required`, a `~/.ssh/config` hint, sandbox and socket denials, other exit 4 | W | toolkit | the same handling as on Sherlock, with the Marlowe master and doctor command |
| `hostname` and `$SLURM_JOB_ID` tell a login node (`login-NN`, no job id, no GPUs) from a job (`nNN`); the names are generic vendor defaults | C | observed | [hosts](validation/observed-marlowe.md#hosts-shells-and-identity), [login nodes](validation/observed-marlowe.md#login-nodes) |
| Agent sessions on login nodes are allowed | C | owner | owner decision 4 below |
| On a login node `shk --site marlowe` runs Slurm locally by itself | C | toolkit | the local executor is chosen when the selected site's Slurm configuration file exists on the machine |

### Login nodes and software (C)

| Statement | File | Kind | Source |
|---|---|---|---|
| Environments may be built on a login node | C | owner | owner decision 4 below; the site lists compiling among login-node uses |
| Large environments go under `/projects` or scratch, not the small home | C | site | Filesystems ("For any large files or conda installs, we recommend using a different filesystem"), Conda, FAQ |
| shk needs Python 3.11 or newer; the system `python3` is older; use the `uv` module or a micromamba or conda environment | C | toolkit, observed | [hosts](validation/observed-marlowe.md#hosts-shells-and-identity); Conda |
| Nothing unattended or outliving the session: no servers, daemons, sync loops or background agents | C | toolkit | a reading of the site's login-node rule: long-running and server-like work belongs on compute nodes |

### Jobs

| Statement | File | Kind | Source |
|---|---|---|---|
| Submit, update and cancel only through shk | W, C | toolkit | as on Sherlock |
| `--partition preempt` with explicit CPUs, memory and walltime, GPUs with `--gpus N` | W, C | toolkit, site | the partition table lists only `preempt`; SLURM on Marlowe: base (Basic Access) accounts use `preempt`; the owner's account holds QOS normal only ([accounts](validation/observed-marlowe.md#partitions-qos-and-accounts)) |
| `srun` and `salloc` allocations are billed jobs too: only with the user's authorization and an explicit `--time` | W, C | toolkit | Apply for Access, recharge rates: usage is billed to the research account, and an allocation is usage; without `--time` the partition's time limit applies ([submission](validation/observed-marlowe.md#submission-verdicts)) |
| The account comes from the configuration's `account`, never `--account`, never a guess from `$PROJECT_ID` | W, C | toolkit, site | SLURM on Marlowe and FAQ: every job must name a valid account; the submit plugin refuses jobs without one even when a default account exists ([submission](validation/observed-marlowe.md#submission-verdicts)) |
| Submit first runs Slurm's test; a refusal is exit 3 with nothing staged | W, C | toolkit | every deterministic refusal on Marlowe is immediate at test time ([submission](validation/observed-marlowe.md#submission-verdicts)) |
| "Requested node configuration is not available" may also mean no schedulable node; read `resources --partition preempt --summary` first | W, C | toolkit | Slurm can give the same message when no node is schedulable, for example while all are drained |
| No GPU model `--constraint`, typed GRES or `--tmp`; no `#SBATCH --exclusive` | W, C | observed, toolkit | Slurm refuses the first three ([submission](validation/observed-marlowe.md#submission-verdicts)); `--exclusive` is accepted, takes all CPUs of a node, and CPU time is billed (Apply for Access) |
| Job scripts: `module` under plain `#!/bin/bash`, Slurm clients on PATH, `TMPDIR` is `/tmp`, job-local space wiped at job end, no GPU without `--gpus`, internet from compute nodes, login-shell environment capture | C | observed, site | [inside a batch job](validation/observed-marlowe.md#inside-a-batch-job); FAQ (node-local scratch wiped at job end, GPUs only when requested) |
| Limits are per project account, shared with its members and changed with demand | W, C | site, observed | SLURM on Marlowe and FAQ (the per-project GPU cap and time limit change with demand); [QOS limits](validation/observed-marlowe.md#partitions-qos-and-accounts) |
| Every array element counts toward the submit limit, whatever the throttle | W, C | observed | an array larger than the account's submit limit was refused ([submission](validation/observed-marlowe.md#submission-verdicts)); a throttle limits running elements, not the elements submitted |
| Reads at least 60 s apart, no polling loops; exit 5 runs `next`; exit 6 stops; C: a Slurm timeout is retried once after a minute | W, C | toolkit | as on Sherlock |
| Cancel only exact verified own jobs; never cancel and resubmit to adjust a queued job; `update` lowers walltime or an array's throttle | W, C | toolkit | a resubmission loses the queued job's age, the largest priority weight ([scheduler](validation/observed-marlowe.md#partitions-qos-and-accounts)) |

### Cost and waiting

| Statement | File | Kind | Source |
|---|---|---|---|
| Usage is charged to the PI per GPU-hour and per CPU-hour, CPU-only jobs included | W, C | site | Apply for Access, recharge rates: billing applies monthly to GPU-hours, CPU-hours and project storage, charged to the research account; FAQ; the site's chargeback script prices CPU and GPU time ([charging](validation/observed-marlowe.md#charging)) |
| Treat every attempt as billed, failed and requeued ones too | W, C | toolkit | every attempt holds an allocation for its elapsed time |
| A real submit or allocation needs the user's authorization of the campaign, stated as an upper bound (tasks x GPUs and CPUs x walltime x (1 + retries)) | W, C | owner, toolkit | owner decision 2 below; that it covers `srun` and `salloc` allocations too is the toolkit's |
| CPU-only work that is not the preparation or check of a Marlowe GPU job runs on the workstation or on Sherlock | W, C | owner | owner decision 2 below |
| The start time of a preflight or test-only reply is not a prediction; never relay it as one | W, C | observed | [test-only estimates](validation/observed-marlowe.md#test-only-estimates-and-real-starts) |
| Small GPU requests usually start within hours, requests near a whole node can wait days, shorter limits tend to start sooner | W, C | observed | [queue waits](validation/observed-marlowe.md#queue-waits) |
| After a submit report the job id and end the wait; check again when the user asks | W, C | toolkit | waits of hours to days make waiting in the session wasteful |

### Preemption

| Statement | File | Kind | Source |
|---|---|---|---|
| Batch or hero work can preempt a `preempt` job at any time; it is requeued and restarts from the top of its script | W, C | site | SLURM on Marlowe; FAQ, "My preempt job keeps stopping and restarting"; Saving Checkpoints |
| Preemption is routine, not an anomaly | W, C | observed | [preemption](validation/observed-marlowe.md#preemption-and-requeue) |
| Requeue stays on | W, C | owner, site | owner decision 3 below; Saving Checkpoints calls requeue the default on Marlowe |
| Atomic checkpoints on scratch every few minutes, resume from the newest | W, C | site | Saving Checkpoints (write to scratch, replace atomically, load the newest on restart); FAQ |
| The protected window is short | W, C | observed | a job becomes preemptible a few minutes after it starts ([preemption](validation/observed-marlowe.md#preemption-and-requeue)) |
| Never rely on a signal | W, C | toolkit | the signal sequence of a real preemption is not observed on Marlowe, and no usable signal window was seen on Sherlock |
| Stop when `SLURM_RESTART_COUNT` passes the authorized retry bound | W, C | toolkit, owner | preemption requeues a job many times ([preemption](validation/observed-marlowe.md#preemption-and-requeue)); the retry bound is part of the authorization (owner decision 2) |
| A job in REQUEUE_HOLD or held after a failed launch is reported with its job id and restart count and recovered only with the user's consent by `cancel` then `submit --resubmit REASON`, never `shk release` | W, C | toolkit | such holds occur ([queue](validation/observed-marlowe.md#partitions-qos-and-accounts)); `shk release` releases only holds that `submit --hold` set |

### Data

| Statement | File | Kind | Source |
|---|---|---|---|
| `remote_root` lies under `/scratch/PROJECT/USER` | W, C | toolkit, observed | per-user directories exist under the project's scratch ([storage](validation/observed-marlowe.md#storage)) |
| Scratch is shared by the project, not backed up and "may be purged"; `/projects` is backed up and billed; home is small | W, C | site | Filesystems; FAQ, "Is /scratch backed up, and can files be deleted automatically?"; Saving Checkpoints; Apply for Access (project storage rate) |
| Keep what must last in `/projects` (W adds: or on the workstation) | W, C | site, toolkit | Filesystems ("keep anything you cannot regenerate in /projects/") |
| Push and pull copy small and medium trees over the login-node master; large data goes through Globus, outside shk | W | site, toolkit | FAQ, "How do I migrate data off /scratch?" (Globus for large transfers, rsync and scp for small and medium ones); Globus; `shk` has no transfer-node alias for Marlowe |
| Pull verifies the job output's `SHA256SUMS` | W | toolkit | as on Sherlock |
| Push and pull are refused on Marlowe (exit 3 `transfer_on_marlowe`); transfers start outside | C | toolkit | the data is already on the cluster |
| Access only your own or explicitly shared files and jobs; visibility is not authorization | W, C | site | Usage Violations, the quoted provision on unauthorized access |
| Low and Moderate Risk data only; ask when unclear | W, C | site, toolkit | About ("Data Classification: Low & Moderate Risk"); the University IT risk classification table approves Marlowe for low and moderate risk data, not for general high risk data or PHI; asking when unclear is the toolkit's |

### Partition profiles

The closing block of both files is generated from
`src/sherlock_kit/data/partitions-marlowe.json`. Listing is the authorization
fact (toolkit). The `preempt` profile's preemptible and requeue values are site
facts (SLURM on Marlowe) and owner decision 3; its text repeats the site's
restart and charging statements and the per-account limits. `batch` and `hero`
are not listed: they need suffixed accounts and other QOS, which the owner's
account does not hold.

## Left out of the rules files

By design, the rules files leave out these site statements:

- every number the site may change: the `preempt` time limit and per-project
  GPU cap, the submit and running-job limits, default memory per CPU, the node
  counts and time limits of `batch` and `hero`, the home quota and the default
  scratch allocation, and the backup schedule. The site says that `preempt`
  limits change with demand; the dated values are in
  [observed Marlowe behavior](validation/observed-marlowe.md) only, and the test
  before submit reports a request that exceeds the current ones;
- the site's advice to check current limits with `sinfo` and `sacctmgr`: the
  Slurm clients are not on the PATH of a plain ssh command, and the test before
  submit and `shk resources` cover the agent's needs;
- the recharge rates and the free introductory GPU-hours for new PIs: rates
  change (the site gives notice), the free hours belong to suffixed accounts on
  `batch`, and the rules say only that usage is charged;
- suffixed accounts (`-pmNN`, `-plNN`, `-free`), the `batch` and `hero`
  partitions, the charging of a `-pl` account's GPU-hours on `preempt`, and
  `sreport` usage checks: this release covers `preempt` with the base account
  only;
- `squeue --start` and pending reasons for a start estimate: test-only
  estimates proved unrelated to real starts, and the pending reason is in
  `shk status --details`;
- connection reuse settings (the site suggests a short `ControlPersist`),
  `sshfs`, the unsupported VS Code and Cursor Remote - SSH extensions and the
  Open OnDemand apps: human setup, not agent rules;
- CUDA MPS (`module load mps`), NCCL settings, Apptainer cache and bind paths,
  Docker being unsupported, the `nvhpc` module and the site's example job
  script: software practice that does not change how shk is used; the cluster
  file and the skill carry the observed job-script facts instead;
- inode consolidation, the `storage-quota` helper, `chgrp` after moving files
  between projects, delayed quota release on `/projects`, and the advice not to
  write frequent checkpoints to `/projects`: recommended practice beyond the
  summary (the rules already put checkpoints on scratch);
- contacts (the Slack channel, support and policy addresses, security contacts)
  and the consequences of a violation: agents do not contact the site, and the
  rules state the restrictions, not their sanctions;
- the compliance columns of the University IT table (NIST SP 800-171, NIH
  controlled-access data, FAR 52.204-21, CMMC Level 1), none of which Marlowe
  meets: the rules carry the risk classes only, and "ask when unclear" covers
  regulated data.

## Toolkit rules stricter than the site

These are `shk` decisions, not site requirements:

- Every Marlowe command carries `--site marlowe`; a command run against the
  wrong configuration is refused instead of acting on Sherlock.
- Only `preempt` is listed, with the base account from the configuration,
  never guessed from `$PROJECT_ID` and never suffixed.
- A plain submit runs Slurm's test first and stages nothing on a refusal.
- Partition, CPUs, memory and walltime are explicit; Marlowe has no default
  partition or time, and the partition's default memory is not relied on.
- No GPU model constraint, typed GRES, `--tmp` or `#SBATCH --exclusive`.
- Reads at least 60 s apart and no polling loops; the site states no cadence.
- A queued job is changed with `shk update`, never cancelled and resubmitted;
  a job held after a requeue is recovered by cancel and resubmit with the
  user's consent, never released.
- Every attempt counts as billed.
- `push` and `pull` are refused on Marlowe and use the login-node master from
  outside; there is no transfer-node alias in `shk`.
- shk is not called inside Marlowe jobs in this release.
- Agents never authenticate and never contact Marlowe staff.

## Owner choices

Decisions of the toolkit's owner, labeled `Owner:` in the rules:

1. Agent sessions on the workstation may drive Marlowe over the user's open
   master, the same as an agent running on Marlowe; the site's restriction on
   access paths that bypass SUNet ID, password and Duo stays a site rule.
2. A real submit needs the user's authorization of a campaign: the agent states
   the upper bound (tasks x GPUs and CPUs x walltime x (1 + retries)) and the
   yes covers exactly that; no numeric cap is coded. CPU-only work that is not
   the preparation or check of a Marlowe GPU job runs on the workstation or on
   Sherlock.
3. Requeue is the default on `preempt`.
4. Agent sessions on Marlowe login nodes are allowed, and environments may be
   built there.
