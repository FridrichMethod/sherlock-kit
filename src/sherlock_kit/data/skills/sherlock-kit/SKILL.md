---
name: sherlock-kit
description: Run, track and cancel Slurm jobs and job arrays on Stanford's Sherlock cluster, move data to and from Sherlock with checksummed transfers, and check partition occupancy before using borrowed partitions, all through the shk CLI. Use for any Sherlock job, data transfer, queue or occupancy question, or when shk reports an SSH or authentication problem.
---

# sherlock-kit (`shk`)

`shk` submits and tracks Sherlock jobs by name and moves data through the DTN. Slurm is the only source of truth; `shk` keeps no local state. Every operational command prints one JSON document on stdout; `shk rules` and `shk --version` print text.

## Setup check

1. Prefer the project environment's `shk` (`.venv/bin/shk`, or `shk` and `python -m sherlock_kit` in the activated environment), else the global `~/.local/bin/shk`. Not `uv run shk`: it locks and syncs the project environment first. Ask before any `pip install`, upgrade or version switch.
2. Run `shk --version`: `sherlock-kit X.Y.Z (FORM)`. FORM is `git <commit>` (installed from a tag), `editable <path>`, `submodule <path>` (the dotfiles copy), `source <path>` (a development checkout: never use it for real work) or `unknown`. This skill requires shk 0.4 or newer; with an older one, or when a flag here is missing, follow `shk <cmd> --help` instead.
3. If no Sherlock rules are loaded in this session, read them first: `shk rules workstation` on a workstation, `shk rules cluster` on Sherlock.
4. `shk doctor` is read-only and offline (`--remote` adds one read-only call on Sherlock).

## Exit codes

| Code | Meaning | Do |
|---|---|---|
| 0 | ok | continue |
| 1 | local error: config, spec, verification, validator, malformed reply | fix the input; do not retry unchanged |
| 2 | usage error | read `shk <cmd> --help` |
| 3 | refused by policy or Slurm: live duplicate, finished task without `--resubmit`, missing `--courtesy-ack`, an `#SBATCH` directive shk owns, Slurm rejected, push or pull on Sherlock | read the message; change the request, not the guard |
| 4 | Sherlock unreachable without a new login | `auth_required`: ask the user to run the hint (`ssh sherlock-plain`, or `ssh -fN sherlock-dtn`), then retry once. `unavailable` with a `~/.ssh/config` hint: the client config needs fixing; logging in again does not help. Other `unavailable` or `timeout` (hint `run shk doctor`): run `shk doctor`, retry once after about a minute, then stop and tell the user |
| 5 | outcome unknown after a dispatched submit or cancel | run the `shk status` command in the reply's `next`, not before the time it names (at least 60 s); never resubmit blind |
| 6 | status ok, but the newest job of a name has an anomaly | stop and report it to the user |

## Configuration

`shk` reads `[tool.sherlock-kit]` from `pyproject.toml` (or top-level keys in `sherlock-kit.toml`) in the nearest directory at or above the working directory that has one; from elsewhere pass `--project-dir DIR`. Keys: `project` (required to submit), `remote_root` (absolute Sherlock path, required to submit and, without `--anywhere`, to push), optional `control_host`, `data_host`, timeouts and `[tool.sherlock-kit.validators]`. Unknown keys are errors. Never put secrets there.

## Submit

```sh
shk submit --task train-a --script job.sh --partition normal --cpus 4 --mem 16G --time 2h --dry-run
shk submit --task train-a --script job.sh --partition normal --cpus 4 --mem 16G --time 2h
```

- Always `--dry-run` first: it is offline and prints the job name, the sbatch argv and the frozen spec.
- The name is `shk.<project>.<task slug>.<hash>` from project, `--campaign` (default `default`) and `--task`. The same task always gets the same name, so a second submit is refused (exit 3) while a job of that name is pending or running, even with `--resubmit`.
- A done or failed task is refused unless `--resubmit "<reason>"` names why it runs again; find the cause of a failure first.
- Deduplication reads only `squeue` and the last 30 days of `sacct`. A task whose jobs are older than that submits again as new, without `--resubmit`, even when its run directory still exists: check before rerunning old work.
- Partition, CPUs, memory and walltime are explicit. GPUs only with `--gpus N` on a partition that allows them; `--constraint` instead of excluding nodes. Requeue only on `owners`, whose scripts must checkpoint periodically.
- The job runs in `--run-dir` (default `<remote_root>/runs/<name>`), which keeps `.shk/<name>.<jobid>.sh` and `.json` as provenance. `#SBATCH` lines in the script may not set what shk owns, in any form sbatch accepts (`-p`, `-Hb`, `--part`, ...): account, exclude, GPUs or gres, partition, array, dependency, begin or job name; heterogeneous jobs (`hetjob`, `packjob`) are refused.
- Chain stages with `--after-ok JOBID` (repeatable; several ids separated by spaces, never commas) instead of polling. Inside a job script never call `shk submit` or start an agent.

