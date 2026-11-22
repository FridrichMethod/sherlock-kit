# Installation and connection

`shk` is installed per project, from a pinned release, and reaches Sherlock
through OpenSSH control masters that the user opens. This guide covers the
install forms, environments on Sherlock and inside jobs, and the SSH setup. The
[command reference](cli.md) describes what each command does once it runs.

## Install forms

Install from a release tag, as below; `main` can be ahead of the latest release.
Older installs should follow their own `shk <command> --help`.

`shk --version` prints `sherlock-kit 0.9.0 (FORM)`, where FORM names the install:

| Form | How | FORM |
|---|---|---|
| Project environment (recommended) | `pip install 'sherlock-kit @ git+https://github.com/FridrichMethod/sherlock-kit@v0.9.0'`, or a uv source with `tag = "v0.9.0"` | `git <commit>` |
| Submodule | a consumer checks out this repository as a Git submodule and invokes its stdlib-only `bin/shk` entry point | `submodule <path>` |
| Editable | `pip install -e <path>` into an environment you choose; it follows that tree | `editable <path>` |

A wheel or path install reports `unknown`; a plain checkout run through `bin/shk`
reports `source <path>` and is for development only. Invoke the project
environment's executable directly, or activate that environment before using `shk`.
Installation and updates belong to the project; no global launcher is required.

```toml
[tool.uv.sources]
sherlock-kit = { git = "https://github.com/FridrichMethod/sherlock-kit", tag = "v0.9.0" }
```

## Environments on Sherlock

On Sherlock, create the project-pinned environment under `$GROUP_HOME` (a
small personal one may live in `$HOME`), never under scratch, with Python 3.11
or newer. A small pure-Python environment such as the one that holds `shk` may
be created on a login node: that is job preparation under site rule 1.
Compiling and heavy installs run inside a Slurm allocation, for example an
`sh_dev` session; fetch sources or build artifacts beforehand if compute nodes
cannot reach their source. For example, using a release wheel fetched
beforehand:

```sh
python3 -m venv "$GROUP_HOME/demo/.venv-shk"
"$GROUP_HOME/demo/.venv-shk/bin/python" -m pip install --no-index --no-deps /path/to/sherlock_kit-0.9.0-py3-none-any.whl
"$GROUP_HOME/demo/.venv-shk/bin/shk" --version
```

## Inside jobs and controllers

Use the environment's absolute executable path in batch scripts and controllers;
do not assume the intended environment is active in every job. The project records
the pinned version and environment path. For example, inside the appropriate job:

```sh
"$GROUP_HOME/demo/.venv-shk/bin/shk" manifest /path/to/run/out
"$GROUP_HOME/demo/.venv-shk/bin/shk" --executor local --project-dir /path/to/project status --all
"$GROUP_HOME/demo/.venv-shk/bin/shk" --executor local allocation --pid "$WORKER_PID" --job-id "$SLURM_JOB_ID"
"$GROUP_HOME/demo/.venv-shk/bin/shk" --executor local requeue --self --expect-name "$SHK_NAME"
```

A local controller uses that same absolute path for its authorized `submit` and
status calls. Allocation evidence requires the actual worker PID; self-requeue
requires the job identity, partition and checkpoint/retry conditions documented
in [release and requeue](cli.md#release-and-requeue). These examples do not
allocate resources or authorize a workload; see
[controllers inside a job](cli.md#controllers-inside-a-job).

## SSH control masters

`shk` never logs in: it rides OpenSSH control masters that you open yourself,
one on a Sherlock login node and one on a data transfer node (DTN). A minimal
`~/.ssh/config` for both, with your SUNet ID in place of `SUNETID`:

```sshconfig
Host sherlock-plain
  HostName login.sherlock.stanford.edu
  User SUNETID
  ControlMaster auto
  ControlPersist yes
  ControlPath ~/.ssh/%C

Host sherlock-dtn
  HostName dtn.sherlock.stanford.edu
  User SUNETID
  ControlMaster auto
  ControlPersist yes
  ControlPath ~/.ssh/%C
```

These two names are only the defaults of `control_host` and `data_host`; any
other OpenSSH aliases work when set in `[tool.sherlock-kit]`, as long as the two
differ and each sets `ControlMaster`, `ControlPersist` and `ControlPath` as
above. Open both masters yourself first (`ssh sherlock-plain`,
`ssh -fN sherlock-dtn`), then run `shk doctor` from the project directory: it
checks Python, ssh, rsync, the configuration and both masters without changing
anything.

On Sherlock itself `shk` needs no master: the same programs run locally, as
[Configuration](cli.md#configuration) describes.

## Agents in a sandbox

Codex may need per-command approval to reach the existing SSH control socket,
even for offline `doctor`. A sandbox startup failure or a local socket permission
denial does not mean the user needs a new login. Approve the bounded command;
do not disable the sandbox or grant write access to all of HOME.
`submit --dry-run` needs no SSH socket or network access.
