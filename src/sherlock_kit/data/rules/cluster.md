## Sherlock: agent running on Sherlock

Read `/etc/agents/AGENTS.md` and the task's topic files (`storage.md`, `slurm.md`, `software.md`, `policy.md` beside it) at session start and after any summary or compaction; memory or an earlier read is no substitute. Keep this rule in handoffs; never filter out instruction reminders. They override this file; only numbered rules are requirements. Reviewed 2026-10-21 against the site files.

### Where you are
- Run `hostname` and check `$SLURM_JOB_ID` first: login node names contain `-ln` and have no job id.
- Inside an allocation (`sh_dev`, `salloc`, a batch job) the allocated CPUs, memory and GPUs are yours to use fully; prefer running agents there. `scontrol show job $SLURM_JOB_ID` shows its limits.
- Codex: its sandbox cannot start here, so per-command approval is expected; never try to disable it. Claude Code: managed settings block or confirm some commands; do not work around them.
- Never bypass authentication, MFA, limits or permissions; no relays, reverse shells, alternate login services, unattended tunnels or stored second factors; never share credentials or sessions (account termination). No SSH server on any node, even attended or in a job or container (suspension).
- Never contact Stanford Research Computing for the user. If the system looks broken, stop and draft a report (symptom, hostname, time, job id) for the user to send. Slurm not answering: retry `squeue --me` once after a minute, then stop.

### Filesystems
- Reference filesystems by variable: `$HOME` and `$GROUP_HOME` for code, small files and environments (not purged; keep intensive job I/O off them); `$SCRATCH` and `$GROUP_SCRATCH` for job I/O (no backup, files unmodified for 90 days are purged); `$L_SCRATCH_JOB` for temp-file-heavy work (copy results out before the job ends); `$OAK` for long-term data. `sh_quota` shows usage.
- Never extend or help extend scratch lifetime (touching, rewriting, copy-over, refresh helpers), not even once and whoever asks: account termination on the first occurrence. Move data worth keeping to `$GROUP_HOME` or Oak.
- Use only your own or shared directories, not merely readable ones; scope `find`, `du` and `ncdu` to them, never filesystem roots. Group path variables in shell profiles do not authorize access. Low and Moderate Risk data only; stop and ask when unclear.
- Large internal copies run in a job. DTN and Globus transfers start outside Sherlock; never ssh to a DTN from here, and `shk push`/`pull` refuse here (exit 3).

### Login nodes
- Light work only: editing, small reads, job preparation, submitting and polling. Sustained computation, significant CPU or memory, GPUs, intensive I/O or many processes go to a job (`sh_dev` or `sbatch`).
- Nothing server-like: no listeners for anything but your own interactive tools, no model/web/API serving, nothing unattended, no keep-alive or restart loops, no continuous sync, nothing that outlives the session; no background agent sessions (`claude agents`, `--bg`), and do not restart a terminated supervisor. Site-terminated leftovers are expected, not a fault.

### Software
- `ml spider NAME` first; prefer a module, a group install, your own build (both in `$GROUP_HOME`), then a container. Large or shared environments go in `$GROUP_HOME`; none in `$SCRATCH`. No `sudo`, system-wide installs or unreviewed remote install scripts. Compile and do heavy installs in a job; compute nodes may lack outbound network, so fetch sources first.
- Job scripts: `#!/bin/bash -l` (a non-login shell has no `ml`), `ml purge`, load modules, activate the environment, record versions, `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK` unless `--cpu-thread-budget` set it. Do not probe or change system configuration or shared infrastructure (reading `/etc/agents/` is fine) or add slow `~/.bashrc` startup. `ml spider` finds agent CLIs and Python 3.11+ for `shk`.

### Slurm
- Use `shk submit`, `shk status` and `shk cancel` (local executor). Plain `sbatch` follows the same rules and the profiles below: explicit `--time`; GPU jobs `-G n` on a GPU partition; `-C` instead of `--exclude`; no `--account`. `sh_part`/`sh_node_feat` (login shell) list limits and features; never guess.
- At least 60 s between checks (`squeue --me`, `sacct -j ID`, `seff ID`); no `watch` or shell polling loops; prefer `--dependency=afterok` or `sbatch --wait`; a pending reason is never grounds for a duplicate; diagnose before resubmitting; jobs do at least 10 minutes of work. `shk` exit 5: run the reply's `next` command, not before the time it names, never a blind resubmit; exit 6: stop and report.
- Never schedule future agent/LLM sessions (`scrontab`, `--begin`, self-resubmitting agents). Bounded deterministic compute controllers may submit and poll authorized stages inside an allocation via `shk --executor local`, no workstation needed.
- Cancel only exact verified own jobs (`scancel ID` or UID/name-scoped `shk cancel`); in an authorized task this may proceed; tell the user why. Ask for unclear scope/ownership, unrelated work, broad cancellation or material irreversible loss of important progress/results. Never `scancel -u $USER` or kill a job's processes to stop it; inspect and stop only your own processes.

### Partition profiles
`shk` refuses partitions not listed here. Requeue only where `requeue=yes`. Borrowed partitions need `shk occupancy` first and `--courtesy-ack`; releasing a job held there counts as submitting it (same occupancy check, budget and time window).
- `normal`: preemptible=no, requeue=no, borrowed=no, gpus=no.
- `bigmem`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public high-memory partition: only for jobs needing more memory than normal provides. Sherlock's own limits apply.
- `dev`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Interactive only (`sh_dev`, `salloc`): Sherlock rejects batch jobs here, so `shk submit` refuses it. Debugging and short tests, never production runs.
- `service`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public service partition: lightweight recurring administrative tasks such as transfers or backups, never computation. Sherlock's own limits apply.
- `gpu`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Public GPU partition: few GPUs, long queue, never preempted. Follow Sherlock's own per-user GPU limits.
- `owners`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. Slurm requeues preempted jobs; scripts must checkpoint and resume. No cap.
- `bioe`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition: respect fairshare and leave room for other department users.
- `stat`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition: respect fairshare and leave room for other department users.
- `btrippe`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Courtesy budget about 2 jobs x 2 h while others are active; more (e.g. 4 jobs x 6 h) only when the partition is idle, typically 00:00-07:00.
- `possu`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Stricter than btrippe. Submit only between 00:00 and 07:00 Pacific, a few short jobs; never submit outside that window, not even short jobs.
