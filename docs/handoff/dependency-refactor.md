# Dependency Refactor Implementation Plan

> **For agentic workers:** Use the `subagent-driven-development` skill for task implementation and independent review. The requested execution method is parallel workers in separate branches and worktrees, with one lead responsible for integration, main, and cleanup. Independent workstreams below run concurrently; dependent steps remain sequential. Checkboxes describe acceptance steps, not a shared execution log.

**Goal:** Adopt useful standard Python tooling, evaluate a bounded Pydantic validation refactor, and deliver reviewed, fully tested, clean local main in a release-ready state.

**Architecture:** Keep the stateless Slurm interface, fixed POSIX remote programs, OpenSSH, rsync, CLI, and public JSON contracts. Use PyPA build for packaging and Hypothesis for bounded property tests. Evaluate Pydantic only at configuration and receipt input boundaries, preserving existing Python interfaces and transfer semantics.

**Tech stack:** Python 3.11 through 3.14, setuptools, PyPA build, unittest, Hypothesis, existing pre-commit tools, and provisionally Pydantic 2.12 or newer within major version 2.

**Spec:** The design contract in this document and the existing contracts in [design.md](../design.md), [allocation.md](../allocation.md), [README.md](../../README.md), [CONTRIBUTING.md](../../CONTRIBUTING.md), and [AGENTS.md](../../AGENTS.md). Read those files before execution. This document is the implementation handoff, not a record of test results.

## Authorization and completion boundary

The implementation session is authorized to implement this plan, create local task branches and worktrees, make atomic local commits, integrate accepted work into main, and remove verified task leftovers. The plan-writing session does not implement the refactor.

Do not push, open a PR, tag, publish, deploy, update consumer projects, or operate Sherlock as part of this task. Release-ready here means the exact committed main revision passes the required checks and installation matrix, has reviewed distribution contents, and has no task worktrees or unexplained changes left behind. Do not claim publication or remote CI success from local checks.

The integration lead makes the Pydantic adoption decision using Task C's technical gate. This is not a new permission checkpoint. A rejected pilot is an acceptable outcome when the useful build and test improvements are complete and all provisional runtime dependency changes are removed. Do not leave an experimental implementation or unused dependency in main.

## Design contract

### Included changes

- Replace ad hoc packaging orchestration with the standard build frontend, including a wheel built from the sdist and isolated installation checks.
- Add bounded Hypothesis tests for existing pure parsers and reducers. Preserve unittest and the authoritative test runner.
- Evaluate one strict validation mechanism for configuration and receipt identity fields. Keep the public configuration dataclass and receipt compatibility.
- Update dependency declarations, isolated lint dependencies, test setup, CI, installation documentation, and package descriptions to match the accepted implementation.
- Integrate, review, validate, and clean up all task-owned implementation artifacts.

### Excluded changes

- No Click/Typer, Paramiko/Fabric, PySlurm, parser framework, retry framework, filesystem framework, psutil, or atomic-write library migration.
- No whole-repository model rewrite, public JSON schema expansion, blanket TypedDict conversion, or additional jsonschema dependency.
- No Slurm native JSON experiment, live cluster validation, new scheduler features, site policy edits, dependency-free fallback, or unrelated cleanup.
- Keep canonical JSON bytes, job-name hashes, manifest format, partition data, fixed remote programs, resource semantics, and CPU thread behavior unchanged.
- Keep existing version metadata and release history unless a concrete consistency fix is needed. Do not choose a new release number or fabricate a release date; publication remains a separate action.

### Dependency policy

Standard-library mechanisms remain preferred when sufficient; zero runtime dependencies are not an invariant. Add a dependency only for an accepted implementation with a concrete maintenance benefit.

The lead owns all declarations. Add `build>=1.2.2,<2` and `hypothesis>=6.142,<7` to the development extra. Use `pydantic>=2.12,<3` provisionally for the pilot. Verify these bounds against official metadata and Python 3.11/3.14 before retaining them; if a lower bound cannot satisfy a required check, raise it based on that check rather than leaving an untested support claim. Do not pin transitive runtime dependencies in the library. Record exact resolved versions in the final execution report.

If Pydantic is accepted, add it to `[project].dependencies` and the isolated mypy hook's `additional_dependencies` with the same constraint. Do not add `pydantic-settings`, environment-file discovery, or an alternative configuration precedence system. Preserve the existing ruff and mypy pins unless a demonstrated compatibility issue requires a separately justified change.

