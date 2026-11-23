# Installation and connection

`shk` is installed per project, from a pinned release, and reaches Sherlock
through OpenSSH control masters that the user opens. This guide covers a
project's pin and configuration, the install forms, environments on Sherlock
and inside jobs, and the SSH setup; [Marlowe](#marlowe) covers the second
packaged site. The [command reference](cli.md) describes what each command does
once it runs.

## Pin and configuration

A project that uses `shk` tracks two separate things: the pin decides which
`shk` runs, and the configuration (`[tool.sherlock-kit]`, see
[Configuration](cli.md#configuration)) tells that `shk` which project it
serves. Neither replaces the other, and neither holds secrets.

- **Pin** a release tag as a dependency of the project itself:

  ```toml
  [project]
  dependencies = [
      "sherlock-kit @ git+https://github.com/FridrichMethod/sherlock-kit@v0.11.0",
  ]
  ```

  pip and uv both read this line, and a lock file such as `uv.lock` records the
  tag's commit. A uv project may instead list plain `sherlock-kit` with the
  `[tool.uv.sources]` entry under [Install forms](#install-forms). Never pin
  `main` or another branch. `shk` uses only the standard library, so it rarely
  conflicts with the project's other dependencies.

- **A separate environment** is for a project environment that cannot take
  `shk`: Python older than 3.11, or a Conda or GPU stack that should not
  change. Give `shk` its own small environment, track the same requirement
  line in a file such as `requirements-shk.txt`, and call that environment's
  `shk` by absolute path. On Sherlock it lives under `$GROUP_HOME`
  ([Environments on Sherlock](#environments-on-sherlock)).

- **Configure** in the project's `pyproject.toml` when it has one, else in a
  `sherlock-kit.toml` at the repository root; in one directory the
  `pyproject.toml` table wins. A Marlowe configuration lives in its own
  directory ([A project on both clusters](#a-project-on-both-clusters)).

- **Agent copies**: run the pinned environment's `shk install-skill --project .`,
  with the `--rules` option the project uses; adding `--check` compares the
  copies without writing and exits 1 when one is missing or stale.

- **Upgrade** by reading the [changelog](../CHANGELOG.md) entries between the
  two versions, changing the tag in the pin, relocking (`uv lock`) or
  reinstalling, checking `shk --version`, and rerunning `install-skill` with
  the same options so that the copies match the new version.

## Install forms

Install from a release tag, as below; `main` can be ahead of the latest release.
Older installs should follow their own `shk <command> --help`.

`shk --version` prints `sherlock-kit 0.11.0 (FORM)`, where FORM names the install:

| Form | How | FORM |
|---|---|---|
| Project environment (recommended) | `pip install 'sherlock-kit @ git+https://github.com/FridrichMethod/sherlock-kit@v0.11.0'`, or a uv source with `tag = "v0.11.0"` | `git <commit>` |
| Submodule | a consumer checks out this repository as a Git submodule and invokes its stdlib-only `bin/shk` entry point | `submodule <path>` |
| Editable | `pip install -e <path>` into an environment you choose; it follows that tree | `editable <path>` |

A wheel or path install reports `unknown`; a plain checkout run through `bin/shk`
reports `source <path>` and is for development only. Invoke the project
environment's executable directly, or activate that environment before using `shk`.
Installation and updates belong to the project; no global launcher is required.

```toml
[tool.uv.sources]
sherlock-kit = { git = "https://github.com/FridrichMethod/sherlock-kit", tag = "v0.11.0" }
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
"$GROUP_HOME/demo/.venv-shk/bin/python" -m pip install --no-index --no-deps /path/to/sherlock_kit-0.11.0-py3-none-any.whl
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
[controllers inside a job](cli.md#controllers-inside-a-job). Inside a
Marlowe job the commands carry `--site marlowe`: see
[inside a Marlowe job](#inside-a-marlowe-job).

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

## Marlowe

`shk` drives Stanford's Marlowe cluster with `--site marlowe`; the
[command reference](cli.md#sites) lists what the site changes. It needs a
release whose `shk --help` lists `--site`.

### The `marlowe-plain` master

Marlowe uses one alias for both hosts: commands and rsync both go through a
login node, since Marlowe has no transfer node for rsync. A stanza in the style
of the Sherlock ones, with your SUNet ID in place of `SUNETID`:

```sshconfig
Host marlowe-plain
  HostName login-01.marlowe.stanford.edu
  User SUNETID
  ControlMaster auto
  ControlPersist yes
  ControlPath ~/.ssh/%C
```

Open it yourself (`ssh marlowe-plain`, with your password and Duo), then run
`shk --site marlowe doctor`: one `ssh -O check` serves both hosts. Another
alias works when the Marlowe configuration sets it as `control_host` and
`data_host`, as long as it is not one of Sherlock's defaults.

Marlowe's Connecting page recommends authenticating once and reusing that
connection. Its advice: name one login node (`login-01`, `login-02` or
`login-03` under `marlowe.stanford.edu`) rather than the round-robin
`login.marlowe.stanford.edu`, because a reused connection stays on the node
that the first one reached, and keep one name per node, since two names for
one node make two connections and two logins; its example sets
`ControlPersist 10m`, `ServerAliveInterval 60` and `ServerAliveCountMax 3`;
on a shared or unattended machine, close the master with
`ssh -O exit marlowe-plain` when done, or lower `ControlPersist`. Repeated
failed logins can block the client address on the login nodes. `shk` works
with any `ControlPersist`: it needs a running master and a usable
`ControlPath`, and never opens a master itself.

### A project on both clusters

A project that uses both clusters keeps its Sherlock table where it is and
adds a Marlowe configuration in its own directory:

```toml
# <repo>/marlowe/sherlock-kit.toml
site = "marlowe"
project = "demo-mw"                        # distinct from the Sherlock value
remote_root = "/scratch/m000000/USER/demo"
account = "marlowe-m000000"
```

`m000000` stands for the project id and USER for the user name. `account` is
the base project account, `marlowe-<project id>`;
`sacctmgr show assoc user=$USER format=Account%30,QOS` on Marlowe lists your
accounts.
The distinct `project` gives the two clusters distinct job names.

From `<repo>/marlowe` or below, `shk --site marlowe ...` finds that
configuration. From the repository root, where the Sherlock table is the
nearest, add `--project-dir marlowe`; without it the command is exit 1
`site_mismatch`. A pinned release without site support exits 1
`config_invalid` on every command that reads the Marlowe file. The Marlowe
skill and rules install beside the Sherlock ones:

```sh
shk --site marlowe install-skill --project . --rules workstation
```

### Remote root on Marlowe

`push` creates REMOTE but never its parent, so create `remote_root` once
before the first push; a missing parent fails as `transfer_failed` (rsync
exit 11), and Sherlock has the same rule. Either push a directory that holds `inputs/` to
`remote_root` itself, whose parent `/scratch/PROJECT/USER` exists:

```sh
shk --site marlowe push staging/ /scratch/m000000/USER/demo
```

or create it over the open master:
`ssh -oBatchMode=yes -oProxyCommand=false marlowe-plain mkdir -p /scratch/m000000/USER/demo`.
Marlowe's scratch is shared by the project, not backed up and "may be
purged"; keep what must last in `/projects/PROJECT` or on the workstation.

### `shk` on a Marlowe login node

People and agent sessions may run `shk` on a Marlowe login node. The system
`python3` there is 3.10, older than `shk` needs: create an environment with
Python 3.11 or newer through the `uv` module (`module load uv`) or a
micromamba or conda environment, and install the project's pinned release
into it as in [install forms](#install-forms). Environments may be built on a
Marlowe login node, an owner choice that the Marlowe cluster rules carry; put
large ones under `/projects/PROJECT`, not in the small home directory.

With a Marlowe configuration in reach, or with `--site marlowe`, `shk` picks
the local executor by itself there, because Marlowe's Slurm configuration file
exists on the machine: the programs run locally through the same prelude, and
`doctor` reports `"executor": "local"` with no masters. `push` and `pull` are
refused there (exit 3 `transfer_on_marlowe`): start transfers from the
workstation, or copy with `cp` or `rsync` on Marlowe without `shk`. The rules
for an agent session there are `shk --site marlowe rules cluster`. A job
calls `shk` as [inside a Marlowe job](#inside-a-marlowe-job) shows.

### Inside a Marlowe job

A Marlowe job calls `shk` by the absolute path of an environment with Python
3.11 or newer, since the system `python3` of a compute node is 3.10
([observed](validation/observed-marlowe.md#inside-a-batch-job)). Keep that
environment under `/projects/PROJECT`, which is backed up, rather than on
scratch, which may be purged. Every command that reaches Slurm carries
`--site marlowe --executor local`: the job's run directory holds no
configuration, and no environment variable selects a site. With
`--site marlowe` the executor is local by itself on a compute node; the
explicit `--executor local` makes a command on the wrong machine fail instead
of dialing. For example, with the project's pinned environment in
`/projects/m000000/USER/demo/.venv-shk`:

```sh
/projects/m000000/USER/demo/.venv-shk/bin/shk --site marlowe --executor local allocation --pid "$WORKER_PID" --job-id "$SLURM_JOB_ID"
/projects/m000000/USER/demo/.venv-shk/bin/shk --site marlowe --executor local status --job-id "$SLURM_JOB_ID"
/projects/m000000/USER/demo/.venv-shk/bin/shk --site marlowe --executor local requeue --self --expect-name "$SHK_NAME"
/projects/m000000/USER/demo/.venv-shk/bin/shk --site marlowe --executor local --project-dir /projects/m000000/USER/demo/marlowe submit --task stage-2 --script /projects/m000000/USER/demo/stage-2.sh --partition preempt --cpus 8 --mem 64G --gpus 1 --time 2h --after-ok "$SLURM_JOB_ID"
/projects/m000000/USER/demo/.venv-shk/bin/shk manifest out
```

- `requeue --self`, `allocation`, `status --job-id`, `status --name` and
  `manifest` need no configuration; `manifest` reads no site and never uses
  the network.
- `submit`, `status --task` and `cancel --task` need `--project-dir DIR`,
  where DIR holds a Marlowe configuration that exists on Marlowe, such as the
  `marlowe` directory of a checkout of the project there. From a compute
  node Slurm's test (`sbatch --test-only`, through `submit --preflight`) is
  [observed](validation/observed-marlowe.md#shk-inside-a-batch-job), a
  submit that creates a job is not; a refusal that Slurm's test before submit
  reports is exit 3 with nothing staged. A restarted attempt (preemption or
  self-requeue) runs an in-job `submit` again, and `shk` refuses that
  duplicate with exit 3 and `error` `live` (or `done` or `failed` once the
  stage ended): treat that refusal as already submitted, and any other exit 3
  as a failure.
- A configuration of another site at or above the run directory, such as the
  Sherlock table of a two-cluster repository whose checkout holds the run
  directories, makes `--site marlowe` exit 1 `site_mismatch`; `--project-dir`
  is the way out there too.
- `requeue --self` needs `preempt`, the one Marlowe partition whose profile
  allows requeue, and the job name that `submit` exports as `SHK_NAME`.
- Without `--site marlowe` (and with no Marlowe configuration in reach)
  nothing reaches a cluster: with `--executor local`, as in the examples, and
  for `requeue --self` and `allocation` in any case, a command is exit 1
  `site_mismatch`, whose hint names `--site marlowe`; without either flag,
  `status --job-id` and `status --name` are exit 4 naming `sherlock-plain`,
  which inside a Marlowe job means that `--site marlowe` is missing, not that
  a login is needed.

In one CPU-only Marlowe job, `allocation`, `status --job-id`,
`requeue --self`, `manifest` and `submit --preflight` with `--project-dir`
ran ([observed](validation/observed-marlowe.md#shk-inside-a-batch-job)); a
`submit` that creates a job from inside one, array elements and GPU jobs are
tested offline against Marlowe-shaped records only. What a job may do with
`shk` (steps of its own work, within the authorized bound, never a wait or a
status loop) is in
[controllers inside a job](cli.md#controllers-inside-a-job); the Marlowe
skill's section "Inside a job" holds a job-script template for the walltime.
The examples above authorize no workload; their `submit` and `requeue --self`
lines start billed work.

## Agents in a sandbox

Codex may need per-command approval to reach the existing SSH control socket,
even for offline `doctor`. A sandbox startup failure or a local socket permission
denial does not mean the user needs a new login. Approve the bounded command;
do not disable the sandbox or grant write access to all of HOME.
`submit --dry-run` needs no SSH socket or network access.
