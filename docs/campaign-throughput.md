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

## Select compatible resources by completion time

Keep partition, GPU count, CPUs, memory, walltime and constraint explicit in the
submission. The owner defaults below are agent guidance, not additional TOML
keys, automatic hardware selection or a new site policy.

Before submitting a GPU job, check how many eligible nodes can fit its CPU and
host memory request alongside its GPUs. A node with a free GPU can already have
most of its CPUs or scheduler memory allocated. Start with the worker's useful
thread count and a representative peak host memory measurement (for example,
`sacct` `MaxRSS` of the step that ran the worker: the batch step, or the
`srun` step that launched it), plus headroom for the largest input and model
loading. Check GPU memory, kernels and numerical precision separately.

Compare plausible requests in one current snapshot. For example, if a worker
uses four threads and peaks at 12 GiB of host RAM, compare a 24 GiB request
with a 48 GiB request.

```sh
shk resources --partition owners --summary --by sku \
  --shape measured:cpus=4,mem=24G,gpus=1 \
  --shape larger:cpus=4,mem=48G,gpus=1
```

Read `slots` for the GPU features the workload accepts. For combined SKU and
class constraints, apply both conditions to the per-node output from
`shk resources --partition owners`. Each slot requires enough unallocated CPUs,
host RAM and GPUs on the same node. If an eligible node has one free GPU,
four free CPUs and 32 GiB of scheduler memory left, only the 24 GiB request
fits there. Choose a request that covers the workload and fits more available
capacity. More fitting slots can improve backfill opportunities. Priority,
reservations and other pending work still determine the start time.

Use a broad OR constraint over compatible GPUs rather than one SKU without a
technical reason. Discover current feature names and limits with `sh_node_feat`
and `sh_part`; inspect the actual memory and architecture with `shk resources`.
The candidate set includes A100_PCIE, A100_SXM4, A40, H100_SXM5, H200_SXM5,
L40S, RTX_3090, TITAN_V, V100_PCIE, V100S_PCIE and V100_SXM2. Add compatible
newly discovered models instead of treating that list as a permanent allowlist.
Avoid RTX_2080Ti and Pascal-era P100/P40/TITAN_Xp by default. TITAN_V (12 GB)
and RTX_3090 (24 GB) require sufficient workload memory headroom. Small MIG
slices require their own capacity and partition checks; interactive dev is not a
production batch fallback. Narrow the OR set for concrete memory, architecture,
kernel or dtype requirements, recording why.

Use `shk submit --prefer` for a soft preference over the compatible, discovered
L40, A and H families and future B or newer families. They form one preferred
tier; OR order does not rank those families. The tier is purely a
compute-capability ordering, so it applies across projects. `--prefer` is soft:
Slurm tries it first, then the fallback `--constraint`. With the candidate
inventory above, the preferred expression is:

```text
GPU_SKU:A100_PCIE|GPU_SKU:A100_SXM4|GPU_SKU:A40|GPU_SKU:H100_SXM5|GPU_SKU:H200_SXM5|GPU_SKU:L40S
```

