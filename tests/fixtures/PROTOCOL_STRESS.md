`protocol_stress.py` is an offline safety protocol soak fixture. It never invokes
SSH, sbatch, a Slurm status command, or a nested scheduler. Synthetic transport
acknowledgements and accounting records are explicitly labelled in the report.
Only the containing CPU pilot can provide real submit/status/fetch acceptance.

Run the fixture with the frozen installed toolkit's Python, inside a compute
allocation, using a fresh output directory on `$L_SCRATCH_JOB`:

```sh
"$FROZEN_TOOLKIT_PYTHON" tests/fixtures/protocol_stress.py \
  --duration-seconds 600 --seed 20261007 \
  --output "$L_SCRATCH_JOB/protocol-stress-NEW_ID"
```

For local development only, prepend `PYTHONPATH=src` to that command. The fixture
does not add a checkout to Python's import path; the report identifies the policy,
installed revision/mode, fixture and imported protocol module hashes. Seal the
fixture source itself in the outer pilot's input manifest. Preserve `report.json`
and `cases.jsonl` in that pilot's declared artifact bundle before job termination.
For failures, also preserve the bounded `active-case` counterexample directory.

One `normal`, one CPU, 2 GiB, 30 minute job can combine one 600 second soak with
the consumer's single sealed provenance audit. The fixture keeps at most three
short workers active, all numerical thread limits set to one, fewer than 200 KiB
of source payload per case, and a 64 MiB audit log. Cases are sequential and their
successful private databases/staging trees are removed before the next case.
There is no sleep or test-suite repetition. The source payload, resource vector,
task/input identity, accounting arrays, cache timing and race/fault configuration
change with each independent reproducible case seed. The rsync path includes
spaces, quotes and Unicode; payload bytes and manifests vary independently.

The eight families exercise concurrent admission/deduplication, process death
after durable dispatch claim, late acknowledgement versus reconciliation,
cross-process query cadence, real death at five artifact durability boundaries
and during partial transfer, concurrent artifact fetch, unsafe bundle rejection,
and retry/accounting idempotence. SQLite integrity, capacity, reservations,
manifest/inventory hashes, unrelated-data preservation and receipt identity are
checked on every applicable case. Changed validators/manifests, same-mtime bad
stage bytes, symlinks, array parents, steps and missing restart history are
included. Status query timing uses an explicitly synthetic logical clock;
no site poll is hidden in the fixture.

`report.json` is fsynced after each completed case and includes counts per
semantic bucket and invariant, actual process death count, elapsed time and a
SHA-256 chain over every JSONL case record, plus CPU time and Linux peak RSS for
the parent and children. Every coordination record includes actual attempt UUID,
logical task identity, final state, reservation and accounting result. New JSONL records are fsynced before
the report advances. A failed invariant stops immediately and records its seed,
case index, full parameters, traceback and retained fixture path. Reproduce it
with the same top-level seed and `--replay-index INDEX` into a fresh output root.
An interrupted report remains `running`; it is not accepted as a pass.

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
