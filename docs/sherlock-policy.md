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
files carry `Reviewed 2026-10-21`, the date of their last comparison with those
files; `tests/test_release_metadata.py` pins that date.

| File | Last modified | Topics |
|---|---|---|
| `/etc/agents/AGENTS.md` | 2026-10-19 | conditions of use, where you are, numbered rules 1-12, support |
| `/etc/agents/policy.md` | 2026-10-13 | data risk, sanctions, prohibited mechanisms, unattended sessions, processes, agent notes |
| `/etc/agents/slurm.md` | 2026-10-01 | login and compute nodes, discovery, requests, monitoring, arrays, Slurm outages |
| `/etc/agents/software.md` | 2026-10-09 | modules, preference order, Python environments, compiling, licenses |
| `/etc/agents/storage.md` | 2026-10-06 | filesystems, purge, I/O patterns, transfers, usage |

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
| 1 Login nodes | Connection (light, bounded calls only) | Login nodes |
| 2 Job requests | Jobs | Slurm |
| 3 Storage defaults | Data | Filesystems |
| 4 Purge | Data | Filesystems |
| 5 Access controls | Connection | Where you are |
| 6 Authorized access only | Data, Jobs | Filesystems, Slurm |
| 7 Data risk | Data | Filesystems |
| 8 Software installs | n/a | Software |
| 9 System configuration | n/a | Software |
| 10 Scheduler etiquette | Jobs | Slurm |
| 11 Filesystem etiquette | n/a | Filesystems |
| 12 Read current instructions | Introduction | Introduction |
| Support, agent notes | Connection (Codex sandbox) | Where you are, Login nodes |

How the rules files carry particular site rules:

- Rule 1 allows workflow managers that only submit and poll jobs; rule 10
  forbids scheduling future agent sessions. The rules files therefore allow
  bounded deterministic compute controllers inside an allocation, using
  `--executor local` without a workstation dependency, and forbid agent or LLM
  rescheduling. Site limits, login-node restrictions, the 60 s cadence and
  access controls still bind.
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
  carry it in their introductions. `shk` removes the login banner from the
  output it parses but writes each banner line once per process to stderr, so
  the reminder still reaches the agent.
- The site sanctions purge evasion with account termination on the first
  occurrence. Both files forbid helping to extend scratch lifetime; the site
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
- Both files take limits and features from `sh_part` and `sh_node_feat` in a
  login shell; a non-login ssh command does not find them.
- No agent module is named: the site files name none, and agents find modules
  with `ml spider`.

The rules files also say that `dev` takes no batch jobs. This comes from
[observed Slurm behavior](validation/observed-slurm.md#submission), not from
the site files: Sherlock rejects them, so `shk submit` refuses them offline.

## Left out of the rules files

By design, the rules files leave out:

- recommended practice generally;
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
  the `owners` profile requires scripts to checkpoint and resume instead.

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
  itself checks neither occupancy nor the clock.
- Requeue only where the profile allows it (`owners`); everything else is
  submitted with `--no-requeue`.
- `#SBATCH` lines may not set the directives `shk` owns (account, exclude, GPUs,
  partition, array, dependency, begin, job name, hold, and comment when a
  submission token sets it) or start a heterogeneous job.
- A live job name is never submitted twice; a finished one only with
  `--resubmit REASON`.
- `shk cancel` is scoped to one job name and the current UID. Routine cancellation of exact verified own jobs in an authorized task may proceed; tell the user why, since `shk cancel` records no reason. Ask for unclear ownership/scope, unrelated work, broad cancellation, or material irreversible loss of important progress/results. Never cancel all of the user's jobs (`scancel -u $USER`), although the site does not forbid it.
- Remote work goes only through `shk` or bounded OpenSSH over the user's
  control masters, and jobs only through `shk submit`, never raw `sbatch` over
  ssh.
- Jobs on `owners` must checkpoint periodically (the site accepts checkpoint or
  requeue); live requeues gave no usable SIGTERM window
  (docs/validation/observed-slurm.md).
- `shk` never logs in (master-only preflight) and never retries a mutation.
- `push` and `pull` are refused on Sherlock: transfers start outside.
- No GPU work while developing or testing sherlock-kit itself.
