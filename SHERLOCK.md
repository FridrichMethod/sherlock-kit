# Sherlock operational policy

Before doing anything on Sherlock, read `/etc/agents/AGENTS.md` and the relevant
`slurm.md`, `storage.md`, `software.md`, and `policy.md` in `/etc/agents/`.
Check `hostname` and `SLURM_JOB_ID` to determine whether you are on a workstation,
login node, or allocated compute node. These instructions apply also when operating
Sherlock from a local workstation over SSH.

Reviewed 2026-10-07. Installed site instructions were read on that date; public
sources are linked below. Current site instructions take precedence over this
dated summary. Site requirements, recommendations, and toolkit choices are labeled.

## Compact policy delivered to agents

<!-- SHERLOCK-KIT:PROJECTION:BEGIN -->
When working on or connecting to Sherlock: read `/etc/agents/AGENTS.md` and the
relevant topic guides before acting; check `hostname` and `SLURM_JOB_ID` first.

- Never extend `$SCRATCH` or `$GROUP_SCRATCH` lifetime artificially: no touching,
  rewriting, copying over, or refresh/keepalive helpers for purge evasion, even
  once. This can cause account suspension. Normal useful computation, checkpoint
  writes, and legitimate transfers are allowed; keep durable copies elsewhere.
- Sustained computation, heavy builds/installations, and intensive internal copies
  require a Slurm allocation. External bulk transfers use DTNs or Globus. DTNs
  have no interactive shell; never run control commands there or SSH to them from
  Sherlock. Use explicit paths because DTN default paths differ from login nodes.
- Slurm requests need explicit walltime and correct CPU/GPU resources and partition.
  Sherlock does not support `--account` or node exclusion (`--exclude`, `-x`);
  use supported constraints and discover hardware with `sh_part`/`sh_node_feat`.
- Leave at least 60 seconds between status checks; prefer dependencies and shared
  cached queries. No `watch` or shell polling loops, duplicate pending submissions,
  or blind failure retries. Batch short work into at least ten minutes of real useful
  work, preferably thirty; never pad jobs with sleeps.
- Resolve storage via environment roots on the intended host, then validate paths
  and authorization. Avoid intensive `$HOME` job I/O; persistent `$GROUP_HOME`
  environments and small reads are allowed. Stage file-heavy work in
  `$L_SCRATCH_JOB`; export needed outputs before job end. `$L_SCRATCH` cleanup
  follows the user's last job on a node. Scratch has no backup and a 90-day purge.
- Use only authorized directories/jobs and Low/Moderate Risk data. No borrowed
  access inferred from scheduler visibility or old scripts, credential/session
  sharing, authentication bypass, system configuration probing, or other-user scans.
  Cancel jobs through Slurm. Never publish secrets or private grant/configuration.
- Check `ml spider`; prefer modules, suitable existing group environments, then an
  authorized `$GROUP_HOME` environment/container. Initialize batch environments
  explicitly; keep shell startup cheap. No unattended login-node agent servers,
  indefinite restart supervisors, or recurring future agent sessions; preserve
  managed client/sandbox restrictions. The site module is `pi-coding-agent`.
- Use bounded noninteractive OpenSSH, normally `sherlock-plain`, and human
  authentication bootstrap. No authentication storms or stored MFA/passwords.
  A lost mutation reply is unknown; preserve its reservation and reconcile identity
  before retrying. `ssh -O check` only checks a local master. Doctor is read-only.

Full policy and provenance: `shk policy`; installed identity: `shk policy --identity`.
The toolkit's transport/projection does not enforce arbitrary scripts or certify
scientific results. Consumer resources, grants, budgets, and validators are explicit.
<!-- SHERLOCK-KIT:PROJECTION:END -->

## Site requirements and recommendations

The compact rules above reflect the mandatory installed agent guide and topic
guides read on 2026-10-07. A job's ten-minute useful-work floor comes from that
guide; thirty minutes and dependencies are site recommendations, not a reason to
add sleeps. An observed partition/node is inventory, never evidence of a grant.
Borrowed admission requires grantee, allowed use, validity window, limits, and an
evidence reference. Hardware eligibility depends on workload dtype, memory, GPU
count, CUDA architectures, and kernels. V100 lacking native bf16 does not itself
disqualify GROMACS mixed-precision MD; validate the actual build.

