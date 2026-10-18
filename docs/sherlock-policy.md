# Sherlock policy: provenance and toolkit choices

The delivered policy is the two rules files,
`src/sherlock_kit/data/rules/workstation.md` (agents that reach Sherlock over
SSH) and `src/sherlock_kit/data/rules/cluster.md` (agents running on Sherlock).
They are short operational summaries, not copies: Sherlock's own files bind
every automated tool, whether it runs on a node or drives Sherlock remotely,
and take precedence. Only their numbered rules are requirements; the rest is
recommended practice.

## Source

Read on 2026-10-10 (Pacific) from a Sherlock login node, one attended read-only
`cat` over the existing control master:

| File | Last modified | Topics |
|---|---|---|
| `/etc/agents/AGENTS.md` | 2026-10-09 | conditions of use, where you are, numbered rules 1-11, support |
| `/etc/agents/policy.md` | 2026-09-28 | data risk, sanctions, prohibited mechanisms, unattended sessions, processes, agent notes |
| `/etc/agents/slurm.md` | 2026-10-01 | login and compute nodes, discovery, requests, monitoring, arrays, Slurm outages |
| `/etc/agents/software.md` | 2026-10-09 | modules, preference order, Python environments, compiling, licenses |
| `/etc/agents/storage.md` | 2026-10-06 | filesystems, purge, I/O patterns, transfers, usage |

Human documentation: <https://www.sherlock.stanford.edu/docs/>. The workstation
rules were rechecked on 2026-10-11 against the same five site files fetched
through the attended control master; the cluster rules retain their 2026-10-10
review. The added workstation sandbox guidance distinguishes local access
denials from absent SSH masters. The site's Codex guidance still requires
per-command approval when its sandbox cannot start, without disabling it.
Re-read the site files before changing the rules and update the review date
and this record.

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
therefore retains the task authorization, with a recorded reason; ambiguous,
unrelated, broad or materially destructive cancellation requires clarification.
No job was cancelled to test this wording.

## Where each site rule lands

| Site rule | Workstation file | Cluster file |
|---|---|---|
| 1 Login nodes | Connection (bounded calls only) | Where you are |
| 2 Job requests | Jobs | Slurm |
| 3 Storage defaults | Data | Filesystems |
| 4 Purge | Data | Filesystems |
| 5 Access controls | Connection | Where you are |
| 6 Authorized access only | Data | Filesystems, Slurm |
| 7 Data risk | Data | Filesystems |
| 8 Software installs | n/a | Software |
| 9 System configuration | n/a | Software |
| 10 Scheduler etiquette | Jobs | Slurm |
| 11 Filesystem etiquette | n/a | Filesystems |
| Support, agent notes | n/a | Where you are |

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
  the owner's courtesy terms, carried as text.
- Requeue only where the profile allows it (`owners`); everything else is
  submitted with `--no-requeue`.
- `#SBATCH` lines may not set the directives `shk` owns (account, exclude, GPUs,
  partition, array, dependency, begin, job name, hold, and comment when a
  submission token sets it) or start a heterogeneous job.
- A live job name is never submitted twice; a finished one only with
  `--resubmit REASON`.
- `shk cancel` is scoped to one job name and the current UID. Routine cancellation of exact verified own jobs in an authorized task may proceed; record the reason. Ask for unclear ownership/scope, unrelated work, broad cancellation, or material irreversible loss of important progress/results.
- `shk` never logs in (master-only preflight) and never retries a mutation.
- `push` and `pull` are refused on Sherlock: transfers start outside.
- No GPU work while developing or testing sherlock-kit itself.
