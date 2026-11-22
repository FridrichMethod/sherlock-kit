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
4. On a Marlowe login node shk needs Python 3.11 or newer, and the system `python3` is older: use the `uv` module or a micromamba or conda environment. There `shk --site marlowe ...` runs Slurm locally (no ssh) by itself.

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
shk --site marlowe status --task train-a
mkdir -p results
shk --site marlowe pull /scratch/m000000/USER/demo/runs/NAME/out results/NAME
```

- `--dry-run` is offline and prints the job name, the `sbatch` argv (with `--account`) and the frozen spec. A real submit needs the user's authorization of the campaign, as the rules say.
- The name is `shk.<project>.<task slug>.<hash>` from project, `--campaign` (default `default`) and `--task`. A second submit of a live name is refused (exit 3); a done or failed task needs `--resubmit "<reason>"`, after its cause is known. Deduplication reads `squeue` and the last 30 days of `sacct`.
- Partition, CPUs, memory and walltime are explicit; GPUs only with `--gpus N` (shk sends an untyped `-G N`). `preempt` is the only listed partition; `batch`, `hero` and every other partition are refused (exit 3 `unknown_partition`). Requeue follows the profile, so `preempt` jobs are submitted with `--requeue` and append every attempt to the same Slurm log.
- `submit` reports `submitted` (exit 0) with `job_id`. Report the job id and end the wait; Marlowe waits range from minutes to days.
- For many independent tasks submit one array, `--array N[%M]`; every element counts toward the project account's submit limit, whatever the throttle M.
- Status reads at least 60 s apart, respecting `next_check_after`; one `shk --site marlowe status --all` instead of a loop per task; never `watch` or poll. States: `new`, `live` (pending, running or completing), `done`, `failed`, `stale` (provenance but no Slurm rows in the window; treated as `new`). `current` describes the newest job of the name: `current.tasks.by_state` counts Slurm states, `current.max_restart` the restarts. A preempted and requeued attempt on `preempt` is not an anomaly.
- `shk --site marlowe cancel --task train-a` cancels only the live jobs of that name. `shk --site marlowe update --job-id N --expect-name NAME --time DUR` lowers a pending job's walltime, and `--throttle M` changes an array's throttle; never cancel and resubmit to adjust a queued job.

## Exit codes

| Code | Meaning | Do |
|---|---|---|
| 0 | ok | continue |
| 1 | configuration (missing `account`, `site_mismatch`, `config_invalid`), spec, verification, validator, malformed reply, failed remote read | fix the input; for `site_mismatch` pass `--project-dir` to the Marlowe configuration; for `malformed_read` follow its hint (one read retry after 60 s) |
| 2 | usage error | read `shk <cmd> --help` |
| 3 | refused, and nothing was submitted, staged, cancelled, released, requeued or updated: live duplicate, finished task without `--resubmit`, an unlisted partition, an `#SBATCH` directive shk owns, a request that Slurm's test refuses (plain submit or `--preflight`), a remote guard that stopped before `sbatch`, `scancel` or `scontrol` ran, push or pull on Marlowe | read the message; change the request, not the guard |
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
- The job runs in `--run-dir` (default `<remote_root>/runs/<name>`), which keeps `.shk/<name>.<jobid>.sh` and `.json`; `$SHK_NAME` holds the job name. `#SBATCH` lines may not set what shk owns (account, exclude, GPUs or gres, partition, array, dependency, begin, job name, hold). No GPU model `--constraint`, `--tmp` or `#SBATCH --exclusive`.
- Preemption restarts the script from the top. Write atomic checkpoints (write, then rename) under `/scratch/PROJECT/USER` every few minutes, resume from the newest one on start, do not depend on a signal, and stop at the authorized retry bound, for example `[ "${SLURM_RESTART_COUNT:-0}" -le 3 ] || exit 1` near the top.
- Do not call shk inside a Marlowe job: this release exports no site into jobs. Write the manifest with the one-liner below as the job's last step.

## Data

- `shk --site marlowe push LOCAL REMOTE` copies over the login-node master into REMOTE (absolute, under `remote_root` unless `--anywhere`) and never deletes. REMOTE's parent must exist (otherwise exit 1 `transfer_failed`, rsync exit 11), so create `remote_root` once before the first push: push a directory that holds `inputs/` to `remote_root` itself, whose parent `/scratch/PROJECT/USER` exists, or run `ssh -oBatchMode=yes -oProxyCommand=false marlowe-plain mkdir -p /scratch/m000000/USER/demo`. Check that push exits 0 before submitting a job that reads REMOTE, since a failed push can leave a truncated file. Push and pull are for small and medium trees; large data goes through Globus, outside shk.
- `shk --site marlowe pull REMOTE LOCAL [--validate NAME] [--replace]` needs `REMOTE/SHA256SUMS` and an existing parent of LOCAL, copies exactly the listed files into a private stage beside LOCAL, verifies every hash, runs validator NAME from the configuration's `[validators]` table (the stage is its last argument, read-only), then renames the stage onto LOCAL and writes a receipt. `already_present` rests on that receipt; LOCAL is not verified again.
- The job writes the manifest over its output directory DIR (for example `out/` under the run directory, never the run directory itself): `(cd DIR && find . -name '.shk*' -prune -o -type f ! -name SHA256SUMS -printf '%P\0' | sort -z | xargs -0r sha256sum --) > DIR/SHA256SUMS`.
- On Marlowe itself push and pull are refused (exit 3 `transfer_on_marlowe`): transfers start outside Marlowe.
- `/scratch/PROJECT` is shared by the project, not backed up and may be purged; `/projects/PROJECT` is backed up and billed; home is small.
- Marlowe takes Low and Moderate Risk data only, never High Risk data or PHI; ask the user when the classification is unclear.

## Never

- Authenticate for the user, retry a login in a loop, or store passwords or Duo secrets.
- Start a tunnel, VPN, relay or code server that reaches Marlowe without SUNet ID, password and Duo, or run one on Marlowe.
- Run raw `sbatch`, `scancel`, `squeue` loops or `scontrol update` when shk covers the task, or `scancel -u $USER`.
- Release a job held after a requeue (`shk release` is only for holds that `submit --hold` set); report it and recover it with the user's consent by `shk --site marlowe cancel` and `shk --site marlowe submit --resubmit REASON`.
- Resubmit after exit 5 or a failure without checking status and finding the cause.
- Wrap shk in an outer timeout shorter than its own: a mutation in flight then becomes outcome unknown.
- Submit GPU work while developing or testing sherlock-kit itself, or use a development checkout of it for real work.
- Use `--account`, a partition other than `preempt`, a GPU model constraint, typed GRES or `--tmp`.
