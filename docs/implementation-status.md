# Implementation status

Updated 2026-10-08 UTC (2026-10-07 America/Los_Angeles). Mandate: complete
Phases 1–5 under plan §§10–11 and 13. Phase 1–2 was published first; work continued
through the authorized rasraser pilot and both actual clients.

## Phase acceptance

| Phase | Implementation and acceptance evidence | State |
|---|---|---|
| 1 foundation | e7cd5d9 / 446549f; source-backed policy, bounded transport, read-only doctor, independent archive/wheel/venv installation | accepted; intermediate release f91b68b |
| 2 delivery | dotfiles a5536d6 / 298f14a; real Codex and Claude global instruction/precedence probes, temporary Stow and config checks, canonical frozen installation | accepted; published |
| 3 authorized pilot | local rasraser b0d22bc / ecd6be2 / 97b00e2 / 8e39a79 / 488a401; real CPU job, full result path and SIGTERM/resume evidence | accepted; research commits local only |
| 4 shared orchestration | 1d7f880 / 6890a19 / fa85201 / 3f02864 / ff223902; shared submit, status/reconcile and fetch, concurrency/identity/crash tests, live DTN promotion and offline recovery across updates | accepted; published |
| 5 guard/adapters | 7078fea / e4cd633 / 4dde513; namespaced skills, both actual clients' trust/denial/benign and failure observations, same canonical toolkit | accepted; published; production registration opt-in |

