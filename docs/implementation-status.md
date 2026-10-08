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
| 3 rasraser CPU pilot | local b0d22bc; next shared adapter pending | real local sealed audit + real synthetic SIGTERM/resume; live path pending | local commits only |
| 4 shared orchestration | 1d7f880 / 6890a19 | 64/64 tests including frozen wheel/install; Astra boundary review PASS; live pilot gate pending | tested implementation candidate |
| 5 opt-in guard/adapters | 7078fea merged; dotfiles 000798f candidate | pure guard tests PASS; actual Codex registration untrusted; trust/block/client skills pending | guard candidate; activation inactive |

## Next actions

Publish the independently tested candidate, upgrade the dotfiles pin from canonical
main, verify installed adapters in temporary targets, then exercise actual clients.
Prepare one normal CPU job (1 CPU, 2 GiB, zero GPUs, 30-minute walltime) containing
600 seconds of varied seeded protocol verification plus one sealed provenance audit.
The user explicitly endorsed ten-minute verification and Astra confirmed that varied
fault/interleaving coverage is useful work. No sleep, repeated scientific experiment,
historical controller, ledger reset or borrowed partition. No job has been submitted.

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

## Remaining acceptance gates

Phase 3/4 require the new immutable remote CPU pilot's real shared submission,
scheduler identity reconciliation and verified artifact fetch. Phase 5 requires
actual client trust/blocking, false-positive and skill-discovery checks. Codex's
current hook is explicitly untrusted; no internal trust database or bypass flag is
used. Native PowerShell is unavailable, so Windows execution is untested. Preserve
rasraser canonical user changes and all historical runs/worktrees. Push only toolkit
and the owner's existing dotfiles remote; pilot research changes remain local.
