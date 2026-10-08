# Implementation status

Updated 2026-10-07 (America/Los_Angeles). Mandate: Phases 1–5, with independent phase acceptance.

## Bootstrap evidence

- Canonical toolkit had only the revised plan and no Git history. GitHub authenticated
  owner is FridrichMethod; that owner's sherlock-kit repository was absent.
- Dotfiles main is clean at 84e1d8a; origin is the owner's existing dotfiles repository.
- Rasraser main is 6b35a75 with user-owned CLAUDE changes and a deleted historical
  instruction file. Preserve them and all ten immutable historical source worktrees.
- Rasraser current handoff says science is delivered and controller restart unsafe.
  Pilot must use a new task identity, run directory and result namespace; no historical reruns.
- Python 3.14.8, Codex 0.161.0, Claude 2.1.293, OpenSSH and rsync are available.
  Native PowerShell is not available locally. Sandbox execution fails at bwrap loopback;
  approved direct escalated commands are used without altering sandbox configuration.
- Sherlock site guides were read through existing authenticated OpenSSH on 2026-10-07;
  public filesystem/connection/job documentation was rechecked. No borrowed grant inferred.

## Ownership and contracts

Lead owns canonical main, status, plan, integration, pilot orchestration and publication.
Independent Astra high reviewer is read-only. Implementation workers use Sol high.
Foundation interface: `sherlock_kit.TransportConfig`, `ssh_argv(config, argv)`,
`run_remote(config, argv, mutation=False)`, `policy_identity()`, `policy_text()`,
`policy_projection()`, `main()`; results expose status/stdout/stderr/returncode.
The transport never automatically retries mutations. Typed orchestration will live
in separate modules only after the pilot contract is established.
Use one authoritative workstation and private durable state outside worktrees.
Instructions use uniquely marked `SHERLOCK-KIT` blocks with policy digest/schema.

## Phase gates

| Phase | Implemented | Verified | Published |
|---|---|---|---|
| 1 foundation | e7cd5d9 merged; provenance fix 446549f | 15 offline/install tests and independent review; accepted | public main f91b68b |
| 2 delivery | dotfiles a5536d6 / 298f14a, canonical aab401c | actual Codex and Claude context probes; merged checks PASS; accepted | toolkit public; dotfiles push follows integration |
| 3 rasraser CPU pilot | local b0d22bc / ecd6be2 / 97b00e2 / 8e39a79 | real sealed audit, SIGTERM/resume and local shared CLI; first live job FAILED duration gate | local commits only |
| 4 shared orchestration | 1d7f880 / 6890a19 / fa85201 / 3f02864 | 68/68 merged tests, frozen wheel/install and Astra boundary review PASS; live fetch gate pending | public main 3f02864 |
| 5 opt-in guard/adapters | toolkit 7078fea / e4cd633 / 4dde513; dotfiles 000798f merged 32e6379 | both actual clients trusted deny/benign, namespaced skills and actual failure-mode observations PASS | public toolkit; final dotfiles publication pending, production activation opt-in |

## Next actions

First Job 46946104 resolved FAILED, with 256 allocated CPU-seconds and reservation
released. Reviewed manual child Job **46948346** is submitted, attempt
**9a801032ba354c27985b00801d5d5e4c**, within the recorded two-attempt ceiling.
Wait for terminal identity and fetch; retain both immutable releases and failure
evidence. All Codex failure-mode observations are complete. Merge/push dotfiles
final delivery only after concurrent canonical write ownership is resolved.
Independent final branch has commit 1703f0e. Another Claude process is confirmed
in canonical dotfiles, with ongoing unrelated README/fcitx5 modifications; preserve
them and user commit e092f49. No two agents may write that checkout concurrently.
Do not duplicate the pending/running job, rerun science, restart a historical controller,
reset a ledger, or infer a borrowed grant. Keep the old frozen runtime for this attempt.

## Foundation review

Independent Astra review accepted foundation core. Fixed archive/sdist provenance
when extraction occurs inside an unrelated Git repository; packaging must identify
the source root rather than inherit the outer checkout revision.
Candidate protocols have 15 submission and 11 real-rsync tests, but Phase 4 remains
pending pilot adoption and further review. They are not in the foundation release.

