# 0001 — `sherlock-kit` foundation and implementation plan

| | |
|---|---|
| **Status** | IMPLEMENTED, 2026-10-08 UTC. Phases 1–5 accepted; see [implementation evidence](../implementation-status.md). Original baseline and design rationale below are retained. |
| **Decision** | Start with shared Sherlock instructions, a small OpenSSH helper library, a read-only doctor, and tested installation. Extract orchestration only after a real consumer proves the contracts. |
| **Completion target** | Complete Phases 1–5 under the phase gates. Phases 1–2 are the first publishable milestone, not a stopping point. |
| **Initial scope** | `~/data/repos/sherlock-kit` and scoped integration in `~/dotfiles`. Research repositories remain reference sources until a particular pilot is explicitly authorized. |
| **Implementation entry point** | Read the entire plan, then follow the phase gates in §11 and the Git/multi-agent runbook in §13. The reusable launch prompt is in §14. |
| **Publication** | The owner authorizes implementation commits, worktrees, phase merges to `main`, and pushes to their public GitHub `sherlock-kit` repository; exact boundaries are in §13.1. |
| **Verification baseline** | Official documentation, actual local source, and read-only checks on Sherlock on 2026-10-07. Inventory and tool behavior must be rechecked when implementation starts. |

## 1. Problem and minimum useful outcome

About ten research repositories share a local-workstation → OpenSSH → Sherlock Slurm → artifact
transfer workflow. They duplicate transport, operational instructions, resource choices, and
recovery logic. Claude Code and Codex should receive the same Sherlock rules and call the same
installed helper code.

The initial release should remove this duplication without creating a new workflow engine. Its
minimum useful outcome is:

- One reviewed, versioned Sherlock operational policy visible to both agents, including when they
  run locally and operate on Sherlock over SSH.
- A small Python module using the system OpenSSH client, plus `shk doctor` and `shk policy`.
- A reproducible install through dotfiles, with a fixture proving instruction delivery and helper
  behavior for a new consumer.
- Explicit limits: this release does not make existing repository scripts safe by interception,
  provide exactly-once Slurm submission, or replace their journals and scientific validators.

The position that “a roughly 200-line shared module and one shared Markdown file get most of the
value” is persuasive. Use that as the architectural starting point, not a rigid line limit. Tests,
packaging, and installation glue are additional. A general scheduler, plugin, shell parser,
remote daemon, and multi-site framework are not prerequisites.

The full implementation continues from that milestone through one authorized pilot, the shared
orchestration contracts it establishes, and tested opt-in guard/tool adapters. Keeping the first
release small does not authorize stopping the task there. A general shell parser, remote daemon,
multi-site support, and migration of every research repo remain outside the completion target.

### 1.1 Reference implementations, not correctness certificates

Read these implementations before extracting anything:

| Reference | Root relative to `~/data/research/` | Useful existing components |
|---|---|---|
| R | `rasraser` | `src/rasraser/workflow/{slurm,resources,admission,budget,sweep_cost}.py` |
| G | `flexid/gen_compas` | `src/gen_compas_flow/{remote,executors/slurm,transfer}.py` |
| F | `flexid/flexid_vs` | `src/flexid_vs/{backends/slurm,backends/remote,workflow/store}.py` |
| B | `se3diff/bioemu-ft` | DDP job scripts, checkpoint layout and trainer checkpoint resolution |

These sources contain valuable mechanisms and unresolved failure cases. They do not establish
that submission is exactly once, that every fetch is atomic, or that all workloads share one
resource profile. File:line references in §5 are review-time coordinates, not permanent APIs.
Survey counts of scripts or skills are context, not design invariants.

## 2. Scope and authorization boundaries

The foundation changes the toolkit and the existing dotfiles integration only. It does not silently
migrate research repos, rewrite their instructions, alter active jobs, repair historical ledgers,
change SSH authentication, or deploy code to shared Sherlock storage.

“Zero research-repo changes” is appropriate for the first extraction, but insufficient to declare
a general orchestration layer ready. Phase 3 requires one named, authorized pilot with a small
adapter and an end-to-end acceptance report. Until then, describe the result as a foundation.
An isolated consumer fixture inside this repository is necessary for the foundation and does not
substitute for that pilot.

Implementation agents own Phases 1–5 and may publish accepted milestones under §13 without asking
again about ordinary commits, merges, or pushes. They must not treat this plan as authorization
to spend GPU time, cancel/requeue existing jobs, change another person's files, or migrate an
unspecified research repository. Identify a suitable pilot and request its missing scope/resource
authorization early, while continuing independent work. Once that prerequisite is satisfied,
continue through the later phases without asking for a new implementation mandate.

If a pilot, authentication, grant, or required client check is unavailable, report that specific
blocked gate and finish all independent work. Do not silently relabel Phases 3–5 as future work or
claim the entire plan is complete at the MVP milestone. A borrowed-partition grant is not needed
if the selected pilot can use an already authorized ordinary partition.

Existing research instruction conflicts and other source findings are recorded in Appendix B.
Do not turn the extraction into unrelated cleanup.

## 3. Verified constraints and their actual implications

The labels below separate **CONFIRMED**, **WRONG** claims from the earlier draft, and
**UNVERIFIABLE** details. Public docs and the installed site instructions have distinct provenance.
On 2026-10-07, the Sherlock login banner directed automated tools to read
`/etc/agents/AGENTS.md`; that file and its `slurm.md`, `storage.md`, `software.md`, and `policy.md`
companions were read on `sh02-ln04.stanford.edu`. The numbered rules are mandatory; topic guidance
and personal preferences must not all be relabeled as site prohibitions.

### 3.1 Authentication: reuse OpenSSH, retain uncertainty

**CONFIRMED:** Sherlock's public [connection documentation][s-connection] says external SSH
public-key authentication is unsupported. Password/Kerberos and GSSAPI authentication involve
Duo; the documentation recommends multiplexing and describes a 25-hour Kerberos ticket. No
public-key exception for this external-login workflow was found. Internal cluster authentication
and undocumented exceptions were not established.

**WRONG:** this does not prove that all SSH libraries or tools are unusable, or that an already
authenticated connection cannot support unattended work. For example, [rclone supports an external
SSH command][rclone-ssh]. The practical constraint is that a cold connection or reconnection may
require the human to authenticate; the toolkit must never store passwords, MFA responses, or
shared credentials to avoid this. Globus is a documented option for unattended transfer, not a
proven exclusive automation channel or a guarantee that every grant type is available.

**Decision:** invoke the installed OpenSSH client and honor its configuration. Human bootstrap,
login-host transport, and DTN transport have separate health states. Repeated failed authentication
can cause temporary IP blocking; bounded retries and shared backoff are required. Never turn a
campaign outage into a connection storm. See [authentication failures][s-auth].

### 3.2 Scratch lifetime: prohibit purge evasion, not normal writes

**CONFIRMED:** [the filesystem policy][s-filesystems] prohibits artificial lifetime extension on
`$SCRATCH` and `$GROUP_SCRATCH`, including rewriting, copying over, or touching for that purpose,
even once and even if ineffective. Its current warning is:

> Your account will be suspended on the first occurrence, without prior warning.

The 90-day purge uses content modification, tracked independently of the timestamp displayed by
`ls`. Reading, renaming, ownership/permission changes, and `touch` do not reset it.

**Decision:** state this prominently in both agents' instructions; never provide a keepalive or
refresh command. Normal computation, checkpoint writes, and legitimate transfers are not banned.
A command-string hook cannot reliably infer intent or stop arbitrary programs from rewriting
files. Remove the earlier promise of mechanically preventing every suspension-level violation.

### 3.3 Storage placement and node use

**CONFIRMED, with corrections:** the [filesystem documentation][s-filesystems], [storage
index][s-storage], and installed site guide distinguish persistent, shared scratch, and node-local
storage. The following are documented defaults at the verification date, not immutable inventory:

| Storage | Documented baseline | Design consequence |
|---|---|---|
| `$HOME` | 15 GB, snapshots/backups, no scratch purge | Small configuration is appropriate; avoid intensive job I/O. |
| `$GROUP_HOME` | 1 TB, snapshots/backups, no scratch purge | Persistent shared code, environments, and authorized durable data. |
| `$SCRATCH`, `$GROUP_SCRATCH` | 100 TB / 20 M files each; no backup; 90-day purge | Working data only; a sole durable copy must not live here. |
| `$L_SCRATCH_JOB` | Per-job node-local directory | Stage file-heavy working sets; copy required outputs before job end. |
| `$L_SCRATCH` | Same-user node-local storage shared across jobs | Cleanup is tied to the user's last job on the node, not necessarily this job. |
| `$OAK` | Purchased persistent storage; backup is a separate service | Archive according to its supported access/I/O pattern. |

