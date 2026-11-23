---
name: sherlock-kit-marlowe
description: Run, track, update and cancel Slurm jobs on Stanford's Marlowe cluster (partition preempt, billed per GPU-hour and CPU-hour) and move small and medium data to and from Marlowe with checksummed transfers, all through shk --site marlowe. Use for any Marlowe job, data transfer or queue question, or when shk reports an SSH or authentication problem with Marlowe.
---

# sherlock-kit on Marlowe (`shk --site marlowe`)

`shk` submits and tracks Marlowe jobs by name and moves data over the user's `marlowe-plain` OpenSSH master. Slurm is the only source of truth; `shk` keeps no local state. Operational results print one JSON document with `schema_version: 1` on stdout; `shk rules`, `shk --version` and help print text, and usage errors print text on stderr (exit 2).

Every Marlowe command is `shk --site marlowe ...`. The examples below run where the Marlowe configuration is the nearest one; elsewhere add `--project-dir DIR` after `--site marlowe`. Sherlock work uses the `sherlock-kit` skill; a task lives on one cluster. This skill does not restate the rules: if they are not loaded in this session, read `shk --site marlowe rules workstation` (on a workstation) or `shk --site marlowe rules cluster` (on a Marlowe login node) first. Marlowe has no `/etc/agents`.

## Setup check