Foundation combined validation: 14 foundation tests + 1 verified archive/wheel/isolated
venv installation PASS on main 446549f. No test required network or GPU.
Dotfiles glue a5536d6 is isolated; 10 integration and 63 config-sync tests PASS.
Its pre-commit gate passed after public pin generation. Canonical installation from
the public frozen pin works independently of development worktrees; live Stow links
resolve to canonical dotfiles. An independent temporary installation also passed.

## Delivery evidence

- Codex 0.161.0 actual `debug prompt-input` selected the global policy in clean,
  project AGENTS, AGENTS.override and explicit CLAUDE fallback contexts. Expected
  project sentinels were visible and override precedence was observed.
- Claude Code 2.1.293 actual authenticated tool-free print requests in synthetic
  temporary contexts selected global rules: status cadence 60 seconds, no account,
  no exclusion. Clean, project CLAUDE, project AGENTS and override-directory probes
  all passed; the installed Claude selected AGENTS over AGENTS.override in that last
  fixture. Claude strips Markdown comments from model context, so the provenance
  digest comment is verified on disk rather than claimed model-visible.
- Raw private client receipts remain in `/tmp/sherlock-kit-client-delivery`; only
  synthetic fixture context and loaded global instructions were sent. Automatic
  review initially rejected context export; the user then explicitly authorized it.
  Empty setting-sources disabled instruction discovery. One user-settings probe
  loaded the existing large skill catalog and spent $1.098076 before its $0.50
  budget stop; the CLI limit does not cap a single request in advance. Subsequent
  instruction probes disabled unrelated skills/MCP and used the Haiku alias,
  approximately $0.005 each, with successful results.
- `shk doctor` passed installed/advertised identities and both projection checks.
  Explicit remote doctor passed control context/site-guide/version inventory;
  transfer master is available but DTN protocol capability remains unverified.
  No shell was run on a DTN and no Slurm probe was submitted by doctor.

## Protocol and pilot evidence

Shared engine uses private SQLite admission, one durable dispatch claim, immutable
script snapshots, strict scheduler identity parsing, conservative costs and unknown
reservations. Conflicting successful job IDs quarantine the whole resource scope.
Fetch persists the full manifest, verifies exact inventory and pinned validator
source snapshots, then atomically promotes and publishes a durable receipt.

`SHERLOCK_KIT_BUILD_PYTHON=/tmp/sherlock-kit-build-venv/bin/python python3 -m unittest
discover -s tests -q` on committed main 6890a19: **64 tests PASS, no skips** in 12.891s.
Includes real multiprocessing/process death, real rsync/concurrent fetches,
promotion crash recovery, CLI offline recovery, stale .pyc/path replacement,
remote symlink containment, accounting high-water and conflict-quarantine tests.
Independent Astra review accepted the four final boundary fixes for this explicitly
limited CPU profile; this does not certify live Slurm behavior or general DDP.

Rasraser b0d22bc: one real local audit checked 265 sealed files / 1,706,395,112 bytes
in 2.32s, max RSS 30,744 KiB; report SHA256
`5dd3a5f939234af7d66aab7da09550d7805d7df71d521a2336625cdbd1388c03`.
Independent synthetic worker SIGTERM published a checkpoint and no final report;
resume verified the prefix and finished. Existing environment `make check` passed:
ruff 659 files, mypy 229 sources, pytest 2974 passed / 4 existing skips (279.20s).
These are actual local evidence, not a live submit/reconcile/fetch claim.
After the standalone frozen-validator and real local CLI adapter ecd6be2, the existing
`make check` passed ruff 661 files, mypy 230 sources and **2982 passed / 4 existing skips**
(280.53s). Subsequent parent-directory fsync and composite-validator commits passed
27 focused tests, ruff, mypy and required pre-commit checks. Canonical research science,
evaluation criteria, historical results/controllers/ledgers and user changes are untouched.

