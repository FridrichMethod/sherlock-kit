# sherlock-kit

A shared Sherlock operational policy, bounded OpenSSH transport, diagnostics,
typed CPU submission/reconciliation and manifest-bound artifact transfer.
Requires Python 3.11+, system OpenSSH and rsync for transfers. Read [SHERLOCK.md](SHERLOCK.md)
before operating Sherlock; it distinguishes site requirements from toolkit choices.

Install an exact reviewed 40-character Git revision, without `--editable`, into an
isolated environment. Dotfiles integration records the revision and policy digest
and provides an explicit installation/verification command. Normal shell startup
and Stow do not install or update this package. A clean committed checkout can
also build a frozen wheel (`python -m pip wheel --no-deps .`); builds embed its
Git revision and exact root policy. Source distributions preserve that identity.
An installer that verifies and extracts `git archive REV` may set
`SHERLOCK_KIT_BUILD_REVISION=REV` for its build; the value must be the verified
40-character revision and must match Git when a checkout is present.

```console
shk policy
shk policy --identity
shk policy --projection
shk doctor
shk doctor --remote
```

Local doctor contacts no remote endpoint and writes no state. Remote doctor executes
only its documented inventory: context (`hostname` and `SLURM_JOB_ID`), installed
site-guide reads, and `scontrol --version`. It checks existing control masters for
both endpoints; remote commands use only the control host and prohibit cold
connections. A local master is not evidence that a remote command will succeed.
DTN transfer capability is explicitly unverified; no DTN shell command is attempted.
If authentication is needed, connect manually using the configured host alias and
complete the site's human authentication, then rerun the bounded diagnostic.

Doctor checks advertised provenance with `--advertised-identity FILE` (or
`SHERLOCK_KIT_PIN`) and policy blocks with `--claude-instructions FILE` and
`--codex-instructions FILE` (or `SHERLOCK_KIT_CLAUDE_INSTRUCTIONS` /
`SHERLOCK_KIT_CODEX_INSTRUCTIONS`). JSON pins contain `schema_version`,
`code_revision`, and `policy_sha256`; additional installer metadata is permitted.
Unverified instruction loading is reported honestly: matching text is not proof
that either agent actually loaded it.
Doctor also reports guard runtime and adapter bundle availability. Registration,
current Codex hash trust and blocking remain `unverified`: local disk presence
cannot prove an active client's state. Use the actual client inspection and smoke
tests documented in the opt-in integration before claiming enforcement.

```python
from sherlock_kit import TransportConfig, run_remote

result = run_remote(TransportConfig(), ["hostname"])
assert result.retry_performed is False
```

`ssh_argv(config, argv)` quotes literal arguments for a POSIX-compatible remote
shell and overrides interactive SSH settings. `run_remote` uses connection and
whole-command deadlines, no stdin, and at most one dispatch. Its results expose
`status`, `stdout`, `stderr`, `returncode`, and `dispatched`. Supplying a shell or
mutating program still requires authorization and an appropriate journal; the
helper is not a shell policy evaluator. Mutations with any inconclusive reply
remain `unknown`; no retry or release of reservations follows automatically.

Authentication failures impose a private shared cooldown across repositories and
host aliases. Default state is `$XDG_STATE_HOME/sherlock-kit/auth-backoff.json` or
`~/.local/state/sherlock-kit/auth-backoff.json`; an explicit `backoff_file` may use a
private durable directory. Malformed state blocks dispatch rather than erasing it.
Normal transport holds a bounded local lock; this is one-workstation coordination,
not a distributed controller. Doctor only reads the cooldown and uses existing
masters, so it cannot create an authentication storm or rewrite that state.

Development: `PYTHONPATH=src python3 -m sherlock_kit policy --identity` clearly
reports `install_mode=development`. Run `python3 -m unittest discover -s tests -v`.
See [consumer fixture](tests/fixtures/consumer/README.md) for a synthetic first-day
contract. No live job, GPU use, or historical campaign operation is part of these
tests. The real `submit`, `status`/`reconcile` and `fetch` commands require an
explicit consumer contract; see [orchestration](docs/orchestration.md). Their live
rasraser acceptance remains pending; implementation tests are not a live pilot.
Optional agent adapters and the narrow guard are described in
[guard](docs/guard.md). Neither raw SSH nor arbitrary shell commands are certified
by the toolkit.
