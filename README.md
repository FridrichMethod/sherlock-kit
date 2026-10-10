# sherlock-kit

[![CI](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml)

Shared Sherlock operational policy, bounded OpenSSH transport, read-only
diagnostics, typed submission/reconciliation on packaged partition profiles
through an attempt registry on Sherlock, and verified artifact transfer. Python
3.11+, POSIX for orchestration, OpenSSH, and rsync for transfers.

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
  the local cache root and the registry root, standalone Python API and agent
  delivery.
- [Typed operations](docs/orchestration.md): private consumer configuration,
  partition profiles, single jobs and job arrays, submit preview/apply,
  status/reconciliation of one attempt or `--all`, acknowledging or abandoning
  one attempt, partition occupancy and manifest-bound fetch.
- [Optional guard](docs/guard.md): narrow patterns, one registration owner,
  actual client trust and failure behavior.
- [Maintenance](docs/maintenance.md): updates, local and remote runtime state,
  upgrading from 0.2.0, recovery, artifact retention and release checklist.
- [Acceptance evidence](docs/validation/acceptance.md): real CPU and GPU pilots
  and actual client checks; [synthetic consumer](tests/fixtures/consumer/README.md)
  provides an offline first-day example.

Supported orchestration is one Slurm allocation per attempt, a single job or a
job array of 2 to 1000 tasks, on a packaged partition profile (`shk policy`
lists them: public `normal`, `bigmem`, `gpu`, `dev` and `service`, department
`bioe` and `stat`, borrowed `btrippe` and `possu`, preemptible `owners`). Slurm
requeue is emitted only where the profile allows it, and a requeued script must
checkpoint and resume on its own. The only durable attempt state is an attempt
registry on Sherlock (`registry_root`, shared by every campaign) plus Slurm
accounting: the registry program travels with every call, so any POSIX
workstation with the frozen toolkit and the private configuration can operate,
and the workstation keeps nothing but the authentication cooldown and a 60 s
query cache under an explicit private root. Distributed controllers and general
DDP recovery are not supported. Workload source/runtime/input identities,
authorized storage and the scientific validator are explicit consumer inputs.
`shk occupancy` reports a borrowed partition's current use before a courtesy
submission and gates nothing; `status`/`reconcile --all` cover many open attempts
in one bounded query. An unknown submission outcome is resolved by its job name
from the registry and accounting, never by an automatic retry; a retry of a
logical task must name its `parent_attempt` and is admitted only once the parent
is released. An unexpected preemption on a non-preemptible partition blocks
until an operator acknowledges it, and an attempt that never reached accounting
can be abandoned only after a fresh check on the login node. Fetch checks exact
inventory, bytes and validator identity before atomic promotion and keeps an
attempt sidecar beside the bundle, so local recovery can finish a durable
receipt without network access.

The 0.1.0 CPU pilot and the 0.2.0 owners/btrippe GPU pilot are recorded in the
acceptance evidence; the live pilot of the 0.3.0 registry and array paths is
pending, and natural preemption handling remains offline-tested.

Guard and skill adapters call the same installed toolkit. A file's presence does
not prove active registration, current trust or enforcement; doctor reports those
properties as unverified. Production hooks remain opt-in.

Development and checks: [CONTRIBUTING.md](CONTRIBUTING.md). Release changes:
[CHANGELOG.md](CHANGELOG.md).

Licensed under [MIT](LICENSE).