Actual SIGTERM/resume evidence is `/tmp/shk-real-signal-v2-thoummre/receipt.json`:
first PID 546130 has boot/start-ticks/argv/source identity, SIGTERM exit 1 preserved
one verified prefix, and the same frozen plan resumed successfully over two synthetic
files / 269,484,032 bytes. Astra checked this receipt independently. Real frozen CLI
local lifecycle evidence is `/tmp/shk-rasraser-local-protocol-v2/local-protocol-receipt.json`;
its acknowledgement/accounting are synthetic and its real rsync/promotion recovery
does not substitute for the live scheduler gate.

Merged public 3f02864 passed **68/68 toolkit tests, no skips** (14.796s), including
the independent archive/wheel/venv installation and four bounded stress tests.
The eight-family stress fixture uses real forks, SQLite transactions, fsync, rsync,
process deaths and promotion races. Seeds/case identities/hash-chain/invariants are
recorded. The local 30-second measurement completed 204 cases, 36 real process deaths
and 29 semantic buckets in 30.11036s. Revisited buckets test varied interleavings;
they are not counted as new semantic coverage. No sleep or scheduler polling occurs.

## Live pilot checkpoint

Budget was saved before dispatch: **one normal job, 1 CPU, 2048 MiB, zero GPUs,
1800-second walltime, concurrency one**. A manual ceiling of two attempts/3600
allocated CPU-seconds does not authorize retrying an unknown outcome. Work is one
600-second varied protocol verification followed by one read-only sealed 265-file
provenance audit (1,706,395,112 bytes), with scientific_acceptance=false.

Private namespace `sherlock-kit-audit-20261007-shared-v1` uses a new owned GROUP_HOME
release/run directory and new workstation result/controller namespace. Resolve roots
on the intended host; ownership/canonical paths and DTN/control namespace were verified.
Only private runtime state contains concrete storage paths. Official `devel python/3.14.2`
module was checked via `ml spider`, explicitly initialized and its executable hashed.
Twenty-four release files were uploaded via DTN and checked via the control host;
release files are sealed. File-heavy verification executes in L_SCRATCH_JOB and
required results are exported before the allocation ends. Slurm working directory and
job-ID stdout/stderr are explicitly isolated from HOME and immutable source.

Exactly one real `shk submit --config PRIVATE/controller.json --spec PRIVATE/attempt-spec.json
--apply` succeeded. Attempt **579e7b497b4f494dbf40d02512b414cd**, job **46946104**,
created 2026-10-08 00:36 UTC. Shared scheduler query first observed PENDING, then
RUNNING around 00:42 UTC with identity-bound DBIndex 9797163603423974400. Reservation
remains one and cost remains unfinalized. Queries are separated by at least 60 seconds.
At 00:51 UTC, shared reconciliation resolved FAILED and complete final accounting:
256 CPU-seconds, zero GPU-seconds, reservation zero. Private bounded failure reads
showed the sole acceptance failure: stress finished 10,000 cases in 238.895663s,
stop_reason=case_limit, with 1,687 real process deaths and 29 semantic buckets.
All generated invariants passed, but this is **not** a 600-second accepted soak.
The composite validator correctly refused it. The separate audit completed once;
no final bundle was promoted. A manual child will use a fresh immutable release/run
namespace, preserve parent lineage and reuse only the four completed audit metadata
files after exact frozen-validator and SHA256 checks. It will not rerun the audit.

The manual child was actually submitted once at 2026-10-08 01:01 UTC after Astra
review of the final package, 24-file DTN upload/control SHA verification and sealing,
and real shared CLI preview. Child **9a801032ba354c27985b00801d5d5e4c** links parent
**579e7b497b4f494dbf40d02512b414cd**, Job **46948346**. It retains the 3f02864 frozen
wheel/runtime and audit plan. Public fixture ff223902 supplies the increased finite
40,000-case/64-MiB ceilings; rasraser validator 488a401 preserves the 600-second
minimum, generation_deadline, exact inventory/identity/chain/count checks. Combined
merged toolkit validation passed 68/68, no skips (14.718s).

