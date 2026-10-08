# Installation and configuration

## Frozen installation

Follow the [README](../README.md) to install a reviewed full commit into a
dedicated Python 3.11+ environment. A wheel embeds the exact code revision,
policy SHA256 and schema version. `shk policy --identity` must report
`install_mode=frozen`. A matching projection is evidence of matching bytes,
not proof that an active client loaded instructions.

A clean committed checkout can build a wheel:

```sh
/path/to/build/python -m pip wheel --no-deps --wheel-dir /path/to/output .
```

Source distributions preserve embedded identity. A verified `git archive REV`
may set `SHERLOCK_KIT_BUILD_REVISION=REV` during its build; this must be the exact
verified 40-character commit and agree with Git when a checkout is present.
Archives unpacked under unrelated Git repositories must not inherit their HEAD.

## Explicit state location

Use a private, durable directory outside all consuming worktrees and scratch.
All consumers on the authoritative workstation share the same admission/budget
state; per-worktree databases do not coordinate resources. Private configuration
files are mode 0600 and controller directories are owned mode 0700.

Typed commands require `--config` with an explicit absolute `state_root`. Their
authentication cooldown uses that root unless `transport.backoff_file` or
`SHERLOCK_KIT_STATE_ROOT` is set. For standalone transport calls, select an
absolute root before use:

```sh
export SHERLOCK_KIT_STATE_ROOT=/path/to/private/sherlock-kit
```

The transport places `auth-backoff.json` and its lock directly under that root.
An explicit `TransportConfig(backoff_file=...)` has highest priority. An invalid
explicit root fails closed. Without either, standalone transport retains the
standard `$XDG_STATE_HOME/sherlock-kit` (or `~/.local/state/sherlock-kit`) fallback
for compatibility. Choose an explicit root to avoid that HOME fallback. Policy,
guard and local doctor create no runtime state.

The dotfiles installer accepts `--state-root /path/to/private/sherlock-kit`
(PowerShell `-StateRoot`). It stores the locator in its existing active pointer;
upgrades preserve it. The stable launcher supplies that root unless the caller
has already set `SHERLOCK_KIT_STATE_ROOT`. This does not grant filesystem access
or create a controller. Package environments and client configuration remain
in their documented standard installation locations. Pilot data and private
evidence belong in the user's designated data area, never in public Git.

## Diagnostics and Python API

`shk doctor --advertised-identity PIN.json --claude-instructions CLAUDE.md
--codex-instructions AGENTS.md` checks expected identity and marked policy blocks.
The launcher supplies these through `SHERLOCK_KIT_PIN`,
`SHERLOCK_KIT_CLAUDE_INSTRUCTIONS` and `SHERLOCK_KIT_CODEX_INSTRUCTIONS`.
Missing authentication, identity mismatch and unverified capabilities stay
separate. Remote doctor uses fixed bounded reads and existing masters; a local
master alone does not prove remote or DTN protocol capability.

```python
from sherlock_kit import TransportConfig, run_remote

config = TransportConfig(backoff_file="/private/shared/auth-backoff.json")
result = run_remote(config, ["hostname"])
assert result.retry_performed is False
```

`ssh_argv(config, argv)` quotes arguments for a POSIX-compatible remote shell.
Local execution uses argv, no stdin, connection/whole-command deadlines and at
most one dispatch. Arbitrary remote shells or mutating programs still require
authorization and durable intent; this API is not a command-policy evaluator.
A lost mutation reply is unknown and cannot authorize retry.

## Client adapters

Use the [dotfiles setup guide](https://github.com/FridrichMethod/dotfiles/blob/main/docs/sherlock-kit.md)
for global policy delivery, explicit first-party plugin/skill copying and existing
structured settings synchronization. The package owns its adapter payloads;
dotfiles does not vendor a second engine. Preflight temporary targets before
enabling from a verified canonical checkout. Never point live symlinks at a
temporary worktree.

Claude plugin installation alone does not enable a plugin. Codex skill discovery
is separate from hook registration. Both call the same installed `shk`. Hooks
have one registration owner, remain opt-in, and require actual trust/blocking
checks; see [guard](guard.md).
