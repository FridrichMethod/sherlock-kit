# Sherlock policy: provenance and toolkit choices

The delivered policy is the two rules files,
`src/sherlock_kit/data/rules/workstation.md` (agents that reach Sherlock over
SSH) and `src/sherlock_kit/data/rules/cluster.md` (agents running on Sherlock).
They are short operational summaries, not copies: Sherlock's own files bind
every automated tool, whether it runs on a node or drives Sherlock remotely,
and take precedence. Only their numbered rules are requirements; the rest is
recommended practice.

## Source

First read on 2026-10-10 (Pacific) from a Sherlock login node, one attended
read-only `cat` over the existing control master. The last-modified dates are
the ones listed at the re-read of 2026-10-21 below; the re-reads before
2026-10-18 recorded none.

| File | Last modified | Topics |
|---|---|---|
| `/etc/agents/AGENTS.md` | 2026-10-19 | conditions of use, where you are, numbered rules 1-12, support |
| `/etc/agents/policy.md` | 2026-10-13 | data risk, sanctions, prohibited mechanisms, unattended sessions, processes, agent notes |
| `/etc/agents/slurm.md` | 2026-10-01 | login and compute nodes, discovery, requests, monitoring, arrays, Slurm outages |
| `/etc/agents/software.md` | 2026-10-09 | modules, preference order, Python environments, compiling, licenses |
| `/etc/agents/storage.md` | 2026-10-06 | filesystems, purge, I/O patterns, transfers, usage |

Human documentation: <https://www.sherlock.stanford.edu/docs/>. The workstation
rules were rechecked on 2026-10-11 against the same five site files fetched
through the attended control master. Since the 2026-10-21 re-read below, both
rules files carry that review date. The added workstation sandbox guidance
distinguishes local access denials from absent SSH masters. The site's Codex
guidance still requires per-command approval when its sandbox cannot start,
without disabling it. Re-read the site files before changing the rules and
update the review date and this record.

The lead re-read all five files on sh02-ln04 on 2026-10-12 via the existing
attended control master before the 0.5.0 wording changes. Site rule 1 explicitly
allows workflow managers that only submit and poll jobs. Rule 10 prohibits
scheduling future agent sessions. The updated rules distinguish bounded
deterministic compute controllers inside an allocation, using `--executor local`
without a workstation dependency, from agent or LLM rescheduling. Site limits,
login-node restrictions, the 60 s cadence and access controls still bind.

The lead re-read the site rules on 2026-10-13 before updating cancellation
guidance. The numbered ownership rule permits stopping only one's own
processes and uses scheduler cancellation for jobs. It does not impose
per-job user consent. Routine exact cancellation inside an authorized task
therefore retains the task authorization, and the agent tells the user why;
ambiguous, unrelated, broad or materially destructive cancellation requires
clarification.
No job was cancelled to test this wording.

The 0.5.0 rules edits of 2026-10-18 follow live Slurm observations and the
kit's own behavior, not a new reading of the site files, so the review date
stays 2026-10-13. They say that `dev` takes no batch jobs (Sherlock rejects
them, and `shk submit` refuses them offline), that releasing a job held on a
borrowed partition counts as submitting it, and that the cancellation reason
goes to the user, because `shk cancel` records none ("record a reason" had no
place to go). The profile texts also drop wording that the flags and the block
introduction already state.

The lead re-read all five site files on 2026-10-18 through the attended control
master and listed their modification dates. `AGENTS.md` and `policy.md` changed
on 2026-10-13. Against the copies read at 2026-10-11 04:26 UTC (2026-10-10
Pacific), the only change in `AGENTS.md` is in rule 5: no SSH server of any kind on any node, login or compute, under any name or
port, bound to localhost, inside a job or a container, or attended; a tool that
starts one is disabled or not used, and running one can lead to suspension.
`policy.md` repeats it under Sanctions and Processes and cleanup and drops "SSH
daemons" from its list of unattended mechanisms. Neither rules file carried the
ban, so the review of 2026-10-13 either preceded the change or missed it. Four
readers compared the site files with both rules files, and two refuters checked
each finding. Within the 7 KiB cap the rules files now say:

- Both files carry the SSH-server ban; the workstation file also says never to
  share credentials or sessions.
- Workstation: `sherlock-plain` is a login node for light commands; limits and
  features come from `sh_part` and `sh_node_feat` in a login shell (a
  non-login ssh command does not find them); intensive job I/O stays off
  `$HOME` and `$GROUP_HOME`; the agent works only with its own processes; a
  `--resubmit` reason without a diagnosis is only for a job that did not fail.
- Cluster: on login nodes no listener for anything but the user's own
  interactive tools and no model, web or API serving (the site allows the
  former and forbids the latter); no `watch` or shell polling loops; large or
  shared environments go in `$GROUP_HOME`; no probing of shared infrastructure;
  only one's own or shared directories, never merely readable ones; inspect and
  stop only one's own processes, and never stop a job by killing its processes.
- Both: no helping to extend scratch lifetime (the site also forbids
  recommending it); the cluster file names DTN and Globus transfers rather than
  all external transfers, since fetching sources from Sherlock is allowed.

The additions are paid for by shorter wording and by dropping text found
elsewhere: the `sacct` field list (slurm.md), `shk doctor --remote` and the
read-only note on `shk doctor` in the cluster file (both in the skill), and the
SIGTERM remark, whose evidence stays in docs/validation/observed-slurm.md while
"checkpoint periodically" remains. Left out on purpose, as before: rule 11's
preference for fewer, larger files; rule 3's naming of filesystems by variable
in the workstation file (`remote_root` takes a literal absolute path, a kit
choice); "alternate login services" in the workstation list; and recommended
practice generally. The review date is now 2026-10-18. Re-read the site files
before the next rules change that touches site policy.

The lead re-read the site files on 2026-10-21 through the attended control
master, before the 0.6.0 rules edits. The four topic files are byte for byte the
copies of 2026-10-18. `AGENTS.md` changed on 2026-10-19 only by a new rule 12,
read current instructions: read `AGENTS.md` at the start of each session on
Sherlock and after resuming from a summary or context compaction, and the
relevant topic files before continuing; memory, summaries and an earlier
session's read are not substitutes; handoff summaries keep the requirement; and
instruction reminders are not filtered out. Changes:

- Both rules files carry rule 12 in their introductions.
- `shk` still removes the login banner from the output it parses, but now writes
  each banner line once per process to stderr instead of dropping it.
- The cluster file's `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK` (slurm.md: set the
  application's thread count to the allocation) yields to `--cpu-thread-budget`,
  an opt-in budget at most the allocation.

To stay within the 7 KiB cap, both files drop "the rest is recommended practice"
(implied by "only numbered rules are requirements") and shorten the exit 5
wording. The cluster file also drops its configuration line, which the skill
covers. The workstation file also drops the owners checkpoint sentence, which
the owners profile and the skill keep, and "Keep `shk cancel` UID/name-scoped",
since `shk cancel` has no other scope. The review date is now 2026-10-21.

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

Corrections against the 0.3 policy text: purge evasion is termination of the
account on the first occurrence (it said suspension), and no agent module is
named (the site files name none; agents find modules with `ml spider`).

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
