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

`shk --version` prints `sherlock-kit 0.4.0 (FORM)`, where FORM names the install:

| Form | How | FORM |
|---|---|---|
| Project environment (recommended) | `pip install 'sherlock-kit @ git+https://github.com/FridrichMethod/sherlock-kit@v0.4.0'`, or a uv source with `tag = "v0.4.0"` | `git <commit>` |
| Dotfiles submodule | dotfiles carries this repository as a submodule and links its `bin/shk` to `~/.local/bin/shk`; `bin/shk` is a stdlib-only entry point that runs the submodule's source | `submodule <path>` |
| Editable | `pip install -e <path>` into an environment you choose; it follows that tree | `editable <path>` |

A wheel or path install reports `unknown`; a plain checkout run through `bin/shk`
reports `source <path>` and is for development only. A project environment's
`shk` takes precedence over the global one while that environment is active.

```toml
[tool.uv.sources]
sherlock-kit = { git = "https://github.com/FridrichMethod/sherlock-kit", tag = "v0.4.0" }
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
shk pull /scratch/users/USER/demo/runs/NAME/out results/smoke --validate bundle
```

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
           [--after-ok JOBID ...] [--run-dir PATH] [--courtesy-ack] [--resubmit REASON] [--dry-run]
shk status (--task T [--campaign C] [--run-dir PATH] ... | --name NAME ... | --all) [--since DAYS]
shk cancel (--task T [--campaign C] | --name NAME)
shk push LOCAL REMOTE [--anywhere]
shk pull REMOTE LOCAL [--validate NAME] [--replace]
shk manifest DIR
```

- Operational commands print one JSON document on stdout; `rules` and `--version`
  print text. `shk <command> --help` is the authoritative flag list.
- `submit`: the job name is `shk.<project>.<task slug>.<hash of project, campaign,
  task>`. One read call decides `new`, `live`, `done`, `failed` or `stale`; a live
  name is always refused, a finished one unless `--resubmit REASON`. Then one
  mutation call stages the script under `<run-dir>/.shk/` and runs `sbatch`.
  Defaults: `--campaign default`, `--run-dir <remote_root>/runs/<name>`, `--array
  N%M` with 2 to 1000 tasks. `--dry-run` is offline.
- `status`: one `squeue` and one `sacct` for all selected names; `--since`
  defaults to 30 days (7 with `--all`). `cancel` cancels the jobs of one name.
- `push` rsyncs through the DTN into an absolute REMOTE under `remote_root`
  (`--anywhere` lifts that) and never deletes. `pull` copies exactly the files
  in `REMOTE/SHA256SUMS` into a private stage, verifies them, runs the optional
  validator and renames the stage onto LOCAL with a receipt beside it.
- `occupancy` lists current use of partitions; borrowed partitions need it and
  `--courtesy-ack` before a submission.
- `install-skill` copies the agent skill into a project (`.claude/skills/` and
  `.agents/skills/`) and, with `--rules`, the rules into `.claude/rules/` (Codex
  gets a hint instead); `--check` exits 1 when a copy is missing or stale.

## Configuration

`[tool.sherlock-kit]` in `pyproject.toml`, else the top level of
`sherlock-kit.toml`, found with `--project-dir DIR` (exactly that directory) or
by walking up from the working directory until a directory you do not own.
Unknown keys are errors; nothing secret belongs here.

| Key | Default | Used by |
|---|---|---|
| `project` | none; `^[a-z0-9][a-z0-9-]{0,23}$` | `submit`, `status`, `cancel` |
| `remote_root` | none; absolute, no `..` | `submit`, `push`, `pull` |
| `control_host` | `sherlock-plain` | every remote call |
| `data_host` | `sherlock-dtn` | `push`, `pull` |
| `connect_timeout`, `command_timeout`, `transfer_timeout` | 15, 60, 3600 s | ssh and rsync |
| `[tool.sherlock-kit.validators]` | none; `NAME = ["argv", ...]` | `pull --validate NAME` (gets the stage directory) |

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
| 4 | Sherlock unreachable without a new login: `auth_required` (ask the user to open the master); `unavailable` with a `~/.ssh/config` hint (fix the client config); other `unavailable` or `timeout`, hint `run shk doctor` (run it, retry once after a minute, then stop) |
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
python3 tests/run_suite.py
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
