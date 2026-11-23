## Marlowe from this workstation

These rules cover Stanford's Marlowe only: Sherlock work follows the Sherlock rules, and a task lives on one cluster. Marlowe has no `/etc/agents`: its documentation is the policy and takes precedence, above all the Usage Violations page. Do not carry over Sherlock practice: `sh_*` helpers, `$GROUP_HOME`, `$OAK`, `$L_SCRATCH_JOB`, the 90-day purge and its sanction, `sherlock-dtn`, GPU model constraints, `--prefer` tiers. Site marks the documentation's rules, owner the user's choices; the rest are toolkit rules or observed facts. Keep this in handoffs. Reviewed 2026-11-22 against https://marlowe-research.stanford.edu/documentation/ .

### Site rules
- Site: run nothing that lets a person or a service reach Marlowe without SUNet ID, password and Duo: websocket tunnels and remote-control servers (SSHX, Claude Remote Control/Server), userspace VPNs (Tailscale, ZeroTier), editor web tunnels (VS Code, Cursor, Windsurf), own code servers on login nodes. The user's own SSH forwarding through that login is fine; open no tunnel or forward yourself.
- Owner: the user may drive this workstation session remotely, also through a remote-control relay, and it may then still use the open master; the owner reads the rule above as covering what runs on Marlowe and access by others. Start no relay yourself, and never share the master, credentials or this session with anyone else.
- Site: login nodes are shared, for editing, compiling, submitting jobs and moving data, with short, light commands. Anything sustained, server-like or taking a large share of a node's CPU or memory runs on a compute node through Slurm.
- Never authenticate, retry a login or store passwords or Duo secrets; repeated failed logins can block the client address (site).
- Never contact Marlowe staff or post in their channels; draft the message (symptoms, host, time, job id) for the user.

### Connection
- Owner: agent sessions here may drive Marlowe over the user's open `marlowe-plain` master, as a session on Marlowe would.
- Every Marlowe command is `shk --site marlowe ...`, plus `--project-dir DIR` when the Marlowe configuration is not the nearest; exit 1 `site_mismatch` means the nearest is another cluster's. A reply that names its `executor` without `"site": "marlowe"` came from Sherlock: stop and check.
- Light reads may use `ssh -oBatchMode=yes -oProxyCommand=false marlowe-plain CMD`. That PATH has no Slurm clients: Slurm work goes through shk.
- Exit 4 `auth_required`: ask the user to run the hint (`ssh marlowe-plain`), then retry once. `unavailable` with a `~/.ssh/config` hint: the user fixes what it names; a new login will not help. A sandbox startup failure or local socket permission denial is not a missing master: request per-command approval and retry once; never disable the sandbox. Other exit 4: run `shk --site marlowe doctor`, retry once after a minute, then stop and report.

### Jobs
- Submit, update and cancel only through shk, never raw `sbatch`, `scontrol update` or `scancel`. Use `--partition preempt` with explicit CPUs, memory and walltime, GPUs with `--gpus N`. The account comes from the configuration's `account`: never `--account`, never a guess from `$PROJECT_ID`.
- `srun` and `salloc` allocations are billed jobs too: only with the user's authorization and an explicit `--time`.
- Submit first runs `sbatch --test-only`; a refusal is exit 3, nothing staged: change the request. "Requested node configuration is not available" may also mean no schedulable node: first read `shk --site marlowe resources --partition preempt --summary`.
- No GPU model `--constraint` (no such features), typed GRES or `--tmp` (Slurm refuses them), no `#SBATCH --exclusive` (it takes and bills a whole node's CPUs).
- Site: limits are per project account, shared with its members and changed with demand.
- Every array element counts toward the submit limit, whatever the throttle.
- Read status only when needed (wait exited, user asks, exit 5 `next` due; never resubmit blind), batched (`--all`), at least 60 s apart, ideally 10+ min; no watch or polling loops. Prefer `--after-ok`. Exit 6: stop and report.
- Cancel only exact verified own jobs and say why. Never cancel and resubmit to adjust a queued job; `update` lowers its walltime or an array's throttle.
- In jobs, bounded deterministic scripts may run `shk --site marlowe` within the authorized bound; no job that only waits or polls.

### Cost and waiting
- Site: usage is charged to the PI per GPU-hour and per CPU-hour, CPU-only jobs included.
- Treat every attempt as billed, failed and requeued ones too.
- Owner: a real submit or allocation needs the user's authorization of that campaign. State the upper bound you ask for, tasks x GPUs and CPUs x walltime x (1 + retries); the yes covers exactly that.
- Owner: CPU-only work that is not the preparation or check of a Marlowe GPU job runs on the workstation or on Sherlock.
- The start time in a preflight or test-only reply is not a prediction on Marlowe; never relay it as one. Observed: small GPU requests usually start within hours, requests near a whole node can wait days, and shorter limits tend to start sooner.
- After a submit, report the job id and end the turn or, to be woken, run one bounded `shk --site marlowe wait --task T --timeout DUR` in the background and act on `wait.outcome`. Never detach it, schedule or re-prompt agents.

### Preemption
- Site: batch or hero work can preempt a `preempt` job at any time; it is requeued and restarts from the top of its script.
- Preemption is routine, not an anomaly.
- Owner: requeue stays on (the `preempt` default; no `--no-requeue`).
- Scripts write atomic checkpoints (write, then rename) on scratch every few minutes, since the protected window is short, resume from the newest one, never rely on a signal, and stop when `SLURM_RESTART_COUNT` passes the authorized retry bound.
- A job in REQUEUE_HOLD (`current.tasks.by_state`) or held after a failed launch (its reason in `status --details`) is reported with its job id and restart count (`current.max_restart`). Recover it only with the user's consent: `cancel`, then `submit --resubmit REASON`; never `shk release`.

### Data
- `remote_root` lies under `/scratch/PROJECT/USER`.
- Site: scratch is shared by the project, not backed up and "may be purged"; `/projects` is backed up and billed; home is small. Keep what must last in `/projects` or on the workstation.
- `shk --site marlowe push` and `pull` copy small and medium trees over the login-node master; large data goes through Globus, outside shk. Pull verifies the job output's `SHA256SUMS`.
- Site: access only your own or explicitly shared files and jobs; visibility is not authorization.
- Site: Low and Moderate Risk data only; ask when unclear.

### Partition profiles
`shk` refuses partitions not listed here. Requeue only where `requeue=yes`.
- `preempt`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. Preempted jobs are requeued and restart from the top; scripts must checkpoint and resume. Usage is charged per GPU-hour and CPU-hour. Limits are per project account and shared.