## Global constraints

- Python 3.11 and 3.14 are the required test endpoints; use no newer language or stdlib APIs.
- All repository documentation is English and printable ASCII, tabs, LF, or CR only.
- Preserve unrelated changes. Never reset, stash, overwrite, or delete another session's work.
- Each writer has a separate branch and worktree under this project's ignored `.worktrees/` directory. Reviewers are read-only. Only the lead updates main.
- No actual Slurm or SSH operation is needed. Tests use fakes, and any Slurm tripwire hit fails acceptance.
- `tests/run_suite.py` remains authoritative. Skips fail under `CI` or `SHK_REQUIRE_ALL`; discovery alone is not release evidence.
- Packaging uses `git archive HEAD`. Commit accepted changes before using installation results as evidence for that revision.
- Preparation may fetch development packages and wheels. The test suite and installation subprocesses are offline and cannot repair missing dependencies through the network.
- No runtime state, credentials, private configuration, environments, wheels, logs, databases, or worker scratch files enter commits or distributions.
- Use deliberate staging and conventional commits without agent attribution. Do not weaken checks to make the refactor pass.

## Review focus

1. Strict configuration compatibility: boolean/float/string coercion, explicit empty or null values, full regex matches, and immutable nested validator arguments. Task C pins these cases.
2. Receipt compatibility and failure timing: minimal historical receipts remain usable, malformed input reports a local error, and validation cannot fail after promotion. Task C pins these cases.
3. Genuine installation isolation: wheel, sdist, editable, and source launcher work using only declared dependencies; no borrowed site-packages or network fallback. Task A pins these cases.
4. Hermetic property tests: inherited scheduler/environment settings cannot escape fakes, and Hypothesis cannot leave files in HOME or the checkout. Tasks A and B pin these cases.
5. False reducer invariants: ordering ties and duplicate identities with different facts are not equivalent inputs. Task B limits generators to the documented semantics and retains hand-written regressions.

## Ownership and execution graph

