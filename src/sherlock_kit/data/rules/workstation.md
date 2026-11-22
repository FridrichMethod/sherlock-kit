## Sherlock from this workstation

Sherlock's `/etc/agents/AGENTS.md` takes precedence; only its numbered rules are requirements. At session start and after compaction, run `shk doctor`, then read it and the task's topic files (`storage.md`, `slurm.md`, `software.md`, `policy.md`) over the master: `ssh -oBatchMode=yes -oProxyCommand=false sherlock-plain cat /etc/agents/AGENTS.md`. Never authenticate. Keep this in handoffs; memory is no substitute; never filter reminders. Reviewed 2026-11-22.

### Connection
- Use `shk` or bounded noninteractive OpenSSH over the user's masters: `sherlock-plain` for light commands/installs, `sherlock-dtn` for transfers. Heavy work runs in jobs.
- Exit 4 `auth_required`: ask the user to open `ssh sherlock-plain` (and `ssh -fN sherlock-dtn` for transfers), then retry once. No stored passwords/MFA, relays, reverse shells, unattended tunnels, shared credentials/sessions or SSH servers, even attended or in containers.
- Exit 4 `unavailable` with a config hint: ask the user to fix missing `ControlMaster auto`, `ControlPersist yes` or `ControlPath ~/.ssh/%C`; a new login will not help.
- Sandbox startup failure or local socket permission denial is not a missing master: request per-command approval and retry once. Never disable the sandbox, grant HOME-wide writes or reopen a master.
- Other exit 4: run `shk doctor` (offline), retry once after a minute, then stop and report.

### Jobs
- Submit with `shk submit`, change with `shk update`, never raw ssh sbatch/`scontrol update`. Partition, CPUs, memory and walltime are explicit (toolkit choice; site requires walltime); GPUs via `--gpus`. Compatible partitions join in one value, e.g. `gpu,bioe,stat`; owners/borrowed stay alone. No `--account`/`--exclude`; use `--constraint` and discovered features/limits.
- Update changes one field group per call: a pending job's walltime (decrease only), partition (compatible, non-borrowed) or constraint/prefer, or an array's throttle.
- Names derive from project/campaign/task. Submit refuses live duplicates and finished tasks without `--resubmit "reason"`; diagnose failures first. Exit 5 (unknown outcome): never resubmit blind. Exit 6: stop and report.
- After a submit, end the turn or run one bounded `shk wait --task T --timeout DUR` in the background (its exit wakes you); act on `wait.outcome`. Batch status/resources reads at least 60 s apart (`--all`), ideally 10+ min; read status only when needed (wait exited, user asks, exit 5 `next` due). No `watch`/polling loops; never detach a wait or schedule/re-prompt agent/LLM sessions. Prefer `--after-ok`. Each job/array task does at least 10 useful minutes, ideally 30; never pad with sleep.
- Cancel exact verified own jobs within an authorized task and explain why. Ask for unclear ownership/scope, unrelated/broad cancellation or material irreversible loss. Never cancel all user jobs.
- Bounded deterministic controllers may submit/poll inside allocations with `shk --executor local`. Never submit GPU work while developing/testing sherlock-kit.
- Config: nearest `pyproject.toml` `[tool.sherlock-kit]` or `sherlock-kit.toml`, else `--project-dir DIR`.

### Data
- `shk push`/`shk pull` use the DTN (Globus for large/cross-site data); never run commands there. Pull verifies `SHA256SUMS`, runs `--validate NAME`, then promotes. Push keeps mtimes: old inputs may already be purge-eligible, so stage just before use.
- Never extend scratch lifetime by touch/rewrite/copy-over or refresh helpers, even once: account termination. Keep durable copies in GROUP_HOME/Oak or the workstation; intensive I/O stays off HOME/GROUP_HOME.
- Low/Moderate Risk only; ask when unclear. Access only own/explicitly shared files and jobs and own processes; visibility is not authorization.

### GPU scheduling defaults
- Site/task rules take precedence. Use a broad OR `--constraint` over discovered compatible GPUs, new SKUs too. Avoid RTX_2080Ti/Pascal. Narrow only for evidenced architecture/memory/kernel/dtype needs. Obey MIG rules.
- Soft `--prefer`: one tier of compatible L40/A/H and future B/newer GPUs, purely by compute capability, so cross-project. Slurm tries it before the fallback; both must meet all hard requirements. OR is unordered.
- GPU models are scientifically equivalent and may mix or change on requeue. SKU is provenance, by default no reason for new controls/cohorts/reruns/per-model qualification. Pin inputs/model/seeds/numerical/environment protocol; no bitwise guarantee.
- Prioritize the earliest start; reduce oversized CPU requests. Before GPU submission compare `--shape` slots (useful threads, measured peak RAM plus headroom) in one fresh `shk resources --partition P --summary --by sku`.
- Owners needs proven same-command resume with durable checkpoints, verified units, attempt logs and bounded retries. Requeue restarts the script, not app state.
- Queue independent ready work concurrently. Report ready/unsubmitted/queued/running/blocked. Favor longer useful resumable bundles to amortize queue/setup within limits/courtesy/budgets.
- Snapshots do not guarantee starts. Move units only on terminal/nonrestartable proof or durable revocation across requeue. Keep pending/unknown units owned. Never race duplicates.

### Partition profiles
`shk` refuses partitions not listed here. Requeue only where `requeue=yes`. Borrowed partitions need `shk occupancy` first and `--courtesy-ack`; releasing a job held there counts as submitting it (same occupancy check, budget and time window).
- `normal`: preemptible=no, requeue=no, borrowed=no, gpus=no.
- `bigmem`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public high-memory partition: only for jobs needing more memory than normal provides. Sherlock's own limits apply.
- `dev`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Interactive only (`sh_dev`, `srun`): Sherlock rejects batch jobs, so `shk submit` refuses it. Quick tests and builds, never production runs.
- `service`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public service partition: lightweight recurring administrative tasks such as transfers or backups, never computation. Sherlock's own limits apply.
- `gpu`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Public GPU partition: few GPUs, long queue, never preempted. Follow Sherlock's own per-user GPU limits.
- `owners`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. Slurm requeues preempted jobs; scripts must checkpoint and resume. No cap.
- `bioe`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition: respect fairshare and leave room for other department users.
- `stat`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition: respect fairshare and leave room for other department users.
- `btrippe`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Courtesy budget about 2 jobs x 2 h while others are active; more (e.g. 4 jobs x 6 h) only when the partition is idle, typically 00:00-07:00.
- `possu`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Stricter than btrippe. Submit only between 00:00 and 07:00 Pacific, a few short jobs; never submit outside that window, not even short jobs.
