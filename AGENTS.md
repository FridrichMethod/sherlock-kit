# sherlock-kit development

Read README.md, CONTRIBUTING.md and the relevant docs/ guide.
The lead owns main, integration and publication. Each writer
uses a separate branch and worktree; reviewers are read-only. Preserve user changes.
Use Python 3.11+ and standard-library mechanisms where sufficient. Run
`pre-commit run --all-files` (ruff, mypy, shellcheck, shfmt, mdformat),
`python3 -B tests/run_suite.py` (or `python3 -m unittest discover -s tests -v`) and
isolated installation checks.
Never publish credentials, private configuration, research code/data, or runtime state.
Implement small independently written mechanisms; references do not license copying.
The rules files under `src/sherlock_kit/data/rules/` own the delivered policy; Slurm is
the source of truth; the remote programs are fixed POSIX sh.
Doctor is read-only; mutation ambiguity stays unknown until Slurm accounting resolves it.
Never submit GPU work while developing or testing this repository, operate
historical campaigns, or infer borrowed access.
Only the lead merges to main and tags.

Write all documentation in English. Documentation may contain only printable
ASCII characters (U+0020 through U+007E), tabs, LF and CR, with no exceptions.
This applies to Markdown (.md, .markdown and .mdx), reStructuredText (.rst), text
(.txt), and README, CHANGELOG, CONTRIBUTING, AUTHORS, LICENSE and NOTICE files,
including suffixes, regardless of filename case. Use ASCII punctuation and
write symbols in words or ASCII notation.