| Role | Branch / worktree suffix | Exclusive write ownership |
| --- | --- | --- |
| Lead | `dependency-integration` | `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `.gitignore`, `MANIFEST.in`, `AGENTS.md`, `CONTRIBUTING.md`, `README.md`, `docs/design.md`, this handoff, `bin/shk`, package-level descriptions in `src/sherlock_kit/__init__.py`; integration fixes after owner handoff |
| A: packaging and test infrastructure | `dependency-packaging` | `tests/test_packaging.py`, `tests/run_suite.py`, `tests/fakes/__init__.py`, `tests/test_hermetic.py`, new `tests/prepare_wheelhouse.py` |
| B: property tests | `dependency-properties` | New `tests/test_properties.py`; no production or shared-infrastructure edits |
| C: validation pilot | `dependency-validation` | `src/sherlock_kit/config.py`, receipt-related sections of `src/sherlock_kit/transfer.py`, `tests/test_config.py`, receipt tests in `tests/test_transfer.py`, and `tests/test_cpu_threads.py` only if an additional compatibility case requires it |

Use branch names `refactor/<suffix>` and paths `.worktrees/<suffix>`. If a name is already occupied, inspect it and choose a distinct task-owned suffix; never remove an existing worktree merely to claim its name. Do not create a generic models package for this pilot.

Execution order:

1. Lead establishes the common foundation and interface contracts in Task 0.
2. A, B, and C branch from that common committed foundation and work in parallel.
3. Each writer commits its own work and stops writing before an independent reviewer examines it. With four active slots, release a finished writer's slot before starting a reviewer.
4. Lead integrates A, then B, then C's accepted final state into the integration branch. If the pilot is rejected, C first removes its trial implementation in a normal cleanup commit, as specified below. B and C can use A's finished commits in their own branches for installation validation; do not rebase shared work.
5. Lead finalizes documentation and dependency metadata, validates the integrated commit, fast-forwards main, verifies main, and removes task worktrees and branches.

No worker edits a shared file to unblock itself. Send the needed change to its owner. Cross-cutting fixes belong to the lead only after the original writer has stopped. Keep unfinished work and concise handoff notes in durable project storage; do not use `/tmp` as their only copy.

## Task 0: Establish the integration foundation

**Owner:** Lead.

**Files:** Lead-owned metadata and instruction files from the ownership table.

**Produces:** Committed dependency declarations, available suite/build interpreters, and the exact wheelhouse contract used by A, B, and C.

- [ ] Inspect status, diffs, worktrees, and branches. Read repository instructions. Preserve this handoff if it is untracked; include only this authorized document in an initial documentation commit before branching, or copy it into the integration worktree and commit it there without touching unrelated files.
- [ ] Verify `.worktrees/` is ignored. Create the integration branch/worktree from main. Keep the original main checkout available only to the lead for final integration.
- [ ] Capture a baseline with the existing pre-commit and authoritative suite, including existing packaging checks when prerequisites are available. Separate baseline failures from regressions; do not silently classify an unrun baseline as passing.
- [ ] Verify dependency support and declare the development dependencies and provisional Pydantic dependency. Update `CONTRIBUTING.md` to permit justified dependencies. Keep `AGENTS.md` as the project instruction source and leave `CLAUDE.md` as its importing stub.
- [ ] Prepare task-owned suite and build environments. Do not mutate a different project's environment. The suite interpreter has the project development extra; the build interpreter has build and the backend requirements. Use absolute executable paths when invoking either environment.
- [ ] Commit the foundation, communicate its exact base to workers in messages, and start A, B, and C concurrently. An intermediate branch may have packaging failures until Task A lands; it must not be called release-ready.

### Shared wheelhouse interface

`SHK_BUILD_PYTHON` is an absolute executable path to a prepared interpreter with `build` and all required backend packages. Tests use it without modifying its environment.

`SHK_TEST_WHEELHOUSE` is an absolute directory with `runtime/` and `build/` subdirectories. `runtime/` contains the transitive wheel closure of the archived project's `[project].dependencies`. `build/` contains backend requirements from `[build-system].requires` and any additional requirements exposed by the standard backend hooks. The frontend itself lives in `SHK_BUILD_PYTHON`; it need not be installed in runtime-only targets. The runtime directory may be empty if the pilot is rejected.

Prepare a separate wheelhouse for each test Python and platform under an ignored task-owned path, for example `.state/dependency-refactor/py311/wheelhouse`. Do not assume native dependency wheels are interchangeable across Python versions, architectures, or operating systems. Every target installation uses normal dependency resolution against these directories, never `--no-deps`.

## Task A: Standard packaging and hermetic dependency installation

**Files:** `tests/test_packaging.py`, `tests/run_suite.py`, `tests/fakes/__init__.py`, `tests/test_hermetic.py`; create `tests/prepare_wheelhouse.py`.

**Consumes:** Task 0 dependency metadata and the shared wheelhouse interface.

**Produces:** Offline installation coverage and a standalone preparation command:

```text
BUILD_PYTHON -B tests/prepare_wheelhouse.py --project DIR --dest DIRECTORY [--constraint FILE]
```

The helper is explicit preparation, never imported or invoked during test discovery. Its `main() -> int` uses the standard build frontend/backend interfaces and pip download to prepare wheels. Read requirements from a temporary `git archive HEAD` of the named project without importing `sherlock_kit`; do not maintain another package-name list. Include dynamic build requirements when present. Pass an explicit constraints file to pip when requested; constraints must not alter the committed dependency declaration. Require wheel artifacts for the tested platform and fail clearly when they are unavailable. A new temporary output followed by a rename, or refusal of a nonempty destination, must prevent stale wheelhouses from masquerading as current ones.

- [ ] Extend hermetic tests first: preserve only the three allowed `SHK_*` suite knobs, reject invalid supplied interpreter/wheelhouse paths, and retain fatal tripwire/skip behavior. Add `SHK_TEST_WHEELHOUSE` to `SUITE_KNOBS`; do not widen the prefix exemption.
- [ ] In `tests/fakes/__init__.py`, set `HYPOTHESIS_STORAGE_DIRECTORY` to a child of the existing tripwire temporary directory before Hypothesis imports. Override any inherited location. Add a child-process test proving storage cannot be redirected into HOME or the checkout and that cleanup remains owned by the existing temporary-directory lifecycle.
- [ ] Implement the preparation helper. Verify metadata comes from committed files, a missing runtime wheel causes an offline installation failure, and an empty runtime requirement set is valid. No preparation command belongs in the authoritative suite.
- [ ] Replace `BuildTests.build()` with one default frontend call: `BUILD_PYTHON -B -m build --no-isolation --outdir DIST ARCHIVED_SOURCE`. Use neither `--sdist --wheel` nor `--skip-dependency-check`. Preserve the archive boundary, temporary HOME, and package-data checks.
- [ ] Update wheel tests to install the explicit built wheel into fresh environments with `--no-index --find-links` and normal dependency resolution. Run `pip check`, existing CLI smoke checks, and the installed-wheel expired-status regression. Assert runtime metadata matches the committed declaration and excludes build/Hypothesis.
- [ ] Add an sdist installation test that installs the explicit built sdist offline into a fresh environment and checks CLI behavior and package data. This must exercise a source build, not install a sibling wheel by mistake.
- [ ] Replace editable installation's borrowed-site-packages `.pth` file with ordinary offline PEP 517 build isolation using the build wheelhouse. Run `pip check` and provenance checks. Keep build dependencies isolated from runtime-only targets.
- [ ] Run the two-hop source launcher in a fresh environment containing runtime dependencies but no installed `sherlock-kit` distribution. Prepend that environment's `bin` to PATH so the existing shebang selects it. Keep executable-mode, source provenance, symlink, and no-bytecode assertions.
- [ ] Keep pip subprocesses offline, with temporary HOME, disabled index/cache/version checks, explicit wheelhouse paths, and no inherited user pip configuration. Preserve stripping inherited Python, pip, kit, and virtualenv settings. Missing or incomplete prerequisites fail clearly under required checks; never download or skip around them.
- [ ] Commit the task, then run the authoritative suite against that committed revision. The archive-based packaging tests cannot validate uncommitted edits. Send the lead file ownership, interfaces, test evidence, and any unresolved issues; stop writing before review.

## Task B: Bounded property tests

**File:** Create `tests/test_properties.py`.

**Consumes:** Existing pure functions in config, spec, transfer, slurm, and allocation; Task A's storage isolation. No production change is required for the planned properties.

**Produces:** Unittest-discoverable property tests that run through `tests/run_suite.py`.

- [ ] Import `fakes` before Hypothesis or project modules. Use explicit settings `max_examples=100`, `derandomize=True`, `database=None`, and `deadline=None`. Do not suppress health checks or conditionally skip when Hypothesis is absent. Bound strings to 128 characters, task/range collections to 32 entries, and generated scheduler counts to practical small values; use separate explicit examples for existing documented large limits.
- [ ] Add job-name properties using valid project identifiers and bounded nonempty Unicode task/campaign strings: determinism, full `spec.NAME` match, maximum length 60, and a known golden name preserved from existing tests. Do not assert hash collision freedom or change canonical serialization.
- [ ] Add memory/time/array properties using supported grammars: integer `G` maps to 1024 `M`, positive clock durations round up to whole minutes, and valid array/throttle pairs satisfy existing bounds. Generate invalid inputs by explicit grammar violations, not by assuming arbitrary text is invalid.
- [ ] Add manifest properties for unique safe relative paths and 64-digit hexadecimal digests: ordering does not affect the parsed mapping, hex normalization is stable, duplicate entries fail with `manifest_invalid`, and forbidden path components remain rejected. Avoid real filesystem transfers and escaped GNU filenames outside the supported grammar.
- [ ] Add `slurm.job_id()` aggregate properties using bounded nonnegative ranges and overlaps: output is the sorted union and throttle suffixes do not change membership. Preserve the separate behavior of single-job and explicit array-task IDs.
- [ ] Add GPU TRES properties: a consistent generic total and typed shares are not double counted. Test `slurm.gpus()` and strict `allocation.tres_counts()` against their distinct contracts; the former uses `max(generic, typed)` while the latter rejects contradictory/duplicate evidence. Do not force them to accept or reject the same malformed inputs.
- [ ] Add field-parser properties for `details.fields()` and `allocation.strict_fields()`: at most eight unique legal keys and printable values without `=` round-trip, including interior spaces. Duplicate keys and injected control characters are rejected by the strict parser. Do not claim arbitrary free text containing field-like `Key=value` fragments is unambiguous.
- [ ] Add narrowly generated accounting properties using one fixed base ID, name, and partition, arrays of 2..8 tasks, unique attempt identities, and strictly ordered deciding keys. Record permutation and exact duplicate rows preserve semantic totals/state in this domain. With a matching frozen spec count, removing an entire task prevents final cost; without a spec, use a noncontiguous task set and assert the reported count is null before asserting nonfinal cost. Do not assert equivalence for conflicting tied records or compare unordered evidence lists byte-for-byte.
- [ ] Keep hand-written golden and regression tests. A failing generated example must first be classified as a false property or a real bug. Report real behavior changes to the lead for a separate minimal fix and regression; do not silently expand this test-only workstream.
- [ ] Run focused development checks, then the authoritative suite after Task A is available. Confirm no scheduler calls escape fakes and no Hypothesis files remain in HOME or the checkout. Commit the test module and hand it to an independent reviewer.

## Task C: Pydantic boundary-validation pilot

**Files:** `src/sherlock_kit/config.py`, receipt sections of `src/sherlock_kit/transfer.py`, `tests/test_config.py`, receipt tests in `tests/test_transfer.py`; optional focused addition to `tests/test_cpu_threads.py`.

**Consumes:** The provisional dependency from Task 0 and existing public contracts.

**Preserves these interfaces:**

```python
parse(table: object, path: Path | None = None) -> ProjectConfig
discover(project_dir: str | Path | None = None,
         cwd: str | Path | None = None) -> ProjectConfig | None
