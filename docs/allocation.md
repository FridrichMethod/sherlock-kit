# Local worker allocation evidence

```sh
shk --executor local allocation --pid PID --job-id N_OR_N_TASK \
  [--step-id NUMERIC_STEP] [--hostlist EXPRESSION ...]
```

The caller supplies the worker PID, not the toolkit subprocess or its query
shell. The command never uses SSH and requires no project configuration. The
worker must belong to the current Unix UID, exist on this host, and have stable
Linux `/proc/PID/stat` start ticks and kernel affinity before and after evidence
collection. Ownership is checked before reading process contents. A dead,
zombie, replaced or changing process cannot produce complete success.

The command reads actual Slurm job, optional numeric step and local node
records with fixed POSIX shell programs, all within the configured
`command_timeout` (default 60 seconds). Every reply line is one record, parsed
strictly: malformed or duplicate fields fail. A reply may hold several records:
`scontrol show job N` for the array element that keeps the array's meta record
(`JobId` equal to `ArrayJobId`) also returns every sibling element. The proof
keeps only the one record whose `JobId` is the selected `N`, or whose
`ArrayJobId_ArrayTaskId` is the selected `N_TASK`; zero or several matching
records fail. The step of an array element is queried and identified as
`ArrayJobId_ArrayTaskId.STEP`, which is how Slurm prints it; any other step as
`JobId.STEP`.

`scontrol pidinfo PID` proves the supplied PID belongs to the selected physical
job. An optional numeric step additionally requires exactly one matching row in
`scontrol listpids JOBID.STEP`; listpids takes and prints only physical job IDs.
Environment variables alone never prove allocation membership. At most 32
caller hostlists are accepted. Scheduler and caller hostlists are expanded
through Slurm; duplicate or malformed hosts fail.

A `pidinfo` reply has exactly one job identity line,
`Slurm JobId=N ends at TIME`; only N is compared, and a step suffix after it
(such as `.batch` or `.interactive`) is ignored. It may include one nonnegative
remaining-time line: `slurm_get_rem_time is N` (the
[SchedMD troubleshooting example](https://slurm.schedmd.com/SLUG19/Troubleshooting.pdf),
page 14), or `Job remaining time is N seconds` (the
[current SchedMD implementation](https://github.com/SchedMD/slurm/blob/master/src/scontrol/info_job.c),
also printed inside a step on Slurm 25.11). Duplicate/conflicting identity
lines, multiple auxiliary lines, malformed counters and unrecognized output
fail. Both lines remain in the raw evidence and its hash; remaining time never
substitutes for job identity.

## JSON schema version 1

Success has `ok: true`, `action: "allocation"`, `executor: "local"`,
`complete: true` and a UTC `queried_at`. Failure produces the ordinary error
object, never a partially successful allocation proof. Unsupported SSH
execution exits 3 (`local_only`); invalid exact job selectors use
`spec_invalid`. Invalid or contradictory evidence exits 1
(`allocation_invalid`), including a query that Slurm answers with an error,
such as an unknown job or a PID outside every job; the message carries Slurm's
own text. A Slurm controller that does not answer exits 1 (`malformed_read`)
with the hint to retry once after 60 seconds; the recognized messages are
`Unable to contact slurm controller`,
`Socket timed out on send/recv operation` and
`Zero Bytes were transmitted or received`. A collection that exceeds
`command_timeout` exits 4 (`timeout`), like every other read. Neither means the
PID is outside the job.

| Object | Fields |
|---|---|
| `process` | `pid`, `uid`, `hostname`, `cpu_affinity`, `start_ticks`, `job_membership_verified`, `step_membership_verified` |
| `job` | `query_id`, `job_id`, `array_job_id`, `array_task_id`, `uid`, `state`, `partition`, `nodes`, `num_nodes`, `allocated_cpus`, `allocated_cpu_tres`, `allocated_gpus`, `allocated_gpu_tres`, `fields`, `raw`, `sha256` |
| `step` | null or `query_id`, `step_id`, `uid`, `state`, `partition`, `nodes`, `num_nodes`, `tasks`, `allocated_cpus`, `allocated_cpu_tres`, `allocated_gpus`, `fields`, `raw`, `sha256` |
| `node_records` | one local allocated node record containing `node`, `fields`, `raw`, `sha256` |
| `hostlists` | records containing `expression` and expanded `nodes` |
| `membership_evidence` | `job` and optional `step` records containing `source`, `raw`, `sha256` |

IDs are strings; absent array IDs are null. Counts, PID, UID, start ticks and
CPU indices are integers; missing numeric facts remain null. Step membership
is null when no step is requested, otherwise true only with verified proof.
`step.query_id` is the step ID Slurm prints: `ArrayJobId_ArrayTaskId.STEP` for
an array element, otherwise the physical job ID, a dot and the numeric step ID.
`membership_evidence.job.source` is `scontrol pidinfo`;
`membership_evidence.step.source` is `scontrol listpids`, or the step record is
null. `raw` is the selected record line (stripped scheduler output), hashed as
UTF-8 with SHA-256.

Actual job `AllocTRES` and actual step `TRES` are normalized; job and step
records carry no allocated GRES field unless detail output is requested, which
the command does not do. Requested resources, CUDA visibility and parent
resources never substitute for actual step resources. Typed GPU TRES are summed
when no generic GPU total exists; typed subsets cannot exceed a stated total.
Other TRES entries (memory, nodes, billing) stay in `fields` and are not
interpreted. A populated actual TRES without GPU entries establishes
CPU-only use; unknown or missing actual TRES fails. `allocated_cpus` is the
record's CPU count (`NumCPUs`, or step `CPUs`, else TRES `cpu`) and must agree
with TRES `cpu` when both exist; `allocated_cpu_tres` is TRES `cpu` alone, and
`allocated_gpu_tres` equals `allocated_gpus`. Known step CPU/GPU counts cannot
exceed the known parent allocation counts. Missing CPU/node/task counts are
retained as null.

The local node record always verifies `NodeName`. Numeric-step collection also
requires numeric `CPUTot`, `ThreadsPerCore`, `CoresPerSocket` and `Sockets` facts.
The generic proof permits multiple nodes, tasks, GPUs and SMT. It does not
compare affinity cardinality to an allocation CPU count, enforce a project
partition/resource profile, infer GPU device visibility, meter costs or certify
scientific results. Those decisions belong to the caller.
