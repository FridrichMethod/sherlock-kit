# sherlock-kit

[![CI](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml)

`shk` is a small command-line interface for agents (and people) that submit and
track Slurm jobs, move data and read partition occupancy on Stanford's Sherlock
cluster, from a workstation over OpenSSH or on Sherlock itself. It is one
standard-library Python package (3.11+) plus fixed POSIX `sh` programs that run
on the login node. Slurm is the only source of truth: a job's name is derived
from its logical task, and `shk` keeps no local state, cache or ledger.

`shk` does not authorize resources, infer borrowed access, police what a job
script does or certify scientific results. The operating rules for agents ship
with the package (`shk rules workstation`, `shk rules cluster`); Sherlock's own
`/etc/agents/AGENTS.md` takes precedence over them.

## Install

This development tree implements the 0.5.0 CLI contract; it does not publish a
release tag. The install examples below retain the previous published tag;
0.5.0 integrations must install a verified immutable commit until a release is
published. Older installs should follow their own `shk <command> --help`.

`shk --version` prints `sherlock-kit 0.5.0 (FORM)`, where FORM names the install:

| Form | How | FORM |
|---|---|---|
| Project environment (recommended) | `pip install 'sherlock-kit @ git+https://github.com/FridrichMethod/sherlock-kit@v0.4.1'`, or a uv source with `tag = "v0.4.1"` | `git <commit>` |
| Dotfiles submodule | dotfiles carries this repository as a submodule and links its `bin/shk` to `~/.local/bin/shk`; `bin/shk` is a stdlib-only entry point that runs the submodule's source | `submodule <path>` |
| Editable | `pip install -e <path>` into an environment you choose; it follows that tree | `editable <path>` |

A wheel or path install reports `unknown`; a plain checkout run through `bin/shk`
reports `source <path>` and is for development only. A project environment's
`shk` takes precedence over the global one while that environment is active.

```toml
[tool.uv.sources]
sherlock-kit = { git = "https://github.com/FridrichMethod/sherlock-kit", tag = "v0.4.1" }
```

On Sherlock, run `shk` with Python 3.11 or newer from a module or a `$GROUP_HOME`
environment; heavier installs belong in an `sh_dev` session.

## Quick start

`shk` never logs in. Open the control and data masters yourself first
(`ssh sherlock-plain`, `ssh -fN sherlock-dtn`); `~/.ssh/config` needs
`ControlMaster auto`, `ControlPersist yes` and `ControlPath ~/.ssh/%C` for both
aliases. Then add the project configuration and work from the project directory:

```toml
[tool.sherlock-kit]
project = "demo"
remote_root = "/scratch/users/USER/demo"

[tool.sherlock-kit.validators]
bundle = ["python", "-m", "demo.validate_bundle"]
```

```sh
shk doctor                  # offline: Python, ssh, rsync, config, both ssh masters
shk submit --task smoke --script job.sh --partition normal --cpus 1 --mem 2G --time 20m --dry-run
shk submit --task smoke --script job.sh --partition normal --cpus 1 --mem 2G --time 20m
shk status --task smoke     # at least 60 s apart; the reply says when (next_check_after)
mkdir -p results            # pull creates LOCAL, never its parent
shk pull /scratch/users/USER/demo/runs/NAME/out results/smoke --validate bundle
```

Codex may need per-command approval to reach the existing SSH control socket,
even for offline `doctor`. A sandbox startup failure or a local socket permission
denial does not mean the user needs a new login. Approve the bounded command;
do not disable the sandbox or grant write access to all of HOME.
`submit --dry-run` needs no SSH socket or network access.

The job writes `out/SHA256SUMS` as its last step (`shk manifest out`, or the
`sha256sum` one-liner in the skill), over its output directory, never the run
directory itself; `shk pull` verifies every file against it before the copy
appears under `results/`.

## Commands

```text
shk [--project-dir DIR] [--executor auto|ssh|local] <command>
shk --version
shk doctor [--remote]
shk rules {workstation,cluster}
shk install-skill --project DIR [--client claude|codex|all] [--rules workstation|cluster] [--check] [--force]
shk occupancy --partition P [--partition P ...]
shk submit --task T [--campaign C] --script FILE --partition P --cpus N --mem SIZE --time DUR
           [--gpus N] [--tasks N] [--array N[%M]] [--constraint X] [--signal S] [--requeue|--no-requeue]
           [--after-ok JOBID ...] [--run-dir PATH] [--courtesy-ack] [--resubmit REASON] [--submission-token TOKEN] [--hold] [--dry-run|--preflight]
shk resources --partition P [--partition P ...]
shk --executor local allocation --pid PID --job-id N_OR_N_TASK [--step-id NUMERIC_STEP] [--hostlist EXPRESSION ...]
shk release --job-id ID --expect-name NAME --expect-token TOKEN
shk requeue --self --expect-name NAME [--expect-token TOKEN]
shk status (--task T [--campaign C] [--run-dir PATH] ... | --name NAME ... | --all | --job-id ID ...) [--since DAYS] [--details]
shk cancel (--task T [--campaign C] | --name NAME)
shk push LOCAL REMOTE [--anywhere]
shk pull REMOTE LOCAL [--validate NAME] [--replace]
shk manifest DIR
```

- Operational results print one JSON document with `schema_version: 1` on stdout; `rules`, `--version`
  and help print text, and usage errors print text on stderr (exit 2).
  `shk <command> --help` is the authoritative flag list. `submit`,
  `status`, `cancel` and `occupancy` report `next_check_after`, 60 s after they
  ran.
- `submit`: the job name is `shk.<project>.<task slug>.<hash>` of project,
  campaign and task. One read call (`squeue` and the last 30 days of `sacct`)
  decides `new`, `live`, `done`, `failed` or `stale` (spec files in the run
  directory but no Slurm rows, submitted like `new`); a live name is always
  refused, a finished one unless `--resubmit REASON`. Then one mutation call
  stages the script under `<run-dir>/.shk/` and runs `sbatch`; a lost reply is
  exit 5, and the reply's `next` names the `shk status` command and when to run
  it. Defaults: `--campaign default`, `--run-dir <remote_root>/runs/<name>`,
  `--array N[%M]` with 2 to 1000 tasks. `--after-ok` is repeatable and takes
  several job ids (`--after-ok 111 222`), each a single `N` or `N_TASK`: `shk`
  writes the `--dependency` string itself, so a `,` or `?` list is refused.
  `--dry-run` is offline. `--preflight` is mutually exclusive and sends the
  same script/resources to `sbatch --test-only` as a read: no job, run directory
  or provenance is created. Its `preflight` object retains returncode, stdout
  and stderr; rejection is exit 3, read failure is never unknown.
  `--submission-token` accepts 1-128 public characters `[A-Za-z0-9_.:-]` (first
  alphanumeric), freezes it in the spec and sets Slurm Comment. `--hold` freezes
  a user hold. Tokens correlate attempts and contain no secrets.
- The script's leading `#SBATCH` lines may set anything except what `shk` owns
  (`-A`/`--account`, `-x`/`--exclude`, `-G`/`--gpus*`/`--gres`,
  `-p`/`--partition`, `-a`/`--array`, `-d`/`--dependency`, `-b`/`--begin`,
  `-J`/`--job-name`, `-H`/`--hold`, `--comment`) and a `hetjob` or `packjob` separator: heterogeneous jobs
  are refused. Matching follows sbatch's own parsing, so abbreviated long
  options (`--part=gpu`) and short flags bundled after value-less ones
  (`-Hp gpu`) count too.
- `status`: one `squeue` and one `sacct` for all selected names; `--since`
  defaults to 30 days (7 with `--all`). Give `--campaign` once for every
  `--task` or once per `--task`, and `--run-dir` once per `--task` or not at
  all. Repeatable `--job-id N` or `N_TASK` is mutually exclusive with task,
  name and all selectors; it includes legacy names and only current UID rows.
  `--details` returns the evidence contract described below. `cancel` cancels
  the live jobs of one name.
- `resources` reads node state, partitions, active features and configured/
  allocated CPU, memory and GPU capacity once. `free_memory_mb` uses CfgTRES
  minus AllocTRES; OS FreeMem never substitutes. Missing facts are null and
  `complete: false`. Availability is a snapshot, not a resource reservation.
- `allocation` verifies an explicit local worker PID, own UID, physical job/array
  identity, current host and optional numeric step membership. It preserves actual
  resources, topology, raw records and hashes; missing counts remain null and no
  project resource policy is inferred. It refuses SSH, exited/reused PIDs and
  ambiguous or contradictory evidence. See [the allocation contract](docs/allocation.md).
- `release` checks one exact ID, current UID, expected name/token and user hold
  immediately before `scontrol release`. `requeue --self` has no arbitrary ID
  option: it requires the local allocation (`SLURM_JOB_ID`), actual Slurm PID
  membership in that physical job, current UID and name, the token only when
  `--expect-token` is given, RUNNING state, a partition whose profile allows
  requeue (today `owners`) and Requeue=1. An array element is verified and
  requeued as `ArrayJobId_ArrayTaskId`, never the bare array ID, which would
  requeue every element. A lost mutation reply is exit 5; Slurm ownership checks
  still apply, and concurrent user changes can race the identity read.
  Checkpoint durability and retry limits belong to the caller.
- `push` rsyncs LOCAL/ through the DTN into an absolute REMOTE under
  `remote_root` (`--anywhere` lifts that) and never deletes. Pushed files keep
  their local modification times (`rsync -t`, so a re-push skips unchanged
  files instead of rewriting them); inputs that are old locally can therefore be
  purge-eligible on scratch soon after a push.
  REMOTE's parent must already exist. For a new remote root, push a staging
  directory containing `inputs/` to the root itself, for example
  `shk push staging/ /scratch/users/USER/demo` when `/scratch/users/USER` exists.
  Check for exit 0 before submitting a job that needs those inputs.
- `pull` copies exactly the files in `REMOTE/SHA256SUMS` into a private stage
  beside LOCAL, verifies them, runs the optional validator and renames the stage
  onto LOCAL. With NAME the last component of LOCAL, the stage is
  `.NAME.shk-stage` (a rerun resumes it), `.NAME.shk-lock` serializes concurrent
  pulls, the receipt is `.NAME.shk-receipt.json`, and `--replace` keeps the
  previous copy as `.NAME.shk-old-<UTC>`; projects can gitignore `.*.shk-*`. The
  validator runs from the project root (the directory of the configuration file)
  with the stage directory as its last argument, passes with exit 0 and must
  treat the stage as read-only: whatever it writes is promoted with the data.
- `occupancy` lists current use of partitions. Borrowed partitions (`btrippe`,
  `possu`) need it and `--courtesy-ack` before a submission, and such a
  submission reports their occupancy and courtesy text, with a warning when
  others are running there and you already have 2 jobs there or ask for more
  than 2 h.
- `install-skill` copies the agent skill into a project (`.claude/skills/` and
  `.agents/skills/`) and, with `--rules`, the rules into `.claude/rules/` (Codex
  gets a hint instead); `--check` exits 1 when a copy is missing or stale.

## Detailed status contract

`status --details` adds `details = {queue, accounting, jobs, complete, queried_at, history_window_days, missing_job_ids, missing_control_job_ids, selection, allocation_evidence_complete, history_complete, control_identities_complete, malformed}`. Queue and
accounting are lists of full records, before summary deduplication; every
record includes the original `raw` line. Their normalized fields are
`job_id`, `job_id_raw`, `cluster`, `db_index`, `state`, `partition`,
`elapsed_seconds`, `allocated_cpus`, `allocated_tres`, `submitted_at`,
`started_at`, `ended_at`, `restarts`, `exit_code`, `nodes`, `name`, `comment`
and `uid`. Missing fields are null, numeric facts are integers, timestamps
are UTC strings, IDs (including DBIndex) remain strings. Pending queue CPUs,
start and end are null because squeue supplies requests or predictions there.

`jobs` holds queued physical `scontrol` identities as `{job_id, name, comment, uid, fields, raw}`; `fields` retains original Slurm names and string values,
including request TRES, Features, TimeLimit, Requeue and array identity. All
records are restricted to the current UID. Partial tools, missing requested
IDs, malformed rows or missing accounting cost identity mark evidence
incomplete, retain available facts and prevent a final cost claim. AllocTRES
may be absent for an explicitly pending, unstarted allocation with zero CPUs
and elapsed time; that is structurally complete and still has nonfinal cost.
Missing allocation TRES after a job started and restart-history gaps are
incomplete. Queued physical IDs must all have verified control identities.
An exact array-element selection reports `selection.partial_array_ids`, entry
`scope: partial_array`, unknown whole-array state/count/missing values, and
nonfinal cost; sibling states cannot be inferred from a selected element.
A complete query does not establish unlimited historical coverage: it is bounded by
`history_window_days`, and array cardinality without provenance is inferred.

A bounded deterministic workflow manager may run inside its allocated Slurm
job and call `shk --executor local` to submit/poll authorized later compute
stages at intervals of at least 60 seconds, with no workstation dependency.
This does not authorize scheduling future agent or LLM sessions. Controllers
own their bounded retries, checkpointing, scientific validation and accounting.

## Configuration

`[tool.sherlock-kit]` in `pyproject.toml`, else the top level of
`sherlock-kit.toml`, found with `--project-dir DIR` (exactly that directory) or
in the nearest directory at or above the working directory that has one.
Unknown keys are errors; nothing secret belongs here.

| Key | Default | Used by |
|---|---|---|
| `project` | none; `^[a-z0-9][a-z0-9-]{0,23}$` | `submit`, `status --task`, `cancel --task`; `status --all` lists only this project's names |
| `remote_root` | none; absolute, no `..` | `submit` (default run directories, also for `status`), `push` (unless `--anywhere`) |
| `control_host` | `sherlock-plain` | every remote call |
| `data_host` | `sherlock-dtn` | `push`, `pull` |
| `connect_timeout`, `command_timeout`, `transfer_timeout` | 15, 60, 3600 s | ssh and rsync |
| `[tool.sherlock-kit.validators]` | none; `NAME = ["argv", ...]` | `pull --validate NAME` runs argv plus the stage directory, only from a configuration file you own |

`doctor`, `rules` and `occupancy` work without a configuration. On Sherlock
(`sh<N>-ln<N>` login nodes, `SLURM_CLUSTER_NAME=sherlock`, a compute node inside a
job, or `SHK_EXECUTOR=local`) the same programs run locally and `push` and `pull`
are refused.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | local error: configuration, spec, verification, validator, malformed reply |
| 2 | usage error |
| 3 | refused by policy or Slurm: live duplicate, finished task without `--resubmit`, missing `--courtesy-ack`, an `#SBATCH` directive `shk` owns, Slurm rejected the job, push or pull on Sherlock |
| 4 | SSH transport or master check unavailable: `auth_required` (ask the user to open the master); `unavailable` with a local permission denial (check the sandbox; request per-command approval in Codex); `unavailable` with a `~/.ssh/config` hint (fix the client config); other `unavailable` or `timeout`, hint `run shk doctor` (run it, retry once after a minute, then stop) |
| 5 | outcome unknown after a dispatched mutation: run the `shk status` command in the reply's `next`, not before the time it names (at least 60 s) |
| 6 | status ok, but the newest job of a requested name has an anomaly |

Errors print `{"ok": false, "error": TOKEN, "message": ..., "hint": ...}`.

## Agents

- Rules: `src/sherlock_kit/data/rules/workstation.md` (agents on a workstation)
  and `cluster.md` (agents on Sherlock), at most 7 KiB each, each ending with the
  partition profiles generated from `data/partitions.json`.
- Skill: `src/sherlock_kit/data/skills/sherlock-kit/SKILL.md`, for Claude Code and
  Codex alike.
- Global delivery: dotfiles links both into `~/.claude/rules/`, `~/.claude/skills/`
  and `~/.agents/skills/`. Per project: `shk install-skill --project .`.

## Development

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pre-commit install
.venv/bin/pre-commit run --all-files    # ruff, mypy, shellcheck, shfmt, mdformat
python3 -B tests/run_suite.py
```

The suite is offline: fake `ssh` and Slurm binaries, real local `rsync`, `sh` and
`dash`. A skipped test fails the run when `CI` or `SHK_REQUIRE_ALL` is set.
`SHK_BUILD_PYTHON` (an interpreter with `setuptools>=80` and `wheel`) enables the
sdist, wheel and editable-install checks, which build committed `HEAD`. See
[CONTRIBUTING.md](CONTRIBUTING.md), [docs/design.md](docs/design.md),
[docs/sherlock-policy.md](docs/sherlock-policy.md),
[observed Slurm behavior](docs/validation/observed-slurm.md) and
[CHANGELOG.md](CHANGELOG.md).

Licensed under [MIT](LICENSE).