_read_receipt(path: Path) -> dict[str, Any] | None
_write_receipt(path: Path, receipt: dict[str, Any]) -> None
```

Keep `ProjectConfig` fields, defaults, positional/keyword construction, `root`, equality, and frozen dataclass behavior. `DEFAULTS == parse({})` remains true. Use a strict Pydantic boundary or compatible validated dataclass implementation; do not leak BaseModel objects or Pydantic exceptions into public results. Validation belongs at the boundary, not at every internal use. Prefer local private types over a new generic validation abstraction.

- [ ] Add compatibility tests before changing validation: explicit empty project/root is invalid but omission keeps empty defaults; omission disables CPU thread budget but explicit `None` at `parse()` is invalid; integer fields reject bool, float including `1.0`, numeric strings, zero, and negatives.
- [ ] Pin regex full-match behavior with trailing newlines/control characters; unknown keys including `path` fail as `config_invalid` with the file location and allowed-key hint. Preserve distinct hosts, remote path normalization, configuration discovery precedence, and exact `--project-dir` behavior.
- [ ] Pin validators input/output behavior: input is a nonempty list of strings per name, tuples are rejected as input, output is an immutable mapping of tuples, and mutation of the original input cannot alter the result. Attribute reassignment raises `dataclasses.FrozenInstanceError`. Pydantic `frozen=True` alone is insufficient.
- [ ] Replace generic type/range/unknown-key validation with one authoritative declaration. Keep project-specific path and host relationships explicit. Translate boundary errors to the existing `ShkError` envelope; retain TOML parsing/discovery and all CLI exit codes. Delete superseded generic validation helpers when the pilot is accepted.
- [ ] Add receipt reader compatibility tests: valid emitted receipts, unknown metadata, omitted nonidentity metadata, `{}`, absent/null validator, and a missing file retain existing idempotence/refusal/replace behavior. `shk_version` is provenance, not a reader-version gate. Do not add a receipt schema version or rewrite receipts during reads.
- [ ] Validate receipt identity field types while tolerating optional metadata. Invalid JSON, non-object JSON, or invalid identity-field types return `ShkError(EXIT_LOCAL, "receipt_invalid", ...)` with a useful path instead of an internal traceback. Missing or null digest continues to behave as no matching receipt. Preserve files on failure. This named error is the only intended externally visible behavior improvement in the pilot.
- [ ] If using model validation for newly emitted receipts, perform it before `_promote()`. Preserve exactly the seven existing receipt fields, timestamp format, and the clock sampled immediately before promotion. Keep atomic receipt writing, validator execution, backup/rollback, and the no-DTN `already_present` path unchanged.
- [ ] Run config, CPU thread, transfer, and CLI regressions. After Task A is integrated into the worker branch, commit and run the full authoritative suite and isolated installation checks. Do not replace canonical JSON, Slurm parsing, frozen spec documents, or public output dictionaries with model serialization.
- [ ] Give the lead a concise adoption comparison: remaining generic validation code, duplicated field declarations/adapters, error compatibility, and cold `--version`/`submit --dry-run` startup measurements against the baseline using equivalent environments. Measurements inform review; LOC alone is not an acceptance target.

### Adoption gate

Accept the pilot only when its maintenance benefit is clear across both input boundaries, the compatibility tests pass, the public dataclass remains compatible, and Pydantic installs/imports on both required Python versions with the offline wheelhouses. Test the declared Pydantic lower bound and a current compatible version, not only the newest resolver result. For the provisional floor, use a task-owned constraints file containing `pydantic==2.12.0`, a separate fresh suite environment installed with that constraint, and a fresh wheelhouse prepared with `--constraint FILE`. Assert the installed version before the focused boundary/CLI checks and isolated installation checks. Repeat for each Python endpoint; a current-version environment or wheelhouse is not evidence for the lower bound. Adjust the constraint if the declared floor changes.

Reject or narrow the pilot when preserving the interfaces requires mirrored validation systems or extensive adapters, optional receipt metadata becomes unnecessarily restrictive, or dependency compatibility is unproven. The lead decides without another user approval round. If rejected, C removes only its own trial implementation and pilot-only tests with a reviewed patch and a normal cleanup commit; do not reset or rewrite branch history. The lead removes the provisional runtime declaration and mypy hook dependency, then integrates the cleaned branch so its commits are preserved and ordinary merged-branch cleanup is possible. The final source tree contains no rejected implementation, unreachable fallback code, experimental schemas, or inaccurate runtime-dependency claims. Any independently useful receipt error fix must be reviewed and tested as an explicit small change, not carried over accidentally.

## Task D: Integrate and document the accepted state

**Owner:** Lead, with read-only reviewers.

**Files:** Shared files from the ownership table and narrowly scoped integration fixes after ownership handoff.

- [ ] Obtain independent reviews of A, B, and C against their contracts. Review correctness, compatibility, isolation, and missing tests before style. Resolve findings in the owning worktree while it still exists.
- [ ] Merge accepted task branches into the integration branch in dependency order. Preserve branch ancestry so later cleanup can use ordinary `git branch -d`; avoid squash/cherry-pick integration solely for convenience. Resolve conflicts deliberately and rerun affected checks.
- [ ] Finalize the runtime decision and metadata. Update stdlib-only claims only where the accepted implementation makes them false: project description, package/launcher docstrings, README, CONTRIBUTING, and design guide. Retain `bin/shk` at its contract path with executable mode. A source/submodule invocation uses an interpreter with declared dependencies installed; no bootstrap installer runs implicitly.
- [ ] Update CI's existing Python 3.11/3.14 matrix to install the suite's runtime/development requirements, prepare a separate build environment and per-interpreter wheelhouse, then invoke `python -B tests/run_suite.py` with both absolute prerequisite variables and skips fatal. Online setup and offline tests are separate steps.
- [ ] Make the isolated mypy hook resolve accepted runtime imports. Test with a fresh task-owned `PRE_COMMIT_HOME` at least once. Do not use `ignore_missing_imports`, disable rules, or change existing linter versions merely to hide failures.
- [ ] Replace any single-wheel `--no-index --no-deps` install instructions with normal dependency resolution from a complete target-compatible wheelhouse if runtime dependencies are accepted. Keep Sherlock environment creation inside an allocation and avoid live demonstrations. Update development instructions to use the suite environment, not an undeclared system Python.
- [ ] Review distribution inclusion rules. `MANIFEST.in` recursively includes docs/tests, so scratch notes or wheelhouses beneath those directories would ship in sdists. Retain this intentional handoff, but remove task scratch files and inspect actual archives. Keep delivered rules and skill content unchanged unless an installation statement directly requires correction; site policy is out of scope.
- [ ] Inspect the complete diff for stale imports, dead helpers, provisional dependencies, duplicate validators, fallback implementations, altered JSON/hash bytes, and unneeded files. Commit the accepted integrated state before final packaging validation.

## Task E: Final checks, main integration, and cleanup

**Owner:** Lead. Reviewers remain read-only.

For each Python endpoint, establish `.venv-py311` / `.venv-py314` and matching `.venv-build-py311` / `.venv-build-py314` under task-owned storage. The suite environment contains the project development extra; the build environment contains build and backend requirements. Prepare wheelhouses from the committed integration revision. The following commands illustrate one endpoint; run the same checks for the other with its own interpreter and wheelhouse:

```sh
.venv-build-py311/bin/python -B tests/prepare_wheelhouse.py \
    --project . --dest .state/dependency-refactor/py311/wheelhouse

