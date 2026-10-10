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
  A lost mutation reply is unknown; the attempt record on Sherlock and Slurm
  accounting are the truth: run `shk status --attempt ID` before any retry.
  `ssh -O check` only checks a local master. Doctor is read-only.
- Consumer partitions are packaged toolkit profiles (`shk policy --identity` reports
  `partitions_sha256`); unknown partitions are refused, borrowed partitions need an
  identity-bound grant and `shk occupancy` first, and `--requeue` is emitted only
  where the profile allows it:
  - `bigmem`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public high-memory partition: only for jobs that need more memory than normal provides. Follow Sherlock's own limits for it.
  - `bioe`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition, not borrowed: respect fairshare and leave room for other department users.
  - `btrippe`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Borrowed from another group. Run `shk occupancy` first. Courtesy budget about 2 jobs x 2 h while others are active; more (e.g. 4 jobs x 6 h) only when the partition is idle, typically 00:00-07:00.
  - `dev`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Public development partition: debugging and short tests only, never production runs. Follow Sherlock's own limits for it.
  - `gpu`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Public GPU partition: few GPUs, long queue, never preempted. Follow Sherlock's own per-user GPU limits.
  - `normal`: preemptible=no, requeue=no, borrowed=no, gpus=no.
  - `owners`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. Preemptible: Slurm requeues preempted jobs; scripts must checkpoint and resume. No cap.
  - `possu`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. Borrowed from another group and stricter than btrippe. Run `shk occupancy` first. Submit only between 00:00 and 07:00 Pacific, a few short jobs; never submit outside that window, not even short jobs.
  - `service`: preemptible=no, requeue=no, borrowed=no, gpus=no. Public service partition: lightweight recurring administrative tasks such as transfers or backups, never computation. Follow Sherlock's own limits for it.
  - `stat`: preemptible=no, requeue=no, borrowed=no, gpus=yes. Department GPU partition, not borrowed: respect fairshare and leave room for other department users.

Full policy and provenance: `shk policy`; installed identity: `shk policy --identity`.
The toolkit's transport/projection does not enforce arbitrary scripts or certify
scientific results. Consumer resources, grants and validators are explicit.
<!-- SHERLOCK-KIT:PROJECTION:END -->

## Site requirements and recommendations

The compact rules above reflect the mandatory installed agent guide and topic
guides read on 2026-10-07. A job's ten-minute useful-work floor comes from that
guide; thirty minutes and dependencies are site recommendations, not a reason to
add sleeps. An observed partition/node is inventory, never evidence of a grant.
Borrowed admission requires grantee, allowed use, validity window, partitions,
scope and an evidence reference. Hardware eligibility depends on workload dtype,
memory, GPU count, CUDA architectures, and kernels. V100 lacking native bf16 does
not itself disqualify GROMACS mixed-precision MD; validate the actual build.

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
unknown, never proof of rejection. The attempt record on Sherlock and Slurm
accounting resolve an unknown attempt; never retry it. A scheduler-visible token
is not an idempotency key. Scheduler completion, execution receipts, artifacts,
and scientific validation remain separate. Source/runtime/input/policy and
attempt identities must bind outputs; checkpoints need workload-specific
validation.

Durable attempt state is an attempt registry on Sherlock (`registry_root`, an
owned 0700 directory on the control host) plus Slurm accounting; there is no
local ledger, reservation, budget or cost cap. Every typed command ships the
registry program to the login node, where the runner validates the record and
the script bytes, takes the registry lock, writes `record.json` before `sbatch`,
claims the logical task marker and records the reply, and where the reader and
the event writer resolve attempts with the same code against a fresh `sacct`
query taken there. Files are create-once and never rewritten; the task marker
is the only file replaced, and only by an admitted retry that names its
`parent_attempt`. A retry is admitted only when the parent is `not_sent`,
`abandoned` or `terminal` without a `COMPLETED` task; absence from accounting
is never release.

Accounting is reconciled per array task and Slurm restart: the highest restart
is authoritative, a task is terminal only with a complete restart history, cost
sums `ElapsedRaw x AllocCPUS` and GPU seconds over complete task-and-restart
groups, and `cost.known` is set only when every task is terminal with complete
accounting and none is missing. On a profile that is not preemptible (every
profile except `owners`) any `PREEMPTED` or `REQUEUED` row, or a restart above
zero, is an anomaly: the job identity is adopted and `status`/`reconcile` exit 2
as `unexpected_preemption` until an operator has investigated and runs
`shk reconcile --attempt ID --acknowledge-preemption`, which writes a per-task
waiver under the registry lock from a fresh query. An attempt with no accounting
rows after 900 s and no known job id is `abandonable`; only
`shk reconcile --attempt ID --abandon`, after a fresh `sacct` and `squeue`
check on the login node, closes it. `reconcile` writes `resolved.json` for
terminal attempts, which bounds `--all` to the open ones.

The workstation keeps only the shared authentication cooldown and a 60-second
query cache (`auth-backoff.json`, `query-cache.json`) under one explicit private
state root; typed commands refuse to run without it rather than fall back to
`$HOME`. Residual limits: `--all` may miss an attempt directory created seconds
earlier on another login node while `--attempt ID` is exact; an array task
never seen in accounting keeps its attempt `identified` with a missing count;
acknowledging a preemption needs the network; a destination fetched by 0.2.0
needs one remote fetch before `fetch --local`.

DTN transfers share the control endpoint's authentication cooldown: an active
cooldown refuses a transfer before rsync starts, and an rsync authentication
failure (exit 255 with an authentication message) arms it. Transfers are not
serialized under the shared lock, so N concurrent fetches may each make one
authentication attempt before the first failure arms the cooldown.

Production installations are frozen revisions with policy SHA256 and schema version.
Development overrides are visibly labeled. Compare the installed revision/policy
with advertised instruction provenance before new managed mutations. Old attempts
remain recoverable under their recorded policy; diagnostics and verified recovery
must not depend on adopting a new policy. A projection is instruction delivery,
not proof that clients loaded it or enforcement against other tools.
