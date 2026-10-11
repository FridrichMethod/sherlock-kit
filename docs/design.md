# Design (0.4.0)

`shk` is a thin, stateless interface between an agent and Sherlock's Slurm and
filesystems. It answers three questions with as little machinery as possible:
is this logical task already in Slurm, what is its state, and did this data
arrive intact. Everything else (authorization, scientific validity, what a job
script does) stays with the user, the site rules and the project.

## Pieces

| Module | Role |
|---|---|
| `cli.py` | argparse frame, `Context` (lazy config, executors, clock), JSON output, exit codes |
| `errors.py` | exit codes, `ShkError(code, token, message, hint)`, `Outcome(payload, code)` |
| `config.py` | `[tool.sherlock-kit]` discovery and validation |
| `executor.py` | `SshExecutor` and `LocalExecutor`, the master preflight, detection, `run_program` |
| `partitions.py` | the packaged partition table and the generated rules block |
| `doctor.py` | read-only checks |
| `spec.py`, `slurm.py` | resources, job name, sbatch argv; `submit`, `status`, `cancel`, `occupancy` |
| `transfer.py` | `push`, `pull`, `manifest` |
| `install.py` | `rules`, `install-skill` |
| `data/remote/*.sh` | the fixed POSIX `sh` programs run on the login node |
| `data/rules/*.md`, `data/skills/sherlock-kit/SKILL.md` | agent content, delivered by dotfiles or `install-skill` |

`bin/shk` is a stdlib-only entry point: it follows symlinks to the repository,
puts its `src/` first on `sys.path`, disables bytecode (a dotfiles submodule
stays clean) and refuses Python older than 3.11. The version is the literal
`sherlock_kit.__version__`, which `pyproject.toml` reads as its dynamic version, so
the running code always reports its own version; `shk --version` names the
provenance (`git`, `editable`, `submodule`, `source` or `unknown`).

## No state

`shk` writes nothing outside the paths a command names: no state root, no cache,
no cooldown file, no ledger, nothing under `HOME` (a test runs `--version`,
`rules`, `doctor` and `submit --dry-run` with an empty `HOME` and checks it stays
empty). The facts live where they already are:

- Slurm (`squeue -t all`, `sacct`) is the only record of what was submitted.
- The run directory on Sherlock keeps `.shk/<name>.<jobid>.sh` and `.json` (the
  script as submitted and its frozen spec) as provenance; on scratch it is
  subject to the 90-day purge like everything else there.
- `pull` keeps its stage, lock and receipt beside the project's own destination.
- The project configuration is a project file.

## Configuration

`--project-dir DIR` reads exactly DIR. Otherwise discovery walks up from the
working directory to the first directory with a configuration; in each directory
`pyproject.toml` with `[tool.sherlock-kit]` wins over `sherlock-kit.toml`.
Unknown keys, bad values and unreadable files are `config_invalid`. Remote paths
are absolute and contain no `..`; the remote user comes from `id -un` inside each
remote program.

## Executors and the master preflight

`detect()` picks the executor: `--executor` or `SHK_EXECUTOR` first; else local
on a Sherlock login node (`^sh\d+-ln\d+$`), with `SLURM_CLUSTER_NAME=sherlock`,
or on an `sh<N>-` compute node inside a job; else ssh. It never probes system
configuration and never infers Sherlock from `sbatch` being on `PATH`.

Every ssh dial is preceded by `ssh -O check -oBatchMode=yes HOST`, which only
asks the local control socket:

- no master: exit 4 `auth_required`, hint `ssh sherlock-plain` (or
  `ssh -fN sherlock-dtn` for the data host). Only the `-O check` ran;
- no usable `ControlPath`: exit 4 `unavailable` with a `~/.ssh/config` hint,
  because logging in again would not help;
- no `ssh`: exit 4 `unavailable`, hint `install OpenSSH`.

The dial itself is `ssh -T -oBatchMode=yes -oRemoteCommand=none -oConnectTimeout=N -oConnectionAttempts=1 -oStrictHostKeyChecking=yes -oUpdateHostKeys=no -oControlMaster=no -oPermitLocalCommand=no -oClearAllForwardings=yes -oForwardAgent=no -oProxyCommand=false HOST CMD`, where
CMD is one `shlex.join` of a literal argv. The check and the dial are separate
processes: if the master dies in between, or refuses the session, OpenSSH would
fall back to a fresh login. `-oProxyCommand=false` turns that fallback into an
immediate failure without changing the control path, so `shk` cannot cause an
authentication attempt, let alone a storm.

