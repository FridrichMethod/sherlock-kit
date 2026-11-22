## Marlowe: agent running on Marlowe

These rules cover an agent session on a Marlowe login node. Marlowe only: Sherlock work follows the Sherlock rules, and a task lives on one cluster. Marlowe has no `/etc/agents`: its documentation is the policy and takes precedence, above all the Usage Violations page. Do not carry over Sherlock practice: `/etc/agents`, `sh_*` helpers, `$GROUP_HOME`, `$OAK`, `$L_SCRATCH_JOB`, the 90-day purge and its sanction, `sherlock-dtn`, GPU model constraints, `--prefer` tiers, shk inside jobs. Site marks the documentation's rules, owner the user's choices; the rest are toolkit rules or observed facts. Keep this in handoffs. Reviewed 2026-11-21 against https://marlowe-research.stanford.edu/documentation/ .

### Site rules
- Site: run nothing that lets a person or a service reach Marlowe without SUNet ID, password and Duo: websocket tunnels and remote-control servers (SSHX, Claude Remote Control/Server: never start one for this session), userspace VPNs (Tailscale, ZeroTier), editor web tunnels (VS Code, Cursor, Windsurf), own code servers on login nodes. Ordinary SSH tunnels and port forwarding are fine. Never share credentials or this session.
- Site: login nodes are shared and are for editing, compiling, submitting jobs and moving data, with short, light commands. Anything sustained, server-like or taking a large share of a node's CPU or memory runs on a compute node through Slurm.
- Never authenticate, retry a login or store passwords or Duo secrets; repeated failed logins can block the client address (site).
- Never contact Marlowe staff or post in their channels. Stop affected work and draft the message (symptoms, host, time, job id) for the user.

### Where you are
- `hostname` and `$SLURM_JOB_ID` tell a login node (`login-NN`, no job id, no GPUs) from a job (`nNN`). The names are generic vendor defaults and do not prove Marlowe.
- Owner: agent sessions on login nodes are allowed.
- Every Marlowe command is `shk --site marlowe ...`, plus `--project-dir DIR` when the Marlowe configuration is not the nearest one; here shk then runs Slurm locally by itself. A reply that names its `executor` without `"site": "marlowe"` came from Sherlock: stop and check.

### Login nodes and software
- Owner: environments may be built on a login node. Large ones go under `/projects/PROJECT` or scratch, not the small home (site).
- shk needs Python 3.11 or newer; the system `python3` is older: use the `uv` module or a micromamba or conda environment.
- Nothing unattended or outliving the session: no servers, daemons, sync loops or background agents.

### Jobs
- Submit, update and cancel only through shk, never raw `sbatch`, `scontrol update` or `scancel`. Use `--partition preempt` with explicit CPUs, memory and walltime, GPUs with `--gpus N`. The account comes from the configuration's `account`: never `--account`, never a guess from `$PROJECT_ID`.
- Submit first runs Slurm's test (`sbatch --test-only`); a refusal is exit 3 with nothing staged: change the request. "Requested node configuration is not available" can also mean that no node is schedulable now: read `shk --site marlowe resources --partition preempt --summary` before changing the request.
- No GPU model `--constraint` (no such features), typed GRES or `--tmp` (Slurm refuses them), no `#SBATCH --exclusive` (it takes and bills a whole node's CPUs).
- Job scripts: `module` works under plain `#!/bin/bash` and the Slurm clients are on PATH. `TMPDIR` is `/tmp`; job-local space is `/local_scratch/$USER.$SLURM_JOB_ID`, wiped at job end. No GPU is visible without `--gpus`. Compute nodes reach the internet. The job environment is captured from a login shell: keep login startup files light and quiet.
- Site: limits are per project account, shared with its members and changed with demand. Every array element counts toward the submit limit, whatever the throttle.
- Reads at least 60 s apart, no watch or polling loops. A Slurm timeout: retry once after a minute, then stop and report. Exit 5: run the reply's `next` no earlier than its time; never resubmit blind. Exit 6: stop and report.
- Cancel only exact verified own jobs and say why. Never cancel and resubmit to adjust a queued job; `shk --site marlowe update` lowers its walltime or an array's throttle.

### Cost and waiting
- Site: usage is charged to the PI per GPU-hour and per CPU-hour, CPU-only jobs included. Treat every attempt as billed, failed and requeued ones too.
- Owner: a real submit needs the user's authorization of that campaign. State the upper bound you ask for, tasks x GPUs x walltime plus the retry bound; the yes covers exactly that.
- Owner: CPU-only work that is not the preparation or check of a Marlowe GPU job runs on the user's workstation or on Sherlock.
- The start time in a preflight or test-only reply is not a prediction on Marlowe; never relay it as one. Observed: small GPU requests usually start within hours, requests near a whole node can wait days, and shorter limits start sooner.
- After a submit, report the job id and end the wait; check again when the user asks.

### Preemption
- Site: batch or hero work can preempt a `preempt` job at any time; it is requeued and restarts from the top of its script. This is routine, not an anomaly.
- Owner: requeue stays on (the `preempt` default; no `--no-requeue`).
- Scripts write atomic checkpoints (write, then rename) on scratch every few minutes, since the protected window is short, resume from the newest one, never rely on a signal, and stop when `SLURM_RESTART_COUNT` passes the authorized retry bound.
- A job in REQUEUE_HOLD (`current.tasks.by_state`) or held after a failed launch (its reason in `status --details`) is reported with its job id and restart count (`current.max_restart`). Recover it only with the user's consent: `shk --site marlowe cancel`, then `shk --site marlowe submit --resubmit REASON`; never `shk release`.

### Data
- Site: `/scratch/PROJECT` is shared by the project, not backed up and "may be purged"; `/projects/PROJECT` is backed up and billed; home is small. `remote_root` lies under `/scratch/PROJECT/USER`; keep what must last in `/projects`.
- `shk --site marlowe push` and `pull` are refused here (exit 3 `transfer_on_marlowe`): transfers start outside Marlowe. Large data goes through Globus.
- Site: access only your own or explicitly shared files and jobs; visibility is not authorization.
- Site: Low and Moderate Risk data only; ask when unclear.

### Partition profiles
`shk` refuses partitions not listed here. Requeue only where `requeue=yes`.
- `preempt`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. Preempted jobs are requeued and restart from the top; scripts must checkpoint and resume. Usage is charged per GPU-hour and CPU-hour. Limits are per project account and shared.