Published scratch fail-safes are 125 TB / 25 M files per user and 250 TB / 50 M per group.
Do not hard-code these as every user's effective allocation, or assume constant node-local capacity.

**WRONG:** all job reads from `$HOME`/`$GROUP_HOME` are forbidden; every `$L_SCRATCH` file disappears
at the end of its creating job; every resolved absolute path is invalid. The installed guide
allows established group layouts and small-file reads. Resolve environment roots on the intended
host, then validate absolute paths and authorized ownership. Storage owner and SSH user are
separate identities; an observed collaborator path is not permission to use that account or tree.

Sustained computation, heavy installation/builds, and intensive internal copies require an
allocation. External bulk transfers use DTNs or Globus. DTNs provide transfer service, not an
interactive command shell, and the site guide says not to SSH to them from Sherlock.
See [data transfer][s-transfer] and [running jobs][s-jobs].

### 3.4 Shell startup hygiene

**CONFIRMED:** slow shell initialization can cause Slurm's `user env retrieval failed requeued
held` state; simultaneous job starts amplify the problem. Avoid expensive filesystem access,
unconditional environment activation, and heavy external sourcing in startup files. Diagnose
without automatically rewriting the user's shell setup or releasing held jobs. Batch scripts must
initialize their required environment explicitly. Source: [storage troubleshooting][s-storage].

### 3.5 SSH aliases and control-master failure

**CONFIRMED locally:** `dotfiles/common/ssh/.ssh/config.d/stanford.conf:1,11,20` defines `sherlock`,
`sherlock-plain`, and `sherlock-dtn`; `lab-ubuntu/ssh/.ssh/config.d/legacy.conf:7` adds an interactive
`RemoteCommand` for `sherlock`. The plain alias avoids the forced interactive setup.

**Decision:** default to `sherlock-plain` and still assert `-T`, `BatchMode=yes`,
`RemoteCommand=none`, and bounded connection and whole-command timeouts. Permit a configured host
alias for users without these dotfiles; do not silently fall back to an interactive alias.

**WRONG:** [`ssh -O check`][ssh-control] verifies the remote connection or cures ambiguous
submissions. It checks the local master; a connection can fail immediately afterward. Use it only
as a diagnostic. A known failure before mutation dispatch is `auth_required`/`not_sent`; loss of
the result after possible dispatch is `unknown`. No automatic resubmission follows an unknown.
A read-only liveness probe must itself have a deadline and does not confer mutation safety.

### 3.6 Site instructions for agents and software

**CONFIRMED:** Sherlock provides coding-agent modules and recommends compute allocations for their
use. The module name is **`pi-coding-agent`**, not `pi`; examples and client flags must be checked
against installed versions. See [coding agents][s-agents], [modules][s-modules],
[software installation][s-install], and [Apptainer][s-apptainer]. The initial architecture keeps
agents on the local workstation.

Carry these additional installed-site rules into the shared operational instructions:

- Explicit job walltime; correct GPU count and GPU partition; discover hardware using `sh_part`
  and `sh_node_feat`. `/etc/agents/AGENTS.md:63` says node exclusion (`--exclude`, `-x`) is not
  supported and directs users to constraints. `/etc/agents/slurm.md:49–50` says Sherlock has no
  `--account`. **Do not require or render either option for Sherlock.** These are supported-site
  usage rules, not a claim about what a generic Slurm parser accepts.
- At least 60 seconds between status checks; prefer job dependencies; no `watch`/shell polling
  loops, duplicate pending submissions, or blind retries of failed jobs. Group short work into
  useful batches of at least about ten minutes, preferably thirty; never pad jobs with sleeps.
- No unattended login-node agent servers, indefinite agent-restart supervisors, or recurring
  future agent sessions. A bounded task or job-monitoring helper does not authorize restarting
  the agent indefinitely. Preserve managed client/sandbox restrictions.
- Access only authorized directories and jobs; no credential/session sharing, authentication
  bypass, system-configuration probing, or broad scans of other users' trees. Cancel Slurm work
  through Slurm rather than killing arbitrary node processes.
- Only Low/Moderate Risk data. Prefer available modules, then suitable existing group
  environments, then an authorized `$GROUP_HOME` environment or container. Check `ml spider`
  before installing; use allocations for heavy builds. Never copy secrets into provenance.

The future Sherlock-specific instruction file should begin with the `/etc/agents/AGENTS.md`
reading requirement and point to the relevant topic guide. Keep source dates and distinguish site
requirements, site recommendations, workload requirements, and the owner's preferences.

### 3.7 Borrowed partitions and workload capabilities

**CONFIRMED snapshot:** `sinfo` on 2026-10-07 reported `btrippe` on `sh04-03n07`, four H100 SXM5
80 GB GPUs, and a seven-day time limit. **UNVERIFIABLE:** who granted this user's access, its
expiry/scope, and a durable non-preemption guarantee. Scheduler visibility is not a grant.

**WRONG:** all existing uses already obey a universal two-GPU/32-task agreement.
`R/src/rasraser/workflow/resources.py:297–311` implements those test limits, while
`flexid/lpla_atlas/scripts/sherlock/submit_chunks.sh:9,19,40–51` describes time-limited borrowed
access and submits four chunks without that shared admission control.

New toolkit-managed borrowed-partition admission stays disabled until its actual grant is
recorded: grantee, allowed use, time window, concurrency/task limits, and evidence reference.
Do not invent the grantor or grandfather a permission from script comments. Historical recovery
and reads remain available. If avoiding a particular borrowed node is necessary, determine a
site-supported positive selection or partition policy; do not reintroduce unsupported `--exclude`.

GPU eligibility belongs to the workload and installed runtime: dtype, memory, compiled CUDA
architectures, kernels, and supported GPU count. Preserve the draft's correction that V100 lacking
native bf16 is not a reason to exclude it from GROMACS mixed-precision MD. A particular GROMACS
build's compatibility still needs validation. CPU work and multi-GPU/DDP work are valid profiles.

### 3.8 Claude Code and Codex mechanics

| Claim | Verification and consequence |
|---|---|
| Claude reads `AGENTS.md` | **CONFIRMED with correction:** supported from 2.1.277; default fallback depends on applicable project Claude instruction files. A global `~/.claude/CLAUDE.md` does not itself suppress project `AGENTS.md`. Use an explicit `@AGENTS.md` adapter where appropriate and test discovery. [Memory][c-memory] |
| Codex instruction precedence | **CONFIRMED:** `AGENTS.override.md`, then `AGENTS.md`, then configured fallback, one selected file per directory, root toward cwd. Here `CLAUDE.md` is a configured fallback. The documented default budget is 32 KiB; documentation differs on the accounting detail, so test the installed version instead of asserting every large `CLAUDE.md` is loaded/truncated. [Guide][o-agents] |
| Both have `PreToolUse` hooks | **CONFIRMED:** a shared decision function is feasible, but tool adapters, registration, trust, and failure behavior require separate tests. Local Codex was 0.160.1. [Claude hooks][c-hooks], [Codex hooks][o-hooks] |
| Hook blocking and trust | Exit-0 `permissionDecision: "deny"` JSON is supported by both. Claude exit 2 blocks with stderr feedback. **Codex does not support `permissionDecision: "ask"`; that hook failure lets the tool continue.** New/changed non-managed Codex hook definitions need trusted current hashes or are skipped. Timeouts/errors are not a portable fail-closed mechanism. |
| Permission matching through SSH | **CONFIRMED:** Claude does not semantically unwrap SSH. Broad `Bash(ssh *)` can allow arbitrary remote commands when allow rules apply. **WRONG:** literal argument restrictions are impossible; they are possible but are not a remote-shell policy evaluator. [Permissions][c-permissions] |
| Local marketplaces | **CONFIRMED:** a local directory and relative plugin paths are supported; in-place edits can reload without a version bump. This is a development mechanism, not an immutable production pin. [Marketplaces][c-marketplaces] |
| Plugin Markdown always loads | **WRONG:** plugin-root `CLAUDE.md` and plugin-bundled rules are not automatic global instructions; skill bodies load on invocation. Explicit instruction delivery is required. [Plugin reference][c-plugins], [skills][c-skills] |
| Codex consumes Claude plugins identically | **WRONG:** Codex has its own installation/discovery paths. `.agents/skills/` and `~/.agents/skills/` support symlinked skills; the shared library/policy can be identical while adapters differ. [Codex skills][o-skills] |

Continuations of an existing interactive shell, including Codex `write_stdin`, are another reason
that a pre-tool command hook is not a complete command-execution boundary.

### 3.9 Dotfiles conventions

Read `~/dotfiles/README.md` and `~/dotfiles/AGENTS.md` in full before implementation. Three
constraints are **CONFIRMED**, but the earlier architectural conclusions were too strong:

1. Live Claude settings and Codex config are regular files, not Stow symlinks. Their portable
   baselines are excluded by `.stowrc:6,13` and merged by existing sync helpers. Hook integration
   must use that mechanism. `lib/config_sync.py:20–39,129–132` currently has no Codex hook key in
   its portable allowlist; adding TOML alone will not install a hook. Its current list validator
   accepts strings, not nested hook objects (`:83`), so future support also needs a real validator.
2. `AGENTS.md:81` excludes third-party skill payloads from dotfiles. This does **not** require a
   plugin: the awesome-skills installer does not delete non-colliding skills by default
   (`awesome-skills/install.sh:24,95–109`; update wrapper `:119–120`). First-party skills can use
   a clearly owned namespace. A source submodule would itself vendor payloads, so the prior
   “no skill payload enters dotfiles” claim was incorrect.
3. `autoMode.classifyAllShell = true` suspends Claude shell allow rules in auto mode; explicit
   ask/deny rules still apply. That is defense in depth, not proof that a hook is complete.

Additional integration constraints: arrays replace rather than append during merge
(`lib/config_sync.py:162–168`); preserve the existing `check-git-hooks` registration. Normal sync
must not rewrite portable sources. Match POSIX/Windows installer validation, preserve file modes,
and avoid colliding common/host-layer Stow targets. Register each hook once per tool, not in both
the baseline and a plugin. `stow-all.sh:154–159` auto-stows common packages, another reason not to
hide a source checkout inside a new common package.

## 4. Architecture: a small foundation, explicit later contracts

The initial package contains a shared operational document and a small module, with ordinary
packaging/tests. The module provides configuration validation, safe OpenSSH command construction,
bounded read-only execution, and doctor/policy entry points. Local execution uses argv rather than
`shell=True`; remote argument serialization still needs correct shell quoting and tests. An argv
interface does not make an arbitrary remote shell string safe. The module does not submit jobs
or inspect arbitrary shell scripts for compliance.

Separate these responsibilities:

| Artifact | Responsibility |
|---|---|
| `SHERLOCK.md` | Canonical operational instructions, sources, and boundaries for humans and agents. |
| `AGENTS.md` and `CLAUDE.md` | Instructions for developing this repository; Claude's adapter can import `AGENTS.md`. These are not automatically delivered to other repositories. |
| `src/sherlock_kit.py` | Small shared helper module; split into a package only when implemented responsibilities justify it. |
| `shk doctor`, `shk policy` | Read-only diagnosis and policy/provenance display; no unimplemented submit/fetch/reconcile commands. |
| `tests/fixtures/consumer/` | A new-repository fixture with fake SSH/scheduler responses and explicit workload configuration. |
| Dotfiles adapters | Deliver a compact shared policy block to each actual global instruction entry point and install the same toolkit revision. |

Start with a standard-library implementation where sufficient. Avoid schema frameworks, policy
code generation frameworks, and a plugin as initial dependencies. A thin command entry point is
useful; a large CLI surface before there is a consumer is not.

### 4.1 Policy identity and limits of generation

`SHERLOCK.md` owns operational prose. Machine-enforced parameters have one typed source and may
render a small marked section; site prose must not be reduced to an inaccurate TOML boolean such
as `HOME.job_io = false`. A check can establish that generated text matches its input. It cannot
prove the implementation enforces it, every client loaded it, or another tool did not bypass it.

Each installed release reports its code revision, policy content digest, and schema version.
Instruction projections record their source digest and are verified during dotfiles checks.
Static site rules, discovered inventory, personal preferences, and private grant/config values
are separate inputs. Do not publish local grants or machine-specific paths in the public package.

## 5. Extraction map and contracts for future orchestration

These are requirements for later extraction, not a demand to implement eight modules in Phase 1.
Read both the cited mechanism and its surrounding lifecycle. Do not copy legacy behavior merely
because it appears in three repositories.

### 5.1 Corrected module-by-module fusion

| Area | Read first | Required correction or missing load-bearing behavior |
|---|---|---|
| Transport | `G/src/gen_compas_flow/remote.py:78,133` | Keep explicit noninteractive options, argument quoting, mutation previews, and deadlines. `executors/slurm.py` runs on the remote side; its `:472` rejection classification must not be transplanted onto an ambiguous SSH exit 255. `-O check` is diagnostic only. |
| Rendering | `R/.../workflow/resources.py:366`; `F/src/flexid_vs/backends/slurm.py:53`; `G/.../executors/slurm.py:95–125` | Use workload capability checks, including compiled CUDA support. F's single safe-character regex rejects the valid OR character `\|` and signal syntax such as `B:USR1@60`; it is not a validator for every directive. Validate each field/constraint grammar. Remove site-unsupported account/exclude defaults. |
| Submission and admission | `R/.../workflow/slurm.py:357,583,605–607`; **`R/.../workflow/admission.py:145–201`**; `F/.../backends/slurm.py:166`; `G/.../executors/slurm.py:467`; `F/.../workflow/store.py:110,269,289` | Keep durable intent and shared admission locking. R's response parsing is not a strict full match; prefer F/G parsing. Unknown attempts consume reservations. Zero scheduler matches never proves non-submission. Resolve uncertainty with identity-bound evidence before retrying; confirmed terminal failures may create new attempts under the workload retry policy. |
| Reconciliation and cost | `F/.../workflow/store.py:373–437`; `G/.../executors/slurm.py:517–561`; `R/.../workflow/sweep_cost.py:137–243` | These already combine scheduler evidence, identity, and output validation: the earlier “none does this” claim was wrong. Add shared query caching, explicit lag/retention uncertainty, array/step/restart accounting, and bounded query scopes. Do not promise one query for arbitrary distinct requests. |
| Transfer | `F/.../backends/remote.py:381–435`; `G/src/gen_compas_flow/transfer.py:418–462,619–729` | F has per-file verified promotion, not bundle atomicity, and compact kinds do not impose byte limits. G is the better bundle starting point, but deleting transfer state at `:441–449` before verification and crashing between promotion and receipt/location updates at `:451–452` need recovery fixes. Use one correctness protocol across sizes. |
| Preemption | `R/.../workflow/slurm.py:304–336`; `B/src/bioemu_ft/trainer/checkpointing.py:249–305`; `F/.../workflow/store.py:440–517` | `resolve_resume_path` is in trainer checkpointing, not `config/exp_layout.py`. Borrow F's task/attempt/protocol/source/runtime checkpoint validation. Checkpoint filenames and generic traps alone are insufficient; scientific payloads need adapters and tested signal/resume behavior. |
| Prologue | `R/scripts/legacy/_common.sh:13–24`; `G/.../executors/slurm.py:301–308`; `G/src/gen_compas_flow/runtime/native.py:11–23` | R's legacy helper uses mutable submit-directory code, `$HOME/miniconda3`, and possibly repo-local caches. Do not copy those defaults. Keep explicit environment/container exports; verify scratch/temp roots and fail if required storage is absent. |
| Budget | `R/.../workflow/budget.py:71–78,118–124,216–247,269–332`; `R/.../workflow/resident.py:190,345` | Preserve the shared absolute ledger, supervisor-lock inheritance, and charging of initialization, failures, idle time, and crash recovery. The clamp applies to capped budgets; unlimited mode and transitions need explicit semantics. A constant alone is not authorization to raise a frozen budget. |

Here `R/.../workflow/` expands to `R/src/rasraser/workflow/`,
`G/.../executors/` to `G/src/gen_compas_flow/executors/`, and
`F/.../` to `F/src/flexid_vs/`. All three reviewed journals use atomic JSON documents, not JSONL.

### 5.2 Submission, concurrency, and crash recovery

The safe promise is **at most one automatic submission attempt until its outcome is resolved**,
not exactly-once execution. A scheduler-visible token helps discovery; it is not a Slurm
idempotency key.

- Record durable intent before dispatch, including logical task identity, attempt UUID,
  code/input/runtime/policy digests, resource request, and ownership. Use atomic replacement and
  appropriate file/directory `fsync`; keep intent after a crash.
- Share admission and budgets outside worktrees. Aggregate a borrowed limit by cluster,
  principal/grant and shared resource across all consuming projects; scope logical task
  deduplication separately by project/campaign/task, and local GPU exclusion by host/device.
  Reserve under a lock before dispatch. Two repositories or worktrees must not each admit the
  full shared grant or duplicate a logical task just because they have separate journals.
- Phase 1 does not implement admission. The first orchestration release may require **one
  authoritative workstation**. Local `flock` does not coordinate two machines; multi-controller
  support requires a shared transactional coordinator and fencing, not an extra local lock.
- Distinguish `not_sent`, `submitting`, `submitted`, proven `rejected`, and `unknown`. Timeouts,
  disconnects, parse failures, and local crashes after possible dispatch become unknown. Release
  an unresolved submission's reservation only with evidence of non-dispatch/rejection or a
  reconciled terminal outcome; ordinary completed attempts release concurrency while retaining
  their measured budget charge. A lost connection is not release evidence.
- A conclusively failed accepted job may create a new attempt after failure investigation and
  under its workload's retry/checkpoint policy. That is distinct from replaying an unresolved
  submission. Link attempts and retain their cumulative budget/restart history.
- Strictly parse `sbatch --parsable` responses. Match attempts through scheduler identity plus
  cluster/user/time/token; array tasks and steps need their own identities. Do not cancel or adopt
  a job merely because its name resembles a worktree prefix.
- `squeue` absence, accounting delay, purged accounting, unsupported comment retention, and
  expired remote receipts are inconclusive. Preserve unknowns and offer an explicit reconciliation
  report. If state is irrecoverable, require a documented human resolution, not an automatic retry.

Never hold an admission lock while awaiting interactive authentication. Keep local state private,
versioned, and on non-purged storage; quarantine malformed state instead of replacing it with an
empty ledger. Recover local monitor processes using PID plus process-start identity, not PID alone.

### 5.3 Scheduler truth, filesystem truth, and policy changes

Maintain separate scheduler, execution-receipt, artifact, and scientific-validation states.
A completed scheduler allocation does not prove valid output; a file's existence does not prove
that this attempt produced it. Durable remote receipts must bind outputs to the immutable
source/runtime/input/attempt identity. Where the sources disagree, retain the evidence and report
an unresolved state; do not silently overwrite the ledger to match whichever source was read last.

Cache equivalent status requests with a defined freshness and scope, honoring the site's minimum
cadence. Cost accounting must avoid double counting array parents, steps, duplicates, and restarts;
record missing or estimated accounting explicitly. Finite retention argues for timely receipts,
not frequent polling.

Freeze effective policy and schema per admitted attempt. New admissions use the current policy;
old attempts must remain readable and recoverable using their recorded semantics. An update or
expired borrowed grant must not strand status/fetch operations. A safety revocation stops new
admission; any cancellation of already running jobs is an explicit authorized action. Do not
reinterpret old resources through a new allowlist, as `G/.../executors/slurm.py:334–355` can do.

### 5.4 Artifact transfer is a recoverable transaction

Use a control host for manifests/receipts and a data host for bulk bytes. DTNs cannot execute the
`ssh cat` control operations used by `G/transfer.py:576–602,627,719`. Resolve and verify the same
remote filesystem namespace for both hosts; never rely on their different default directories.

One protocol should cover both compact and large bundles:

1. Pin an immutable manifest containing relative paths, sizes, digests, and producer identity.
   Validate path containment, symlinks, reserved metadata names, item count, total size, and local
   capacity before transfer. Define real byte limits for compact defaults.
2. Lock the destination and transfer into a private same-filesystem staging directory. Keep
   resumable state until verification and promotion have completed; never expose the staging tree
   as the final result or overwrite unrelated destination data.
3. Verify exact inventory, size, checksums, and a stable source manifest. A partial, corrupt,
   source-mutated, or scientifically invalid bundle is not complete.
4. Atomically promote the complete bundle and durably record its receipt. A crash before/after
   either operation must be recoverable: recognize an already-promoted matching destination and
   finish the receipt idempotently. A mismatching existing destination is a conflict.

Use tested rsync semantics, not separate weak/strong integrity paths based on artifact kind.
Transport checksums and scientific validation are distinct. Secrets, credentials, sockets, shell
history, and unrelated files never enter a default artifact bundle.

### 5.5 Workload, preemption, and budget boundaries

A workload adapter declares launch topology, runtime/environment identity, inputs, expected
outputs, scientific validation, and checkpoint save/restore/validation. One GPU/one rank is an
optional profile, not a universal invariant: `B/scripts/sbatch/repro_fig4_mgnify_ddp.sbatch:8,51–58`
uses two GPUs and `torchrun`.

Jobs execute an immutable, identity-bound source/runtime release, not a mutable mirror or another
agent's worktree. Publish that release before admission and retain it while attempts may need
recovery. A code digest in a ledger is insufficient if the actual job reads changing files.

For a preemptible profile, test signal propagation to the real worker, DDP shutdown, atomic
checkpoint publication, immutable checkpoint generations, and bounded cumulative requeues. Do
not certify resume capability from the presence of `--requeue` or a file named `checkpoint.pt`.
A non-resumable workload needs an explicit suitable execution profile, not a fabricated resume
certificate. Package installation and shared cache population need a single-writer/publish
protocol too; concurrent extraction can corrupt shared environments before a job starts.

Budgets have explicit scope and mode. Admission reserves capacity; charging accounts for failure
and restart paths and reconciles reservation against measured use. Avoid a global “unlimited
Sherlock” policy or a universal local ceiling inferred from one repository's experiment.

## 6. Minimal configuration and policy separation

Do not ship a large speculative policy schema. Initially support host aliases and explicit
workload-independent transport options, plus private local configuration. A future consumer
profile may look like this; it is illustrative TOML, not an implemented schema:

```toml
schema_version = 1