1. Prefer the project environment's `shk` (`.venv/bin/shk`, or `shk` in the activated environment), not `uv run shk`. Ask before any install, upgrade or version switch. `shk --version` prints `sherlock-kit X.Y.Z (FORM)`; never use a `source` (development) checkout for real work. A release whose `shk --help` lists no `--site` cannot drive Marlowe. A project without `shk` gets, once the user agrees, a dependency on a release tag (0.10.0 or newer), never a branch: `sherlock-kit @ git+https://github.com/FridrichMethod/sherlock-kit@vX.Y.Z`. An upgrade changes that tag, relocks or reinstalls, and reruns `shk --site marlowe install-skill` with the same options.
2. `shk --site marlowe doctor` is read-only and offline: it checks Python, ssh, rsync, the configuration and the `marlowe-plain` master without dialing. `--remote` adds one read-only call on Marlowe.
3. In Codex, a sandbox startup failure or local SSH socket `Permission denied` / `Operation not permitted` is not a missing master: request per-command approval for the blocked command and retry once; never disable the sandbox or grant writes to all of HOME.
4. On a Marlowe login or compute node shk needs Python 3.11 or newer, and the system `python3` is older: use the `uv` module or a micromamba or conda environment (a job calls that environment's shk by its absolute path). On a login node `shk --site marlowe ...` runs Slurm locally (no ssh) by itself.

## Two-cluster layout

A project on both clusters keeps its Sherlock table where it is and adds a Marlowe configuration in its own directory:

```toml
# <repo>/marlowe/sherlock-kit.toml
site = "marlowe"
project = "demo-mw"                        # distinct from the Sherlock project value
remote_root = "/scratch/m000000/USER/demo"
account = "marlowe-m000000"                # the base project account
```

- `account` is required on Marlowe, dry run included, and never guessed from `$PROJECT_ID`. It must be the base account `marlowe-<project id>`: suffixed accounts and `marlowe-users` are refused. shk passes it to `sbatch` as `--account`; an `#SBATCH` line that sets the account is refused (exit 3).
- A distinct `project` gives the two clusters different job names, so a command aimed at the wrong cluster finds nothing instead of the other cluster's job.
- From the repository root, where the Sherlock table is nearest, `shk --site marlowe ...` is exit 1 `site_mismatch`: add `--project-dir marlowe`. An older shk that reads this file exits 1 `config_invalid`.
- The configuration may set `site`, `project`, `remote_root`, `account`, `control_host` and `data_host` (both default to `marlowe-plain`; a Sherlock alias is refused), the timeouts, `cpu_thread_budget` and a top-level `[validators]` table; never partitions, QOS or Slurm paths. Unknown keys are errors. Never put secrets there.

## Workflow

```sh
shk --site marlowe doctor
shk --site marlowe push data/inputs /scratch/m000000/USER/demo/inputs
shk --site marlowe submit --task train-a --script job.sh --partition preempt --cpus 8 --mem 64G --gpus 1 --time 2h --dry-run
shk --site marlowe submit --task train-a --script job.sh --partition preempt --cpus 8 --mem 64G --gpus 1 --time 2h
shk --site marlowe wait --task train-a --timeout 24h --max-pending 12h
shk --site marlowe status --task train-a
mkdir -p results
shk --site marlowe pull /scratch/m000000/USER/demo/runs/NAME/out results/NAME
```

- `--dry-run` is offline and prints the job name, the `sbatch` argv (with `--account`) and the frozen spec. A real submit needs the user's authorization of the campaign, as the rules say.
- The name is `shk.<project>.<task slug>.<hash>` from project, `--campaign` (default `default`) and `--task`. A second submit of a live name is refused (exit 3); a done or failed task needs `--resubmit "<reason>"`, after its cause is known. Deduplication reads `squeue` and the last 30 days of `sacct`.
- Partition, CPUs, memory and walltime are explicit; GPUs only with `--gpus N` (shk sends an untyped `-G N`). `preempt` is the only listed partition; `batch`, `hero` and every other partition are refused (exit 3 `unknown_partition`). Requeue follows the profile, so `preempt` jobs are submitted with `--requeue` and append every attempt to the same Slurm log.
- `submit` reports `submitted` (exit 0) with `job_id`. Report the job id, then end the turn and check when the user asks, or run one bounded wait (below); Marlowe waits range from minutes to days.
- For many independent tasks submit one array, `--array N[%M]`; every element counts toward the project account's submit limit, whatever the throttle M.
- Status reads at least 60 s apart, respecting `next_check_after` (a wait's reply carries it too), by hand only when needed (a wait exited, the user asks, an exit 5 `next` is due) and otherwise rarely, ideally 10 minutes or more apart; one `shk --site marlowe status --all` instead of a loop per task; never `watch` or loop status: `shk --site marlowe wait` watches instead. States: `new`, `live` (pending, running or completing), `done`, `failed`, `stale` (provenance but no Slurm rows in the window; treated as `new`). `current` describes the newest job of the name: `current.tasks.by_state` counts Slurm states, `current.max_restart` the restarts. A preempted and requeued attempt on `preempt` is not an anomaly.
- `shk --site marlowe wait` is one bounded, read-only watch, so that an agent spends no turns on a queued job. It selects names as status does (`--task` or `--name`, both repeatable, or `--all`, with `--campaign`, `--run-dir`, `--since`), needs `--timeout DUR` (1 minute to 168 hours) and reads every `--interval DUR` (default 5m, at least 1m; doubled, up to 30 minutes or the interval if longer, while nothing runs or changes). Its first read comes 60 s after it starts, so it may start right after any reply, and reads stay at least 60 s apart. With `--max-pending DUR` it also ends for a live name that has had no running task for that long, reading on that bound (never sooner than 60 s after the read before). DUR takes the forms of `submit --time`. It prints one reply at the end: the last status read plus `wait` (`outcome`, `names`, `reads`, `started_at`, `timeout_at`).
- Act on `wait.outcome`. `settled` (no name live): check that each is `done`, then pull. `failed` (a listed name has failed, at any read; with `--all`, one the wait saw live): diagnose first, and resubmit only with a found cause and within the authorization. `anomaly` (exit 6): stop and report. `max_pending`: read `current.tasks.by_state` (a REQUEUE_HOLD job stays live: report it as the rules say) and `shk --site marlowe resources --partition preempt --summary`, and consider a shorter walltime with `shk --site marlowe update`. `timeout`: run another wait or end the turn. Any other exit is the read's error: act on its code. Run the wait as a background command that the harness itself tracks and whose exit it reports (Claude Code: Bash `run_in_background`); a shell `&` is not one. Where the harness reports no such exit (in Codex, for example, nothing wakes you when a background command ends), run it in the foreground with a `--timeout` within the tool-call limit, or end the turn; never start it with `&` and never poll its output. Run at most one wait at a time, over every name to watch (repeat `--task`, or `--all`); after a further submit, let the running wait end, or stop it (it is read-only) and start one wait over all names, preferably by repeated `--task`: a restarted `--task` wait reports a name that failed in between at its first read (so list only the names still waited for), an `--all` wait does not, so after one check every name's state. Never detach it so that it outlives the session (`nohup`, a plain `&`, `setsid`, `disown`, a `tmux` or `screen` session left running, cron) or use it to re-prompt an agent.
- `shk --site marlowe cancel --task train-a` cancels only the live jobs of that name. `shk --site marlowe update --job-id N --expect-name NAME --time DUR` lowers a pending job's walltime, and `--throttle M` changes an array's throttle; never cancel and resubmit to adjust a queued job.

## Exit codes

| Code | Meaning | Do |
|---|---|---|
| 0 | ok | continue |
| 1 | configuration (missing `account`, `site_mismatch`, `config_invalid`), spec, verification, validator, malformed reply, failed remote read | fix the input; for `site_mismatch` follow its hint (`--project-dir` to the Marlowe configuration, or the missing `--site marlowe` inside a job); for `malformed_read` follow its hint (one read retry after 60 s) |
| 2 | usage error | read `shk <cmd> --help` |
| 3 | refused, and nothing was submitted, staged, cancelled, released, requeued or updated: live duplicate, finished task without `--resubmit`, an unlisted partition, an `#SBATCH` directive shk owns, a request that Slurm's test refuses (plain submit or `--preflight`), a remote guard that stopped before `sbatch`, `scancel` or `scontrol` ran, push or pull on Marlowe, `requeue --self` outside a job (`self_only`) | read the message; change the request, not the guard |
| 4 | SSH transport or master check unavailable | `auth_required`: ask the user to run the hint (`ssh marlowe-plain`), then retry once. `unavailable` with a `~/.ssh/config` hint: the client config needs fixing; logging in again does not help. Local permission denial: Setup check 3. Other `unavailable` or `timeout`: run `shk --site marlowe doctor`, retry once after about a minute, then stop and tell the user |
| 5 | outcome unknown after a dispatched submit, cancel, release, requeue or update: `sbatch` ran without printing a strict job id, `scancel` or `scontrol` exited nonzero, an update's re-read did not confirm it, or the reply was lost; `detail` carries the evidence | run the command in the reply's `next`, not before the time it names (at least 60 s); never resubmit or repeat a mutation blind |
| 6 | status ok, but the newest job of a name has an anomaly | stop and report it to the user |

## Replies

- Every reply that names its `executor`, and the `doctor` reply, carries `"site": "marlowe"`. Such a reply without it came from Sherlock: stop and check the command.
- Errors print `{"ok": false, "error": TOKEN, "message": ..., "hint": ...}` without a `site` key, except the errors of `status --details`, which keep the read's `executor` and `site`. Hints that name an shk command carry the selector, such as `shk --site marlowe doctor`.
- The exit 5 `next` names one replayable status command that carries the selector (`shk --project-dir DIR --site marlowe --executor KIND status ...`) and the time before which it must not run. Run it as given, at that time or later.

## What Slurm refuses on Marlowe

A plain `submit` first runs Slurm's test (`sbatch --test-only`), after the duplicate read and before staging. When Slurm refuses, `submit` returns that preflight reply unchanged (`"action": "preflight"`, `"ok": false`, `"job_submitted": false`, Slurm's text in `preflight.stderr`), exit 3 with nothing staged: change the request. Refusals seen on Marlowe:

| Request | Slurm's message |
|---|---|
| walltime above the partition limit | `Requested time limit is invalid (missing or exceeds some limit)` |
| a feature no node has (there are no GPU model features) | `Invalid feature specification` |
| typed GRES, `--tmp`, more GPUs on one node than it has | `Requested node configuration is not available` |
| more jobs or array elements than the project account may have submitted | `MaxSubmitJobsPerAccount`, `Job violates accounting/QOS policy (job submit limit, user's size and/or time limits)` |
| one job with more GPUs than the project account's cap | `MaxGRESPerAccount` |

"Requested node configuration is not available" may also mean that no node is schedulable right now: read `shk --site marlowe resources --partition preempt --summary` (a snapshot of free CPUs, memory and GPUs on schedulable nodes, not a reservation; `--shape NAME:cpus=N,mem=SIZE,gpus=N` counts how many such requests fit) before changing a request on that message. `--preflight` runs the same test alone and creates no job. Its `estimate` start time is not a wait prediction on Marlowe: a job estimated more than a day out started within a minute. Never relay it as a prediction. A controller transport failure is exit 1 `malformed_read`, not a refusal.

## Job scripts

- `module` is defined under a plain `#!/bin/bash`, and the Slurm clients are on PATH in a job.
- `TMPDIR` is `/tmp`, not a job directory. Job-local space is `/local_scratch/$USER.$SLURM_JOB_ID`, visible only in the job and wiped when it ends: copy what must survive to scratch.
- No GPU is visible without `--gpus`. Compute nodes reach the internet.
- The job environment is captured from a login shell plus the exported variables, so keep login startup files light and quiet: a slow or failing capture can leave a job requeued and held.
- The job runs in `--run-dir` (default `<remote_root>/runs/<name>`), which keeps `.shk/<name>.<jobid>.sh` and `.json`. `$SHK_NAME` holds the job name and `$SHK_SUBMISSION_TOKEN` the token of `--submission-token`; beside them the job carries the thread defaults of `cpu_thread_budget` (when set) and the Slurm `PATH` entry and `SLURM_CONF` that `sbatch` ran with, and none names the site. `#SBATCH` lines may not set what shk owns (account, exclude, GPUs or gres, partition, array, dependency, begin, job name, hold). No GPU model `--constraint`, `--tmp` or `#SBATCH --exclusive`.
- Preemption restarts the script from the top. Write atomic checkpoints (write, then rename) under `/scratch/PROJECT/USER` every few minutes, resume from the newest one on start, do not depend on a signal, and stop at the authorized retry bound, for example `[ "${SLURM_RESTART_COUNT:-0}" -le 3 ] || exit 1` near the top. `SLURM_RESTART_COUNT` is unset in the first attempt and 1 after one self-requeue (observed); by Slurm's source it counts every restart: preemptions, failed launches and self-requeues alike.

## Inside a job

Observed on Marlowe in one CPU-only job: `doctor --remote`, `status --job-id` and `submit --preflight` (Slurm's test, no job), `allocation` for the batch shell and for an `srun` step, the template below with its `--signal` and `requeue --self` into a second attempt, and `manifest`. Not run there: a GPU job, an array element, a preemption, a real in-job `submit`; those rest on offline tests and Slurm's source.

- shk calls inside a Marlowe job are steps of the job's own work: `requeue --self` before the walltime, `allocation`, `manifest`, `status --job-id` of the job itself, and at most a `submit` of a fixed next stage. The bound the user authorized covers every job the script can submit and every attempt; the stage list is fixed and finite, and no job submits its own kind again. No `shk wait` and no status loop inside a job (a job that waits is billed idle time): chain stages with `--after-ok` and wait from the workstation or a login-node session with `shk --site marlowe wait`. Never start or schedule an agent or LLM session from a job.
- Write every call as `shk --site marlowe --executor local ...` by the absolute path of the environment of Setup check 4, under `/projects/PROJECT` or scratch. With `--site marlowe` the executor is local by itself on a Marlowe node; the explicit flag makes a wrong machine fail instead of dialing. shk reads no site from the environment.
- `requeue --self --expect-name "$SHK_NAME"`, `allocation --pid PID --job-id ID`, `status --job-id ID`, `status --name "$SHK_NAME"` and `manifest DIR` (the job's last step, see Data) need no configuration; `submit`, `status --task` and `cancel --task` need `--project-dir DIR` naming a Marlowe configuration that exists on Marlowe. A restarted attempt (preemption or self-requeue) runs an in-job `submit` again, and shk refuses that duplicate with exit 3 and `error` `live` (or `done` or `failed` once the stage ended): treat that refusal as already submitted, and any other exit 3, such as Slurm's refusal of the request, as a failure. A configuration of another site at or above the run directory (a whole repository pushed under `remote_root`) makes `--site marlowe` exit 1 `site_mismatch`: pass `--project-dir` naming the Marlowe configuration's directory or one without a configuration, such as the run directory.
- Without `--site` an in-job command reaches no cluster. With `--executor local`, and for `requeue --self` and `allocation` in any case, the answer is exit 1 `site_mismatch` with a hint that names `--site marlowe`; with neither flag, `status --job-id`, `status --name`, `cancel --name`, `update`, `release` and `doctor` answer exit 4 naming `sherlock-plain`, which here means the missing flags, not a missing login.
- `requeue --self` needs real Slurm PID membership in the current physical allocation, the current UID and name, RUNNING, a `requeue=yes` partition (`preempt`) and Requeue=1; the token is compared with Slurm Comment only when given, and an array element is requeued as `ARRAYJOBID_TASKID`, never the whole array. Every requeued attempt is billed and is one of the authorized retries, and the job queues again like a new submission (observed: it could start again only about two minutes after the requeue). Nothing after an accepted requeue is sure to run (observed: the reply was written, the next script line never ran): checkpoint first, request last. A lost reply is exit 5: never retry blind.
- `allocation --pid PID --job-id ID [--step-id N]` proves that a PID of the current UID belongs to the job (and step) on this node and reports the allocated CPUs, GPUs and nodes; on Marlowe `gpu_sku_candidates` holds empty `candidates` (untyped GPUs, no `GPU_SKU:` feature).

```bash
#!/bin/bash
# Submit with --time T --signal B:USR1@300, 300 s being well above the time a checkpoint
# save takes: Slurm sends USR1 to this shell alone about 300 s (up to a minute more)
# before the time limit, never during a preemption's grace window. Every other end
# (preemption, time limit, scancel) kills the workload at once: its periodic
# checkpoints are what survive.
shk=/projects/m000000/USER/demo/.venv-shk/bin/shk   # absolute path, Python 3.11 or newer
max_restarts=3                                       # authorized retries; preemptions count too
[ "${SLURM_RESTART_COUNT:-0}" -le "$max_restarts" ] || exit 1

requeue() {
    kill -TERM "$work" 2>/dev/null                   # the workload saves a checkpoint and exits
    wait "$work"
    [ "${SLURM_RESTART_COUNT:-0}" -lt "$max_restarts" ] || exit 1
    "$shk" --site marlowe --executor local requeue --self --expect-name "$SHK_NAME"
    exit                                             # nothing after an accepted requeue is sure to run
}
trap requeue USR1

./work --resume &                                    # directly, not through srun; in the background
work=$!
wait "$work"
status=$?
trap '' USR1                                         # past the workload: finish, never requeue
exit "$status"
```

- The template covers one workload that the batch shell starts directly. By Slurm's source (not observed on Marlowe), started through `srun` the `kill -TERM` becomes SIGKILL of the tasks, and a preemption makes the script exit by itself during the grace window, which counts toward a hold (the fourth such exit holds the job until an operator releases it). Such scripts are outside the template.
- Choose the signal's lead (300 above) longer than a checkpoint save plus about a minute and well below `--time`: with `--time` at or under the lead every attempt requeues itself at once until the bound. Set `max_restarts` to the authorized retries, which cover the planned walltime segments plus preemptions, routine on `preempt`. The guard stops the work at the authorized bound; an attempt past it still starts and is billed for the seconds before the guard. A job stopped at the bound ends failed with its checkpoint and is resubmitted only with the user's consent. Make a later start of a finished workload cheap (`./work --resume` exits 0 at once): a job can be started again after its work is done (inferred from Slurm's source).
- By Slurm's source (not observed on Marlowe) the batch shell gets no signal at the start of a preemption's grace window, and `--signal` does not fire there; it gets SIGTERM only at the end and is then killed. Periodic checkpoints stay the only guarantee, and a script must not exit by itself when its workload is signaled.

## Data

- `shk --site marlowe push LOCAL REMOTE` copies over the login-node master into REMOTE (absolute, under `remote_root` unless `--anywhere`) and never deletes. REMOTE's parent must exist (otherwise exit 1 `transfer_failed`, rsync exit 11), so create `remote_root` once before the first push: push a directory that holds `inputs/` to `remote_root` itself, whose parent `/scratch/PROJECT/USER` exists, or run `ssh -oBatchMode=yes -oProxyCommand=false marlowe-plain mkdir -p /scratch/m000000/USER/demo`. Check that push exits 0 before submitting a job that reads REMOTE, since a failed push can leave a truncated file. Push and pull are for small and medium trees; large data goes through Globus, outside shk.
- `shk --site marlowe pull REMOTE LOCAL [--validate NAME] [--replace]` needs `REMOTE/SHA256SUMS` and an existing parent of LOCAL, copies exactly the listed files into a private stage beside LOCAL, verifies every hash, runs validator NAME from the configuration's `[validators]` table (the stage is its last argument, read-only), then renames the stage onto LOCAL and writes a receipt. `already_present` rests on that receipt; LOCAL is not verified again.
- The job writes the manifest over its output directory DIR (for example `out/` under the run directory, never the run directory itself) as its last step: `shk --site marlowe manifest DIR` when shk's environment is available in the job (see Inside a job), else `(cd DIR && find . -name '.shk*' -prune -o -type f ! -name SHA256SUMS -printf '%P\0' | sort -z | xargs -0r sha256sum --) > DIR/SHA256SUMS`.
- On Marlowe itself push and pull are refused (exit 3 `transfer_on_marlowe`): transfers start outside Marlowe.
- `/scratch/PROJECT` is shared by the project, not backed up and may be purged; `/projects/PROJECT` is backed up and billed; home is small.
- Marlowe takes Low and Moderate Risk data only, never High Risk data or PHI; ask the user when the classification is unclear.

## Never

- Authenticate for the user, retry a login in a loop, or store passwords or Duo secrets.
- Start a tunnel, VPN, relay or code server that reaches Marlowe without SUNet ID, password and Duo, or run one on Marlowe.
- Run raw `sbatch`, `scancel`, `squeue` loops or `scontrol update` when shk covers the task, or `scancel -u $USER`.
- Poll with `watch` or a status loop, detach a wait so that it outlives the session, or use a wait to re-prompt an agent.
- Run a Marlowe job that only waits or polls or that submits its own kind again, or start or schedule an agent or LLM session from a job.
- Release a job held after a requeue (`shk release` is only for holds that `submit --hold` set); report it and recover it with the user's consent by `shk --site marlowe cancel` and `shk --site marlowe submit --resubmit REASON`.
- Resubmit after exit 5 or a failure without checking status and finding the cause.
- Wrap shk in an outer timeout shorter than its own: a mutation in flight then becomes outcome unknown.
- Submit GPU work while developing or testing sherlock-kit itself, or use a development checkout of it for real work.
- Use `--account`, a partition other than `preempt`, a GPU model constraint, typed GRES or `--tmp`.