Canonical dotfiles main 8d559fec283d5eb6ef969e35b6d1eafb72d933aa includes the
reviewed integration and preserves concurrent user history. Its full pre-commit
and POSIX behavior suite passed after both merges. The macOS physical-temp-root
fixture correction 22f61f1 changes no production checks; regression tests preserve
strict rejection of target/ancestor symlinks. Latest native Linux/macOS/Windows CI all passed: [run 37712971126](https://github.com/FridrichMethod/dotfiles/actions/runs/37712971126).

## Published identity and working ownership

Public repositories: [sherlock-kit](https://github.com/FridrichMethod/sherlock-kit)
and the existing [dotfiles](https://github.com/FridrichMethod/dotfiles). No research
code, raw provenance, credentials, grants or private runtime configuration was
copied into either public repository. Only toolkit and dotfiles are pushed.

The installed immutable code pin is
`bb69e8c5022e6959d1af7b9f5bb590af77baf2cd`, schema 1, policy SHA256
`cb6fe161d747c7a7254d62b46cf5a04fd63cb938afdf06235da69d2a9aa8f582`. Final
acceptance documentation can advance main without changing that tested code pin.
The old `3f02864eec4e76a933b84adac4570dc3b80b9a30` runtime remains installed for
recorded attempts. The lead alone enabled real configuration from canonical
dotfiles after temporary-target checks; live symlinks resolve to canonical sources.
No package installation occurs during shell startup or Stow. Global hook
registration remains inactive unless explicitly adopted and trusted.

Implementation workers used GPT-6.1 Sol high in separate branches/worktrees.
GPT-6 Astra high independently reviewed ambiguity, shared admission, crash recovery,
promotion and the final live closure. The lead's model cannot be switched in-place.
All phase work was committed in small steps, reviewed and rechecked after integration.
Canonical user changes and all historical research worktrees/controllers/ledgers
were preserved. Another dotfiles writer completed its commits and exited before
the lead resumed canonical writes; its history was normally merged, never reset.

## Actual live pilot

Budget was recorded before dispatch: **normal partition, 1 CPU, 2048 MiB RAM,
0 GPUs, 1800-second walltime, concurrency one; at most two jobs and 3600 total
allocated CPU-seconds**. This is an engineering provenance pilot with
`scientific_acceptance=false`, not a scientific experiment.

The first attempt `579e7b497b4f494dbf40d02512b414cd`, job **46946104**, failed
safely: its original 10,000-case cap stopped useful work at 238.895663 seconds.
The composite validator rejected `case_limit`; no bundle was promoted. Its one
sealed provenance audit completed **265 files / 1,706,395,112 bytes**. Final
accounting was 256 CPU-seconds, zero GPU-seconds, reservation zero. The investigation
and failed acceptance are retained, not replaced by a success claim.

After a finite 40,000-case/64-MiB cap correction and independent package review,
the lead submitted exactly one manual linked child at 2026-10-08 01:01 UTC:
attempt `9a801032ba354c27985b00801d5d5e4c`, job **46948346**, a new immutable
release/run/results namespace. It reused only four frozen-valid completed audit
metadata files after exact SHA/size checks, with `audit_reused=true` and
`audit_source_reread=false`. It did not rerun the audit or scientific sources.

- Useful stress: **600.003354 seconds, 25,668 cases, 4,307 real process deaths,
  29 semantic buckets**, seed 20261007, stop reason `generation_deadline`. The
  other 25,639 cases revisit buckets with varied parameters and race trials, not new semantic coverage.
- Real outer Slurm execution ended **COMPLETED / 0:0**, restarts zero; shared
  identity-bound reconciliation finalized 622 allocated CPU-seconds, zero GPU-seconds
  and no reservation. **Both jobs total 878 CPU-seconds**, below the 3600-second cap.
  Peak parent RSS was 80,832 KiB; outer worker elapsed 605.360161 seconds.
- The outer allocation was real; inner scheduler acknowledgements/failure cases
  were synthetic. Stress used actual forks, SQLite, fsync, rsync and process death,
  with no sleeps or repeated hashing to pad runtime. No GPU or borrowed partition
  was used. Historical science, evaluation standards and results were unchanged.
- Real DTN rsync fetched the **six-file, 24,794,728-byte** bundle through the
  shared manifest, exact inventory/hash checks, pinned standalone validator,
  same-filesystem atomic promotion and durable receipt. `fetch --local` then
  recovered the same receipt; sentinel SSH/rsync binaries were never invoked.
- New frozen bb69e8c reconciled/fetched the admitted old 3f02864 attempt without
  reinterpreting its immutable source/runtime/input/policy/validator identity.
  Independent Astra review reran the frozen validator, recomputed the complete
  case chain and manifest, checked the exact four parent metadata hashes, and
  verified SQLite accounting and both released reservations: **PASS**.

Manifest SHA256:
`4d31d8bfbbdd86560d5ef17034d29857b00b84d3d3dae20acc788b10394a2502`.
Case-chain SHA256:
`ff67b7ddb3b6db04f6417fe4d634d02ea27a2632686dc6341b2c200ff6a42af8`.
Audit report SHA256:
`f1e4cfa66a23b7ff951383c33af710f87e927137a83bf1e4ea85bbb1d03685d7`.
Composite validator source SHA256:
`b56918eb4e505a420eedf919f1b48f0168746f6efc534ac6afd7d7a32465f5d3`.
Promotion identity `821a3172e129a2ec3817e34e2b6aa0fc6a9b2eeb6284ab71166ba45967dbab41`
is the canonical SHA256 of the source digest plus function `validate`, not a
validator change. Raw lineage and private input metadata are retained privately.

Durable local receipt: `~/.local/state/sherlock-kit/pilots/`
`sherlock-kit-audit-20261007-shared-v2/phase3-acceptance.json`; its
`results/accepted-bundle` holds the verified bundle. Research handoff and adapter
remain in the local-only `pilot/sherlock-kit-cpu` branch/worktree.

## Tests and actual client observations

Toolkit command:
`SHERLOCK_KIT_BUILD_PYTHON=/tmp/sherlock-kit-build-venv/bin/python python3 -m unittest discover -s tests -q`.
Merged implementation 07f5332: **68/68 PASS, no skips** (14.718 seconds).
Final bb69e8c code plus acceptance documentation rerun: **68/68 PASS, no skips**
(14.789 seconds). Tests include
real multiprocessing/admission races, durable unknown reservations, strict scheduler
identity and unsupported arrays/restarts, query failures/cache/cadence, conservative
cost high-water, scope quarantine, old-policy recovery, transfer corruption/links,
concurrent fetchers, actual promotion crashes and frozen-source/pyc replacement.
Archive/wheel/isolated-venv identity is independently checked without inheriting
an unrelated outer Git checkout.

Rasraser's existing environment `make check`: ruff 661 files, mypy 230 sources,
**2982 passed / 4 pre-existing skips** (280.53 seconds). Later bounded cap/fsync/
composite-validator changes: **27 focused tests**, ruff, mypy and scoped
pre-commit PASS. Final evidence-only pilot commit is
`c4d3284fc90c8b3a3a28b4539cdc0b32eeafc9b7`, also scoped-hook clean. A final
whole-tree pre-commit probe found unrelated baseline mdformat/shfmt drift in four
existing files. Its automatic edits were reversed exactly; no unrelated formatting
was committed. This does not change the passing existing `make check` result. Actual SIGTERM over two synthetic files / 269,484,032 bytes
preserved one verified checkpoint prefix; same frozen plan resumed and reverified
it successfully. Private receipt `/tmp/shk-real-signal-v2-thoummre/receipt.json`
was independently reviewed. Local protocol acknowledgement/accounting fixtures
are explicitly synthetic and are not the live scheduler acceptance above.

Dotfiles: **67 config-sync and 12 integration tests PASS**, full required
`pre-commit run --all-files` and merged `tests/run.sh --ci` PASS. Locally unavailable
PowerShell tests are reported as skipped; native Windows CI supplies their evidence.
The previously published c1520d9 run 37712009437 passed Linux and Windows; macOS
failed two fixture paths, now corrected and reproduced with a real symlink TMPDIR.
Final run **37712971126** on canonical published **8d559fec** completed SUCCESS:
Linux full behavior/lint/pre-commit, macOS system Bash/BSD behavior (including
the formerly failing step), and Windows native PowerShell behavior all PASS.
[Actual run](https://github.com/FridrichMethod/dotfiles/actions/runs/37712971126).

Actual versions: Codex **0.161.0**, Claude Code **2.1.293**, local Python 3.14.8,
allocated module Python 3.14.2; module availability, executable SHA and batch
initialization were checked. Sherlock site guides were read on the intended host.
Control is bounded noninteractive OpenSSH; bulk transfer uses the DTN with explicit
paths and no DTN shell. Status calls were separated by at least 60 seconds.
Doctor remains read-only and reports its own unprobed remote capabilities honestly.

Both clients' real global instructions loaded in clean/project/override fixtures.
Codex actual prompt inspection established AGENTS.override precedence and explicit
CLAUDE fallback. Claude selected AGENTS rather than AGENTS.override in that
fixture; Markdown digest comments are stripped from its model context and checked
on disk. One early Claude probe loaded the existing large skill catalog and spent
$1.098076 before a $0.50 CLI stop; the limit does not cap one request in advance.
Later scoped probes used Haiku and excluded unrelated catalogs, around $0.005 each.

Both actual clients blocked the harmless forbidden-name canary and executed benign
canaries. Codex used normal `/hooks` UI hash review (Active 0 → Trusted/Active 1),
with no trust database editing or bypass. Both namespaced skills invoked the same
canonical frozen toolkit. Final Claude plugin probe cost $0.00275431. Tests used
explicitly authorized synthetic contexts, never research or copied credentials.
Claude missing/malformed/timeout handlers allowed execution. Codex untrusted/
changed-hash handlers were skipped, malformed/timeout allowed execution, while a
trusted missing Python handler exited 2 and blocked. These observations are not
universal enforcement guarantees. Safe structured evidence is in
[Codex failure modes](validation/codex-hook-failure-modes.md). Raw rollouts remain
private. Initial automatic-review refusals were resolved by explicit client
authorization or narrower structured evidence; none remains a blocker.

## Completion and supported limits

All required Phase 1–5 acceptance gates passed. Toolkit implementation and this
record are retained on canonical main and published; canonical dotfiles main is
clean, published and three-platform CI accepted. There is no remaining required
implementation or external access blocker. No further Slurm job, scientific rerun,
historical-controller restart or research push is needed or authorized by this pilot.

Accepted scope is one authoritative POSIX workstation and immutable CPU allocation.
Arrays/requeue/restarts, distributed controllers, general DDP/GPU recovery and
other research migrations are not certified. Windows instructions/guard/install
adapters do not imply a Windows controller. Optional production hooks require
explicit registration, current trust and a blocking smoke test; doctor deliberately
reports those live-client properties as unverified. These are documented scope
limits, not substituted or skipped acceptance results.