## Arrays

For many independent tasks submit one array: `--array N%M` runs N tasks (2 to 1000), at most M at a time; each task reads `$SLURM_ARRAY_TASK_ID` (0 to N-1) and logs to `slurm-%A_%a.*`. Size tasks to at least 10 minutes of real work, ideally 30; never pad with `sleep`.

## Status

```sh
shk status --task train-a
shk status --all
```

- At least 60 s between checks; respect `next_check_after` in every submit, status, cancel and occupancy reply. One `shk status --all` (or several `--task` in one call) instead of a loop per task. Never `watch` or poll in a loop.
- `--since DAYS` sets the `sacct` window (default 30, 7 with `--all`). With several `--task`, give `--campaign` once for all of them or once per `--task`.
- States: `new` (no job), `live` (pending, running or completing; throttle-held array tasks count as pending), `done` (all tasks completed), `failed` (finished, at least one task not completed), `stale` (provenance files but no Slurm rows in the window; treated as `new`).
- `current` describes the newest job of the name (tasks, exit codes, restarts, cost); older jobs are in `history`. An anomaly such as `unexpected_preemption` or `duplicate_live` gives exit 6: stop and report.

## Data

```sh
shk push data/inputs /scratch/users/USER/project/inputs
mkdir -p results
shk pull /scratch/users/USER/project/runs/NAME/out results/NAME --validate bundle
```

- `shk push LOCAL REMOTE` copies into REMOTE (absolute, under `remote_root` unless `--anywhere`) and never deletes. Pushed files keep their local modification times, so inputs that are old locally can be purge-eligible on scratch soon after the push: push them shortly before the jobs that read them.
- `shk pull REMOTE LOCAL` needs `REMOTE/SHA256SUMS` and an existing parent directory of LOCAL, copies exactly those files into a private stage beside LOCAL (`.NAME.shk-stage` for LOCAL `.../NAME`, with `.NAME.shk-lock`), verifies every hash, runs the validator named by `--validate`, then renames the stage onto LOCAL and writes `.NAME.shk-receipt.json`. An existing LOCAL is kept unless `--replace` (the old copy stays as `.NAME.shk-old-<UTC>`); a rerun resumes the stage.
- The validator is the project's argv from `[tool.sherlock-kit.validators]`; it runs from the project root with the stage directory as its last argument, exit 0 passes, and it must treat the stage as read-only (what it writes is promoted).
- The job writes the manifest as its last step: `shk manifest DIR` when shk is available there, else `(cd DIR && find . -name '.shk*' -prune -o -type f ! -name SHA256SUMS -printf '%P\0' | sort -z | xargs -0r sha256sum --) > DIR/SHA256SUMS`. DIR is the job's output directory (for example `out/` under the run directory), never the run directory itself, which holds `.shk/` and the Slurm logs still being written.
- Never touch or rewrite scratch files to keep them from the 90-day purge; copy what must last to `$GROUP_HOME`, Oak or the workstation.

## Occupancy and borrowed partitions

- `shk occupancy --partition btrippe` shows who uses a partition now. Run it before every submission to a borrowed partition (`borrowed=yes` in the rules' partition profiles), read the courtesy text, then submit with `--courtesy-ack`. The submit reply repeats the occupancy and courtesy text and warns, without refusing, when others are running there and you already have 2 jobs there or ask for more than 2 h.
- `btrippe`: about 2 jobs x 2 h while others are active. `possu`: submit only between 00:00 and 07:00 Pacific, a few short jobs. Unlisted partitions are refused; scheduler visibility is never authorization.

## Cancel

`shk cancel --task train-a` (or `--name NAME`) cancels only the live jobs of that one name and reports their ids and `next_check_after`. Cancel only when the user asks.

## On Sherlock

On a login node or inside an allocation `shk` uses the local executor (no ssh); `SHK_EXECUTOR=local` or `--executor local` forces it. `shk push` and `shk pull` are refused there (exit 3): the data is already on the cluster, and large internal copies run in a Slurm job. Follow `shk rules cluster`.

## Never

- Authenticate for the user, retry a login in a loop, or store passwords or MFA secrets.
- Run raw `sbatch`, `scancel` or `squeue` loops over ssh when `shk` covers the task, or `scancel -u $USER`.
- Resubmit after exit 5 or a failure without checking `shk status` and finding the cause.
- Submit GPU work while developing or testing sherlock-kit itself.
- Use a partition that is not listed, or a borrowed one without `shk occupancy` and `--courtesy-ack`.
- Extend scratch lifetime, scan other users' directories, or handle High Risk data.
- Use a development checkout of sherlock-kit instead of an installed `shk`.
