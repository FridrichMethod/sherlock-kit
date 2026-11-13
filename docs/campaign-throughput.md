# Running a parallel GPU campaign

Keep ready, independent work moving while other work prepares, validates or waits
in the queue. A requested GPU count is a target, not an allocation guarantee.
Distinguish work that is ready but not submitted from work that Slurm has queued.
The first is a project coordination issue; the second can reflect priority,
backfill, reservations or resource constraints.

This guide describes a project workflow. Kit supplies submission, evidence and
transfer primitives; it does not maintain a campaign ledger, certify model
compatibility or choose scientific retry policies. Existing site rules,
partition permissions and project pins still apply. See the
[design contract](design.md) and [allocation evidence](allocation.md).

## Before submitting: record a plan that can run

Use one coordinator with a durable lock for short ledger, intent, token and epoch
transactions. Persist a worker lease and process identity before releasing the
lock; do not hold it while waiting for a bundle, job or agent to finish. Verify
PID plus process start identity before replacing a local worker; an old heartbeat
alone does not prove it stopped. An unresolved launch intent still requires
reconciliation before another launch.
Record the following before dispatch:

- Immutable inputs, model/checkpoint and source identities, numerical settings,
  seeds, per-unit budget, expected outputs and acceptance criteria. Keep these
  fixed when queue pressure changes; a smaller scientific budget is a different
  experiment.
- All planned units and their unique owner, assignment epoch and attempt history.
  Track mutually exclusive states such as preparing, ready, queued, running,
  unknown-held, blocked, accepted and failed. Their counts must sum to the planned
  denominator. Also report expected outputs, accepted outputs and outputs from
  all attempts separately. An unavailable model remains in the denominator.
- A named blocker and next action for every preparing or blocked unit. Readiness
  for a first GPU pilot is distinct from permission to scale an admitted lane.
- An authorized target of unique live remote bundles and local workers, distinct
  from the hard campaign cap and smaller pilot cap. Count pending, held, running,
  unknown and auto-requeue-pending jobs as reserved slots until Slurm evidence permits release; a
  changed source hash does not create capacity or release an old owner. Set
  bounded technical retry and no-progress requeue budgets.

Define an admission lane by the compatibility facts that matter: model and
checkpoint, frozen source and runtime, numerical configuration, required input
features and actual GPU architecture or feature. Scope a blocker to lanes that
share its cause. A shared broken input or container can block several lanes;
an adapter-specific failure need not block another model.

Run ordinary CPU preparation and regression tests locally, in parallel where
independent: input parsing, feature construction, manifest validation, recovery
fault injection and static dependency checks. Stage frozen inputs, weights and
sources before their jobs need them. Create Sherlock environments only inside
an allocation under the permitted persistent location. Keep necessary allocation,
environment and provenance checks in each job, but do not put a general CPU test
suite on every GPU job's startup path.

## Queue independent first pilots as they become ready

1. For each needed, CPU-ready lane, reserve one first-pilot slot and a unique
   unit. Save the exact request and submission token durably before calling the
   project-pinned Kit. Use the existing dry-run and preflight workflow.
2. Submit other independent ready pilots within the campaign and pilot caps
   without waiting for the first pilot to finish. A global gate such as "model A
   must finish before model B can queue" requires an actual shared dependency.
   Do not wait for every lane's CPU preparation to finish either.
3. Run a complete production-sized unit through the real launcher, container,
   actual worker, native model, output validator, immutable export and verified
   ingestion. Record resolved runtime settings, actual GPU feature, source and
   checkpoint identity, output count and hashes, elapsed time and peak memory.
   An exact, valid production unit can count once toward the planned campaign.
4. Admit scaling only for the demonstrated configuration and hardware. An SSH
   connection, accepted preflight, CPU import or allocation proof is not proof
   that the model ran correctly. Use the actual worker PID for allocation
   evidence; environment variables or guessed cross-container ancestry are not
   substitutes.
5. Keep one active first pilot per unadmitted lane. Different tokens, bundle
   names or incidental fingerprint differences must not bypass that lease.
   Validate additional hardware only when needed for ready work; do not build a
   model-by-GPU compatibility matrix before making progress. A pilot on one GPU
   does not admit every feature in a broad request's OR constraint.

For example, suppose three independent lanes have work. Two have passed CPU
checks and the third has a broken input. Queue one full-budget pilot for each of
the first two, within authorized limits, and keep the third explicitly blocked.
Once either pilot passes, fill that admitted lane's available slots while the
other pilot runs or waits. An idle local GPU can take a separately assigned ready
unit on a compatible lane; it cannot take a unit still owned by a queued job.

A Slurm `--after-ok` dependency checks an exit status, not scientific acceptance.
Use it for admission only if the predecessor's success path includes the required
validation. Depending on an entire multi-unit bundle waits for its last unit.
Where useful, the project can validate its first unit before continuing the same
bundle, or use a held successor and release it after verified admission. Release
must still meet current partition permissions, occupancy requirements and caps.

## Keep the coordinator productive

