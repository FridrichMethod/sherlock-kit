# sherlock-kit development

Read README.md, CONTRIBUTING.md and the relevant docs/ guide.
The lead owns main, integration and publication. Each writer
uses a separate branch and worktree; reviewers are read-only. Preserve user changes.
Use Python 3.11+ and standard-library mechanisms where sufficient. Run
`python3 -m unittest discover -s tests -v` and isolated installation checks.
Never publish credentials, private configuration, research code/data, or runtime state.
Implement small independently written mechanisms; references do not license copying.
SHERLOCK.md owns operational policy. Doctor is read-only; mutation ambiguity stays
unknown until the registry on Sherlock and Slurm accounting resolve it. There is
no local ledger: `sherlock_registry` is shipped as source to the login node and
must stay standard-library only with no sibling imports.
Never submit GPU work while developing or testing this repository, operate
historical campaigns, or infer borrowed access.
Only the lead activates tested configuration from canonical checkouts.
