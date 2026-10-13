# Contributing

Read [AGENTS.md](AGENTS.md), [README.md](README.md) and [docs/design.md](docs/design.md)
first. Keep changes small and preserve other people's work. Commits use
conventional messages (`feat:`, `fix:`, `docs:`, `test:`, ...), stage files by
explicit path and carry no agent attribution.

## Checkouts and worktrees

The owner's development checkout lives at `~/data/repos/sherlock-kit`. It is a
working copy only: apart from its own development environment, never install
from it, link to it or point a project at it; installs come from a tag, the
dotfiles submodule or an editable install of a tree you chose (see the README).
Concurrent writers each use their own branch and Git worktree (for example
`.worktrees/<name>`, ignored by Git); reviewers are read-only. Only the lead
merges to `main` and tags releases.

## Checks

Write documentation in English using printable ASCII, tabs and newlines only,
with no exceptions; [AGENTS.md](AGENTS.md) defines the covered filenames. The
English documentation hook rejects non-ASCII characters and other control
characters. The language requirement is also checked during review.

Ordinary tests need no credentials, no Sherlock connection and no GPU. They run
the real remote `sh` programs locally through a fake `ssh`, fake Slurm binaries
and real `rsync`. Install `rsync` and `dash`, then set up a development
environment once (`.venv*/` is ignored by Git):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pre-commit install
python3 -m venv .venv-build
.venv-build/bin/python -m pip install 'setuptools>=80' wheel
```

Before each commit (the installed hook runs the same checks on staged files):

```sh
.venv/bin/pre-commit run --all-files
SHK_BUILD_PYTHON="$PWD/.venv-build/bin/python" SHK_REQUIRE_ALL=1 python3 -B tests/run_suite.py
git diff --check
```

- pre-commit runs ruff (lint and format), mypy on `src/` and `bin/shk`
  (fully annotated, `[tool.mypy]` in `pyproject.toml`), shellcheck `--shell=sh`
  and shfmt on the remote programs, mdformat on the documentation (never on
  package data: the rules and the skill are delivered byte for byte) and the
  usual file checks, plus the English documentation hook. Tests are linted,
  not type-checked. The `ruff` and `mypy`
  pins in the `dev` extra equal the ruff-pre-commit and mirrors-mypy `rev`s;
  bump each pin together with its hook.
- Tests stay on `unittest`; `tests/run_suite.py` runs them all.
- Run the suite on the oldest and newest supported Pythons (3.11 and 3.14; CI runs
  both). With `CI` or `SHK_REQUIRE_ALL` set, any skip fails the run; without
  `SHK_BUILD_PYTHON` the packaging tests skip, so that run is not a release check.
- The packaging tests build committed `HEAD` (`git archive`), not the working tree:
  commit first, then rerun on the clean revision.
- Use `python3 -B` (or `PYTHONDONTWRITEBYTECODE=1`) so no `__pycache__` lands in
  a tree that rsync or the dotfiles submodule would see. `python3 -I` ignores
  `PYTHONPATH`; use `PYTHONPATH=src python3 -B -m sherlock_kit` for repository code.

## Boundaries

- Python is standard library only and runs on 3.11 to 3.14 (no newer syntax or
  APIs). Keep it lean and readable: small functions, external input validated
  once at the boundary, and no defensive code against situations that cannot
  realistically happen. Lean is not a line count; do not compress code to save
  lines.
- Remote programs under `src/sherlock_kit/data/remote/` are fixed POSIX `sh`,
  passed literally, linted with `shellcheck -s sh` and formatted with
  `shfmt -ln posix -i 4` (both in pre-commit); they print only `SHK_`
  lines and sanitize `SBATCH_*`, `SQUEUE_*`, `SACCT_*` and `SLURM_*`.
- Contract paths: `src/sherlock_kit/data/rules/workstation.md`,
  `src/sherlock_kit/data/rules/cluster.md`,
  `src/sherlock_kit/data/skills/sherlock-kit/SKILL.md` and `bin/shk` (mode
  100755). dotfiles links them; renaming one is a breaking change that needs a
  coordinated dotfiles change. Symlinks in the repository are relative.
- The rules files start with an H2, have no front matter, HTML comments, `@`
  imports or relative links, stay within 7 KiB, and mention only released CLI.
  The skill's front matter holds `name` and `description` only.
- `partitions.json` is the only partition source. After changing it, paste the
  output of
  `PYTHONPATH=src python3 -B -c "from sherlock_kit.partitions import markdown; print(markdown(), end='')"`
  over the `### Partition profiles` block at the end of both rules files.
- Re-read Sherlock's `/etc/agents/*.md` before changing the rules, update the
  "Reviewed" date and [docs/sherlock-policy.md](docs/sherlock-policy.md).
- Never commit runtime state, private configuration, credentials, research code
  or data. Public examples use synthetic paths and names.
- Live work on Sherlock needs explicit authorization, a named workload and CPU
  only: never submit GPU work while developing or testing this repository.

Report sensitive issues privately to the repository owner; never put secrets or
private reproductions in public issues.