Keep the broader compatible set in `--constraint` so other acceptable GPUs can
run when the preferred set is unavailable. Slurm tries `--prefer` independently
before `--constraint`, not as their intersection: the preferred set must be a
subset of the allowed hardware and both expressions must include every hard
memory, architecture or other feature requirement. Adapt both lists to the task;
add a future SKU only after discovery, never by guessing a feature or using a
family wildcard. This does not reserve GPUs, promise a start time or justify
waiting indefinitely for a preferred card. See the
[Slurm sbatch reference](https://slurm.schedmd.com/sbatch.html#OPT_prefer).

GPU models are scientifically equivalent: they may be mixed, and a job may get
another model after a requeue. The model alone is no reason for new controls,
cohorts, reruns or per-model qualification. Scientific comparison defaults treat
compatible GPUs as equivalent while retaining the actual GPU as provenance. A
different SKU alone does not require new controls, a separate cohort or
rerunning complete results. Keep effective inputs, model/checkpoint, seeds,
numerical settings and environment protocol fixed. This is a comparison
convention, not a bitwise-reproducibility claim or proof of binary
compatibility. Explicit scientific requirements can override it. Use
representative native validation for real compatibility uncertainties; do not
build a complete model-by-SKU qualification matrix before submitting ready work.

Prioritize the earliest start, and compare expected time to completion across
authorized partitions: queue wait, startup/loading, useful computation and likely
recovery. Department capacity, resumable owners and the public gpu partition are
options, alone or together in one partition set; none has a universal priority.
Borrowed btrippe/possu follow the existing courtesy budgets and time window
unchanged. Prove same-command, idempotent checkpoint/resume before owners, alone
or in a set: a set with `owners` is preemptible and requeues by default, so its
script needs the same-command resume that `owners` needs even when the attempt
starts on another member. Slurm requeue does not implement application
recovery. Do not depend on receiving a usable preemption signal before saving
progress.

A set that names `bioe` or `stat` is sized for the department partition, not
for `owners`. A job that names either, alone or in a set, falls under their
courtesy text: respect fairshare, leave room for other department users and run
few GPUs there (stat's courtesy text refers to bioe's). Both are small: 20 GPUs
in `bioe` and 8 in `stat`
([observed 2026-11-25](validation/observed-slurm.md#partition-tiers-and-preemption)).
Read `shk occupancy --partition bioe --partition stat` first: your own
`running_gpus` per partition is what the department loses; `pending_jobs`
counts records, so a pending array is one record and a pending set job appears
in every partition it lists. Bound what you can run there from your own live
submissions that name the partition: the sum of min(tasks, `%M`) times GPUs
per task. Raising a throttle with `shk update --throttle` or releasing a held
job that names `bioe` or `stat` is the same decision as a submission. Submit
the rest to `owners,gpu` or `owners` as different work units, never the same
unit twice. These terms are text only: shk counts nothing, warns of nothing and
refuses nothing there, and keeping to them is the agent's judgment.

Three kit mechanisms help find the earliest start. Each has limits, and none
chooses a request, lowers a floor or ranks GPU models:

- A compatible partition set. `shk submit --partition owners,gpu` passes one
  comma-separated value to Slurm, which starts the job in whichever member can
  start it first. A set is compatible when every member is listed and takes
  batch jobs, none is borrowed, and, with `--gpus` above 0, every member allows
  GPUs. So `owners`, `gpu`, `bioe` and `stat` combine, `normal`, `bigmem` and
  `service` combine too, and `btrippe` and `possu` stay alone: a borrowed
  member is exit 3 `partitions_incompatible` (submit a borrowed partition
  alone). Separate submissions carry different work units, never the same unit
  twice. A repeated `--partition` flag is not a list: the last one wins. A set
  states that the job may run on any member, so it follows every member's
  rules and courtesy text wherever it starts. Requeue defaults to on when a
  member allows it (`owners`); `--no-requeue` is accepted, and `--requeue` on a
  set without such a member is exit 3 `requeue_not_allowed`. A requeued job
  keeps its whole list and can start on another member and another GPU model
  (Slurm source, not observed live). The members of a pending set are tried
  department partitions first, then gpu, then owners. A record waiting in
  `bioe` or `stat` can preempt `owners` jobs on that department's nodes, your
  own included (Slurm has no same-user exemption: source-confirmed; such a
  preemption is not observed). The tiers and nodes behind both were
  [observed on 2026-11-25](validation/observed-slurm.md#partition-tiers-and-preemption):
  PriorityTier 1000 for `bioe` and `stat`, 10 for `gpu`, 1 for `owners`, and
  every `bioe` and `stat` node is also an `owners` node.
  The kit sets no number for `owners,gpu`; Slurm does: every pending task of a
  set that lists `gpu` counts against the submit limits of the gpu partition
  QOS, per user and per account, until it starts (Slurm source; Sherlock's
  values are not verified). Before a real submit of a set that lists `gpu`, run
  the same request with the real `--array` under `--preflight`, and send work
  beyond what gpu admits to `owners` alone. A rejection is exit 3 under
  `--preflight` and exit 5 on a real submit. Sherlock refused an update to
  `normal,bigmem` for one member's accounting or QOS policy
  ([observed](validation/observed-slurm.md#job-updates-in-place)).
  `shk status --task` adds `current.ran_on` for such a job, the sorted
  partitions in which a task or attempt started. Without a spec (`--all`,
  `--name`, `--job-id`) there is no `ran_on`: `current.partition` is the first
  row's text, which for an array whose tasks started on different members names
  only one of them or the pending list. The per-row partition of
  `status --details`, or `--task`, tells where each ran. Sherlock's acceptance of
  a list in a real submission is not observed live.
- The preflight estimate. `shk submit --preflight` adds `estimate` beside the raw
  `preflight` reply: a `parsed`, `absent` or `unparsed` status, Slurm's start
  time, nodes and partition, the line it was read from and `observed_at`. It is
  advisory, not a reservation and not a promise, and an accepted request can
  carry no estimate (`absent`, with null `start`, `nodes`, `partition` and
  `line`). The start is the text Slurm
  printed, in the local time of the `sbatch` process (`start_zone` is
  `sbatch-local`). For a partition list Slurm names the first partition, in its
  own priority order, that can run the job and estimates its start there: it is
  not the earliest start over the set (source-confirmed, not observed live).
  Comparing partitions takes one preflight
  per partition, for a bounded set of requests and without a polling loop.
- The resources summary.
  `shk resources --partition P [--partition P ...] --summary` reads one snapshot
  and reports per requested partition, and once over all of them, the node
  counts by class and the configured, allocated and free CPUs, memory and GPUs.
  `--by sku` and `--by class` add breakdowns by the exact set of a node's
  `GPU_SKU:` or `CLASS:` features, and each `--shape NAME:cpus=N,mem=SIZE[,gpus=N]`
  adds the slots of that request shape that fit on countable nodes. It is a
  snapshot heuristic: it does not model fairshare, QOS or partition limits,
  reservations, preemption or pending work, reserves nothing and predicts no
  start. Unknown capacity stays null, never 0, numbers beside unknown nodes are
  lower bounds, and overlapping partition groups or breakdowns must not be added
  together. A summary of live node records is not observed live.

## Size bundles to amortize queues

Prefer fewer, longer useful allocations when repeated queue waits and model
startup dominate. A bundle can contain many independently checkpointed units;
each seed or sample does not need its own job. Multi-hour jobs are appropriate
when measured work, partition limits, courtesy and recovery support them. There
is no general two-hour cap inherited from examples. Do not request maximum
walltime without a workload reason: longer requests can also wait longer for
backfill. Balance this tradeoff using observed queue delay and measured unit time,
not a rule that shorter jobs are always better.

Keep enough independent bundles queued to use authorized parallel capacity,
while avoiding excessive fragmentation. Preserve the full per-unit scientific
budget and export completed units without waiting for the entire bundle.
Periodically inspect batched status and resource evidence at bounded decisions,
respecting every next-check time and at least 60 seconds between checks, without
polling loops. Adjust future GPU constraints, partitions, bundle sizes and
walltime to useful capacity and measured progress. Changing ownership of already
submitted units still requires the safe-transfer conditions below; a free-looking
node does not authorize duplicate submissions or guarantee an earlier start.

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
sources before their jobs need them. Create Sherlock environments under the
permitted persistent location: a login node takes small pure-Python installs
and checks that end in seconds, while compiling, heavy installs and tests
beyond seconds run in one `dev` run (`srun --partition=dev --time=MIN CMD`, see
[environments on Sherlock](install.md#environments-on-sherlock)) or in a batch
job. Keep necessary allocation, environment and provenance checks in each job,
but do not put a general CPU test suite on every GPU job's startup path.

### Run different revisions from one Git mirror

A project can keep one Git mirror on Sherlock and add a detached worktree for
each revision that concurrent jobs need. The project creates and removes these
worktrees with Git; Kit does not manage them. It submits the project's batch
script and transfers its inputs and outputs.

Before submission, record each job's absolute worktree path, full commit SHA,
interpreter or container image and its own writable output directory. At job
startup, check the commit with a read-only command such as
`git -C "$WORKTREE" rev-parse HEAD`, the working directory, the runtime
identity and the file paths of the modules actually imported. An editable
install (`pip install -e`) in a shared environment imports the checkout it was
installed from, not the job's worktree: put the worktree's source directory
first on `PYTHONPATH`, or install each revision into its own environment. Jobs
at the same commit can share one worktree when each writes its outputs to its
own run directory.

Never change a worktree, environment or model asset in place while a submitted
job may still use it. A pending job starts later and a requeued job runs its
script again, so either would pick up the change. Put a new version at a new
path and point later submissions at it. Remove an old worktree or asset only
after Slurm accounting shows every job that references it as terminal and
unable to restart; pending, running, requeued and unknown jobs keep it.

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
4. A passed pilot admits scaling of its demonstrated configuration over the
   lane's compatible OR set, the set already narrowed by its hard
   requirements; it is not repeated per SKU. Validate a member of that set
   separately only when it carries a concrete open uncertainty that the pilot
   did not exercise: less memory headroom than the pilot's GPU, or kernel,
   dtype or CUDA-architecture support that is actually in doubt for that
   member. A different SKU or architecture alone is no such doubt, and there
   is no per-architecture gate. An SSH connection, accepted
   preflight, CPU import or allocation proof is not proof that the model ran
   correctly. Use the actual worker PID for allocation evidence; environment
   variables or guessed cross-container ancestry are not substitutes.
5. Keep one active first pilot per unadmitted lane. Different tokens, bundle
   names or incidental fingerprint differences must not bypass that lease.
   Validate additional hardware only when ready work needs it and such an
   uncertainty is open; do not build a model-by-GPU compatibility matrix before
   making progress. A pass on one GPU does not prove memory headroom on a
   smaller member of the OR set: encode that hard requirement in the
   constraint, or validate that member, before relying on it.

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

A bounded change of a pending request uses `shk update`, which changes the job
in place. It keeps the job id, so the units stay with that job and jobs that
depend on it (`--after-ok`) keep waiting for it, and it settles its outcome
from a re-read of every record. Its limits:

- One field group per call: `--time`, `--partition`, `--throttle`, or
  `--constraint` and `--prefer` together, since both must carry every hard
  requirement.
- Time, partition and features change only while every record of the job is
  pending; the time limit only decreases.
- A partition move goes to a listed, non-borrowed partition or set whose
  requeue default, on when a member allows requeue, equals the job's Requeue
  flag, so never to `btrippe` or `possu`, and takes a GPU job only to
  partitions that allow GPUs. A requeueable job therefore moves only to
  `owners` or a set with `owners`, any other job only to a target without it,
  and dropping `owners` from a pending set is a cancel and resubmit for a job
  submitted with the default requeue ([command reference](cli.md#update)). A
  move can lower the job's age priority for a while.
- Slurm checks constraint and preference names against the features of the
  whole cluster, not of the job's partitions: check them with `shk resources`
  first, or the job is accepted and never starts.
- An array's throttle can be lowered in any state, and raised except on a
  borrowed partition.
- CPUs, memory, GPUs and the request of a running job do not change: cancel
  and resubmit those, once Slurm shows the old job terminal.

The frozen spec keeps the request as submitted;
`shk status --job-id N --details` shows the current one. An exit 5 reply from
`shk update` is settled by the status read in its `next` before any further
change.

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
| Many jobs are pending despite free-looking GPUs | Complete status, current request and schedulable resources | Consider a bounded request change with `shk update`, which keeps the job id and its dependents, or a safe transfer; do not assume a scheduler defect. |
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
