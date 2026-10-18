---
name: sherlock-kit
description: Run, track and cancel Slurm jobs and job arrays on Stanford's Sherlock cluster, move data to and from Sherlock with checksummed transfers, and check partition occupancy before using borrowed partitions, all through the shk CLI. Use for any Sherlock job, data transfer, queue or occupancy question, or when shk reports an SSH or authentication problem.
---

# sherlock-kit (`shk`)

`shk` submits and tracks Sherlock jobs by name and moves data through the DTN. Slurm is the only source of truth; `shk` keeps no local state. Operational results print one JSON document with `schema_version: 1` on stdout; `shk rules`, `shk --version` and help print text, and usage errors print text on stderr (exit 2).

## Setup check

1. Prefer the project environment's `shk` (`.venv/bin/shk`, or `shk` and `python -m sherlock_kit` in the activated environment), else the global `~/.local/bin/shk`. Not `uv run shk`: it locks and syncs the project environment first. Ask before any `pip install`, upgrade or version switch.
2. Run `shk --version`: `sherlock-kit X.Y.Z (FORM)`. FORM is `git <commit>` (installed from a tag), `editable <path>`, `submodule <path>` (the dotfiles copy), `source <path>` (a development checkout: never use it for real work) or `unknown`. The controller extensions require shk 0.5 or newer; with an older one, or when a flag here is missing, follow `shk <cmd> --help` instead.
3. If no Sherlock rules are loaded in this session, read them first: `shk rules workstation` on a workstation, `shk rules cluster` on Sherlock.
4. `shk doctor` is read-only and offline (`--remote` adds one read-only call on Sherlock).
5. In Codex, a sandbox startup failure or local SSH socket `Permission denied` / `Operation not permitted` does not mean a missing master. Request per-command approval for the blocked command, then retry once; never disable the sandbox or grant writes to all of HOME. Even offline `doctor` needs control-socket access; `submit --dry-run` does not. On Sherlock, per-command approval is expected because the Codex sandbox cannot start there.

## Exit codes

| Code | Meaning | Do |
|---|---|---|
| 0 | ok | continue |
| 1 | config, spec, verification, validator, malformed reply | fix input errors before retrying; for `malformed_read`, follow its hint if present (one read retry after 60 s; `shk doctor --remote` checks remote tools) |
| 2 | usage error | read `shk <cmd> --help` |
| 3 | refused by policy or Slurm: live duplicate, finished task without `--resubmit`, missing `--courtesy-ack`, `dev` (no batch jobs), an `#SBATCH` directive shk owns, Slurm rejected, push or pull on Sherlock | read the message; change the request, not the guard |
| 4 | SSH transport or master check unavailable | Local permission denial: follow Setup check 5. `auth_required`: ask the user to run the hint (`ssh sherlock-plain`, or `ssh -fN sherlock-dtn`), then retry once. `unavailable` with a `~/.ssh/config` hint: the client config needs fixing; logging in again does not help. Other `unavailable` or `timeout` (hint `run shk doctor`): run `shk doctor`, retry once after about a minute, then stop and tell the user |
| 5 | outcome unknown after a dispatched submit, cancel, release or requeue | run the `shk status` command in the reply's `next`, not before the time it names (at least 60 s); never resubmit blind |
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
- Partition, CPUs, memory and walltime are explicit. GPUs only with `--gpus N` on a partition that allows them; `--constraint` instead of excluding nodes. Requeue only on `owners`, whose scripts must checkpoint periodically. `dev` takes no batch jobs (Sherlock rejects them, so submit refuses it, exit 3): use an interactive `sh_dev` or `salloc` session there.
- The job runs in `--run-dir` (default `<remote_root>/runs/<name>`), which keeps `.shk/<name>.<jobid>.sh` and `.json` as provenance. `#SBATCH` lines in the script may not set what shk owns, in any form sbatch accepts (`-p`, `-H`, `--part`, ...): account, exclude, GPUs or gres, partition, array, dependency, begin, job name, hold, and comment when `--submission-token` sets it; heterogeneous jobs (`hetjob`, `packjob`) are refused.
- `--preflight` (mutually exclusive with `--dry-run`) reads Slurm validation using `sbatch --test-only`, creates no job or provenance, and retains returncode/stdout/stderr; quota refusal is exit 3, read failures are never unknown. `--submission-token TOKEN` sets a public Slurm Comment, exported to the job as `$SHK_SUBMISSION_TOKEN`; `--hold` needs a token and sets a user hold for `shk release`. Both are frozen in the spec.
- Chain stages with `--after-ok JOBID` (repeatable; several ids separated by spaces, never commas) instead of polling. Bounded deterministic compute controllers inside a Slurm allocation may call `shk --executor local` for authorized later compute stages, without a workstation; never schedule future agent/LLM sessions.

## Arrays

For many independent tasks submit one array: `--array N[%M]` runs N tasks (2 to 1000), optionally at most M at a time; each task reads `$SLURM_ARRAY_TASK_ID` (0 to N-1) and logs to `slurm-%A_%a.*`. Size tasks to at least 10 minutes of real work, ideally 30; never pad with `sleep`.

## Status

```sh
shk status --task train-a
shk status --all
```

- At least 60 s between checks; respect `next_check_after` in every submit, status, cancel and occupancy reply. Read the local UTC clock before the next check instead of estimating elapsed time. One `shk status --all` (or several `--task` in one call) instead of a loop per task. Never `watch` or poll in a loop.
- `--since DAYS` sets the `sacct` window (default 30, 7 with `--all`). With several `--task`, give `--campaign` once for all of them or once per `--task`.
- States: `new` (no job), `live` (pending, running or completing; throttle-held array tasks count as pending), `done` (all tasks completed), `failed` (finished, at least one task not completed), `stale` (provenance files but no Slurm rows in the window; treated as `new`).
- `current` describes the newest job of the name (tasks, exit codes, restarts, cost); older jobs are in `history`. An anomaly such as `unexpected_preemption` or `duplicate_live` gives exit 6: stop and report.

