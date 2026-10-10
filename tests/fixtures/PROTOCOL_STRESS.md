`protocol_stress.py` is an offline safety protocol soak fixture for the 0.3.0
registry model. Every runner, reader and event writer it exercises is the shipped
`python3 -c` stub (`sherlock_registry.program_argv`) executed as its own process
against a private temporary `registry_root`, exactly as the workstation CLI ships
it to a login node. `sbatch`, `sacct` and `squeue` are fixture scripts on a PATH
that holds nothing but them and a link to `env`, so the program under test cannot
reach the site's Slurm; their replies are synthetic and labelled so in the report.
The fixture never invokes SSH and never submits anything to a scheduler. Only the
containing CPU pilot can provide real submit/status/fetch acceptance.

Run the fixture with the frozen installed toolkit's Python, inside a compute
allocation, using a fresh output directory on `$L_SCRATCH_JOB`:

```sh
"$FROZEN_TOOLKIT_PYTHON" tests/fixtures/protocol_stress.py \
  --duration-seconds 600 --seed 20261007 \
  --output "$L_SCRATCH_JOB/protocol-stress-NEW_ID"
```

For local development only, prepend `PYTHONPATH=src` to that command. The fixture
does not add a checkout to Python's import path; the report identifies the policy,
installed revision/mode, fixture hash, the SHA-256 of every imported protocol module
(`sherlock_registry`, `sherlock_orchestration`, `sherlock_artifacts`,
`sherlock_commands`) and the shipped registry program's hash and argv size, so the
bytes that ran on the login node can be matched to the bytes the soak exercised.
Seal the fixture source itself in the outer pilot's input manifest. Preserve
`report.json` and `cases.jsonl` in that pilot's declared artifact bundle before job
termination. For failures, also preserve the bounded `active-case` counterexample
directory, which retains the registry tree, the fake tool call log and any stage.

One `normal`, one CPU, 2 GiB, 30 minute job can combine one 600 second soak with
the consumer's single sealed provenance audit. The fixture keeps at most three
short workers active (stub processes or forked workers), all numerical thread
limits set to one, fewer than 200 KiB of source payload per case, and a 64 MiB
audit log. Cases are sequential and their registries, local caches and staging
trees are removed before the next case. There is no sleep or test-suite
repetition beyond a sub-second `sbatch` delay that widens the admission race.
The source payload, resource vector, task identity, accounting rows, cache timing
and race/fault configuration change with each independent reproducible case seed.
The rsync path includes spaces, quotes and Unicode; payload bytes and manifests
vary independently.

The seven families are:

- `submit_race`: three runners race one logical task (or three distinct ones);
  the registry lock and the task marker admit exactly one attempt per task, the
  losers are refused naming the winner, `sbatch` runs once per admitted attempt
  with exactly the frozen option list and script bytes, and never inherits
  `SBATCH_*`.
- `runner_death`: the fake `sbatch` kills its runner with SIGKILL either before
  replying or after printing the job id. `record.json` and the marker are durable,
  no reply file exists, the dead runner's lock does not outlive it, the attempt is
  `inconclusive`, duplicate and parent retries are refused, `--abandon` is refused
  while the attempt is young, and once accounting names the job the attempt is
  `terminal` with the `submitted_unrecorded` flag; a `FAILED` end releases the
  retry and moves the marker, a `COMPLETED` end never does.
- `query_cache`: three forked processes ask one bounded scope at the same logical
  instant through `sherlock_commands.cached_query`; exactly one query runs, the
  others take the sentinel or the body, a failed query leaves the sentinel for the
  cadence, the cache refreshes at 60 s, a forgotten scope is re-queried, and the
  cache file stays private with its own lock, never the transport cooldown's.
- `resolution`: a real submission followed by randomized accounting (single job or
  array of 2-6 tasks, `normal` or `owners`, restarts, running tasks, one pending
  aggregate row, a missing task, an anomaly on a non-preemptible profile) through
  the shipped reader; the shipped program and the imported `resolve()` must agree,
  per-task counts and the summed cost must match the generated plan, identical
  duplicate rows, step rows and another attempt's rows change nothing, `released`
  follows the COMPLETED rule, the anomaly list is exactly empty on a regular plan
  and exactly `acknowledged_preemption` after the waiver, `event resolved` closes
  exactly a terminal attempt once and removes it from `--open`, and `event ack`
  waives exactly the anomalous task at its top restart and is refused everywhere
  else.
- `fetch_death`: real death at five artifact durability boundaries and during a
  partial transfer; the attempt sidecar is durable before the transaction, a
  corrupt same-mtime stage byte is repaired, and the rerun promotes exactly the
  manifest's inventory with one sidecar.
- `fetch_race`: two fetchers of one bundle; one promotes, one recovers, one sidecar.
- `fetch_reject`: false or mutating validators, a changed source manifest, a stale
  extra stage, a conflicting existing destination, a symlink in the stage and a
  foreign attempt sidecar are refused without promotion and without touching
  unrelated data.

`check_registry` runs after every registry family: the root, `attempts/` and
`tasks/` are owned 0700, every file is 0600, no symlink or `.tmp` survives, every
attempt directory holds one `record.json` whose id and key match, stamped by this
program revision, at most one of `submitted.json`/`not_sent.json`, only known event
files, and every task marker names an existing attempt of that key. Status query
timing uses an explicitly synthetic logical clock; no site poll is hidden in the
fixture.

`report.json` is fsynced after each completed case and includes counts per
semantic bucket and invariant, actual process death count (killed runners and
crashed fetchers), elapsed time and a SHA-256 chain over every JSONL case record,
plus CPU time and Linux peak RSS for the parent and children (stub processes and
rsync included). Every registry case record includes the attempt ids, task keys,
registry files and parent links the case left behind. New JSONL records are fsynced
before the report advances. A failed invariant stops immediately and records its
seed, case index, full parameters, traceback and retained fixture path. Reproduce
it with the same top-level seed and `--replay-index INDEX` into a fresh output
root. An interrupted report remains `running`; it is not accepted as a pass.

The 600 seconds bound *generation*: the last active case finishes or fails within
its bounded worker/rsync deadlines. Thus runtime can slightly exceed 600 seconds;
the enclosing 30 minute walltime must include startup, final report and audit.
The actual outer job elapsed time is the evidence for the site's useful-work
duration, not the configured duration. Failure exits early, even if this leaves
the job shorter than ten minutes. The fixed semantic buckets usually saturate in
the short measurement; continued unique parameter draws provide race/failure
robustness evidence rather than claiming new semantic coverage. A report exposes
both novel and revisited bucket counts. `--max-cases` (at most 40,000) is a second
bound; reaching it ends the soak and must be considered when checking duration.

Use `--duration-seconds 30` for the initial measurement and the unittest's 16 case
smoke for normal CI. The 600 second acceptance run belongs to the real outer job
once, rather than repeating it locally and remotely to fill runtime.