Results map to `complete`, `failed` (a read exited nonzero), `unknown` (a
mutation exited nonzero or timed out after dispatch: never retried),
`auth_required` or `unavailable` (ssh exit 255 with or without authentication
text, such as a dropped network) and `timeout` (a read); these exit 4 with the
hint `run shk doctor`. The Sherlock login banner is dropped. A timeout or
interrupt kills the child's whole process group. The local executor runs the
same programs with `sh -c` and no preflight; the data executor on any Sherlock
host refuses (exit 3), because transfers start outside Sherlock and the site
forbids ssh to a DTN from Sherlock.

## Job names

```text
shk.<project>.<slug(task)[:20]>.<sha256(canonical([project, campaign, task]))[:10]>
```

At most 60 characters of `[a-z0-9.-]`. Retries reuse the name; the newest base
job id under a name is the current job and older ones are history. The `.`
separator makes `shk.<project>.` an unambiguous prefix for `status --all`.

## Submit

1. **Offline checks.** The partition is listed in `partitions.json`; GPUs only
   where the profile allows them; requeue only where the profile allows it
   (today `owners`), else `--no-requeue`; the `--mem`, `--time` and `--array`
   grammar, at least one CPU and task; a script of at most 1 MiB whose leading
   `#SBATCH` lines do not set a directive `shk` owns (`-A`/`--account`,
   `-x`/`--exclude`, `-G`/`--gpus*`/`--gres`, `-p`/`--partition`,
   `-a`/`--array`, `-d`/`--dependency`, `-b`/`--begin`, `-J`/`--job-name`) or
   start a heterogeneous job (`hetjob`, `packjob`); a borrowed partition needs
   `--courtesy-ack`. The denylist follows sbatch's getopt parsing: abbreviated
   long options (`--part`) and short flags bundled after value-less ones
   (`-Hp`) count. `--constraint` and `--signal` go to sbatch unchanged, which
   checks them itself; each `--after-ok` value (repeatable) must be one job id,
   `N` or `N_TASK`, because `shk` joins them into its own `--dependency`
   string, where a `,` or `?` would add clauses. Build the sbatch argv
   (explicit name, partition, CPUs, memory, minutes, run directory, log paths,
   `--export=ALL,SHK_NAME=<name>`, `singleton` for single jobs) and the frozen
   spec. `--dry-run` stops here, with no network at all.
2. **Read call** (`query.sh`, shared with `status`): one `squeue -t all` and one
   `sacct --duplicates` over the last 30 days for the user and name, plus the
   newest spec file; for a borrowed partition also its occupancy. Only
   `SHK_`-prefixed lines count, and the read is complete only with `SHK_END`
   and every tool returning 0 (else exit 1 `malformed_read`). Every remote
   program first unsets `SBATCH_*`, `SQUEUE_*`, `SACCT_*` and `SLURM_*` (except
   `SLURM_CONF`).
3. **Local decision** (pure): `new` (no rows) submits; `live` (any task pending,
   running, suspended or completing, including throttle-held array tasks that
   `squeue` reports only as an aggregate) is refused even with `--resubmit`;
   `done` and `failed` are refused unless `--resubmit REASON`; `stale` (spec files
   but no Slurm rows) is informational and treated as `new`. On a borrowed
   partition (`btrippe`, `possu`) the reply carries the occupancy and the
   courtesy text, and a warning when others are running there and you already
   have 2 jobs there or ask for more than 2 h; it never refuses.
4. **Mutation call** (`submit.sh`, the script on stdin): a live guard, staging of
   the script and spec under `<run-dir>/.shk/`, a digest check, `sbatch` on the
   staged script, then exactly one terminal marker: `SHK_SUBMITTED <jobid>`
   (exit 0), `SHK_LIVE`, `SHK_REJECTED <rc>` with stderr or `SHK_REFUSED <why>`
   (nothing ran; exit 3), or `SHK_UNPARSED` (exit 5). A transport failure after
   dispatch or a missing or
   duplicate marker is exit 5: the reply's `next` names the `shk status` command
   and the time before which not to run it. Running the same `shk submit` again
   is safe: it sees `live`, `done` or `failed`, or `new` if nothing reached
   Slurm.

## Status, cancel and occupancy

