## Sherlock: agent running on Sherlock

The site banner requires reading `/etc/agents/AGENTS.md` first. It and its topic files (`storage.md`, `slurm.md`, `software.md`, `policy.md` beside it) override this file; only its numbered rules are requirements, the rest is recommended practice. Reviewed 2026-10-12 against the site files.

### Where you are
- Run `hostname` and check `$SLURM_JOB_ID` before anything else: login node names contain `-ln` and have no job id.
- Inside an allocation (`sh_dev`, `salloc`, a batch job) the allocated CPUs, memory and GPUs are yours to use fully; prefer running agents there. `scontrol show job $SLURM_JOB_ID` shows its limits.
- Codex: its sandbox cannot start here, so per-command approval is expected; never try to disable it. Claude Code: managed settings block or confirm some commands; do not work around them.
- Never bypass authentication, MFA, limits or permissions; no relays, reverse shells, alternate login services, unattended tunnels or stored second factors; never share credentials or sessions (account termination).
- Never contact Stanford Research Computing for the user. If the system looks broken, stop and draft a report (symptom, hostname, time, job id) for the user to send. Slurm not answering: retry `squeue --me` once after a minute, then stop.

### Filesystems
- Reference filesystems by variable: `$HOME` and `$GROUP_HOME` for code, small files and environments (not purged; keep intensive job I/O off them); `$SCRATCH` and `$GROUP_SCRATCH` for job I/O (no backup, files unmodified for 90 days are purged); `$L_SCRATCH_JOB` for temp-file-heavy work (copy results out before the job ends); `$OAK` for long-term data. `sh_quota` shows usage.
- Never extend scratch lifetime (touching, rewriting, copy-over, refresh helpers), not even once and whoever asks: termination of the account on the first occurrence. Move data worth keeping to `$GROUP_HOME` or Oak.
- Scope `find`, `du` and `ncdu` to your own or authorized project directories; never scan filesystem roots or other users' directories. Group path variables in shell profiles are not evidence of authorization. Low and Moderate Risk data only; stop and ask when unclear.
- Large internal copies run in a Slurm job. External transfers start outside Sherlock (DTN, Globus); never ssh to a DTN from here, and `shk push` and `shk pull` refuse here (exit 3).

### Login nodes
- Light work only: editing, small reads, job preparation, submitting and polling. Sustained computation, significant CPU or memory, GPUs, intensive I/O or many processes go to a job (`sh_dev` or `sbatch`).
- Nothing server-like: no listeners, nothing unattended, no keep-alive or restart loops, no continuous sync, nothing that outlives the session; no background agent sessions (`claude agents`, `--bg`), and do not restart a terminated supervisor. Leftovers terminated by the site are expected, not a fault.

### Software
- `ml spider NAME` first; prefer a module, then a group install in `$GROUP_HOME`, then your own build in `$GROUP_HOME`, then a container. Never put environments in `$SCRATCH`. No `sudo`, system-wide installs or unreviewed remote install scripts. Compile and do heavy installs inside a job; compute nodes may lack outbound network, so fetch sources first.
- Job scripts: `#!/bin/bash -l` (a non-login shell has no `ml`), `ml purge`, load modules, activate the environment, record versions, `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK`. Do not probe or change system configuration (reading `/etc/agents/` is fine) or add slow startup to `~/.bashrc`. Find agent CLIs and Python with `ml spider`; `shk` needs Python 3.11 or newer.

### Slurm
- Prefer `shk submit`, `shk status` and `shk cancel`: the same contract as from a workstation, run by the local executor. Plain `sbatch` follows the same rules and the partition profiles below: explicit `--time`; GPU jobs `-G n` on a GPU partition; `-C` instead of `--exclude`; no `--account`. `sh_part` and `sh_node_feat` (login shell) list partitions and node features.
- At least 60 s between checks (`squeue --me`, `sacct -j ID -o JobID,State,Elapsed,MaxRSS,ExitCode`, `seff ID`); prefer `--dependency=afterok` or `sbatch --wait`; a pending reason is never grounds for a duplicate; never resubmit without finding the cause; jobs do at least 10 minutes of work. `shk` exit 5: run the `shk status` command in the reply's `next`, not before the time it names, never a blind resubmit; exit 6: stop and report.
- Never schedule future agent/LLM sessions (`scrontab`, `--begin`, self-resubmitting agents). Bounded deterministic compute controllers may submit and poll authorized stages inside an allocation via `shk --executor local`, no workstation needed.
- Cancel only your own jobs, with `scancel ID` or `shk cancel`, and only when the user asks; never `scancel -u $USER`; never kill processes on compute nodes.
- `shk doctor` is read-only. `shk` reads `[tool.sherlock-kit]` from the nearest `pyproject.toml` (or `sherlock-kit.toml`) at or above the working directory; from elsewhere pass `--project-dir DIR`.

### Partition profiles
`shk` refuses partitions not listed here. Requeue only where `requeue=yes`. Borrowed partitions need `shk occupancy` first and `--courtesy-ack`.
- `normal`: preemptible=no, requeue=no, borrowed=no, gpus=no.
- `bigmem`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public high-memory partition: only for jobs that need more memory than normal provides. Follow Sherlock's own limits for it.
- `dev`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Public development partition: debugging and short tests only, never production runs. Follow Sherlock's own limits for it.
- `service`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public service partition: lightweight recurring administrative tasks such as transfers or backups, never computation. Follow Sherlock's own limits for it.
- `gpu`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Public GPU partition: few GPUs, long queue, never preempted. Follow Sherlock's own per-user GPU limits.
- `owners`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. Preemptible: Slurm requeues preempted jobs; scripts must checkpoint and resume. No cap.
- `bioe`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition, not borrowed: respect fairshare and leave room for other department users.
- `stat`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition, not borrowed: respect fairshare and leave room for other department users.
- `btrippe`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Borrowed from another group. Run `shk occupancy` first. Courtesy budget about 2 jobs x 2 h while others are active; more (e.g. 4 jobs x 6 h) only when the partition is idle, typically 00:00-07:00.
- `possu`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Borrowed from another group and stricter than btrippe. Run `shk occupancy` first. Submit only between 00:00 and 07:00 Pacific, a few short jobs; never submit outside that window, not even short jobs.