[site]
name = "sherlock"
control_host = "sherlock-plain"
data_host = "sherlock-dtn"

[transport]
connect_timeout_seconds = 15
command_timeout_seconds = 60

[workload]
profile = "ml-bf16-single-gpu"  # Separate profiles for CPU, MD, DDP, etc.
gpu_count = 1
requires_native_bf16 = true

[storage]
working_root_env = "SCRATCH"
node_working_root_env = "L_SCRATCH_JOB"
durable_root_env = "GROUP_HOME"

[borrowed_access]
enabled = false                # A real grant is required before enabling admission.

[budget]
mode = "capped"
# A consumer supplies its explicit ceiling and concurrency limits before admission.
```

Do not put mandatory `--account`, `--exclude`, a global SKU allowlist, credentials, collaborator
paths, or fabricated `authorized_by` values into the site defaults. Workload requirements are
validated against discovered hardware and the installed runtime; a declared dtype is not proof
that every bundled kernel supports a GPU. Grant evidence and private path overrides live outside
the public repository. Existence of a configurable field never constitutes authorization.

## 7. Guard policy: stage deployment, narrow the promise

A narrow guard is a Phase 5 deliverable against recognizable mistakes, after the toolkit has an
actual adopter; its activation is opt-in. It must not globally block existing wrappers for lacking a nonexistent shared journal,
require unsupported site options, or assume it can infer scientific checkpoint validity from a
command string.

Hard-deny proven violations in the typed toolkit API: forbidden site options, an invalid resource
request, unauthorized borrowed admission, replay of an unknown attempt, or a transfer escaping its
allowed destination. For raw tool calls, use a small tested set of unambiguous patterns, such as a
known purge-refresh helper or explicit high-frequency polling. Opaque shell/Python commands are
unknown, not certified safe; broad heuristic denies would break legitimate work without securing
the boundary. Do not automatically rewrite or rerun denied commands.

When implementing Phase 5 hooks:

- Use one pure decision function and separate Claude/Codex input adapters. Common blocking output
  is exit-0 `hookSpecificOutput` with `hookEventName: "PreToolUse"`,
  `permissionDecision: "deny"`, and a useful `permissionDecisionReason`.
- Never emit Codex `ask`. Put any required clarification in an explicit agent/user workflow;
  site-prohibited behavior is not made permissible by an approval prompt.
- No network calls, scheduler polling, or long locks inside the hook. Check runtime availability,
  trust/hash status, and effective registration through doctor and end-to-end client tests.
- Missing, untrusted, timed-out, or malformed hooks cannot be advertised as enforcement. Document
  the observed failure mode for each client, including continued interactive sessions.
- Register once per client through dotfiles. The thin Claude plugin must not duplicate that
  registration and must not become the sole policy-delivery path. Its namespaced skills and the
  Codex skill adapter invoke the same installed toolkit; no orchestration logic belongs in them.

## 8. Dotfiles integration and instruction delivery

### 8.1 Existing ownership stays explicit

| Artifact | Planned owner and delivery |
|---|---|
| Toolkit source | This repository only; no source submodule under a Stow package. |
| Installed helper | One pinned external package installation, independent of Claude/Codex. |
| Global Claude instructions | Existing tracked `common/claude/.claude/CLAUDE.md`, delivered by its existing Stow symlink. |
| Global Codex instructions | Existing tracked `common/codex/.codex/AGENTS.md`, delivered by its existing Stow symlink. |
| Installation pin and policy projections | Small reviewed metadata/generated blocks in dotfiles, derived from the same toolkit release. |
| Live structured settings | Existing Claude/Codex sync helpers and portable baseline allowlists. |
| Phase 5 opt-in hooks/skills | Explicit per-tool installation with a single owner, tested trust, and no duplicate hooks. |

Add a small, marked Sherlock policy block to both **tracked global instruction sources**, preserving
all unrelated content. Generate it from the pinned `SHERLOCK.md` during an explicit reviewed
update, with a source digest. Normal sync/doctor is read-only with respect to the portable sources;
it must not write through the live Stow symlinks or silently rewrite tracked files. Include the
critical rules in the loaded block and a path/command for the full policy. Do not assume a linked
Markdown document or skill body is automatically in either agent's context.

Generate only uniquely marked blocks, not the entire global files: their tool-specific prose may
intentionally differ. First-time insertion is explicit; subsequent updates reject missing,
duplicate, or malformed markers. Compare the generated block bodies and provenance, not whole
file equality. No extra runtime policy materialization or new config-sync engine is needed.

The block applies when working on or connecting to Sherlock, from any host. Test it in clean and
existing-project contexts, including project `CLAUDE.md`, `AGENTS.md`, `AGENTS.override.md`, and a
Codex fallback configuration. Verify the actual instruction files/context each installed client
selects; a successful package import is not an instruction-delivery test.

### 8.2 Sync, host details, and optional remote copies

Keep structured merges in `lib/config_sync.py`. Any future hooks need the Codex portable-key
allowlist, validation of structured hook objects, preservation of existing hook arrays and
host-local registrations, trust handling, and focused POSIX/Windows tests.
Do not use a second JSON/TOML merger. Keep installed package paths out of portable baselines;
resolve them through a stable launcher/config convention and verify the resolved release.

Do not add `sherlock/claude/.claude/CLAUDE.md` or the equivalent Codex target: common and host
Stow packages do not merge file bodies. A short conditional clause in the existing global block
can say to read `/etc/agents/AGENTS.md` when actually on Sherlock; this avoids a target collision.

A remote policy copy is optional and needs an authorized persistent location. It is not required
for locally running agents and must not become an automatic write to group storage. If later
needed, publish immutable versioned content under an authorized `$GROUP_HOME` path. Jobs pin a
version; doctor reports drift without insisting every in-flight job use the newest policy. Never
refresh scratch timestamps to preserve a policy file.

## 9. Distribution and a new repository's first day

Use one reviewed revision or immutable release artifact across hosts. Dotfiles records the pin and
an explicit setup step installs/verifies it; production is not an editable checkout or a moving
branch/tag. Normal Stow, login profiles, and automatic updates must not install dependencies or
require `shk` merely to configure a host. Offline checks can validate committed pin/projection
metadata without it; an explicit toolkit doctor reports its missing/inactive state. A development
override may point to `~/data/repos/sherlock-kit`, must be visibly reported by doctor, and must not
silently affect a running campaign. Avoid a second checkout hidden inside dotfiles.

Pulling dotfiles changes its live instruction symlinks immediately, before a matching executable
is necessarily installed. Make this upgrade gap explicit: doctor compares installed policy/code
identity with the advertised pin; future typed admission blocks new managed submissions on a
mismatch, while diagnostics and appropriate recovery/verified fetch remain available. The policy
bundled with an attempt's installed revision remains authoritative for its recorded semantics.
Activate future hooks only after installation, per-client trust, and a negative blocking smoke
test succeed. Do not call a missing or skipped hook active protection.

A Claude local-directory marketplace remains an optional development convenience for the Phase 5
plugin. It is not needed for the MVP or for Codex parity. Decide package, policy, and
instruction-projection updates together; report mismatched digests before the next mutation.
An old installed revision remains available while its jobs need reconciliation.

A new consumer should be able to:

1. Install the pin and run `shk policy`; both agents receive the compact policy through their real
   global entry points. No research-specific file is required merely to read the rules.
2. Run `shk doctor` locally without contacting Sherlock. It reports package/policy identity,
   instruction/config integration, OpenSSH resolution, and missing optional capabilities.
3. Request a bounded read-only remote diagnostic explicitly. Reuse authenticated connections;
   otherwise return `auth_required` and the manual connection steps. Verify control and transfer
   endpoints separately without shell execution on the DTN. Never run a hidden `sbatch` probe.
4. Exercise the isolated consumer fixture and describe its own source/runtime, resources,
   authorized storage, inputs, outputs, budget, and scientific validator before adoption.
5. For an authorized pilot, use the existing proven workflow through a small adapter. Verify one
   complete submit/status/fetch/result path and simulated unknown/crash paths. There is no
   `shk submit` command until its submission protocol is implemented and accepted.

Doctor output must distinguish unavailable authentication, unsupported capability, configuration
mismatch, and actual policy violation. It must not print full environment dumps, tokens, private
SSH material, or unredacted configuration. Local config/state files use private permissions.

Doctor dispatches a fixed, documented inventory of bounded read operations with validated
arguments, not a user-supplied remote command. Its local argv helper is a transport mechanism,
not proof that an arbitrary command is read-only. For the DTN, use a documented non-mutating
transfer-protocol probe only after establishing support; otherwise report configuration/master
state and **remote capability unverified**. Never improvise a DTN shell probe to make a check pass.

## 10. Validation and acceptance evidence

Tests should exercise boundaries and failure recovery, not mirror implementation line by line.
The foundation's required checks are:

- Pure SSH construction/quoting tests, explicit interactive-option overrides, command deadlines,
  missing binaries/config, malformed output, authentication-required behavior, and no mutation
  from doctor. Use fake executables/responses; ordinary tests require neither network nor GPUs.
- A new-consumer fixture and policy/instruction projection checks. Test precedence with both
  clients' supported versions and record untested clients/platforms honestly.
- Dotfiles sync checks: existing hooks/permissions and local keys survive as intended; array
  replacement is understood; read-only `--check` writes nothing; source symlinks and live config
  modes are preserved; a package/projection mismatch produces a clear diagnostic. Stow and config
  integration tests use temporary homes/targets and cannot retarget the user's live symlinks.
- Packaging/install tests from the pinned artifact in a temporary environment, independent of
  the development checkout. Public examples use synthetic paths/data, never copied credentials.

Before adding shared orchestration, require tests for two concurrent worktrees, controller crash
before/after dispatch, death of the control master mid-submit, strict scheduler parsing, duplicate
logical tasks, accounting lag/retention, arrays/restarts, grant expiry, and policy changes with
jobs in flight. Confirm that unknown outcomes cannot trigger an automatic duplicate submission.

Transfer tests must cover partial/corrupt data, changed manifests, path/symlink escapes, disk-full,
concurrent fetchers, stale stage state, and crashes on both sides of promotion/receipt publication.
Preemption tests must use a workload adapter that actually saves, validates, and resumes state.
Future hook tests must exercise real registration/trust/block behavior in each client as well as
unit-level JSON responses. Live jobs or failure injection against real campaigns require separate
authorization; use deterministic simulations by default.

Record exact commands, versions, outcomes, and limitations in a short implementation status file
when implementation begins. Do not claim a skipped live or client-specific check passed.

## 11. Phases and stopping rules

| Phase | Deliverables | Acceptance gate |
|---|---|---|
| **1 — Foundation** | Source-backed `SHERLOCK.md`, repository developer instructions, small helper module, `shk policy`/local doctor, package and offline tests. | Rules reflect §3, helper is tool-independent, install works, doctor performs no mutations. |
| **2 — Delivery and consumer fixture** | Pinned dotfiles integration, both actual global instruction projections, bounded opt-in remote doctor, isolated consumer fixture and documented day-one path. | Dotfiles checks preserve existing behavior; effective instruction delivery is verified in both clients; foundation release is on `main` and published under §13. If a required client check is blocked, publish independent tested work but mark Phase 2 acceptance pending. |
| **3 — Authorized pilot** | One named research-repo adapter, explicit runtime/resource/output contract, complete result path and recovery evidence. | Named repository and live resource use are authorized; the pilot passes §10. No claim of general orchestration before this gate. |
| **4 — Evidence-driven extraction** | Shared submission/admission, rendering, reconciliation, transfer, and budget behavior needed by the pilot, with tested workload/prologue/preemption adapters and a minimal real CLI (`submit`, `status`/`reconcile`, `fetch`). | The pilot uses the shared path; each capability passes §5's concurrency, crash, and identity contracts; active attempts remain recoverable across updates. No stub commands or unvalidated generic checkpoint engine. |
| **5 — Guard and agent adapters** | Narrow opt-in hooks for both clients, thin namespaced Claude plugin and Codex skills, and documented installation/adoption using the pilot. Additional repo migrations are separately scoped. | Real registration/trust/blocking and false positives tested in both clients; no duplicate hooks or inferred plugin instruction loading; all adapters call the same installed toolkit. |

**Current implementation mandate: complete Phases 1–5.** Publish Phases 1–2 as an intermediate
milestone and continue. A phase gate orders dependent work and establishes evidence; it is not a
request to stop at every milestone. Prepare the pilot choice and exact live-test budget early,
obtain any genuinely missing authorization, and then proceed under the existing full-plan mandate.
Continue independent implementation/tests while a prerequisite is pending, without pretending
the dependent acceptance gate has passed.

Full completion requires one accepted pilot and the toolkit/tool adapters above, not all ten repo
migrations. Reuse the pilot to constrain abstractions and split modules only when needed. Do not
drop a required phase because the work is long; use the committed status record across sessions.
Any proposed scope reduction must be explicit and agreed with the owner, not hidden as an MVP.

### 11.1 Dependencies and alternatives

Use subprocess/OpenSSH for the small helper because it fits the existing authenticated setup.
Do not claim that authentication logically disqualifies every other library. Re-evaluate external
SSH-capable tools when a demonstrated requirement warrants it.

Do not adopt a distributed task framework merely to wrap `sbatch`, but do not dismiss submitit,
Dask, Hydra, SkyPilot, or templates through unverified maintenance claims. A cluster-side adapter
can be legitimate even when the agent is local. Compare maintenance cost and required semantics
against the actual pilot before writing a general scheduler.

A custom ledger is not intrinsically required. Choose atomic JSON, SQLite, or another appropriate
transactional store only after identifying authority, locking, durability, migrations, and recovery
requirements. The three references' atomic JSON choices are evidence to inspect, not a mandate
for an invented JSONL architecture.

## 12. Decisions on the original Q1–Q8

| Question | Recommendation and reason |
|---|---|
| **Q1 — Distribution** | One pinned installation mechanism across hosts; explicit development override. No submodule and no host-dependent production marketplace policy. This avoids duplicate checkouts and makes the code/policy identity inspectable. |
| **Q2 — Alias or overrides** | Both: default to `sherlock-plain`, retain explicit noninteractive OpenSSH options, allow a configured alias. An alias helps ergonomics; options express the transport invariant. |
| **Q3 — Marlowe** | Defer it. Do not add a stub to prove an abstraction. Add another site only with its own verified policy and real consumer. |
| **Q4 — Guard strictness** | Deny proven violations in the typed API; defer broad global shell interception. A narrowly tested optional hook is useful, but cannot infer intent or protect arbitrary wrappers. Never emit Codex `ask`; it is unsupported and can fail open. |
| **Q5 — Is `shk` too ambitious?** | The original all-at-once implementation was too broad. Ship policy + a small OpenSSH library + doctor + delivery/consumer tests first, then finish Phases 3–5 through a real pilot. The roughly 200-line-module alternative is the starting point, not the final stopping criterion; broad multi-site/general-shell abstractions remain excluded. |
| **Q6 — Borrowed access** | Unresolved, not preauthorized. The hardware mapping was verified; the grant was not. Require a real scope/time/limit/evidence record before new toolkit-managed `btrippe` admission. Do not block historical status/fetch. |
| **Q7 — Completeness/logs** | Scheduler state, identity-bound execution receipts, and validated artifacts determine completion together. Logs are diagnostic evidence. Scientific success predicates and checkpoint validity belong to workload adapters; no generic SIGTERM/OOM regex certifies success. |
| **Q8 — Slurm version/JSON** | Read-only `scontrol --version` returned **25.11.8** on 2026-10-07. The earlier ≥25.11 JSON threshold was false; JSON support existed years earlier. Probe needed commands/fields and test fixtures by capability; retain an explicit parsable fallback where supported. Version alone does not settle schemas or accounting comment retention. |

## 13. Implementation runbook for Codex and Claude Code

This section is the execution protocol for the next implementation agent. Keep work moving until
the current phase's gate is met; use commits and a concise status record so a new session can
continue without reconstructing a long conversation.

### 13.1 Granted actions and boundaries

When the owner launches implementation using §14, the agent is authorized to:

- Implement Phases 1–5 in `sherlock-kit` and scoped dotfiles integration; run relevant local tests
  and read-only diagnostics; delegate bounded work to multiple agents. Select/propose the pilot
  early and obtain its particular repository/live-resource authorization before those actions.
- Initialize Git in this directory if it is not already a repository; use `main`, task branches,
  separate worktrees, small coherent commits, local phase merges, and ordinary non-force pushes.
- Publish this toolkit to the owner's **public** GitHub `sherlock-kit` repository. Inspect the
  authenticated GitHub identity and existing remotes first. If that user's `sherlock-kit` does not
  exist and the owner is unambiguous, creating that exact public repository is included. If it
  exists, reconcile with its history; never replace it with an unrelated initial history.
- Commit and push the scoped dotfiles changes to its existing owner-controlled origin, preserving
  its current visibility and repository policies. `main` is the toolkit integration target; use
  dotfiles' existing default branch. Honor protected-branch requirements with a PR if necessary,
  and do not bypass required checks or reviews.

This authorization does not permit force pushes, history rewriting, changing another repository's
visibility, publishing research code/data or private configuration, deleting unmerged work, or
altering active campaigns. Never commit credentials, SSH sockets/keys, tokens, private grant
evidence, `.env` files, machine-local state, or copied scientific datasets. Public release permission
is not proof that private reference code is redistributable: inspect licensing/ownership before
copying code; independently implement small mechanisms when reuse rights are unclear. Preserve
third-party notices where reuse is permitted and do not invent a project license for the owner.

Do not ask again for routine commits, merges, or pushes covered above. Ask only for an ambiguous
remote/owner, a protected action requiring an external decision, or a genuinely missing pilot/grant
boundary; meanwhile complete work that does not depend on it. Record any actual automatic approval
rejection with its action and reason. This plan-editing session itself does not initialize Git,
create a repository, or push anything.

### 13.2 Bootstrap and inspect before writing

1. Read this entire plan and applicable repository instructions. Re-read dotfiles' own README and
   AGENTS before editing it. Inspect files, `git status`, relevant diffs, remotes, current branch,
   worktrees, and any existing implementation/status file. Existing changes belong to the user
   unless clearly made by this task.
2. At review time this toolkit directory had no `.git`. Check again rather than assuming. If it
   is uninitialized, inspect the intended GitHub repository before choosing between importing its
   existing history and initializing a new local `main`. Never clone over populated files, stage
   the entire home directory, or overwrite a changed plan.
3. If both local and remote histories already exist, fetch and compare them. Merge compatible
   progress normally. An unrelated history, ambiguous owner, or unexplained user work needs a
   specific resolution; do not force push, reset, stash, amend, or rebase user work.
4. Create a short `docs/implementation-status.md` with the current phase, accepted contracts,
   task ownership, branches/worktrees, checks, source/tool versions, and unresolved gates. Keep
   secrets and host-specific runtime state out. The lead owns this file and this plan.
5. Establish package/API and integration contracts before dispatching write tasks. Use the
   smallest repo-native tooling; do not introduce a framework merely to run this implementation.

### 13.3 Parallel work with one writer per checkout

The lead agent is the integrator. Only it updates the canonical toolkit checkout, merges into
`main`, publishes, or changes shared coordination documents. Each implementation sub-agent gets a
separate branch **and** worktree, a bounded deliverable, owned paths, agreed interfaces, and checks.
A branch alone does not isolate two writers in the same directory. Read-only reviewers may share
access but must be instructed not to modify files.

If the canonical checkout has unrelated edits or is owned by another active writer, use a separate
lead integration worktree while preserving it. Do not switch its branch or make it clean by
discarding/stashing work. Return the canonical checkout to `main` only when that is safe; otherwise
report the specific handoff conflict as an incomplete final-state step.

Example branch/worktree names, adjusted if already in use:

```text
main                                      lead-owned canonical checkout
impl/p1-policy                            ../sherlock-kit-worktrees/p1-policy
impl/p1-transport                         ../sherlock-kit-worktrees/p1-transport
impl/p2-consumer-tests                     ../sherlock-kit-worktrees/p2-consumer-tests
impl/sherlock-kit-integration              separate worktree of ~/dotfiles
```

Suitable independent tasks after interfaces are fixed: policy/source audit, helper implementation,
consumer/failure fixtures, and dotfiles integration. Do not give two agents ownership of the same
file, package manifest, lockfile, or API contract. Start dependent tasks after their prerequisite is
merged, or provide a precise agreed base commit. Use the available agent slots, not an arbitrary
large fan-out. If sub-agents are unavailable, execute these bounded tasks sequentially with the
same branch/test discipline.

Worktrees isolate files, not the real home directory or global package installation. Sub-agents
must test Stow/config generation in temporary homes/targets and install packages into isolated
environments; they must not run a worktree's installer against the user's home. Only the integrator
activates reviewed configuration from the canonical checkout. Because the existing global
instruction symlinks point into dotfiles, even merging those source files changes live policy;
coordinate that merge with the explicit package activation and version checks in §9.

Each sub-agent returns its branch/commit IDs, exact changed paths, checks and results, known
limitations, and handoff notes. It does not merge to `main`, push, or edit another agent's checkout.
Resolve integration conflicts in the lead-owned branch/worktree, preserving both intended changes
and rerunning the checks affected by the resolution. Do not recreate another agent's finished work.

### 13.4 Commit, validate, and merge at phase boundaries

Commit each coherent, reviewable unit once its focused checks pass; do not accumulate the whole
implementation into one final commit. Before every commit inspect the diff and stage explicit
paths or hunks. Do not use blanket `git add .`, commit secrets/unrelated user work, or add AI
attribution / `Co-Authored-By` trailers. A commit should describe the behavior and relevant reason.

For each completed phase:

1. Collect all task reports; inspect diffs against their agreed base and obtain a read-only review
   focused on correctness, concurrency, site rules, and integration risk.
2. Merge prerequisite branches in order into the lead-owned integration target, without rebasing
   or discarding other agents' work. Choose a normal merge/fast-forward consistent with repository
   conventions; a merge commit is useful when it preserves a meaningful multi-commit task.
3. Run the phase acceptance checks on the **combined merged revision**, not only on individual
   worktrees. Resolve failures and commit the fix before treating the phase as complete.
4. Update the status file with evidence and limitations, commit it, and place the accepted phase
   at toolkit `main` HEAD. If concurrent changes arrived on the remote, fetch/merge and rerun the
   affected checks before a non-force push. No untested “fix during push” path.
5. Push the accepted phase rather than waiting until the entire plan is done. For cross-repository
   integration, publish the toolkit revision first, then commit/test/push the dotfiles pin that
   references it. Never publish a pin that cannot be fetched. Avoid a circular dependency between
   the toolkit revision and the dotfiles status record.

Cross-repository publication is not atomic. If the second push fails, report the exact toolkit
and dotfiles revisions and pending action; the already-published toolkit must remain usable by
itself. Do not roll back unrelated remote changes to simulate atomicity.

A failed or interrupted phase leaves an honest committed status checkpoint where appropriate;
do not label it accepted or merge broken implementation merely to create a checkpoint. Before a
long session ends, record the next concrete step so another agent can resume from existing commits.

### 13.5 Public publication and final state

Before every public push, inspect the actual staged/tracked content and newly reachable commits
being published for credentials, private paths/configuration, research data, and unlicensed copied
payloads. The first push requires checking the entire history being made public.
Use existing secret checks if available and inspect examples/fixtures manually; do not upload local
logs to a third-party scanner. Runtime/private configuration belongs in ignored files outside the
release. Ensure `.gitignore` covers generated environments, caches, state, and worktrees.

Do not silently create a second repository, retarget `origin`, change visibility, or bypass branch
protection to make a push succeed. When a required external review remains pending, keep the tested
branch/PR and report that main publication is incomplete.

The foundation milestone is accepted when Phases 1–2 pass. **The whole plan is complete only when:**

- All five phases meet their acceptance gates, including the authorized pilot, shared
  orchestration/recovery tests, and both clients' real instruction/hook/skill delivery. An
  unavailable required check leaves acceptance pending, even if the independent implementation
  has been published. Distinguish implemented, verified, and published status explicitly.
- All accepted toolkit implementation is committed and reachable from `main` HEAD; the canonical
  toolkit checkout is on `main`, with no task-owned uncommitted implementation stranded elsewhere.
- The public toolkit origin contains that main revision, and the scoped dotfiles change/pin is
  integrated and pushed to its existing default branch. A protection/access blocker leaves that
  publication incomplete; reporting it does not itself satisfy the gate.
- Reinstallation from the published pin works independently of a development worktree. The final
  report names toolkit/dotfiles revisions and the pilot's separate revision/report, checks, and
  any remaining limitations. A blocked required phase leaves the full plan incomplete even when
  earlier accepted milestones have been published.

Remove only the current task's clean, fully merged worktrees/branches after verifying they contain
no unique commits or untracked/user files. Keep uncertain or unmerged work. Never use forced
cleanup to achieve a cosmetically clean status. Retain the canonical repository and main history.

### 13.6 Suggested model allocation (advisory, 2026-10-07)

Default to **GPT-6.1 Sol with high reasoning** for the lead and implementation workers. For this
plan, use an independent **GPT-6 Astra high** reviewer for submission uncertainty, concurrency,
artifact promotion/recovery, and final phase acceptance; use `xhigh` for a specific difficult
protocol or unresolved failure, rather than increasing every routine task to the maximum.
If only one model is convenient, Sol high is the recommended starting configuration. If maximum
quality takes priority over cost, Astra high can lead while Sol high implements bounded subtasks.

This is a task-specific engineering recommendation, not a measured benchmark of this repository.
The current official model pages describe [Sol][o-sol] as near-Astra capability at lower cost and
[Astra][o-astra] as the most capable model for demanding work. Local Codex model metadata confirmed
both offer high/xhigh. Model availability and Codex usage limits must be checked in the actual
client; API token prices are not a measurement of this user's Codex quota consumption.

Choose model/effort in the client or a supported explicit sub-agent override. Prose in a prompt
does not guarantee a running agent can switch its own model. Keep these choices advisory rather
than a dependency of toolkit runtime or CI. No model tier replaces tests, independent review,
or the requirement to continue through the agreed phase gates.

## 14. Reusable implementation launch prompt

The owner can paste this into a new Codex or Claude Code session:

```text
Implement the full plan. Read the entire file before acting:
/home/fridrichmethod/data/repos/sherlock-kit/docs/plans/0001-sherlock-kit-foundation.md