At each bounded decision point:

1. Read the local UTC clock. Batch due status checks, respect every
   `next_check_after` and keep checks at least 60 seconds apart. Resolve unknown
   mutations from Slurm evidence before making a replacement submission; incomplete evidence leaves
   ownership and capacity reservations intact.
2. Ingest verified completed units and update the ledger. Publish immutable
   per-unit exports so durable collection need not wait for the whole bundle.
   Preserve failed attempts, costs and raw outputs even when they are not accepted.
3. Assign independent ready work to idle compatible local capacity and refill
   admitted remote lanes toward the authorized live-bundle target, within the
   campaign cap. The smaller pilot cap limits only unadmitted pilots, not admitted
   production jobs. Whenever below target, report ready-but-unsubmitted count, age
   and reason. Explain each idle slot by its actual blocker; queued reservations
   are not running GPUs.
4. Reassess measured runtime, peak memory and queue delay when they can change a
   decision. A resources snapshot is not a reservation: apparent free GPUs alone
   do not prove that a pending request is wrong or will start immediately.
5. Give each delegated task one writer, a concrete deliverable, a next action and
   an explicit wake-up condition. A message to an idle agent may not start a turn;
   use the orchestration system's dispatch or follow-up action and verify that it
   started. Parallel CPU preparation does not grant another GPU lease. Do not
   schedule future agent or LLM sessions inside Slurm jobs.

Amortize environment checks, model loading and scheduler overhead with a bundle
of sequential units per allocated GPU. A recovery/statistics unit is not
necessarily a scheduler submission. Choose bundle size and walltime from measured
work plus reasonable margin, while preserving each unit's full scientific budget.
Use arrays for suitably sized homogeneous work within the documented limits;
do not submit one job per small sample or pad short work with sleeps.

## Rebalance with evidence and retain ownership

Set a bounded review point for prolonged queueing. Compare queued work's observed
wait, measured local completion time, setup and transfer costs, and the time
needed to confirm a safe cancellation. Inspect the frozen CPU, memory, walltime
and GPU constraints for unnecessary restrictions. Adjust future requests only
within known compatibility and authorized partition rules; shorter walltime must
still fit the complete unit or the project's declared resumable checkpoint scheme.
Queue predictions are estimates, not deadlines or promises.

To transfer units between workers, require either complete evidence that the old
job is terminal and cannot restart, or a project-level durable revocation ACK
that remains effective across requeue. A cancellation ACK alone is insufficient.
A revocation protocol must stop old claims and writes for the transferred units,
acknowledge that they are quiescent, and be consulted on every old-worker restart.
Persist the new owner and epoch before dispatch; workers must reject stale epochs
both when claiming units and when publishing accepted results. Keep existing
accepted output immutable. Do not hedge with duplicate local and pending remote
owners, or release ownership because a dependency has not yet completed.

Resume the same bundle command after interruption. For a fixed-size seed unit,
skip only a complete, checksummed unit that passes the declared technical and
protocol acceptance checks. Run an interrupted unit in a new attempt with the
same seed and full budget; do not concatenate partial samples. A complete result
with poor scientific quality remains a result, not a reason to retry for a better
score. Other checkpoint schemes must state their equivalent guarantees up front.

## Diagnose the observed failure mode

| Observation | Check | Action |
|---|---|---|
| Ready lanes have no queued pilot | Submission ledger and actual dependencies | Queue independent first pilots within both caps; scope the blocker. |
| A pilot imports successfully but fails before inference | Real wrapper, container, PID and environment evidence | Fix the failed launch boundary; keep independent lanes progressing. |
| A GPU job spends startup time on general CPU tests | Startup timeline | Move those tests into local preparation; retain mandatory in-allocation gates. |
| Many jobs are pending despite free-looking GPUs | Complete status, frozen request and schedulable resources | Consider a bounded request change or safe transfer; do not assume a scheduler defect. |
| Local GPU is idle while work is ready | Compatibility, ownership and admission leases | Dispatch an unowned ready unit, or complete a verified ownership transfer first. |
| A mutation reply is lost, or an owners job is pending again | Saved token, known job ID and detailed status | Check Slurm evidence; retain ownership and the reserved slot. |
| A seed restarts or a low-quality result is produced | Unit completeness versus scientific QC | Rerun only interrupted technical units; retain complete low-quality outputs. |
| Setup dominates many small jobs | Measured startup and unit walltime | Bundle more units without changing their scientific budgets. |
| Preparation appears stalled with an idle agent | Agent state and last dispatched task | Explicitly wake it with a bounded task; a queued message is not execution. |

Report achieved concurrency and the complete unit-state denominator, not only a
target GPU count. Separate CPU preparation, ready-to-submit delay, scheduler wait,
startup and native inference walltime. Allocation GPU-seconds measure reserved
resource cost, not GPU utilization; do not add them to native walltime as though
they were the same measurement. Preserve unknown timings and failed allocations,
including allocations that never reached inference. This distinguishes a
coordination opportunity from shared-scheduler waiting without overstating either.
