# Contributing

Read [SHERLOCK.md](SHERLOCK.md), [AGENTS.md](AGENTS.md) and the relevant operation
guide. Keep changes small; preserve user changes and scientific boundaries.
Concurrent writers use separate Git worktrees/branches. Reviewers are read-only.
Commits do not include agent attribution.

## Local checks

Ordinary tests require no credentials, live Sherlock allocation or GPU. They use
real local processes, SQLite, filesystem durability and rsync, with explicitly
synthetic scheduler responses. Install system `rsync`, then create an isolated
packaging environment:

```sh
python3 -m venv .venv-build
.venv-build/bin/python -m pip install 'setuptools>=77' wheel
SHERLOCK_KIT_BUILD_PYTHON="$PWD/.venv-build/bin/python" \
  python3 -m unittest discover -s tests -v
git diff --check
```

Set `SHERLOCK_KIT_BUILD_PYTHON` to include archive/wheel/isolated-install checks.
Without it the packaging test is skipped, so that run alone is not a release
check. CI runs the complete suite on supported minimum/current Python versions.
Python development imports may use `PYTHONPATH=src`; production is frozen.

The packaging test builds committed `HEAD`, not uncommitted changes. After a
commit or integration, rerun the complete command on the resulting clean revision.
A direct wheel build refuses dirty tracked files. Build/install work happens in
isolated environments, never during shell startup.

## Behavioral boundaries

Add meaningful tests for changed admission, uncertainty, cost, identity, query
cadence, validators or transfer durability. Preserve the distinction between
scheduler completion, execution receipts, artifacts and scientific validation.
Do not claim exactly-once execution or distributed locking. Document unsupported
capabilities explicitly rather than introducing stub commands.

Never commit runtime databases, private configurations/grants, credentials,
research source/data or client rollouts. Public examples use synthetic paths.
Never use real historical campaigns for failure injection. New live work requires
an explicit workload, namespace, budget and authorization.

Report sensitive issues privately to the repository owner through a trusted
channel; do not put secrets or private reproductions in public issues.
Release preparation is described in [maintenance](docs/maintenance.md).
