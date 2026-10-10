# Acceptance evidence

Release 0.3.0 replaced the local SQLite ledger with the attempt registry on
Sherlock (`registry_root`) and added job arrays; the 0.1.0 and 0.2.0 evidence
below predates that change and is unaltered. The 0.3.0 registry and array paths
were exercised by the live CPU pilot recorded in "Live registry pilot (0.3.0)".

Foundation and CPU pilot accepted 2026-10-08 UTC. This is engineering acceptance,
not certification of scientific results or arbitrary command compliance.
Original implementation plans and detailed session journals remain in Git history
at commit `18c01d0369637cd6c43c105e67bf1b3d4e5a41c9`; they are not user guides.

## Coverage

| Area | Evidence |
|---|---|
| Policy/transport/doctor | Source-backed rules, quoted bounded argv, no mutation retries, read-only diagnosis, archive/wheel/isolated-install identities |
| Delivery | Both actual global instruction entry points, client precedence probes, temporary Stow/config preservation and frozen canonical installation |
| CPU consumer | Local-only rasraser adapter, frozen identities, actual SIGTERM/checkpoint/resume and real outer Slurm lifecycle |
| Shared operations | Concurrent admission/deduplication, conservative unknown reservations and costs, query cadence, identity conflicts, promotion crashes and old-attempt recovery |
| Guard/adapters | Real scoped registration/trust/denial/benign and namespaced skill checks in both clients; one installed toolkit |

Original merged toolkit had **68/68 tests with no skips**. The final accepted
commit 18c01d0 rerun completed in 14.830 seconds, including packaging. Tests use
real local SQLite/process/rsync/fsync behavior and explicit synthetic scheduler
responses. They do not masquerade as live scheduler acceptance. Subsequent
release-maintenance changes must pass the full suite and CI independently.

Research checks: existing `make check` passed **2982 tests / 4 pre-existing skips**,
ruff 661 files and mypy 230 sources; final protocol/validator changes passed
27 focused tests and scoped hooks. An unrelated all-files formatting probe found
four existing baseline differences; those automatic edits were reverted. Pilot
branch `pilot/sherlock-kit-cpu` remains local, evidence commit
`c4d3284fc90c8b3a3a28b4539cdc0b32eeafc9b7`. Science, criteria, historical results,
controllers and ledgers were not changed or restarted.

