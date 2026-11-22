# sherlock-kit development

Read README.md, CONTRIBUTING.md and the relevant docs/ guide.
The lead owns main, integration and publication. Each writer
uses a separate branch and worktree; reviewers are read-only. Preserve user changes.
Use Python 3.11+ and standard-library mechanisms where sufficient. Run
`pre-commit run --all-files` (ruff, mypy, shellcheck, shfmt, mdformat),
the development environment's `python -B tests/run_suite.py` (authoritative: unlike `python -m unittest discover -s tests -v`
it fails a run that reached the Slurm or ssh tripwire) and isolated installation checks.
Prepare the build interpreter and per-Python wheelhouse as described in
CONTRIBUTING.md; set `SHK_BUILD_PYTHON`, `SHK_TEST_WHEELHOUSE` and `SHK_REQUIRE_ALL=1`
for required checks. Test both Python 3.11 and 3.14 from committed HEAD.
Never publish credentials, private configuration, research code/data, or runtime state.
Implement small independently written mechanisms; references do not license copying.
The rules files under `src/sherlock_kit/data/rules/` own the delivered policy; Slurm is
the source of truth; the remote programs are fixed POSIX sh.
Doctor is read-only; mutation ambiguity stays unknown until Slurm accounting resolves it.
Never submit GPU work while developing or testing this repository, operate
historical campaigns, or infer borrowed access.
Marlowe is the second site: its partition table is `partitions-marlowe.json`,
its rules provenance `docs/marlowe-policy.md`. Every Marlowe job is billed:
develop and test there with `--dry-run`, `--preflight` and held jobs, never a
job that runs, unless the owner authorizes it. Tracked files name a Marlowe
project only as `m000000` (`marlowe-m000000`, `USER`); a test guards this.
Only the lead merges to main and tags.

Keep `docs/` as maintained user/developer reference and necessary compatibility
evidence, with an index in `docs/README.md`. Put temporary implementation plans,
worker handoffs and check logs in the canonical (main) checkout's ignored
`.state/<task>/`, the durable task storage, not in release documentation and
not only inside a worktree: `git worktree remove` deletes a worktree's ignored
files, so copy evidence out to that directory before removing the worktree.
The test wheelhouse that CONTRIBUTING.md prepares under `.state/` is a
disposable build input, not task storage. Before removing a completed plan,
preserve any still-relevant decisions in the appropriate guide and check
incoming links. Git history keeps only what was committed: a plan that was
never tracked exists only in task storage. Do not create a duplicate archive
in `docs/`. Unfinished work and its handoff must remain recoverable.

Write all documentation in English. Documentation may contain only printable
ASCII characters (U+0020 through U+007E), tabs, LF and CR, with no exceptions.
This applies to Markdown (.md, .markdown and .mdx), reStructuredText (.rst), text
(.txt), and README, CHANGELOG, CONTRIBUTING, AUTHORS, LICENSE and NOTICE files,
including suffixes, regardless of filename case. Use ASCII punctuation and
write symbols in words or ASCII notation.
