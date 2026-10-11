## Sherlock from this workstation

These rules apply when an agent here operates Stanford's Sherlock cluster over SSH. Sherlock's `/etc/agents/AGENTS.md` binds remote operation too and takes precedence over this file; only its numbered rules are requirements, the rest is recommended practice. Read it, and the topic file for the task (`storage.md`, `slurm.md`, `software.md`, `policy.md` beside it), once per session over the existing master: `ssh -oBatchMode=yes sherlock-plain cat /etc/agents/AGENTS.md`. Reviewed 2026-10-10 against the site files.

### Connection
- Reach Sherlock only through `shk` or bounded, noninteractive OpenSSH over the user's control masters: `sherlock-plain` for commands, `sherlock-dtn` for transfers. `shk doctor` is read-only: it checks both masters without dialling and exits 4 when the control master is not usable; `shk doctor --remote` adds one read-only call.
- `shk` never logs in. Exit 4 `auth_required`: ask the user to open the master in their own terminal (`ssh sherlock-plain`; for transfers also `ssh -fN sherlock-dtn`), then retry once. Never authenticate for the user or in a loop; no stored passwords or MFA, relays, reverse shells or unattended tunnels.
- Exit 4 `unavailable` with a `~/.ssh/config` hint: the client config lacks `ControlMaster auto`, `ControlPersist yes` or `ControlPath ~/.ssh/%C` for that host. Ask the user to fix the config; logging in again does not help.

### Jobs
- Submit with `shk submit`, never raw `sbatch` over ssh. Give partition, CPUs, memory and walltime explicitly (a toolkit choice; the site requires only the walltime). GPUs only with `--gpus` on a partition that allows them; no `--account` or `--exclude` (use `--constraint`). Many independent tasks go into one array (`--array N%M`).
- The job name derives from project, campaign and task, so a resubmission reuses it: `shk submit` refuses (exit 3) while a job of that name is pending or running, and after it finished unless `--resubmit "<reason>"` is given. Give a reason only after finding the cause of a failure, or to rerun deliberately.
- Exit 5 means the outcome is unknown after dispatch: run `shk status` for that task, not before its `next_check_after`, and never resubmit blind.
- Wait at least 60 s between status checks; one `shk status` over many tasks (`--all`) instead of a loop per task; no `watch` or polling loops; prefer `--after-ok` dependencies. Jobs do at least 10 minutes of useful work, ideally 30: batch short tasks, never pad with `sleep`.
- Exit 6: status worked but the newest job has an anomaly (for example an unexpected preemption): stop and report it. `shk cancel` only when the user asks.
- GPU jobs on `owners` must checkpoint periodically (a requeue gives no usable SIGTERM window). Never submit GPU work while developing or testing sherlock-kit itself.
- `shk` reads `[tool.sherlock-kit]` from `pyproject.toml` (or `sherlock-kit.toml`) at or above the working directory and stops at a directory you do not own; otherwise pass `--project-dir DIR`.

### Data
- Move data with `shk push` and `shk pull` through the DTN (Globus for very large or cross-site data); never run commands on a DTN. `shk pull` needs a `SHA256SUMS` written by the job (`shk manifest DIR`), verifies every file, runs the project's validator with `--validate NAME`, then moves the copy into the project-local destination.
- Never extend `$SCRATCH` or `$GROUP_SCRATCH` lifetime (touching, rewriting, copy-over, refresh helpers), not even once and whoever asks: the site sanction is termination of the account on the first occurrence. Keep durable copies on `$GROUP_HOME`, Oak or this workstation.
- Low and Moderate Risk data only; stop and ask when the classification is unclear. Work only with your own or explicitly shared directories and jobs; scheduler visibility is never authorization.

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
