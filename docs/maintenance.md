# Maintenance and release

## Runtime layout

Runtime state has two homes. The attempt registry on Sherlock, named by the
private configuration's `registry_root`, is the only durable attempt state and
is shared by every campaign of the principal; the workstation keeps a small
private cache root. Keep both separate from completed pilot evidence:

```text
# Sherlock, control host: registry_root (owned, 0700, no symlinks)
/group-home/USER/sherlock-kit/registry/
  .lock                        # flock of the submit runner and event writer
  tasks/<key>                  # latest attempt id of one logical task
  attempts/<attempt>/
    record.json                # frozen spec and sbatch options, pre-sbatch
    submitted.json             # sbatch reply (job id or null)
    not_sent.json              # sbatch never ran for this attempt
    ack-<epoch>.json           # operator waiver of an investigated preemption
    abandoned.json             # operator closure, accounting stayed empty
    resolved.json              # terminal closure written by reconcile

# Workstation: explicit local root (SHERLOCK_KIT_STATE_ROOT/backoff_file)
/private/sherlock-kit/
  auth-backoff.json            # created only by transport when necessary
  auth-backoff.json.lock
  query-cache.json             # 60 s query cache of the typed commands
  query-cache.json.lock
  archive/
    PILOT_ID/                  # retained private evidence and inventory
```

Never edit, rename or delete a registry file by hand: every file is create-once
and readers treat an unexpected or malformed file as an `error` entry for that
attempt. The registry program creates the root and its two subdirectories on
the first submission and refuses a root that is not an owned private directory.
`query-cache.json` and its lock may be removed when no typed command is running
(the next query simply runs again; no attempt state lives there). Never remove
`auth-backoff.json` while a cooldown is armed: it is the shared authentication
cooldown that keeps a failed login from being retried, not attempt state. Set
the launcher/transport root explicitly; see
[installation](installation.md). Do not globally change `XDG_STATE_HOME` to
relocate one application's state. Do not place registries, caches, credentials,
private grants or research inputs in Git.

## Updates and recovery

Publish a reviewed toolkit commit before generating a dotfiles pin that points
to it. Any edit to `SHERLOCK.md` changes `policy_sha256` and the projection, so
the pin and both delivered projections must be regenerated; until then doctor
reports a mismatch and new managed admissions are blocked by design. Verify the
frozen installation, policy/projection and both client adapters in temporary
targets. Only the integrator activates real configuration from a canonical
checkout. Preserve local keys and preflight structured settings before
applying them. Retain old installed environments while recorded attempts require
their runtime; existing virtual environments must not be relocated.

An identity mismatch blocks new managed admissions. Recorded attempts keep their
original policy, source/runtime/input and validator identities. A lost mutation
reply remains unknown; absence from `squeue` or missing accounting is not
rejection. Run `shk status --attempt ID` and let the registry and Slurm
accounting resolve it before considering any new attempt; a retry must name the
attempt it replaces as `parent_attempt` and is admitted only once that attempt
is released. Do not remove or rewrite registry files to make unresolved attempts
disappear; `reconcile --attempt ID --abandon` is the only closure for an
attempt that never reached accounting.

Keep attempt sidecars, staging/transaction records and receipts until artifact
verification and promotion finish. An already-promoted matching destination can
complete its receipt with `fetch --local` from its sidecar; a mismatching
destination or sidecar is a conflict. Do not overwrite unrelated data, modify a
frozen validator or assume a log line establishes scientific acceptance.

## Upgrading from 0.2.0

0.3.0 replaces the local SQLite ledger with the registry on Sherlock and drops
reservations, budgets and limits. Before switching: resolve every 0.2.0 attempt
with the 0.2.0 installation (`status`/`reconcile`, acknowledgements), because
0.3.0 cannot see ledger attempts and will not migrate them; then archive
`coordinator.sqlite3` with the retired configuration as historical evidence.
Edit the private configuration: remove state_root and limits (both are refused
by the unknown-field message), add `registry_root`, and keep the launcher's
`SHERLOCK_KIT_STATE_ROOT` or `transport.backoff_file`, which now names only the
local cache root. A grant may keep its limits member; it is ignored. Create
nothing on Sherlock by hand: the first `submit --apply` creates the registry
root. Destinations fetched by 0.2.0 have no attempt sidecar; run one remote
fetch, which writes it on the recovery branch, before relying on
`fetch --local`. Regenerate the dotfiles pin and both projections, because the
policy text changed.

## Retiring local pilot artifacts

Before archiving, verify that no typed command is running and that every attempt
of the campaign is closed in the registry (`not_sent`, `abandoned` or
`resolved.json` present) through `shk reconcile --all`; an open or `unknown`
attempt is settled only by registry and accounting evidence, never by deleting
its files. Archive the exact manifests, validators, sidecars and receipts with an
inventory of original paths, sizes, hashes and permissions. Use private owned
directories; verify the inventory after moving. The registry itself stays on
Sherlock as cumulative history; do not start a fresh root to hide old attempts.

A retired config may still contain original absolute paths. Mark archived
configs historical and never run managed CLI operations directly against an
archive. Archive permission bits protect privacy; they do not make
owner-editable files cryptographically immutable.

Relocation preserves file bytes, not necessarily ctime or old absolute bindings.
Keep a recovery mapping and original frozen contents. To inspect a retired bundle,
verify manifest hashes and execute the verified standalone validator in a private
working copy, without submitting work or changing the archived registry/spec.
Actual managed recovery requires an explicit reviewed restoration of identities
and locators; do not silently rewrite admitted identities to fit a new path.

Delete only known task-generated temporary directories, inactive build outputs
and unused task environments after inspecting their contents and dependencies.
Use normal `git worktree remove`/merged-branch deletion for completed worktrees.
Retain local research commits and historical source worktrees. Never remove
scientific results, registries or unrelated user files during toolkit cleanup.

## Release checklist

1. Review the diff and changelog; document supported capabilities and limitations.
   Bump `pyproject.toml`, `adapters/claude/.claude-plugin/plugin.json` and the
   newest `CHANGELOG.md` heading together; `tests/test_release_metadata.py`
   enforces that the three agree.
2. Run the complete [contributor checks](../CONTRIBUTING.md), including packaging,
   on clean committed main and require full toolkit CI.
3. Build the wheel and source distribution from that exact committed revision in
   an isolated environment. Install the wheel and a wheel rebuilt from the sdist
   into temporary environments; verify identical code/policy identities and CLI.
4. Verify metadata, README rendering, package contents and the MIT
   license and its inclusion in both artifacts. Never infer a license from the visibility of a repository.
5. Publish toolkit first, then the corresponding tested dotfiles pin. Recheck
   canonical installation, policy projection and dotfiles native CI.
6. A release tag/GitHub release or package-index upload is a separate publication
   action. Do not claim a release exists merely because main is ready to release.

Retain only distributable assets in a release. Private audit inputs, concrete
remote roots, grants, client rollouts, registries, caches and pilot evidence are
excluded. Implementation plans and session journals are available in Git history;
maintained user guides and [acceptance evidence](validation/acceptance.md) define
the release.
