# Maintenance and release

## Runtime layout

Choose one private durable data directory for this toolkit on the authoritative
workstation. Keep live coordinator state separate from completed pilot evidence:

```text
/private/sherlock-kit/
  auth-backoff.json             # created only by transport when necessary
  auth-backoff.json.lock
  coordinator/
    coordinator.sqlite3        # shared history, reservations and budgets
  archive/
    PILOT_ID/                  # retained private evidence and inventory
```

Use the same `coordinator` for every consumer sharing admission/budget scope.
Set the launcher/transport root explicitly; see [installation](installation.md).
Do not globally change `XDG_STATE_HOME` to relocate one application's state.
Do not place databases, credentials, private grants or research inputs in Git.

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
reply remains unknown and reserved; absence from `squeue` or missing accounting
is not rejection. Reconcile identity-bound evidence before considering any new
attempt. Do not reset or replace a ledger to make unresolved attempts disappear.

Keep pinned manifests, staging/transaction records and receipts until artifact
verification and promotion finish. An already-promoted matching destination can
complete its receipt with `fetch --local`; a mismatching destination is a conflict.
Do not overwrite unrelated data, modify a frozen validator or assume a log line
establishes scientific acceptance.

## Retiring local pilot artifacts

Before moving state, stop its writers and verify process ownership and all attempt
states. Unknown/submitting/reserved attempts prevent retirement; `submitting` and
`unknown` are equivalent for retirement, both being durable claims whose scheduler
outcome only identity-bound evidence can settle. A `not_sent` attempt frozen by
0.1.0 cannot be dispatched by this toolkit and holds its reservation until it is
explicitly resolved. Complete accounting and zero reservations are necessary, not
sufficient to discard history.
Archive completed attempts and exact manifests/validators/receipts with an inventory
of original paths, sizes, hashes and permissions. Use private owned directories;
verify the inventory after moving. Keep cumulative ledger history for future
consumers rather than creating a fresh empty database.

A retired config may still contain original absolute paths. If those paths simply
vanish, accidentally running it can create a new empty coordinator. A small
ordinary-file tombstone at the old state root fails closed; a compatibility
symlink is not a safe migration mechanism. Mark archived configs historical and
never run managed CLI operations directly against an archive. Archive permission
bits protect privacy; they do not make owner-editable files cryptographically immutable.

Relocation preserves file bytes, not necessarily ctime or old absolute bindings.
Keep a recovery mapping and original frozen contents. To inspect a retired bundle,
verify manifest hashes and execute the verified standalone validator in a private
working copy, without submitting work or changing the archived ledger/spec.
Actual managed recovery requires an explicit reviewed restoration of identities
and locators; do not silently rewrite admitted identities to fit a new path.

Delete only known task-generated temporary directories, inactive build outputs
and unused task environments after inspecting their contents and dependencies.
Use normal `git worktree remove`/merged-branch deletion for completed worktrees.
Retain local research commits and historical source worktrees. Never remove
scientific results, old controllers or unrelated user files during toolkit cleanup.

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
remote roots, grants, client rollouts, databases and pilot evidence are excluded.
Implementation plans and session journals are available in Git history; maintained
user guides and [acceptance evidence](validation/acceptance.md) define the release.
