# sherlock-kit

[![CI](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml)

Shared Sherlock operational policy, bounded OpenSSH transport, read-only
diagnostics, typed submission/reconciliation on packaged partition profiles and
verified artifact transfer. Python 3.11+, POSIX for orchestration, OpenSSH, and
rsync for transfers.

Read [SHERLOCK.md](SHERLOCK.md) before operating Sherlock. The toolkit does not
authorize resources, infer borrowed access, enforce arbitrary shell scripts or
certify scientific results.

## Install and start

Install a reviewed **full 40-character commit**, rather than a moving branch or
an editable checkout, into a dedicated environment:

```sh
python3 -m venv /path/to/sherlock-kit-venv
/path/to/sherlock-kit-venv/bin/python -m pip install \
  'git+https://github.com/FridrichMethod/sherlock-kit.git@FULL_COMMIT_SHA'
/path/to/sherlock-kit-venv/bin/shk policy --identity
/path/to/sherlock-kit-venv/bin/shk policy
/path/to/sherlock-kit-venv/bin/shk doctor
```

Replace the environment path and SHA first. Local doctor writes nothing and
contacts no remote endpoint. `shk doctor --remote` is an explicit bounded read-only
inventory using existing authenticated connections; it never submits a probe job
or executes a shell on the DTN. Authenticate manually when requested.

For existing [dotfiles integration](https://github.com/FridrichMethod/dotfiles/blob/main/docs/sherlock-kit.md),
use its pinned installer and stable launcher. It projects the same policy into
both clients' real global instructions. Normal Stow and shell startup do not
install dependencies.

## Use

- [Installation and configuration](docs/installation.md): immutable identity,
  explicit state placement, standalone Python API and agent delivery.
- [Typed operations](docs/orchestration.md): private consumer configuration,
  partition profiles, submit preview/apply, status/reconciliation of one attempt
  or `--all`, partition occupancy and manifest-bound fetch.
- [Optional guard](docs/guard.md): narrow patterns, one registration owner,
  actual client trust and failure behavior.
- [Maintenance](docs/maintenance.md): updates, controller state, recovery,
  artifact retention and release checklist.
- [Acceptance evidence](docs/validation/acceptance.md): real CPU pilot and
  actual client checks; [synthetic consumer](tests/fixtures/consumer/README.md)
  provides an offline first-day example.

Supported orchestration is one authoritative POSIX workstation and a single
allocation per attempt on a packaged partition profile (`normal`, `owners`,
`btrippe`). Slurm requeue is emitted only where the profile allows it, and a
requeued script must checkpoint and resume on its own. The GPU, requeue, batch
reconciliation and occupancy paths were exercised by a live owners/btrippe GPU
pilot with one operator-issued requeue; natural preemption handling remains
offline-tested, and the scientific pilot was CPU work on `normal`. Arrays,
distributed controllers and general DDP recovery are not supported. Workload
source/runtime/input identities, authorized storage, budget and scientific
validator are explicit consumer inputs. `shk occupancy` reports a borrowed
partition's current use before a courtesy submission and gates nothing;
`status`/`reconcile --all` cover many open attempts in one bounded query. Unknown
submission outcomes retain reservations and never trigger automatic retries, and
an unexpected preemption on a non-preemptible partition keeps its reservation
until an operator acknowledges it. Fetch checks exact inventory, bytes and
validator identity before atomic promotion; local recovery can finish a durable
receipt without network access.

Guard and skill adapters call the same installed toolkit. A file's presence does
not prove active registration, current trust or enforcement; doctor reports those
properties as unverified. Production hooks remain opt-in.

Development and checks: [CONTRIBUTING.md](CONTRIBUTING.md). Release changes:
[CHANGELOG.md](CHANGELOG.md).

Licensed under [MIT](LICENSE).