The first audit's exact four metadata files passed the frozen inner validator on
Sherlock: complete 265 files / 1,706,395,112 bytes, report SHA256
`f1e4cfa66a23b7ff951383c33af710f87e927137a83bf1e4ea85bbb1d03685d7`.
Child code reads only those metadata, checks their frozen size/SHA before and after
copy, and explicitly records audit_reused=true, audit_source_reread=false and parent
identity. It has no audit run call or scientific source root. A prepared manifest is
durable before producer promotion; actual total bundle must pass the 64-MiB ceiling.
No third attempt is automatic or covered by this recorded two-attempt ceiling.

Child script SHA256 `659b1a040d29c96f3b4445807fda2dbe78e042fdae1ad2314d01c0b99ecdd596`,
validator `b56918eb4e505a420eedf919f1b48f0168746f6efc534ac6afd7d7a32465f5d3`, fixture
`88c1953c1c83adc0cb443e4fb2c799da521ea5815d65d909e172c1212121b25e`.

Frozen identities: toolkit `3f02864eec4e76a933b84adac4570dc3b80b9a30`, adapter SHA256
`9bd8e459fb1759bfafc60fb2b4f2a3179480b278c343c4417ad642a19db3edd1`, input
`f888021dcf3a5962044f397518a0bffc47c5fab556374f393efa113d20b88319`, runtime
`5177ecfebc8716435c90998168fce3ab04cd7ccaad0808fad0cf91b1320702b9`, validator
`5f3c27d15a8b6a07fbcbe2b2ee0b7f56b747f9c83f1f6502f9d80c441bcce399`, and script
`88c7a2041566f6ca79863bb97a4f92096407b9b5a8c8d77a012685e0892871bf`.

## Actual guard and skill delivery

User explicitly authorized both clients' real synthetic model/context requests and
normal Codex `/hooks` UI hash trust. No credentials were copied, no research context
was loaded, and no trust database was edited or trust bypass flag used.

- Claude 2.1.293 actual scoped PreToolUse blocked a harmless forbidden-helper-name
  canary; a continuing session executed a benign canary then denied that canary.
  Actual missing-command, malformed-output and timed-out handlers all allowed the
  synthetic tool to execute: these failures are not enforcement. Its explicit
  `sherlock-kit:sherlock-kit-operate` plugin skill invoked the same frozen toolkit.
- Codex 0.161.0 normal review UI showed Active=0/review required, then Trusted/Active=1
  after normal UI approval. The real forbidden-name tool was denied, and a benign
  tool executed through normal approval. Sandbox bwrap failures were distinguished
  from hook denial. Actual skills/list found the enabled installed user skill;
  GPT-6.1 Sol high loaded its file and invoked canonical shk policy/doctor at 3f02864.
  Existing large-catalog traversal/context-budget warnings were preserved and recorded.
  Five subsequent real failure probes passed their observation checks: untrusted and
  modified definitions were skipped; malformed output and timeout permitted tools;
  a trusted missing Python handler returned exit 2 and blocked its tool. No blanket
  failure-mode claim is made. Safe structured receipts are in
  `docs/validation/codex-hook-failure-modes.{md,json}`; raw private rollouts are not
  published. Automatic review rejected searching a raw rollout, then accepted exact
  synthetic-thread structured tool diagnostics; no unresolved approval blocker remains.
- Adapters were first installed/tested in temporary targets, then enabled only by
  the lead from canonical dotfiles; live symlinks point to canonical sources. The
  shared config merger preserves host hooks and is idempotent: **67 config-sync and
  12 integration tests PASS**, and required full pre-commit checks PASS. Optional
  global hook registration remains inactive; actual client tests used scoped settings.
  Doctor reports availability but leaves registration/trust/blocking unverified.

## Remaining acceptance gates

Phase 3/4 require a duration-qualified child pilot and verified artifact fetch;
first real submission, terminal identity and final accounting have actual evidence
but first acceptance failed safely. Finish Codex
failure-mode observations and resolve dotfiles canonical writer ownership before
final integration/publication. Native PowerShell is unavailable, so Windows execution
is untested; cross-platform fixtures are not native Windows acceptance. Preserve
rasraser canonical user changes and all historical runs/worktrees. Push only toolkit
and the owner's existing dotfiles remote; pilot research changes remain local.
