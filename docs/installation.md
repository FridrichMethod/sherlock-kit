# Installation and configuration

## Frozen installation

Follow the [README](../README.md) to install a reviewed full commit into a
dedicated Python 3.11+ environment. A wheel embeds the exact code revision,
policy SHA256, schema version and `partitions_sha256`, the SHA256 of the packaged
partition profile table `sherlock_kit_data/partitions.json`. `shk policy
--identity` must report `install_mode=frozen`; a frozen install whose packaged
table differs from its embedded hash refuses to report an identity at all. A
matching projection is evidence of matching bytes, not proof that an active
client loaded instructions.

A clean committed checkout can build a wheel:

```sh
/path/to/build/python -m pip wheel --no-deps --wheel-dir /path/to/output .
```

Source distributions preserve embedded identity. A verified `git archive REV`
may set `SHERLOCK_KIT_BUILD_REVISION=REV` during its build; this must be the exact
verified 40-character commit and agree with Git when a checkout is present.
Archives unpacked under unrelated Git repositories must not inherit their HEAD.

## Local cache root and registry root

Typed commands keep two kinds of state. Durable attempt state lives on Sherlock
in the attempt registry named by the private configuration's `registry_root`,
an absolute canonical path on the control host that the registry runner creates
as an owned 0700 directory (with `attempts/` and `tasks/`) on the first
submission; see [typed operations](orchestration.md). Nothing durable about an
attempt lives on the workstation, so any POSIX workstation with the frozen
toolkit and the same private configuration can operate, and no workstation is
"authoritative".

Locally, typed commands keep only the shared authentication cooldown
`auth-backoff.json` and the 60-second query cache `query-cache.json`, side by
side under one explicit private location: `transport.backoff_file` in the
private configuration or the `SHERLOCK_KIT_STATE_ROOT` environment root. With
neither set a typed command refuses to run (`typed commands need an explicit
local state location: set transport.backoff_file or SHERLOCK_KIT_STATE_ROOT`)
rather than fall back to `$HOME`. Use a private, durable directory outside all
consuming worktrees and scratch; private configuration files are mode 0600 and
the local root is an owned 0700 directory. For standalone transport calls,
select an absolute root before use:

```sh
export SHERLOCK_KIT_STATE_ROOT=/path/to/private/sherlock-kit
```

The transport places `auth-backoff.json` and its lock directly under that root,
and the typed commands place `query-cache.json` and its own lock beside them.
An explicit `TransportConfig(backoff_file=...)` has highest priority. An invalid
explicit root fails closed. Without either, standalone transport retains the
standard `$XDG_STATE_HOME/sherlock-kit` (or `~/.local/state/sherlock-kit`)
fallback for compatibility; typed commands never reach it, and standalone API
callers should set an explicit root rather than rely on it. Policy, guard and
local doctor create no runtime state.

The dotfiles installer accepts `--state-root /path/to/private/sherlock-kit`
(PowerShell `-StateRoot`). It stores the locator in its existing active pointer;
upgrades preserve it. The stable launcher supplies that root unless the caller
has already set `SHERLOCK_KIT_STATE_ROOT`; since 0.3.0 that root holds only the
cooldown and the query cache. This does not grant filesystem access or create a
registry. Package environments and client configuration remain in their
documented standard installation locations. Pilot data and private evidence
belong in the user's designated data area, never in public Git.

## Diagnostics and Python API

`shk doctor --advertised-identity PIN.json --claude-instructions CLAUDE.md
--codex-instructions AGENTS.md` checks expected identity and marked policy blocks.
The launcher supplies these through `SHERLOCK_KIT_PIN`,
`SHERLOCK_KIT_CLAUDE_INSTRUCTIONS` and `SHERLOCK_KIT_CODEX_INSTRUCTIONS`.
Missing authentication, identity mismatch and unverified capabilities stay
separate. A pin compares `schema_version`, `code_revision` and `policy_sha256`,
and `partitions_sha256` only when the pin advertises it, so pins written before
partition profiles existed still verify. Remote doctor uses fixed bounded reads
and existing masters; a local master alone does not prove remote or DTN protocol
capability.

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

The shared authentication cooldown arms only when OpenSSH itself exits 255 with
an authentication message (`auth_failure(stderr)`). A remote program's own
"Permission denied" is relayed text with another exit status and never arms it.
`cooldown_active(config)` reports the cooldown and fails closed when its state is
unreadable; `record_auth_failure(config)` arms it explicitly.

`data_transfer(config, source, stage, manifest, timeout=300)` is the data-endpoint
counterpart of `run_remote`: `source` is the host-less absolute remote directory,
the configured `data_host` is prepended, the manifest selects exactly which files
rsync may read, and an active cooldown refuses the transfer before rsync starts.
rsync failures and deadlines raise `sherlock_artifacts.TransferError` (a
`SafetyError` carrying `returncode` and a bounded single-line `stderr_tail`), so
the CLI prints `shk: ...` instead of a traceback. A data-endpoint authentication
failure (rsync exit 255 with an authentication message) arms the same shared
cooldown, so the control and data endpoints never produce two authentication
storms.

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
