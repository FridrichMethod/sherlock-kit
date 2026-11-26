# Sherlock policy: provenance and toolkit choices

The delivered policy is the two rules files,
`src/sherlock_kit/data/rules/workstation.md` (agents that reach Sherlock over
SSH) and `src/sherlock_kit/data/rules/cluster.md` (agents running on Sherlock).
They are short operational summaries, not copies: Sherlock's own files bind
every automated tool, whether it runs on a node or drives Sherlock remotely,
and take precedence. Only their numbered rules are requirements; the rest is
recommended practice.

## Source

The rules follow Sherlock's agent instructions, `/etc/agents/AGENTS.md` and the
four topic files beside it, as last modified on the dates below. Both rules
files carry `Reviewed 2026-11-25`, the date of their last comparison with those
files; `tests/test_release_metadata.py` pins that date.

| File | Last modified | Topics |
|---|---|---|
| `/etc/agents/AGENTS.md` | 2026-11-13 | conditions of use, where you are, numbered rules 1-12, support |
| `/etc/agents/policy.md` | 2026-10-13 | data risk, sanctions, prohibited mechanisms, unattended sessions, processes, agent notes |
| `/etc/agents/slurm.md` | 2026-10-01 | login and compute nodes, discovery, requests, monitoring, arrays, Slurm outages |
| `/etc/agents/software.md` | 2026-11-13 | modules, preference order, Python environments, compiling, licenses |
| `/etc/agents/storage.md` | 2026-11-12 | filesystems, purge, I/O patterns, transfers, usage |

Human documentation: <https://www.sherlock.stanford.edu/docs/>.

Read the site files from the workstation with an attended, read-only `cat`
over the user's existing control master, as the workstation rules file does for
`AGENTS.md`; the command rides the master and cannot log in. Before a rules
change that touches site policy, re-read all five site files, note each file's
last-modified date and compare any changed file with both rules files. Then set
the `Reviewed` date to the date of that re-read, whether or not a site file
changed, in both rules files and in `tests/test_release_metadata.py`, and
update the table above and the sections below.

## Where each site rule lands

| Site rule | Workstation file | Cluster file |
|---|---|---|
| 1 Login nodes | Connection (light, bounded calls only) | Login nodes and software |
| 2 Job requests | Jobs | Slurm |
| 3 Storage defaults | Data | Filesystems |
| 4 Purge | Data | Filesystems |
| 5 Access controls | Connection | Where you are |
| 6 Authorized access only | Data, Jobs | Filesystems, Slurm |
| 7 Data risk | Data | Filesystems |
| 8 Software installs | n/a | Login nodes and software |
| 9 System configuration | n/a | Login nodes and software |
| 10 Scheduler etiquette | Jobs | Slurm |
| 11 Filesystem etiquette | n/a | Filesystems |
| 12 Read current instructions | Introduction | Introduction |
| Support, agent notes | Connection (Codex sandbox) | Where you are, Login nodes and software |

How the rules files carry particular site rules:

- Rule 1 allows workflow managers that only submit and poll jobs; rule 10
  forbids scheduling future agent sessions. The rules files therefore allow
  bounded deterministic compute controllers inside an allocation, using
  `--executor local` without a workstation dependency, and forbid agent or LLM
  rescheduling. Site limits, login-node restrictions, the 60 s cadence and
  access controls still bind.
- Waiting for a job rests on three site texts. Rule 1 says that "workflow
  managers that only submit and poll jobs are fine". Rule 10 asks for at
  least 60 s between status checks, prefers dependencies to polling, says to
  "never leave `watch` or a shell loop polling the scheduler, a filesystem or
  a log file", and forbids scheduling an agent session to run on its own
  later (`scrontab`, `--begin`, self-resubmitting jobs). The "Unattended agent
  sessions" section of `policy.md` allows a bounded task that waits for a
  job, fixes and resubmits, then stops, and forbids supervisors, watchdogs,
  cron-like loops or scripts that keep agent sessions alive, restart them or
  feed them new prompts, and sessions designed to run indefinitely. The
  owner's reading, which the rules apply: a bounded `shk wait`, which must
  end (`--timeout` is required, at most 168 hours), reads at least 60 s
  apart, belongs to the attended session and exits by itself, is such a
  workflow manager poll, not a loop left behind; detaching it (`nohup`, `&`
  past the session, `setsid`, `tmux` or `screen` left running, cron) or
  using it to re-prompt an agent is not.
