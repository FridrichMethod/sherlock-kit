## Sherlock from this workstation

Sherlock's `/etc/agents/AGENTS.md` takes precedence; only its numbered rules are requirements. At session start and after compaction, run `shk doctor`, then read it and the task's topic files (`storage.md`, `slurm.md`, `software.md`, `policy.md`) through the existing master: `ssh -oBatchMode=yes -oProxyCommand=false sherlock-plain cat /etc/agents/AGENTS.md`. Never authenticate. Keep this requirement in handoffs; memory is no substitute and instruction reminders must not be filtered. Reviewed 2026-11-14 against the site files.

### Connection
- Use `shk` or bounded noninteractive OpenSSH over the user's masters: `sherlock-plain` for light commands, `sherlock-dtn` for transfers. Heavy work belongs in jobs. `shk doctor` checks local sockets without dialing.
- Exit 4 `auth_required`: ask the user to open `ssh sherlock-plain` (and `ssh -fN sherlock-dtn` for transfers), then retry once. No authentication loops, stored passwords/MFA, relays, reverse shells, unattended tunnels, shared credentials/sessions or SSH servers, even attended or in containers.
- Exit 4 `unavailable` with a config hint: ask the user to fix missing `ControlMaster auto`, `ControlPersist yes` or `ControlPath ~/.ssh/%C`; logging in again will not help.
- Sandbox startup failure or local socket permission denial is not a missing master: request per-command approval and retry once. Never disable the sandbox, grant HOME-wide writes or reopen a master for this. `submit --dry-run` needs no socket.
- Other exit 4: run `shk doctor`, retry once after about a minute, then stop and report.

### Jobs
- Submit with `shk submit`, not raw ssh sbatch. Explicit partition, CPUs, memory and walltime are toolkit choices; the site requires walltime. Request GPUs with `--gpus` on an allowed partition. No `--account` or `--exclude`; use `--constraint` and discovered features/limits.
- Names derive from project/campaign/task. Submit refuses live duplicates and finished tasks without `--resubmit "reason"`; diagnose failures first. Exit 5 means unknown outcome: use the reply's `next` command no earlier than its time, never resubmit blind. Exit 6: stop and report the anomaly.
- Batch status reads at least 60 s apart (`shk status --all`); no watch or polling loops. Prefer `--after-ok` dependencies. Each job/array task does at least 10 useful minutes, ideally 30; never pad with sleep.
- Cancel exact verified own jobs within an authorized task and explain why. Ask for unclear ownership/scope, unrelated or broad cancellation, or material irreversible loss. Never cancel all user jobs.
- Bounded deterministic controllers may submit/poll inside allocations with `shk --executor local`; never schedule future agent/LLM sessions. Never submit GPU work while developing/testing sherlock-kit.
- Config: nearest `pyproject.toml` `[tool.sherlock-kit]` or `sherlock-kit.toml`; use `--project-dir DIR` elsewhere. Resource choices remain explicit CLI arguments.

### Data
- `shk push`/`shk pull` use the DTN (Globus for large/cross-site data); never run commands there. Pull verifies the job output's `SHA256SUMS`, runs `--validate NAME`, then promotes the copy. Push preserves mtimes: old inputs may already be purge-eligible, so stage just before use.
- Never extend scratch lifetime by touch/rewrite/copy-over or refresh helpers, even once: account termination. Keep durable copies in GROUP_HOME, Oak or the workstation; intensive I/O stays off HOME/GROUP_HOME.
- Low/Moderate Risk only; ask when unclear. Access only own/explicitly shared files and jobs, and only own processes; visibility is not authorization.

### GPU scheduling defaults
- Site/task rules take precedence. Discover features/limits; use broad compatible OR `--constraint`, with new GPUs. Avoid RTX_2080Ti/Pascal. Narrow for memory/architecture/kernel/dtype needs; honor MIG slice/partition rules.
- `--prefer`: one tier of compatible discovered L40/A/H and future B/newer GPUs, then other allowed GPUs. Slurm tries it independently before constraint: both must encode every hard requirement. OR is not ordered.
- SKU is provenance, not a new cohort/control by default. Keep inputs/model/seeds/numerical/environment protocol fixed; no bitwise guarantee. Check compatibility as needed, not every model/SKU pair.
- Choose authorized partitions by queue/setup/compute/recovery time; consider departments/owners. Keep borrowed budgets. Owners needs proven same-command resume: durable checkpoints, verified completed units, attempt logs/bounded retries. Requeue restarts the script, not application state.
- Queue ready independent work concurrently; refill; report ready/unsubmitted, queued, running and blockers. Favor longer useful resumable bundles to amortize queues/setup; honor limits/courtesy/budgets. No padding or tiny jobs.
- Batch status/resources checks at least 60 s apart as needed; snapshots do not guarantee starts. Adapt future requests to evidence. Transfer units only after terminal/nonrestartable proof or durable revocation across requeue; pending/unknown units stay owned. No duplicate queue racing.

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
