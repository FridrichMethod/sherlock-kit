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
records with fixed POSIX shell programs. Each query is limited to 20 seconds;
the complete collection has a 60-second deadline. At most 32 caller hostlists
are accepted. Scheduler and caller hostlists are expanded through Slurm;
duplicate or malformed hosts, ambiguous field/record/PID rows, failed reads and
missing membership proof fail closed. `scontrol pidinfo PID` proves the supplied
PID belongs to the selected physical job. An optional numeric step additionally
requires exactly one matching row in `scontrol listpids JOB.STEP`. Environment
variables alone never prove allocation membership.

## JSON schema version 1

Success has `ok: true`, `action: "allocation"`, `executor: "local"`,
`complete: true` and a UTC `queried_at`. Failure produces the ordinary error
object, never a partially successful allocation proof. Unsupported SSH
execution exits 3 (`local_only`); invalid or unavailable evidence exits 1
(`allocation_invalid`); invalid exact job selectors use `spec_invalid`.

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
`step.query_id` is the physical job ID followed by a dot and the numeric step
ID. `membership_evidence.job.source` is `scontrol pidinfo`;
`membership_evidence.step.source` is `scontrol listpids`, or the step record is
null. Raw strings are stripped scheduler output, hashed as UTF-8 with SHA-256.

Actual job `AllocTRES`, actual step `TRES`, and legacy actual GRES fields are
normalized. Requested resources, CUDA visibility and parent resources never
substitute for actual step resources. Typed GPU TRES may be summed when no
generic GPU total exists; typed subsets cannot exceed a stated total. Legacy
`Gres`, `GRES` and `JOB_GRES` values must agree with one another and with actual
TRES when populated. Null legacy GRES indicates CPU-only use only when actual
TRES does not establish another count. Unknown actual TRES or GRES values fail, including unknown legacy GPU counts.
Known step CPU/GPU counts cannot exceed the known parent allocation counts.
Missing actual resource evidence fails;
missing CPU/node/task counts are retained as null. Only legacy evidence leaves
actual TRES counts null.

The local node record always verifies `NodeName`. Numeric-step collection also
requires numeric `CPUTot`, `ThreadsPerCore`, `CoresPerSocket` and `Sockets` facts.
The generic proof permits multiple nodes, tasks, GPUs and SMT. It does not
compare affinity cardinality to an allocation CPU count, enforce a project
partition/resource profile, infer GPU device visibility, meter costs or certify
scientific results. Those decisions belong to the caller.