Dotfiles main 8d559fec283d5eb6ef969e35b6d1eafb72d933aa passed full pre-commit,
POSIX behavior, 67 config-sync and 12 integration tests. Its real
[CI run 37712971126](https://github.com/FridrichMethod/dotfiles/actions/runs/37712971126)
passed Linux lint/behavior, macOS system Bash/BSD behavior and native Windows
PowerShell behavior. A macOS temporary-directory fixture error was reproduced
and corrected while preserving production symlink rejection.

## Live CPU pilot

Budget recorded before dispatch: normal partition, **1 CPU, 2048 MiB, 0 GPUs,
1800-second walltime, concurrency one; at most two jobs / 3600 allocated CPU-seconds**.
The one sealed provenance audit verified **265 files / 1,706,395,112 bytes**.

First job **46946104**, attempt `579e7b497b4f494dbf40d02512b414cd`, reached its
original 10,000-case cap after 238.895663 seconds. Its validator correctly refused
the duration gate and did not promote a final bundle; cost 256 CPU-seconds.
After investigation and independent package review, exactly one manually linked
child was submitted into a new immutable release/run/results namespace.

Child job **46948346**, attempt `9a801032ba354c27985b00801d5d5e4c`, completed
**600.003354 seconds, 25,668 cases and 4,307 real process deaths**. Seed 20261007,
29 semantic buckets, `generation_deadline` termination. Revisited buckets use
varied parameters/race trials, not additional semantic coverage. The outer
Slurm job was real; inner scheduler acknowledgements and injected failure cases
were synthetic. No sleeps, scientific reruns or repeated hashes padded runtime.

The child reused only the four exact SHA/size-verified completed audit metadata
files: `audit_reused=true`, `audit_source_reread=false`. Shared identity-bound
reconciliation recorded **COMPLETED / 0:0**, no restarts and complete accounting.
Its cost was 622 CPU-seconds: **878 total**, GPU zero, both reservations zero.

Real DTN rsync fetched a **six-file / 24,794,728-byte** bundle through exact
inventory/hash checks, frozen composite validator, atomic promotion and durable
receipt. `fetch --local` recovered the same receipt without invoking sentinel
SSH/rsync binaries. New frozen bb69e8c recovered the original admitted 3f02864
attempt with its old identities. Independent Astra review reran the frozen
validator, recomputed the full chain/manifest, matched parent metadata and
verified SQLite evidence and costs: PASS. `scientific_acceptance=false` throughout.

| Digest | SHA256 |
|---|---|
| Manifest | `4d31d8bfbbdd86560d5ef17034d29857b00b84d3d3dae20acc788b10394a2502` |
| Case chain | `ff67b7ddb3b6db04f6417fe4d634d02ea27a2632686dc6341b2c200ff6a42af8` |
| Audit report | `f1e4cfa66a23b7ff951383c33af710f87e927137a83bf1e4ea85bbb1d03685d7` |
| Validator source | `b56918eb4e505a420eedf919f1b48f0168746f6efc534ac6afd7d7a32465f5d3` |

Promotion identity `821a3172e129a2ec3817e34e2b6aa0fc6a9b2eeb6284ab71166ba45967dbab41`
is the canonical hash of validator source digest plus function `validate`.
It is not validator drift. Raw inputs, private lineage, concrete storage
configuration and full receipts are retained in the private data archive,
with relocation inventory and recovery notes; they are not public package files.

## Live GPU pilot (0.2.0)

Run 2026-10-09 with frozen toolkit `ef0879554ced24984028edda86a3d38da2d3366f`
(policy `561c3eb3…`, partitions `28ed9d1b…`) from one workstation against a new
pilot namespace under `$GROUP_HOME`; synthetic resumable GPU workload (fp16 matrix
products with a durable checkpoint every 60 s of compute) plus the bounded
protocol-stress soak imported from the shipped wheel. No research code or data.
Budget recorded before dispatch: limits 8 CPUs, 2 GPUs, 2 tasks, 36,000 CPU-seconds,
9,000 GPU-seconds; two jobs, one planned requeue. `scientific_acceptance=false`.

`shk occupancy --partition btrippe` parsed live `squeue -O` output before the
borrowed submission: one other user held 2 of the node's 4 H100 GPUs, nothing
pending; the pilot took 1 GPU for 15 minutes under the profile's courtesy text.
The borrowed admission carried an identity-bound grant (group membership and OAK
storage as evidence); a configured grant no longer affected the owners attempt.

| Attempt | Job | Partition | Request | Outcome |
|---|---|---|---|---|
| `f8c19b59` | 47099013 | btrippe | 1 GPU, 4 CPU, 16 GB, 15 min, `--no-requeue` | COMPLETED, 506 s on an H100 80GB; 480.0 s GPU compute in 14,489 steps (median 663 TFLOPS fp16); soak 10000 cases passed |
| `116283cd` | 47099011 | owners | 1 GPU, 4 CPU, 16 GB, 30 min, `--requeue --open-mode=append` | REQUEUED once by the operator after 914 s, then COMPLETED in 363 s on an A100 80GB; 1,200.1 s GPU compute in 13,746 steps across both runs; soak 20000 cases passed |

Requeue drill: `scontrol requeue 47099011` was issued once at 12:12:13 UTC after
fifteen checkpoints. Slurm reported `CANCELLED … DUE TO JOB REQUEUE` two seconds
later; neither the batch shell nor the Python workload observed a SIGTERM window
and the final checkpoint write did not happen, so the restart resumed from the last
periodic checkpoint (899.5 s, 10,323 steps) and recomputed under a minute of work
without double counting. The restart waited 69 minutes for an owners GPU (the
first run had waited 29 minutes); `sacct --duplicates` then showed two rows for one
JobID (`REQUEUED` Restarts=0 ElapsedRaw 914, `COMPLETED` Restarts=1 ElapsedRaw 363,
distinct DBIndex). Reconciliation stored the lower bound 914 GPU-seconds with
`cost_known=0` while the restart was pending and certified 1,277 GPU-seconds /
5,108 CPU-seconds with `cost_known=1` once the history was complete. The
restart-0 soak was killed before its report could be copied, so the owners bundle
holds only the restart-1 soak.

Both bundles were fetched through the DTN with manifest-bound rsync, validated by
the frozen validator (identity `e5b1208db41c1e33…`), promoted atomically
with durable receipts, and recovered offline with `fetch --local`
(`recovered: true`): btrippe 4 files / 9,541,799 bytes, manifest
`e92e078b7b30213d2c713a2932245cf3f2e30ef7c6c36b91af1422e3aa4fb605`; owners 5 files / 19,105,607 bytes, manifest
`60cd3cd4f40ae06d1c3e3721de375f54a82f440e3a7631930a254b3cbf7a738b`. Final shared ledger: both attempts terminal,
zero reservations, 7,132 CPU-seconds and 1,783 GPU-seconds charged for this scope.

Not exercised: natural preemption by a node owner, `unexpected_preemption` on a
non-preemptible partition, `--acknowledge-preemption`, `--all` over hundreds of
attempts, and a dispatcher crash leaving a `submitting` claim (the post-pilot review
found and fixed its exclusion from `--all` in `3e7e798`). Private evidence,
configuration, specs and receipts stay in the owner's data area.

## Live registry pilot (0.3.0)

Run 2026-10-10 UTC with the frozen toolkit 0bd51b0 (`shk` reinstalled through
dotfiles, pin and projection regenerated) against a fresh `registry_root` on
group storage. One attempt, one Slurm job array on `normal`, CPU only; the
workload was a 720-second fuzz of the toolkit's own `resolve()` contract per task
(about 3.8 million randomised attempts per task, all invariants held). This is
engineering acceptance of the registry, array and fetch mechanics, not a
scientific result.

| Item | Observed |
|---|---|
| Submission | `submit --apply`: offline gates, one `id -un` probe, one runner call; reply `SHK_SUBMITTED`, job `47245971`, exit 0. The registry held `record.json` (stamped `created`/`created_on` by the login node), `submitted.json` (sbatch rc 0) and the task marker before the reply reached the workstation. |
| Resources | 4 tasks, throttle 2, 1 CPU, 2 GB, 20 min; `--array=0-3%2`, `slurm-%A_%a.out/.err` under the run directory. |
| `status` 17:26Z | `identified`: tasks `RUNNING 2, missing 2`. Slurm accounting showed no row for the two tasks held by the throttle (no `N_[2-3]` aggregate); `squeue` listed them as `PENDING JobArrayTaskLimit`. |
| `status` 17:50Z | `identified`: `COMPLETED 2, RUNNING 2`, cost lower bound 1,476 CPU-s, `known false`. |
| `status` 17:54Z | `terminal`: `COMPLETED 4`, cost 2,932 CPU-s (738+738+728+728), `known true`, no anomalies. |
| `reconcile --all` | One read, one `event resolved` call; `resolved.json` written (`rows_sha256` recorded); the next listing is empty. |
| Accounting shape | Task rows `47245971_0..3` with raw ids 47245981, 47245982, 47246202 and 47245971: the last task to leave the array kept the array id as its raw id, as the resolver expects. |
| Fetch | Manifest of 4 files / 1,532 bytes written on Sherlock, fetched through `sherlock-dtn` into a 0700 stage, validator passed, atomic promotion, receipt and `.array-soak.shk-attempt.json` sidecar beside the bundle; `fetch --local` recovered the receipt offline (`recovered: true`). |

Not exercised live: lost ssh replies and `--abandon`, duplicate-task refusal,
preemption or requeue of an array task, `--acknowledge-preemption`, borrowed
partitions. Those rest on the offline tests that execute the real registry
program against fake Slurm binaries. One documentation correction followed the
pilot: pending array tasks held by the throttle are reported as `missing`, not
`pending`, because Sherlock's accounting has no row for them yet. The private
configuration, spec, status readings, receipts and bundle live outside Git in the
owner's data area.

## Release-maintenance verification

The cleanup release adds explicit transport/launcher state locators and routes
managed authentication state to the shared controller. **78/78 tests, no skips**,
passed on merged main 7abd733 (20.005 seconds). Packaging now exercises real
sdist creation, frozen-identity reconstruction under an unrelated Git checkout,
wheel payload equality and isolated installation with MIT metadata/file checks.
Synthetic HOME/state inventory remains unchanged by runtime diagnostics.

The initial hosted Python 3.11 job passed. Python 3.14 exposed a CI-only entrypoint
error: running its wrapper from stdin prevented forkserver from importing main.
The corrected runner uses a tracked file and `__main__` guard, preserves the
platform's default process start method, requires packaging and fails any skips.
Current release checks are visible through the
[CI workflow](https://github.com/FridrichMethod/sherlock-kit/actions/workflows/ci.yml).

Private completed evidence was inventoried and relocated to the user's data area.
Independent review checked all original 30 files, 98 supplemental files and the
original standalone validator. The live copied ledger is byte-identical and
retains two terminal attempts, zero reservations and 878 CPU-seconds. Archive
configs remain historical; relocation is not automatic managed recovery.

## Actual clients and limitations

Codex **0.161.0** actual prompt inspection established global delivery,
AGENTS.override precedence and explicit CLAUDE fallback. Claude **2.1.293**
authenticated synthetic probes established global rules; in the override fixture
it selected AGENTS. Claude strips Markdown comments from model context, so
projection digest comments were checked on disk.

Both clients blocked a harmless prohibited-helper-name canary and executed
benign canaries. Codex used normal `/hooks` UI review (untrusted → trusted),
with no trust-store edits or bypass. Both actual namespaced skills called the
same frozen canonical toolkit. No research or copied credentials entered tests.

Claude missing/malformed/timeout handlers continued execution. Codex untrusted/
modified handlers were skipped; malformed/timeout allowed execution; a trusted
missing Python handler exited 2 and blocked. These are version-specific
observations, not portable guarantees; [structured Codex evidence](codex-hook-failure-modes.md)
is retained. Global production hooks remain opt-in; doctor cannot attest live trust.

Accepted scope is one authoritative POSIX workstation and a single allocation per
attempt on a packaged partition profile; requeue only on `owners` with a script
that resumes from its own checkpoints. No arrays, distributed controller, general
DDP/GPU recovery or other research migration is certified. Windows instruction/guard/install adapters
do not imply a Windows controller. Borrowed access was never inferred.