## Controller evidence and exact mutations

- `shk resources --partition owners` (repeatable partition) reads node state, features and configured minus allocated CPU/memory/GPU capacity per node. Count free capacity only on nodes with `schedulable: true`: DOWN, DRAIN, NOT_RESPONDING and similar states make it false, and null means unknown. Missing facts are null and `complete: false`; OS FreeMem never substitutes. It is a snapshot, not a reservation.
- `shk status --job-id ID --details` (repeatable ID, mutually exclusive selector) reads current UID legacy or shk jobs. `details.queue` and `details.accounting` retain raw records with cluster, DBIndex, physical/array IDs, times, UID, Comment, TRES, nodes, restarts and exits. `details.jobs` retains queued physical `scontrol` identities with original `fields` and `raw` for request verification. Incomplete evidence or missing IDs is explicit; do not treat it as permission to resubmit.
- `shk release --job-id ID --expect-name NAME --expect-token TOKEN` releases only a verified exact current-UID job held by `submit --hold --submission-token TOKEN`; both expectations are required. On a borrowed partition a release needs the same occupancy check, budget and window as a submission (see Occupancy below).
- `shk requeue --self --expect-name NAME [--expect-token TOKEN]` runs only with the local executor inside the allocation: it needs real Slurm PID membership in the current physical allocation, the current UID and name, RUNNING, a `requeue=yes` partition (`owners`) and Requeue=1; the token is compared with Slurm Comment only when given. An array element is requeued as `ARRAYJOBID_TASKID`, never the whole array. Lost mutation replies remain unknown (exit 5); never retry blind. Checkpoint durability and maximum retries belong to the project controller.
- `shk --executor local allocation --pid PID --job-id ID [--step-id N]` inside a job proves that a worker PID of the current UID belongs to that job (and numeric step) on this host through `scontrol pidinfo` and `listpids`, and reports the actual allocated CPUs, GPUs, nodes and raw records; for an array element pass `ARRAYJOBID_TASKID`. Missing counts stay null and ambiguous or contradictory evidence fails (exit 1). Use it instead of parsing `scontrol` or trusting environment variables.
- A deterministic controller must keep all actions bounded and authorized, batch status reads at least 60 s apart and stay within its allocation. It must not invoke an LLM or schedule a later agent session.

## Data

```sh
shk push data/inputs /scratch/users/USER/project/inputs
mkdir -p results
shk pull /scratch/users/USER/project/runs/NAME/out results/NAME --validate bundle
```

- `shk push LOCAL REMOTE` copies into REMOTE (absolute, under `remote_root` unless `--anywhere`) and never deletes. Pushed files keep their local modification times, so inputs that are old locally can be purge-eligible on scratch soon after the push: push them shortly before the jobs that read them.
- REMOTE's parent must already exist. For a new remote root, first push a staging directory containing `inputs/` to the root itself, whose parent exists (for example `shk push staging/ /scratch/users/USER/project`). Check that push exits 0 before submitting a job that reads those inputs.
- `shk pull REMOTE LOCAL` needs `REMOTE/SHA256SUMS` and an existing parent directory of LOCAL, copies exactly those files into a private stage beside LOCAL (`.NAME.shk-stage` for LOCAL `.../NAME`, with `.NAME.shk-lock`), verifies every hash, runs the validator named by `--validate`, then renames the stage onto LOCAL and writes `.NAME.shk-receipt.json`. An existing LOCAL is kept unless `--replace` (the old copy stays as `.NAME.shk-old-<UTC>`); a rerun resumes the stage.
- The validator is the project's argv from `[tool.sherlock-kit.validators]`; it runs from the project root with the stage directory as its last argument, exit 0 passes, and it must treat the stage as read-only (what it writes is promoted).
- The job writes the manifest as its last step: `shk manifest DIR` when shk is available there, else `(cd DIR && find . -name '.shk*' -prune -o -type f ! -name SHA256SUMS -printf '%P\0' | sort -z | xargs -0r sha256sum --) > DIR/SHA256SUMS`. DIR is the job's output directory (for example `out/` under the run directory), never the run directory itself, which holds `.shk/` and the Slurm logs still being written.
- Never touch or rewrite scratch files to keep them from the 90-day purge; copy what must last to `$GROUP_HOME`, Oak or the workstation.

## Occupancy and borrowed partitions

- `shk occupancy --partition btrippe` shows who uses a partition now. Run it before every submission to a borrowed partition (`borrowed=yes` in the rules' partition profiles), read the courtesy text, then submit with `--courtesy-ack`. The submit reply repeats the occupancy and courtesy text and warns, without refusing, when others are running there and you already have 2 jobs there or ask for more than 2 h.
- Releasing a job held on a borrowed partition counts as submitting it: `shk release` checks neither occupancy nor the clock, so run `shk occupancy` first and release only within the budget and window that a submission would need.
- `btrippe`: about 2 jobs x 2 h while others are active. `possu`: submit or release only between 00:00 and 07:00 Pacific, a few short jobs. Unlisted partitions are refused; scheduler visibility is never authorization.

## Cancel

`shk cancel --task train-a` (or `--name NAME`) cancels only the live jobs of that one name and reports their ids and `next_check_after`. Routine cancellation of exact verified own jobs in an authorized task may proceed; tell the user why (`shk cancel` records no reason). Ask for unclear ownership/scope, unrelated work, broad cancellation, or material irreversible loss of important progress/results. Keep current UID/name filters and unknown-outcome protection; never cancel all user jobs.

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