Follow §11's phase gates and §13's implementation runbook. Complete Phases 1–5; Phases 1–2
are the first publishable milestone, not the stopping point. Do not merely propose another
plan. Read applicable AGENTS.md/CLAUDE.md files and dotfiles'
README.md/AGENTS.md, inspect existing status/diffs/remotes, and resume any existing work.

I authorize scoped changes to sherlock-kit and its dotfiles integration, Git initialization
if needed, multiple agents in separate branches/worktrees, timely coherent commits,
phase merges to toolkit main, and normal non-force pushes. Publish sherlock-kit to my
public GitHub repository; if it does not exist, create it under my unambiguously verified
account as specified in §13. Push dotfiles only to its existing owner-controlled remote
without changing visibility. Do not ask again for these routine authorized actions.

Use GPT-6.1 Sol high for implementation if available, and GPT-6 Astra high for an independent
review of the critical submission/concurrency/recovery paths. Follow §13.6 when model overrides
are unavailable; do not pretend a prompt alone switches the running model.

Keep one writer per checkout. The lead owns integration and main; delegate bounded,
independent tasks, review their results, and run acceptance checks on merged code.
Preserve unrelated changes. No force push, history rewrite, secrets/private data publication,
or unlicensed copying from research repositories. Verify current official Sherlock rules;
treat the plan's dated facts as evidence to recheck, not permanent inventory.

