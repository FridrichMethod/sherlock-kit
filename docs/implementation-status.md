# Implementation status

Updated 2026-10-07. Mandate: Phases 1–5, with independent phase acceptance.

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
| 1 foundation | e7cd5d9 merged | 15 offline/install tests and independent review; merged retest pending | pending |
| 2 delivery | pending | pending (both actual clients required) | pending |
| 3 rasraser CPU pilot | pending | pending | local commits only |
| 4 shared orchestration | pending | pending | pending |
| 5 opt-in guard/adapters | pending | pending (both actual clients required) | pending |

## Next actions

Build and review foundation and dotfiles integration in isolated worktrees; publish
accepted foundation, then continue pilot, extraction and guard acceptance.
Record precise live CPU budget before dispatch. No GPU budget is authorized.

## Foundation review

Independent Astra review accepted foundation core. Fixed archive/sdist provenance
when extraction occurs inside an unrelated Git repository; packaging must identify
the source root rather than inherit the outer checkout revision.
Candidate protocols have 15 submission and 11 real-rsync tests, but Phase 4 remains
pending pilot adoption and further review. They are not in the foundation release.
