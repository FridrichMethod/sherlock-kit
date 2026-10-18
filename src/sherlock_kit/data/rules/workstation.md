## Sherlock from this workstation

For agents operating Stanford's Sherlock cluster over SSH. Sherlock's `/etc/agents/AGENTS.md` binds remote operation too and takes precedence over this file; only its numbered rules are requirements, the rest is recommended practice. Read it, and the topic file for the task (`storage.md`, `slurm.md`, `software.md`, `policy.md` beside it), once per session after `shk doctor` reports the control master running: `ssh -oBatchMode=yes -oProxyCommand=false sherlock-plain cat /etc/agents/AGENTS.md` rides the master and cannot log in. If it fails, inspect the error using Connection below; never retry in a loop. Reviewed 2026-10-13 against the site files.

### Connection
- Reach Sherlock only through `shk` or bounded, noninteractive OpenSSH over the user's control masters: `sherlock-plain` for commands, `sherlock-dtn` for transfers. `shk doctor` is read-only: it checks both masters without dialling and exits 4 when the control master is not usable; `shk doctor --remote` adds one read-only call.
- `shk` never logs in. Exit 4 `auth_required`: ask the user to open the master in their own terminal (`ssh sherlock-plain`; for transfers also `ssh -fN sherlock-dtn`), then retry once. Never authenticate for the user or in a loop; no stored passwords or MFA, relays, reverse shells or unattended tunnels.
- Exit 4 `unavailable` with a `~/.ssh/config` hint: the client config lacks `ControlMaster auto`, `ControlPersist yes` or `ControlPath ~/.ssh/%C` for that host. Ask the user to fix the config; logging in again does not help.
- Codex sandbox startup failure or local SSH socket `Permission denied` / `Operation not permitted` is not evidence of a missing master. Request per-command approval for the blocked command, then retry once; never disable the sandbox, open all of HOME for writes, or reopen a master solely for this error. `doctor` needs local control-socket access even without `--remote`; `submit --dry-run` needs none.
- Other exit 4 (`unavailable` or `timeout` with the hint `run shk doctor`, such as a dropped network): run `shk doctor`, retry once after about a minute, then stop and tell the user.

### Jobs
- Submit with `shk submit`, never raw `sbatch` over ssh. Give partition, CPUs, memory and walltime explicitly (a toolkit choice; the site requires only the walltime). GPUs only with `--gpus` on a partition that allows them; no `--account` or `--exclude` (use `--constraint`). Many independent tasks go into one array (`--array N[%M]`).
- The job name derives from project, campaign and task, so a resubmission reuses it: `shk submit` refuses (exit 3) while a job of that name is pending or running, and after it finished unless `--resubmit "<reason>"` is given. Give a reason only after finding the cause of a failure, or to rerun deliberately.
- Exit 5 means the outcome is unknown after dispatch: run the `shk status` command in the reply's `next`, not before the time it names (at least 60 s), and never resubmit blind.
- Wait at least 60 s between status checks; one `shk status` over many tasks (`--all`) instead of a loop per task; no `watch` or polling loops; prefer `--after-ok` dependencies. Jobs do at least 10 useful minutes, ideally 30: batch short tasks, never pad with `sleep`.
- Exact verified own cancellations in an authorized task may proceed; record a reason. Ask for unclear scope/ownership, unrelated work, broad cancellation or material irreversible loss of important progress/results. Keep `shk cancel` UID/name-scoped; never cancel all user jobs.
- Exit 6: status worked but the newest job has an anomaly (for example an unexpected preemption): stop and report it.
- Bounded deterministic compute controllers may submit and poll authorized stages inside a Slurm allocation with `shk --executor local`, no workstation needed. Never schedule future agent/LLM sessions.
- GPU jobs on `owners` must checkpoint periodically (a requeue gives no usable SIGTERM window). Never submit GPU work while developing or testing sherlock-kit itself.
- Config: `[tool.sherlock-kit]` in the nearest `pyproject.toml` or `sherlock-kit.toml` at/above cwd; elsewhere use `--project-dir DIR`.

### Data
- Move data with `shk push` and `shk pull` through the DTN (Globus for very large or cross-site data); never run commands on a DTN. `shk pull` needs a `SHA256SUMS` that the job writes over its own output subdirectory (`shk manifest DIR`), verifies every file, runs the project's validator with `--validate NAME`, then moves the copy into the project-local destination. `shk push` keeps local modification times, so inputs that are old here can be purge-eligible on scratch soon after the push: push them shortly before the jobs that read them.
- Never extend `$SCRATCH` or `$GROUP_SCRATCH` lifetime (touching, rewriting, copy-over, refresh helpers), not even once and whoever asks: the site sanction is termination of the account on the first occurrence. Keep durable copies on `$GROUP_HOME`, Oak or this workstation.
- Low and Moderate Risk data only; stop and ask when the classification is unclear. Work only with your own or explicitly shared directories and jobs; scheduler visibility is never authorization.

### Partition profiles
`shk` refuses partitions not listed here. Requeue only where `requeue=yes`. Borrowed partitions need `shk occupancy` first and `--courtesy-ack`.
- `normal`: preemptible=no, requeue=no, borrowed=no, gpus=no.
- `bigmem`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public high-memory partition: only for jobs that need more memory than normal provides. Follow Sherlock's own limits for it.
- `dev`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Interactive only (`sh_dev`, `salloc`): Sherlock rejects batch jobs here, so `shk submit` refuses it. Debugging and short tests, never production runs.
- `service`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public service partition: lightweight recurring administrative tasks such as transfers or backups, never computation. Follow Sherlock's own limits for it.
- `gpu`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Public GPU partition: few GPUs, long queue, never preempted. Follow Sherlock's own per-user GPU limits.
- `owners`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. Preemptible: Slurm requeues preempted jobs; scripts must checkpoint and resume. No cap.
- `bioe`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition, not borrowed: respect fairshare and leave room for other department users.
- `stat`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition, not borrowed: respect fairshare and leave room for other department users.
- `btrippe`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Borrowed from another group. Courtesy budget about 2 jobs x 2 h while others are active; more (e.g. 4 jobs x 6 h) only when the partition is idle, typically 00:00-07:00.
- `possu`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Borrowed from another group and stricter than btrippe. Submit only between 00:00 and 07:00 Pacific, a few short jobs; never submit outside that window, not even short jobs.