Select a pilot and propose its exact repository changes and live-test resource budget early;
obtain the missing authorization before modifying research repos or using those resources.
Do not cancel/requeue existing campaigns or assume borrowed partition access. Continue all
independent work while waiting, and resume dependent phases once authorized. A blocked pilot
does not turn MVP completion into full-plan completion or justify abandoning later phases.
Update docs/implementation-status.md with checks and handoff state. Finish all accepted phases
committed at main HEAD and pushed; report repository URLs, commit IDs, pilot evidence, test
results, and any actual unmet acceptance gate. Never silently reduce the task to Phases 1–2.
Communicate in Chinese, keeping standard technical terms in English.
```

## Appendix A — configuration differences to preserve

Do not mistake today's duplicated values for site invariants. Relevant consumer differences are:
remote layout and authorized storage owner; local/controller identity; runtime source snapshot;
control/data endpoint; grouping (array, bundle, independent); task granularity; CPU/GPU topology;
walltime and memory requirements; partition/grant scope; workload GPU capabilities; environment
bootstrap and container/module versions; output/log placement; checkpoint protocol; budget and
concurrency scope; and scientific success criteria. Site-unsupported flags are not knobs just
because legacy code contains them.

## Appendix B — corrections and separate follow-up findings

1. **Retraction preserved: V100 is not intrinsically an MD bug.** The examined MD script loads
   GROMACS 2025.2 (`turboid/md/charmmgui/protenix/TurboID-biotinolAMP_sample_0/mdrun.sbatch:13,27`).
   Its mixed-precision workload does not require native bf16. Removing V100 solely by copying an
   ML inference policy would be unjustified. Validate the actual CUDA/GROMACS build separately;
   do not promise every build runs on every listed GPU. See [GROMACS installation][gromacs].
2. The review found the same Apptainer digest hard-coded in several code/config/handoff locations.
   Consolidating immutable runtime identity may be useful during a scoped consumer migration;
   do not update research files as part of this foundation.
3. **Correction: missing `--account=ayting` is not a defect.** The installed Sherlock agent guide
   explicitly directs the supported no-account workflow. Do not add that option across repos or
   probe account associations to justify it. Legacy occurrences need a separate reviewed migration.
4. **Retraction preserved: `gen_compas::_walltime` handles >99 hours.** Python `:02d` is a minimum
   width; `100` renders as `100`, and the tested function produced `100:00:00`. No replacement is
   justified by that allegation.
5. A collaborator scratch path in bioemu-ft demonstrates layout variation, not permission to log
   in as that collaborator. Keep authenticated user, storage ownership, and access authorization
   distinct. Do not copy another user's path into public defaults.
6. Exclude generated worktrees, nested test outputs, and scratch fixtures when surveying source.
   The large fixture trees under rasraser distorted simple file-count claims.
7. Existing research instructions can disagree. Rasraser's CLAUDE/AGENTS experiment descriptions
   need a separate scoped reconciliation. Large-file loading needs actual precedence checks:
   `turboid/turboid_analysis/AGENTS.md` is selected by Codex ahead of its larger `CLAUDE.md`, so the
   earlier claim that Codex necessarily truncates that CLAUDE file was false. Proteomics' larger
   fallback document is a separate budget concern to test with the installed client.
8. An unrelated MPI-oriented Slurm skill is not a Sherlock policy source. No global skill removal
   or third-party installer change is necessary for this foundation.

## Appendix C — evidence and verification record

Public primary sources below were checked during the 2026-10-07 review. Installed-tool details
can change. Recheck the relevant page/source when implementing an affected behavior.

- Sherlock: [connection][s-connection], [authentication failures][s-auth],
  [filesystems][s-filesystems], [storage][s-storage], [data transfer][s-transfer],
  [running jobs][s-jobs], [submission options][s-submission], [scheduling][s-scheduling],
  [GPU guide][s-gpu], [coding agents][s-agents], [modules][s-modules],
  [installation][s-install], [Apptainer][s-apptainer], [concepts/data risk][s-concepts].
- Installed Sherlock instructions: `sherlock:/etc/agents/AGENTS.md`, `slurm.md`, `storage.md`,
  `software.md`, `policy.md`; read on `sh02-ln04.stanford.edu`. No public mirror was established.
  These references are remote source locations, not files vendored into this repository.
- Claude: [memory][c-memory], [hooks][c-hooks], [permissions][c-permissions],
  [plugins][c-plugins], [marketplaces][c-marketplaces], [skills][c-skills].
- Codex: [AGENTS guide][o-agents], [hooks][o-hooks], [skills][o-skills]; local package version
  0.160.1 at review. Verify actual hook trust and instruction limits against the installed client.
- Other primary sources: [OpenSSH master commands][ssh-control], [rclone external SSH][rclone-ssh],
  [Slurm accounting][slurm-sacct], [Slurm 2021 JSON presentation][slurm-json],
  [GROMACS 2025.2 installation][gromacs].
- Local sources: the reference paths in §1/§5, `~/dotfiles/README.md`, `~/dotfiles/AGENTS.md`,
  `lib/config_sync.py`, `.stowrc`, `stow-all.sh`, the existing global instruction sources, and the
  SSH fragments in §3.5. Source coordinates refer to the inspected working trees.

Read-only Sherlock checks reused an authenticated master: `hostname`, `scontrol --version`, and
`sinfo -h -p btrippe -o '%P|%N|%G|%l|%a|%f'`, plus reads of the installed agent guides. They confirmed
the login host, Slurm 25.11.8, and the partition inventory in §3.7. They did **not** establish a
borrowed-access grant, execute a job, prove JSON parser compatibility, test DTN shell access, or
validate a live transfer/checkpoint recovery path.

[s-connection]: https://www.sherlock.stanford.edu/docs/advanced-topics/connection/
[s-auth]: https://www.sherlock.stanford.edu/docs/getting-started/connecting/#authentication-failures
[s-filesystems]: https://www.sherlock.stanford.edu/docs/storage/filesystems/
[s-storage]: https://www.sherlock.stanford.edu/docs/storage/
[s-transfer]: https://www.sherlock.stanford.edu/docs/storage/data-transfer/
[s-jobs]: https://www.sherlock.stanford.edu/docs/user-guide/running-jobs/
[s-submission]: https://www.sherlock.stanford.edu/docs/advanced-topics/submission-options/#node-selection
[s-scheduling]: https://www.sherlock.stanford.edu/docs/advanced-topics/scheduling/
[s-gpu]: https://www.sherlock.stanford.edu/docs/user-guide/gpu/
[s-agents]: https://www.sherlock.stanford.edu/docs/software/ai/coding-agents/
[s-modules]: https://www.sherlock.stanford.edu/docs/software/modules/
[s-install]: https://www.sherlock.stanford.edu/docs/software/install/
[s-apptainer]: https://www.sherlock.stanford.edu/docs/software/containers/apptainer/
[s-concepts]: https://www.sherlock.stanford.edu/docs/concepts/
[c-memory]: https://code.claude.com/docs/en/memory
[c-hooks]: https://code.claude.com/docs/en/hooks
[c-permissions]: https://code.claude.com/docs/en/permissions#wrappers
[c-plugins]: https://code.claude.com/docs/en/plugins-reference#standard-layout
[c-marketplaces]: https://code.claude.com/docs/en/plugin-marketplaces#test-an-edit-to-a-plugin
[c-skills]: https://code.claude.com/docs/en/skills
[o-agents]: https://learn.chatgpt.com/docs/agent-configuration/agents-md
[o-hooks]: https://learn.chatgpt.com/docs/hooks
[o-skills]: https://learn.chatgpt.com/docs/build-skills
[o-sol]: https://developers.openai.com/api/docs/models/gpt-6.1-sol
[o-astra]: https://developers.openai.com/api/docs/models/gpt-6-astra
[ssh-control]: https://man.openbsd.org/ssh.1#O
[rclone-ssh]: https://rclone.org/sftp/#sftp-ssh
[slurm-sacct]: https://slurm.schedmd.com/sacct.html
[slurm-json]: https://slurm.schedmd.com/SLUG21/REST_and_Containers.pdf
[gromacs]: https://manual.gromacs.org/documentation/2025.2/install-guide/index.html#cuda