[Filesystem policy](https://www.sherlock.stanford.edu/docs/storage/filesystems/)
describes persistent `$HOME`/`$GROUP_HOME`, unbacked scratch, the 90-day content-based
purge, and the prohibition on lifetime extension. Displayed `ls` timestamps are
not the purge clock; reads, renames, ownership/permission changes, and `touch` do
not reset it. Published quotas are dated defaults, not universal admission limits.
`$OAK` is purchased persistent storage; backup is a separate service.

[Running jobs](https://www.sherlock.stanford.edu/docs/user-guide/running-jobs/),
[GPU guide](https://www.sherlock.stanford.edu/docs/user-guide/gpu/), and
[submission options](https://www.sherlock.stanford.edu/docs/advanced-topics/submission-options/)
provide allocation and hardware guidance. The unsupported account/exclusion rules
are specifically from the installed site agent guides, not generic Slurm syntax.
[Storage guidance](https://www.sherlock.stanford.edu/docs/storage/) explains slow
startup and environment retrieval failures; diagnose without rewriting startup
files or automatically releasing held jobs.

[Data transfer](https://www.sherlock.stanford.edu/docs/storage/data-transfer/)
documents DTN transfer protocols and the different default destination namespace.
Control manifests/receipts belong on the login/control host; transfer bytes through
the authorized data endpoint. Verify that explicit roots refer to the same data.

[Connection options](https://www.sherlock.stanford.edu/docs/advanced-topics/connection/)
describe multiplexing and Kerberos (25-hour tickets at review time). External SSH
public keys are unsupported for this documented login workflow. Existing authenticated
connections may continue working; cold authentication may require the human and Duo.
[Authentication failures](https://www.sherlock.stanford.edu/docs/getting-started/connecting/#authentication-failures)
can cause temporary IP blocking. Do not repeatedly reconnect or store credentials.

[Coding agents](https://www.sherlock.stanford.edu/docs/software/ai/coding-agents/),
[modules](https://www.sherlock.stanford.edu/docs/software/modules/),
[installation](https://www.sherlock.stanford.edu/docs/software/install/),
[Apptainer](https://www.sherlock.stanford.edu/docs/software/containers/apptainer/),
and [concepts](https://www.sherlock.stanford.edu/docs/concepts/) cover software and
data classification. This foundation keeps agents on the workstation.

## Toolkit decisions and limits

Use the system OpenSSH client and existing configuration, with `-T`,
`BatchMode=yes`, `RemoteCommand=none`, explicit connection and whole-command
deadlines, and no automatic retries. Honor host verification and never disable it
to make automation work. Distinguish human bootstrap, control, and transfer health.
The read-only doctor defaults to local checks. Remote checks are explicit and
reuse existing control masters without cold connections; DTN capability remains
unverified until a supported transfer-protocol probe is established.

Transport argv is quoted for a POSIX-compatible remote shell. Supplying `sh -c`
or a program that mutates files is still a mutation; the API does not analyze
arbitrary programs. Before dispatch, missing executable/backoff is not-sent.
Timeout, disconnect, parse failure, or crash after possible mutation dispatch is
unknown, never proof of rejection. Preserve reservations until identity-bound
evidence resolves the attempt. A scheduler-visible token is not an idempotency key.
Scheduler completion, execution receipts, artifacts, and scientific validation
remain separate. Source/runtime/input/policy and attempt identities must bind
outputs; checkpoints need workload-specific validation.

Production installations are frozen revisions with policy SHA256 and schema version.
Development overrides are visibly labeled. Compare the installed revision/policy
with advertised instruction provenance before new managed mutations. Old attempts
remain recoverable under their recorded policy; diagnostics and verified recovery
must not depend on adopting a new policy. A projection is instruction delivery,
not proof that clients loaded it or enforcement against other tools.