SHK_BUILD_PYTHON="$PWD/.venv-build-py311/bin/python" \
SHK_TEST_WHEELHOUSE="$PWD/.state/dependency-refactor/py311/wheelhouse" \
SHK_REQUIRE_ALL=1 \
.venv-py311/bin/python -B tests/run_suite.py

.venv-py311/bin/pre-commit run --all-files
git diff --check
git status --short
```

Preparation uses a fresh destination or a verified replacement; do not reuse stale artifacts after metadata changes. Set any hook cache override to an ignored task-owned directory. Hooks can modify files: inspect, commit justified changes, and rerun relevant checks before reporting an exact revision as passed.

- [ ] Pass the complete authoritative suite on Python 3.11 and 3.14 with zero skips and zero tripwire hits. Pass all pre-commit hooks, including isolated mypy, shellcheck, shfmt, mdformat, and ASCII documentation checks.
- [ ] Pass wheel, sdist, editable, and source-launcher checks with clean environments and normal offline dependency installation. Run `pip check`; verify package data, metadata, provenance, no unexpected HOME writes, and no bytecode in source checkouts. Inspect sdist/wheel contents for private files and task artifacts.
- [ ] If Pydantic is retained, pass focused lower-bound and current-version checks and record the resolved versions/platforms. If a required interpreter, wheel, or tool cannot be obtained, report the concrete blocker and keep the task incomplete; do not label it release-ready.
- [ ] Have a fresh read-only reviewer inspect the final integrated diff and acceptance evidence. Resolve material findings and repeat checks affected by fixes. Keep test logs and measurement evidence out of published documentation and package data.
- [ ] Confirm original main has no unrelated changes before updating it. If main advanced, merge its changes into the integration branch and rerun checks affected by that integration; do not overwrite or rebase another session's work. When clean and validated, the lead runs `git merge --ff-only refactor/dependency-integration` from main.
- [ ] Confirm main names the validated integration commit. Run the authoritative suite and required hooks from main with environments whose editable paths resolve to main, then inspect status/diff again. Packaging must still build committed HEAD; do not report an earlier worktree revision as final evidence.
- [ ] Stop writers and confirm no agent or process uses each task worktree. Inspect its status, diff, untracked/ignored task files, and branch commits. Preserve unexplained or unmerged work. For accepted branches, prove ancestry with `git merge-base --is-ancestor BRANCH main` before ordinary removal.
- [ ] Remove disposable environments, wheelhouses, caches, and scratch files only after identifying exact task-owned paths and preserving needed evidence. Do not use broad deletion globs, `git clean -fdx`, forced worktree removal, or deletion of the repository/root `.worktrees` directory. Route any bounded recursive cleanup through its direct canonical command for automatic review.
- [ ] Remove clean, inactive integrated worktrees with `git worktree remove PATH`, then their merged branches with `git branch -d BRANCH`. Remove the integration worktree and merged branch last. A rejected pilot follows Task C's cleanup-commit-and-merge path, preserving its history without retaining its implementation. If unexplained work remains unmerged, preserve it and report the precise blocker rather than force-deleting it.
- [ ] Confirm final main status is clean, no task process remains, and the worktree list contains no completed task worktrees. Retain only the intended source, tests, and documentation. Report main's revision, accepted/rejected pilot outcome, exact check results and tested versions, cleanup state, and any limitations. State explicitly that nothing was pushed, tagged, or published.

## Reference material

- [PyPA build usage](https://build.pypa.io/en/stable/how-to/basic-usage.html): default sdist-to-wheel construction and frontend options.
- [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/) and [configuration](https://docs.pydantic.dev/latest/api/config/): strict validation and unknown-field handling.
- [Pydantic model immutability](https://docs.pydantic.dev/latest/concepts/models/#faux-immutability): nested values need separate immutability handling.
- [Pydantic 2.12 release](https://pydantic.dev/articles/pydantic-v2-12-release): Python 3.14 support.
- [Hypothesis settings](https://hypothesis.readthedocs.io/en/latest/settings.html) and [integrations](https://hypothesis.readthedocs.io/en/latest/reference/integrations.html): bounded, reproducible unittest-compatible property tests.
- [mypy pre-commit mirror](https://github.com/pre-commit/mirrors-mypy): isolated hook dependency installation.