`status` batches every selected name into one read call. Per name it reports the
state, the current job (partition, array size, task counts by state, exit codes,
restarts, CPU and GPU seconds) and the history, plus `queried_at` and
`next_check_after` (now + 60 s). Anomalies are evaluated on the current job only:
`unexpected_preemption` (a restart or `PREEMPTED`/`REQUEUED` row on a profile
that is not preemptible), `duplicate_live`, `unknown_partition`,
`malformed_rows`; any of them gives exit 6. Nothing is acknowledged or stored,
so nothing needs closing: a deliberate `--resubmit` makes a new current job.

`cancel` resolves one name and cancels only its live jobs (`scancel` scoped by
user and name), reporting the job ids and `next_check_after`; a lost reply is
exit 5 with a `next` like submit's. `occupancy` reads the partition's current use
with the fixed-width `squeue -O` layout confirmed live in 0.2.0.

## Transfer

`push` checks the DTN master, then runs `rsync -rlt --protect-args --partial`
into an absolute REMOTE under `remote_root` (unless `--anywhere`) and never
deletes. `-t` keeps the local modification times: a re-push compares size and
time and skips unchanged files, where rewriting them would refresh their purge
clock on scratch, which looks like purge evasion. The cost is stated plainly:
pushed files keep their local modification times, so inputs that are old
locally can be purge-eligible soon after a push.

`pull` reads `REMOTE/SHA256SUMS` (GNU `sha256sum` format) through the control
host and needs it to list every regular file under REMOTE except `SHA256SUMS`
and `.shk*` entries, so the job writes it over its own output subdirectory, not
the run directory with its `.shk/` and logs. It refuses unsafe entries
(absolute paths, empty, `.` or `..` components, `.shk` components, duplicates).
With NAME the last component of LOCAL, it takes a lock on `.NAME.shk-lock`,
rsyncs exactly the listed files into the private stage `.NAME.shk-stage` beside
LOCAL (pruning anything the manifest does not list, so a rerun resumes it),
verifies the inventory and every hash, re-reads the manifest, then runs the
validator named by `--validate`, if any: its argv from
`[tool.sherlock-kit.validators]` plus the stage directory as the last argument,
from the project root (the directory of the configuration file); exit 0 passes,
and the validator treats the stage as read-only because whatever it writes is
promoted. Then it renames the stage onto LOCAL and writes
`.NAME.shk-receipt.json`. An existing LOCAL with a receipt for the same manifest
is `already_present`; any other existing LOCAL is kept unless `--replace`,
which renames it to `.NAME.shk-old-<UTC>` (never deletes). `manifest DIR`
writes `DIR/SHA256SUMS` inside jobs; the skill's `sha256sum` one-liner writes
the same.

## Weaker guarantees, accepted

- Deduplication uses only `squeue -t all` and `sacct` over the last 30 days
  (`status` takes `--since`). Spec files are reported as `stale` but never block:
  a task with no Slurm rows in that window is submitted again as new, without
  `--resubmit`, even when its run directory still exists, and the new job runs
  in the same run directory.
- Check-then-submit is a race: two submitters can both pass the guard. For a
  single job `singleton` keeps the two from running at once; arrays have no
  backstop; `status` flags `duplicate_live`. There is no exactly-once claim.
- `--all` lists names by prefix within a time window; there is no ledger of
  everything ever submitted.
- Cost is informational: no budget, cap or reservation.
- The 60 s cadence is advisory (`next_check_after`), enforced by the rules, not
  by a file.
- A master can die between the check and the dial; `-oProxyCommand=false` makes
  that dial fail instead of logging in, at most once per command.
- The remote side trusts `sh`, `sha256sum`, `squeue`, `sacct` and `sbatch` on the
  login shell's `PATH`.
- Provenance lives in the run directory and is purged with scratch.

## Rules, skill and delivery

The rules files and the skill are package data at fixed paths (contracts C1-C3,
with `bin/shk` as C4). dotfiles links them from its submodule into
`~/.claude/rules/`, `~/.claude/skills/` and `~/.agents/skills/` and generates the
Codex override from the rules, which is why the rules start with an H2 and carry
no front matter, HTML comments or imports. Projects copy the skill with
`shk install-skill`, which follows links inside the project (a shared
`.agents/skills -> ../.claude/skills`) and refuses one that leads out of it, so
it never writes through a link into `HOME` or a dotfiles submodule. Codex has no
rules import: with `--rules` it gets a hint to append `shk rules KIND` to the
project's `AGENTS.md`. The partition block in both rules files is generated by
`partitions.markdown()` and a test keeps it byte-identical to the table.
