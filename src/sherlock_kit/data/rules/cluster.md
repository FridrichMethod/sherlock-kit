## Sherlock: agent running on Sherlock

At session start and after compaction, read `/etc/agents/AGENTS.md` and relevant `storage.md`, `slurm.md`, `software.md`, `policy.md` beside it. They take precedence; only numbered rules are mandatory. Keep this in handoffs; memory is no substitute and reminders must not be filtered. Reviewed 2026-11-14 against the site files.

### Where you are
- Run `hostname`/check `$SLURM_JOB_ID`: login names contain `-ln`, no job id. Use allocations fully; prefer agents there. Check limits with `scontrol show job $SLURM_JOB_ID`.
- Codex sandbox failure is expected: request per-command approval, never disable it. Honor Claude's managed restrictions/confirmations. No authentication/MFA/limit/permission bypass, relays, reverse shells, alternate login services, unattended tunnels or stored second factors; never share credentials/sessions (account termination). No SSH server, even attended or inside a job/container (suspension).
- Do not contact support. Stop affected work if infrastructure seems broken; draft symptoms/host/time/job id for the user. Slurm timeout: retry once after a minute, then stop.

### Filesystems
- Use variables: `$HOME`/`$GROUP_HOME` for code/environments/small files, no intensive I/O; `$SCRATCH`/`$GROUP_SCRATCH` for job I/O, 90-day purge/no backup; `$L_SCRATCH_JOB` for temp data that fits, export before job end; `$OAK` for durable data. Check `sh_quota`.
- Never extend scratch lifetime by touch/rewrite/copy-over or refresh helpers, even once: account termination. Keep durable copies in GROUP_HOME/Oak or the workstation.
- Own/explicitly shared directories only; readable/group paths alone grant no access. Scope scans, never roots. Low/Moderate Risk only; ask when unclear.
- Large internal copies need jobs. DTN/Globus transfers start outside; no ssh to DTN here.

### Login nodes and software
- Light editing/reads/preparation/submitting/polling only; sustained CPU/memory, GPUs, intensive I/O or many processes need jobs. No model/web/API serving; listeners only for own interactive tools. No unattended processes, keep-alive/restart loops, continuous sync or processes outliving the session. No `claude agents`/`--bg` or restarting terminated supervisors; site cleanup is expected.
- Check `ml spider NAME`; prefer modules, group installs, own builds, then containers. Large/shared environments: GROUP_HOME. Scratch only by explicit request with purge/rebuild warning, never refresh. No sudo/system installs/unreviewed install scripts. Compile/heavy installs in jobs; fetch sources beforehand if needed.
- Use `#!/bin/bash -l`, `ml purge`, explicit modules/environment; record versions. Set `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK` unless `--cpu-thread-budget` overrides it. No probing/changing system configuration/infrastructure (except reading `/etc/agents/`) or slow shell startup. Find CLIs/Python with `ml spider`.

### Slurm
- Use local `shk submit`/`status`/`cancel`, explicit resources/profiles, no `--account`/`--exclude`. Discover limits/features. Checks at least 60 s apart; no watch/polling loops; prefer dependencies. Jobs need 10 useful minutes, no padding. Diagnose failures; no pending duplicates. Exit 5: obey `next` time; exit 6: stop/report.
- Bounded deterministic controllers may use `shk --executor local` inside allocations; never schedule future agent/LLM sessions (scrontab, begin, self-resubmission). Never submit GPU work while developing/testing sherlock-kit.
- Cancel exact verified own jobs through Slurm and explain why. Ask for unclear scope/ownership, unrelated/broad cancellation or material irreversible loss. Never cancel all user jobs or kill compute processes instead; inspect only own processes.

### GPU scheduling defaults
- Site/task rules take precedence. Use broad OR `--constraint` of discovered compatible GPUs, new ones too; avoid RTX_2080Ti/Pascal; narrow for architecture needs and only evidenced memory/kernel/dtype needs; obey MIG slice/partition rules.
- Soft `--prefer`: one tier of compatible discovered L40/A/H and future B/newer GPUs, purely by compute capability, so cross-project; Slurm tries it independently, then the fallback constraint: both must encode every hard requirement. OR is not ordered.
- GPU models are scientifically equivalent, may mix or change on requeue; SKU is provenance, by default no reason for new controls/cohorts/reruns/per-model qualification. Fix inputs/model/seeds/numerical/environment protocol; no bitwise guarantee; check compatibility as needed.
- Prioritize the earliest start; reduce oversized CPU requests. Owners needs proven same-command resume: durable checkpoints, verified completed units, attempt logs/bounded retries; requeue restarts the script, not app state.
- Queue/refill ready independent work concurrently; report ready/unsubmitted/queued/running/blocked. Favor longer useful resumable bundles over tiny jobs to amortize queues/setup, within limits/courtesy/budgets.
- Batch status/resources checks as needed, at least 60 s apart; snapshots guarantee no start; adapt future requests to evidence. Move units only on terminal/nonrestartable proof or durable revocation across requeue; pending/unknown ones stay owned; no duplicate queue racing.

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