- The workstation file therefore offers two ways to wait after a submit: end
  the turn and check when the user asks, or run one bounded `shk wait` as a
  background command whose exit the agent is told of, and act on its
  outcome. It keeps reads by hand for
  when they are needed (a wait exited, the user asks, an exit 5 `next`),
  never under 60 s apart and otherwise rare, prefers `--after-ok`, and
  forbids `watch`, a loop of `shk status`, a detached wait and scheduling or
  re-prompting agents. The cluster file allows `shk wait` on a login node
  only in the attended session, never detached, and inside an allocation for
  a bounded deterministic controller. The skill gives the contract, what to
  do after each outcome and the harness note: run the wait as a background
  command that the harness tracks and whose exit it reports, never with a
  shell `&`, which nothing reports and which can outlive the session; where
  the harness reports no such exit, run it in the foreground with a
  `--timeout` within the tool-call limit, or end the turn. It also asks for at
  most one wait at a time, over every name to watch.
- Rule 1 sends sustained computation, significant CPU or memory use, GPUs,
  intensive I/O and many processes to the scheduler and allows light editing,
  inspection and job preparation on login nodes; `software.md` (recommended
  practice) runs compiling and heavy package installs inside a job (`sh_dev`
  for an interactive session, or `sbatch`), never on a login node. Login nodes
  have no GPUs. The rules files therefore allow light work on a login node
  (the workstation file's light commands, small pure-Python installs and
  checks of seconds over `sherlock-plain`, the cluster file's login-node list)
  and send heavy work, compiling, heavy installs and tests beyond seconds to
  jobs. The boundary is "seconds". The small-install allowance is the
  toolkit's reading of rule 1's "job preparation": the site's human Python
  page (recommended practice) treats it more strictly and says that building
  virtual environments "should be done on a compute node", so the allowance
  stops at pure-Python installs, with no compiling and no Conda, CUDA or ML
  stacks. The skill's "Short work" section holds the ladder from the
  workstation to the production job; the installation and campaign guides
  apply it to environments.
- Quick work in a job is one `dev` run, `srun --partition=dev --time=MIN CMD`,
  named that way in the `dev` profile, the `batch_not_allowed` hint and the
  skill. A noninteractive `srun` on `dev` is not a documented site example;
  it is the toolkit's reading. It rests on the site's own rejection message
  for batch jobs on `dev`, "reserved for interactive sessions
  (salloc/srun/sdev and OnDemand)"
  ([observed](validation/observed-slurm.md#submission)), on the GPU page's
  one-shot `srun -p gpu -G 1 nvidia-smi`, on `software.md`'s "inside a job
  (`sh_dev` for an interactive session, or `sbatch`)" and on `slurm.md`'s
  preference for a blocking `sbatch --wait` over a polling loop, all
  recommended practice except the observed message. Rule 2 asks for the
  explicit time limit. `sh_dev` takes no command and opens an interactive
  shell, and a command given to `salloc` runs on the login node where
  `salloc` was invoked, so neither is the agent's path. The form was
  [observed on CPU only](validation/observed-slurm.md#the-dev-partition);
  GPUs on `dev`, the refusal path of `--immediate` and the per-user caps are
  not observed.
- Rule 1 forbids a login-node process "that outlives your interactive
  session" and arranging for work "to survive your disconnection". A `dev`
  run's `srun` client is a light process on the login node that belongs to
  one blocking, attended call, never detached (`nohup`, a plain `&`, `tmux`,
  `screen`, `salloc --no-shell`). When the local call is lost or killed, the
  job keeps running and its `srun` client stays on the login node until the
  script ends or the time limit
  ([observed](validation/observed-slurm.md#the-dev-partition)). The toolkit
  reads the clause as asking that such a client be ended, not left behind:
  keep `--time` tight, give the tool call a timeout above `--time` plus a
  start allowance, and after a lost call wait 60 s, read the job once with
  `shk status --job-id N` and end it with `scancel N`, the exact id (rule 6
  stops jobs with `scancel`; `shk cancel` addresses only shk job names).
  Work that does not fit a bounded call moves to a batch job.
- Rule 10 says to "aggregate short tasks so that the work inside a job lasts
  at least 10 minutes, ideally 30". Both rules files repeat it with that verb
  for every job (the workstation file adds array tasks). The toolkit's
  reading, approved by the owner on 2026-11-25: one bundled `dev` run for a
  task's checks may take less than 10 minutes; a planned series of short
  runs, a retry loop and padding are what the clause forbids, and a rerun
  after a diagnosed failure is fine. Batch jobs and array tasks keep the
  10-minute line. The basis is the clause's verb, "aggregate"; the site's
  sentence "Refuse only what these rules explicitly prohibit" (Conditions of
  use); and the site's own seconds-long one-shot `srun` examples on the GPU
  page and its "small representative test" before scaling up (Before
  significant work), both recommended practice. Neither the rules files nor
  the skill state the reading as an exemption. The skill's operating text:
  one run per task with every pending check in one script, never a planned
  series of short runs, a retry loop or padding, and one rerun once the cause
  of a failure is found; work that exceeds `dev` goes to a short batch job
  through `shk submit`, aggregated to 10 useful minutes or more.
- Rule 5 forbids any SSH server on any node, login or compute, under any name
  or port, bound to localhost, inside a job or a container, or attended; a tool
  that starts one is disabled or not used, and running one can lead to
  suspension. `policy.md` repeats it under "Sanctions" and "Processes and
  cleanup". Both rules files carry the ban and forbid sharing credentials or
  sessions.
- The site's numbered ownership rule permits stopping only one's own processes
  and uses scheduler cancellation for jobs; it imposes no per-job user consent.
  Routine exact cancellation inside an authorized task therefore retains the
  task authorization, and the agent tells the user why; ambiguous, unrelated,
  broad or materially destructive cancellation requires clarification. This
  guidance rests on that reading of the site rules, not on a live test.
- Rule 12 (read current instructions) requires reading `AGENTS.md` at the start
  of each session on Sherlock and after resuming from a summary or context
  compaction, and the relevant topic files before continuing; memory, summaries
  and an earlier session's read are no substitute, handoff summaries keep the
  requirement, and instruction reminders are never filtered out. Both files
  carry it in their introductions in compressed form: read at session start
  and after compaction, keep the requirement in handoffs, memory is no
  substitute, and reminders are not filtered. `shk` removes the login banner
  from the output it parses but writes each banner line once per process to
  stderr, so the reminder still reaches the agent.
- The site sanctions purge evasion with account termination on the first
  occurrence. Both files forbid extending scratch lifetime by touch, rewrite,
  copy-over or refresh helpers, even once, and name that sanction; the site
  also forbids recommending it.
- On login nodes the cluster file allows listeners only for the user's own
  interactive tools and forbids model, web or API serving, as the site does.
- The cluster file names DTN and Globus transfers rather than all external
  transfers, since fetching sources from Sherlock is allowed.
- `slurm.md` asks to set the application's thread count to the allocation. The
  cluster file's `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK` yields to
  `--cpu-thread-budget`, an opt-in budget of at most the allocation.
- The site's Codex note requires per-command approval when the Codex sandbox
  cannot start, never disabling it. Both files carry it, and the workstation
  file tells local access denials apart from absent SSH masters.
- Both files say to discover limits and features and name no command for it.
  The skill names `sh_part` and `sh_node_feat`, which exist in a login shell
  only; a non-login ssh command does not find them.
- No agent module is named: the site files name none, and agents find modules
  with `ml spider`.

The rules files also say that `dev` is interactive only, so `shk submit`
refuses it. That `dev` takes no batch jobs comes from
[observed Slurm behavior](validation/observed-slurm.md#submission), not from
the site files: Sherlock rejects them, so `shk submit` refuses them offline.

## Left out of the rules files

By design, the rules files leave out:

- recommended site practice not needed by the operational summary;
- rule 11's preference for fewer, larger files;
- in the workstation file, rule 3's naming of filesystems by variable
  (`remote_root` takes a literal absolute path, a kit choice) and "alternate
  login services" in its list of forbidden mechanisms.

To stay within the 7 KiB cap, they also leave out text that is implied or
stated elsewhere:

- a statement that everything but the numbered rules is recommended practice,
  which "only numbered rules are requirements" implies;
- the `sacct` field list, which the site's `slurm.md` gives;
- in the cluster file, `shk doctor --remote`, the note that `shk doctor` is
  read-only and the `[tool.sherlock-kit]` configuration line that the
  workstation file has; the skill covers all three;
- in the workstation file, a separate item saying that `owners` jobs must
  checkpoint periodically, which the `owners` profile and the skill state, and
  the "UID/name-scoped" qualifier for `shk cancel`, which has no other scope;
- any note on SIGTERM handling: live requeues gave no usable SIGTERM window
  ([observed live](validation/observed-slurm.md#requeue-and-preemption)), and
  the `owners` profile requires scripts to checkpoint and resume instead;
- in the cluster file, that `shk push` and `shk pull` refuse to run on
  Sherlock, which `shk` enforces (exit 3) and the skill states;
- in both files, the limits of `shk update` (one field group per call, a time
  limit only decreased, a partition only within the job's requeue and
  preemptible class and never a borrowed one), which `shk` enforces (exit 1
  or 3) and the skill and the [command reference](cli.md#update) state;
- on `bigmem` and `service`, the reminder that Sherlock's own limits apply,
  which holds for every partition: both files say to discover limits, and the
  site's own discovery commands (`sh_part`, `scontrol show partition`) show
  them;
- in the cluster file, `scontrol show job $SLURM_JOB_ID` for an allocation's
  limits and elapsed time, one of the site's own discovery commands, which
  `AGENTS.md` names under "First, find out where you are";
- in the workstation file, that `submit --dry-run` needs no socket, which the
  skill's setup check and the [installation guide](install.md) state;
- in the GPU scheduling defaults, the comparison of authorized partitions by
  queue, setup, compute and recovery time and the pointer to department and
  `owners` partitions, which the skill and the
  [campaign guide](campaign-throughput.md) carry beside the earliest-start
  priority. That section also does not repeat four statements that each file
  makes elsewhere: the borrowed budgets, which the partition profiles give; the
  aggregation of short tasks into 10 useful minutes per job and the ban on
  padding, the discovery of partition limits and features, and the 60 s cadence
  for status and resources checks, which the workstation file's Jobs and the
  cluster file's Slurm section give;
- in the GPU scheduling defaults, the check of a real compatibility
  uncertainty and the reading of `resources --summary` slots for the eligible
  GPU features only, which the skill and the campaign guide give;
- the `submit --preflight` estimate and the `resources --summary` output
  fields, and in the cluster file the compatible partition list of `submit`,
  which the skill describes.

## Toolkit choices stricter than the site

These are `shk` decisions, labeled as such in the rules, not site requirements:

- Partition, CPUs, memory and walltime are always explicit (the site requires
  only the walltime).
- Only partitions listed in `partitions.json` are accepted; listing is the
  authorization fact, and scheduler visibility never is.
- Borrowed partitions (`btrippe`, `possu`) need `shk occupancy` and
  `--courtesy-ack`; their budgets and the possu 00:00-07:00 Pacific window are
  the owner's courtesy terms, carried as text. Releasing a job held there counts
  as submitting it (same occupancy check, budget and window); `shk release`
  itself checks neither occupancy nor the clock. `shk update` refuses borrowed
  targets: a move to `btrippe` or `possu` (exit 3 `borrowed_not_allowed`) and
  a throttle raise on an array with a record on one of them; the caller
  cancels, runs `shk occupancy` and resubmits with `--courtesy-ack`. Lowering
  that throttle, or moving a pending job off a borrowed partition, is not
  refused.
- Requeue only where the profile allows it (`owners`); everything else is
  submitted with `--no-requeue`.
- Only `submit` and `update` (for a pending job) take a partition list;
  `resources` and `occupancy` take one name per flag. A list is one
  comma-separated value on the command line such as
  `--partition gpu,bioe,stat`, never a TOML key, and only for a compatible set,
  derived from the profile fields: every member is listed and takes batch
  jobs, none is borrowed, all share one `requeue` and one `preemptible` value,
  and every member allows GPUs when GPUs are requested. So `gpu`, `bioe` and
  `stat` combine, and `owners`, `btrippe` and `possu` stay alone. By the same
  profile fields `normal`, `bigmem` and `service` are in the class too: a set
  states that the job may run on any member, and every member's courtesy text
  applies wherever it starts.
- `#SBATCH` lines may not set the directives `shk` owns (account, exclude, GPUs,
  partition, array, dependency, begin, job name, hold, and comment when a
  submission token sets it) or start a heterogeneous job.
- A live job name is never submitted twice; a finished one only with
  `--resubmit REASON`.
- `shk cancel` is scoped to one job name and the current UID. Routine cancellation of exact verified own jobs in an authorized task may proceed; tell the user why, since `shk cancel` records no reason. Ask for unclear ownership/scope, unrelated work, broad cancellation, or material irreversible loss of important progress/results. Never cancel all of the user's jobs (`scancel -u $USER`), although the site does not forbid it.
- Remote work goes only through `shk` or bounded OpenSSH over the user's
  control masters, and batch jobs only through `shk submit`, never raw
  `sbatch` over ssh. The one job outside `shk` is a bounded `dev` run,
  `srun --partition=dev --time=MIN CMD`, since `dev` takes no batch jobs (see
  the rule 1 bullets under "Where each site rule lands").
- Changes to a pending job, and to an array's throttle, go through
  `shk update`, never raw `scontrol update`: one field group per call, a time
  limit only decreased (Slurm accepts a pending increase up to the partition's
  MaxTime), a partition only within the job's requeue and preemptible class
  and never a borrowed one, and an outcome settled by a re-read of every
  record rather than by the exit status of `scontrol`.
- Jobs on `owners` must checkpoint periodically (the site accepts checkpoint or
  requeue); live requeues gave no usable SIGTERM window
  (docs/validation/observed-slurm.md).
- Status reads are rarer than the site's 60 s floor: `shk wait` makes its
  first read 60 s after it starts, then reads every 5 minutes by default and
  backs off to 30 minutes while nothing runs or changes, and reads by hand
  outside a wait are rare, ideally 10 minutes or more apart. These are
  owner-approved defaults.
- `shk` never logs in (master-only preflight) and never retries a mutation.
- `push` and `pull` are refused on Sherlock: transfers start outside.
- No GPU work while developing or testing sherlock-kit itself.

## Owner scheduling preferences

The GPU scheduling sections of both delivered rules and the installed skill are
owner defaults, not Slurm enforcement or site rules. They favor the earliest
start, broad compatible GPU choices with a soft preference for compatible
L40/A/H and future B/newer GPUs, expected completion time across authorized
partitions (in the skill and the campaign guide), longer useful resumable
bundles, parallel ready work and bounded resource-aware rebalancing. Oversized
CPU requests are reduced: before GPU submission, requests sized to the
worker's useful threads and measured peak host RAM plus headroom are compared
by their `shk resources --summary --shape` slots, which count how many whole
requests fit in the GPUs, CPUs and scheduler memory still unallocated on each
node. Only the memory, kernel and dtype requirements that
have real evidence are kept, beside the site rules; RTX_2080Ti and Pascal-era
GPUs are avoided by default, and real architecture needs still narrow the
choice.

GPU models are scientifically equivalent: they may be mixed, and a job may get
another model after a requeue. The model alone is no reason for new controls,
cohorts, reruns or per-model qualification. Actual GPU model remains
provenance; it alone does not require new scientific cohorts or controls.
Effective numerical and model protocols remain fixed, and explicit project
requirements can narrow these defaults. No GPU/partition TOML keys are added.
`shk submit --prefer` forwards an optional Slurm soft constraint; the preference
is selected explicitly by the caller, not silently injected. Its tier is purely
a compute-capability ordering, so it applies across projects. `--prefer` is
soft: Slurm tries it first, then the fallback `--constraint`. The partition table, including btrippe and possu courtesy, is unchanged.

The candidate feature set comes from `sh_node_feat` and aggregate `shk resources`
evidence checked on 2026-11-14. It includes RTX_3090 (24 GB) and TITAN_V (12 GB),
in addition to A100, A40, H100, H200, L40S and V100 variants. The discovered
A30_MIG-1g.6gb is an interactive dev resource. B200 was not in that snapshot;
future compatible models should be included when discovered. This is inventory,
not a speed benchmark or a permanent compatibility allowlist. RTX_2080Ti and
Pascal-era P100/P40/TITAN_Xp are outside the owner's default production choices.

Site rule 8 permits explicitly requested scratch environments with a purge warning;
the cluster summary honors that choice and does not impose an additional ban.
Site rule 10 sets a polling floor and minimum useful job duration, not a maximum
bundle length. Longer jobs and periodic capacity checks still obey partition limits,
borrowed courtesy, at least 60 seconds between checks and the ban on polling loops;
one bounded `shk wait` at a time keeps both by construction.
