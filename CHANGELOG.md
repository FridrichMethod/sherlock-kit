# Changelog

## 0.10.0

Stanford's Marlowe cluster is a second packaged site. Sherlock stays the
default: its `sbatch` argv, replies, hints, exit 5 `next`, remote programs,
rules files, skill, partition table and executor detection are unchanged,
apart from the version text and the changes listed under "Visible on
Sherlock". The Marlowe argv, the reply additions (`site`, `estimate.note`)
and the Marlowe content paths (rules, skill and their project copies) are
provisional and may change in a later release.

Sites:

- The global option `--site {sherlock,marlowe}` and the configuration keys
  `site` (`sherlock` or `marlowe`, absent means `sherlock`) and `account`. A
  command that can reach a cluster takes `--site`, else the found
  configuration's `site`, else Sherlock; no environment variable selects a
  site. A `--site` that names another site than the found configuration is
  exit 1 `site_mismatch`, whose hint names `--project-dir`. `rules` and
  `install-skill` take the site from `--site` alone and never read a
  configuration.
- `sites.py` holds the two site records: default hosts (`marlowe-plain` for
  both Marlowe hosts), the Slurm client environment, the account rule, the
  on-cluster test, the site instructions file, the test before submit, the
  partition table, the rules files and the skill. No limit, duration, price
  or node shape is packaged.
- `account` must fully match `marlowe-[a-z]+[0-9]+`, which refuses
  `marlowe-users` and every suffixed account, and is `config_invalid` in a
  Sherlock configuration; a `control_host` or `data_host` that is a default
  alias of the other site is `config_invalid` too. A release without site
  support reads a Marlowe configuration as `config_invalid`.

Marlowe:

- The remote programs run after the fixed prelude `data/remote/site-env.sh`,
  which appends `/cm/shared/apps/slurm/current/bin` to `PATH` and exports
  `SLURM_CONF`: Marlowe's Slurm clients are not on the login `PATH`.
- `submit` requires `account`, dry run included (exit 1 `config_required`,
  with a hint to ask the user which base project account to charge), and
  passes `--account=<account>` directly after `--partition=`; the frozen spec
  gains no key.
- A plain `submit` runs Slurm's test (`sbatch --test-only`) after the
  duplicate read and before staging, and returns the preflight reply
  unchanged when Slurm refuses: exit 3, nothing staged.
- Every Marlowe preflight reply ends `estimate` with the provisional `note`
  "not a wait prediction on Marlowe; see the Marlowe rules": the start that
  the test prints is no wait prediction there.
- `data/partitions-marlowe.json` lists `preempt` alone (preemptible, requeue,
  GPUs). `unknown_partition` names the selected site's partitions, and the
  status anomalies and `update`, `occupancy` and `resources` use the selected
  site's table. The generated rules block names borrowed partitions only for
  a table that has one.
- Replies that print `executor` add `"site": "marlowe"` after it, and so does
  `doctor`; the `shk` commands in hints and the exit 5 `next` carry
  `--site marlowe`; messages name Marlowe (`Marlowe unreachable`,
  `is not a directory on Marlowe`), and the `--since` hint says that shk
  reads at most 31 days of accounting on every site.
- `doctor` checks the one `marlowe-plain` master once, reports no
  `site_instructions`, reports `site_mismatch` as a problem without checking
  masters, and with an invalid configuration checks the defaults of the site
  that `--site` names.
- `push` and `pull` go over `marlowe-plain`; on Marlowe they are refused with
  exit 3 `transfer_on_marlowe`.
- The local executor is chosen where Marlowe's Slurm configuration file
  exists, on a Marlowe login or compute node; that test never selects the
  site. A forced local executor (`--executor local`, `SHK_EXECUTOR=local`)
  where the selected site's test fails and another site's holds is exit 1
  `site_mismatch`.
- `shk --site marlowe rules workstation` and `shk --site marlowe rules cluster`
  print the new `data/rules/marlowe-workstation.md` and `marlowe-cluster.md`,
  and `shk --site marlowe install-skill` copies the new skill
  `sherlock-kit-marlowe` to `.claude/skills/sherlock-kit-marlowe/` and
  `.agents/skills/sherlock-kit-marlowe/` and, with `--rules`, the rules to
  `.claude/rules/sherlock-kit-marlowe.md`, beside the Sherlock copies. The
  help texts still name Sherlock.

Visible on Sherlock:

- `shk --help` lists `--site`.
- The hint of an unknown configuration key lists `account` and `site`, and a
  Sherlock configuration may write `site = "sherlock"`.
- `account` in a Sherlock configuration is `config_invalid` with the message
  "account is set, but site sherlock takes no account" instead of an unknown
  key, and a `control_host` or `data_host` of `marlowe-plain` is
  `config_invalid`.
- `--site marlowe` in a Sherlock project is exit 1 `site_mismatch`, for a dry
  run too: a dry run resolves the site.
- With an invalid configuration and invalid arguments, `status --since`,
  `resources`, `occupancy` and `update` answer `config_invalid` first, where
  they answered `spec_invalid` or `unknown_partition`: they resolve the site,
  which reads the configuration, before they check their arguments.

Documentation:

- `docs/marlowe-policy.md` records the source of every Marlowe rules sentence
  (documentation pages retrieved 2026-11-21, owner choices, toolkit choices),
  and `docs/validation/observed-marlowe.md` records Slurm 25.05.2 on Marlowe
  and `shk`'s own commands there, among them one CPU-only job that `shk`
  submitted, that ran and whose output `pull` verified. The command
  reference, the design guide, the installation guide (the `marlowe-plain`
  stanza, a project on both clusters, the remote root, `shk` on a login
  node), the README and the contributor guides describe the site.

## 0.9.0

Additions. Nothing else changes: the replies, frozen specs, `sbatch` argv
and remote programs of the other commands are byte-identical to 0.8.0
(`release` and `requeue` keep `control.sh`) apart from the version text and
the `batch_not_allowed` message for `dev`, which quotes the new `dev`
courtesy text, and `shk --help` lists the new command.

- `shk update --job-id N --expect-name NAME [--expect-token TOKEN]` changes
  one field group of one own job in place and keeps its job id, so its
  dependencies and the jobs that depend on it stay: `--time DUR` (a decrease,
  not below TimeMin), `--partition P[,P...]` (a listed partition or
  compatible set of the job's requeue and preemptible class), `--constraint X`
  and `--prefer X` (sent together), each only while every record pends, or
  `--throttle M` (an array's ArrayTaskThrottle, 1 to 1000, in any state).
  Zero or several groups are exit 1 `spec_invalid`, because Slurm applies the
  keys of one `scontrol update` one at a time without rollback; so are an
  `N_TASK` job id, an empty, too long or out-of-alphabet feature value
  (whitespace, `,` or `=`), the feature value `(null)` (how Slurm prints an
  unset value) and a throttle of 0.
- The partition checks of `submit` apply offline (`unknown_partition`,
  `batch_not_allowed`, `partitions_incompatible`), and a move to a borrowed
  partition is exit 3 `borrowed_not_allowed`: cancel, run `shk occupancy`,
  then resubmit with `--courtesy-ack`. The fixed program `update.sh` reads
  every record of the job once and refuses with exit 3 `remote_refused`
  before any change, naming the reason: identity (`name`, `uid`, `token`,
  `id`), a record that is not pending, a time limit increase or a value below
  TimeMin, another requeue or preemptible class, GPUs on a partition without
  them, a time limit above a member's MaxTime, a throttle on a job that is not
  an array, or a throttle raise on a borrowed partition.
- When every record already shows the value the reply is `unchanged`, with
  nothing dispatched. Otherwise one `scontrol update` runs, never retried, and
  one re-read decides: `updated` (exit 0) only when every record shows the
  requested value, whatever `scontrol` returned, and exit 5 otherwise, with
  `next` naming `shk status --job-id N --details`. The reply carries the
  field, the requested Slurm values, the records before and after, and
  Slurm's messages.
- `update` writes no file: the frozen spec stays the request as submitted, so
  `current.ran_on` still follows its partition list.

Guidance:

- Before GPU submission, the rules files, the skill and the campaign guide
  size a request to the worker's useful threads and measured peak host RAM
  plus headroom, and compare candidate requests by their
  `shk resources --summary --shape` slots in one fresh snapshot: a node whose
  GPU is free but whose CPUs or scheduler memory are taken has no slot. The
  accepted owner GPU defaults all stay: GPU models are scientifically
  equivalent, snapshots do not guarantee starts, the `--prefer` tier is purely
  a compute-capability ordering, so it applies across projects, and requeue
  restarts the script, not application state. None of the five site files had
  changed at the review of 2026-11-20.
- Login nodes follow site rule 1 and the site's `software.md`: small installs
  and checks that end in seconds may run on a login node; compiling, heavy
  installs and tests that need GPUs, many cores, much memory or more than
  about a minute run in an allocation, for quick ones the interactive `dev`
  partition (`sh_dev`, or `srun --partition=dev` with an explicit `--time`),
  not a batch job. The `dev` courtesy text in `partitions.json`, and so the
  generated partition block of both rules files, reads "Interactive only
  (`sh_dev`, `srun`): Sherlock rejects batch jobs, so `shk submit` refuses
  it. Quick tests and builds, never production runs." `workstation.md` uses
  `sherlock-plain` for light commands and installs, `cluster.md` adds installs
  to the light work allowed on login nodes, and both carry
  `Reviewed 2026-11-21`. The skill states the boundary, and the skill, the
  installation guide and the campaign guide allow a small pure-Python
  environment, such as the one that holds `shk`, to be created on a login
  node, with compiling and heavy installs inside an allocation.
- The rules files and the skill changed, for request sizing, for
  `shk update` (change a pending job with it, never with raw
  `scontrol update`) and for the login-node guidance, so
  `shk install-skill --check` reports installed copies as stale; run
  `shk install-skill` again, with the same `--rules`, to refresh them.

Documentation:

- The campaign guide describes running several revisions of a project from
  one Git mirror with a detached worktree per revision: what to record before
  submission, what to check at job startup (including an editable install
  that imports another checkout), and why a worktree, environment or model
  asset stays unchanged until every job that references it is terminal.
  Documentation only; no CLI change.

- The README is an overview with highlights, a diagram, a quick start and a
  command table. The command contracts, configuration keys and exit codes
  are in the new command reference, `docs/cli.md`, split into a section per
  command; install forms, environments on Sherlock and the SSH setup are in
  `docs/install.md`. Documentation only; no CLI change.

- `shk update` is observed on Sherlock (Slurm 25.11.9) with held pending jobs:
  time, features and throttle updates, the `unchanged` replies, the refusals,
  and two updates that Slurm rejected, which kept the record and read as exit
  5 followed by a status read. `docs/validation/observed-slurm.md` records
  them, and the design guide and the skill say what is still not observed live:
  a partition move that Slurm accepts and an update of an array with a started
  task. No CLI change.

## 0.8.0

Additions. Without the new options, replies, frozen specs and `sbatch` argv
are byte-identical to 0.7.1 apart from the version text and the compatibility
notes below.

- `shk submit --partition P,P...` takes compatible partitions as one
  comma-separated value, Slurm's own syntax (`--partition gpu,bioe,stat`).
  Every member must be listed and take batch jobs; the set is compatible when
  no member is borrowed and all members share one `requeue` and one
  `preemptible` value, and `--gpus` above 0 needs every member to allow GPUs.
  This is derived from the existing profile fields alone: `partitions.json`,
  the profiles and the courtesy texts are unchanged. So `gpu,bioe,stat`
  combines, `owners`, `btrippe` and `possu` stay alone, and `normal`, `bigmem`
  and `service` are in the class too; every member's courtesy text applies
  wherever the job starts. An incompatible set is exit 3
  `partitions_incompatible`, whose message names the borrowed members or
  lists every member with both values. The argv holds the value as given in
  the usual position, the job name does not depend on the partition, and the
  frozen spec adds `resources.partitions` only for more than one member. Only
  `submit` takes a list; `resources` and `occupancy` keep one name per
  `--partition` flag.
- `shk status --task` reports `current.ran_on`, directly after
  `current.partition`, for a job whose frozen spec lists several partitions:
  the sorted distinct partitions in which a task or attempt started, `[]`
  while no row shows a start. `current.partition` stays Slurm's text of the job's first
  row. `unknown_partition` and `unexpected_preemption` judge the members of a
  comma list; `--all`, `--name` and `--job-id` read no spec and print no
  `ran_on`.
- `shk resources --summary` reports the same single read as counts instead of
  per-node records: countable, unschedulable and unknown nodes and their
  configured, allocated and free CPUs, memory and GPUs, for the union of the
  shipped nodes and for each requested partition. `--by sku` and `--by class`
  add breakdowns by the `GPU_SKU:` and `CLASS:` features, and
  `--shape NAME:cpus=N,mem=SIZE[,gpus=N]` adds slot counts. A value with
  nothing known behind it is null, never 0; the reply carries its rules as
  four fixed texts and ranks nothing. `--by` and `--shape` without `--summary`
  are exit 1 `spec_invalid`, offline. Without `--summary` the reply is
  unchanged.
- `shk submit --preflight` adds `estimate` directly after `preflight`: a
  `status` of `parsed`, `absent` or `unparsed`, the start time as Slurm printed
  it with `start_zone: "sbatch-local"`, the node list and partition as printed,
  the line it was read from, and `observed_at`. Nothing is converted or
  guessed, and no key holds the temporary job id. The estimate is advisory;
  for a partition list Slurm names the first partition, in its own priority
  order, that can run the job, not the earliest start over the set. The
  `preflight` object, the exit codes and `preflight.sh` are unchanged.
- `shk allocation` adds `gpu_sku_candidates` after `membership_evidence`: per
  node record, the `GPU_SKU:` features as candidates for the GPU model,
  `ambiguous` for several candidates, several GPU types in `Gres` and
  `CfgTRES` or a MIG marker, and the three source fields, with a fixed
  `meaning` text. It reads no more from Slurm and never states which device
  the job got.
- `shk pull` states its payload-only contract: LOCAL holds exactly the regular
  files that `SHA256SUMS` lists and the directories they imply, without
  `SHA256SUMS` itself and without empty remote directories, and a validator
  must treat the stage as read-only, which the kit does not enforce. The
  `pulled` and `already_present` replies end with the constant
  `payload_contract` object that names it. Pull behavior and the receipt are
  unchanged.

Compatibility notes:

- Three replies gain one additive key each: `submit --preflight`
  (`estimate`), `allocation` (`gpu_sku_candidates`) and `pull`
  (`payload_contract`, in `pulled` and `already_present`).
- Two status verdicts change for rows that hold a comma-separated partition
  list, which shk 0.7.1 could not submit: an shk-named job on a list of listed
  partitions no longer gets `unknown_partition` (exit 6 in 0.7.1), and an
  interrupted attempt whose partition text is a list of listed,
  non-preemptible partitions gets `unexpected_preemption` (0.7.1 skipped a
  list). `unknown_partition` looks at every row of the job, not only the
  first, which differs only when the rows of one shk-named job differ.
- A `submit --partition` value with a repeated member (`gpu,gpu`) is exit 1
  `spec_invalid`; in 0.7.1 every value with a comma was exit 3
  `unknown_partition`. A repeated `--partition` flag keeps the 0.7.1 behavior:
  the last one wins.
- A job submitted with a partition list must be read with 0.8.0 or newer:
  older versions report `unknown_partition` (exit 6) while Slurm prints the
  list.
- The help texts of `submit --partition`, `resources`, `pull` and
  `pull --validate` changed; help is text, not a reply.

Guidance:

- The rules files, the skill and the guides add the owner's latest GPU
  guidance beside the accepted preferences, which all stay: GPU models are
  scientifically equivalent, so they may be mixed and a job may get another
  model after a requeue, and the model alone is no reason for new controls,
  cohorts, reruns or per-model qualification. Prioritize the earliest start,
  reduce oversized CPU requests, and keep only the memory, kernel and dtype
  requirements that have real evidence, and the site rules. The optional
  `--prefer` tier stays: it is purely a compute-capability ordering, so it
  applies across projects, and it is soft (Slurm tries it first, then the
  fallback `--constraint`). Nothing is tightened. Site-rule text and
  `Reviewed 2026-11-14` are unchanged.
- In both rules files the `### GPU scheduling defaults` section is rewritten,
  and both files stay within the 7168-byte cap. The comparison of authorized
  partitions by queue, setup, compute and recovery time and the pointer to
  department and `owners` partitions leave that section; the skill and the
  campaign guide carry them. The section leaves the borrowed budgets to the
  partition profiles, and the ban on padding and tiny jobs and the discovery
  of limits and features to the Jobs section of `workstation.md` and the Slurm section of
  `cluster.md`, which state them. `workstation.md` adds one sentence on compatible
  partition lists under Jobs. `cluster.md` drops the sentence that `shk push`
  and `pull` refuse there, which `shk` still enforces (exit 3) and the skill
  states, and its Slurm section writes the command list as
  `shk submit`/`status`/`cancel`. The skill, the campaign guide and the policy
  guide describe the new mechanisms, and the policy guide lists what the rules
  files leave out.
- The rules files and the skill changed, so `shk install-skill --check`
  reports installed copies as stale; run `shk install-skill` again, with the
  same `--rules`, to refresh them.

Documentation:

- The README, the design guide and the allocation contract describe partition
  lists, `ran_on`, the resource summary, the preflight estimate, the GPU SKU
  candidates and the payload-only pull. The Slurm compatibility evidence adds
  the source-confirmed facts these rest on, with links to the
  `slurm-25-11-9-1` tag, the attended read-only `--preflight` check of a
  partition list and what its exit 0 does not show, and lists the
  multi-partition, estimate, summary and GPU-type behavior as not observed
  live.

## 0.7.1

Intentional compatibility changes. Controllers that key on exit codes see more
exit 5 and never a false exit 3 after `sbatch` or `scancel` has run:

- `shk submit` no longer reports exit 3 `slurm_rejected`. Any result after
  `sbatch` was started other than a strict job id is exit 5 `action: unknown`,
  with `sbatch exit RC` and Slurm's stderr text in `detail` and the staged
  `NAME.STAMP.sh`, `.json` and `.err` kept under `<run-dir>/.shk/`. The remote
  marker `SHK_REJECTED` and the error token `slurm_rejected` are gone. A
  nonzero `sbatch` can follow an accepted submission, so no error text is
  treated as a definitive refusal; `shk submit --preflight` still gives a
  definitive Slurm verdict (exit 3) without creating a job. A name whose
  submission never landed reads `stale` and submits like `new` when it has no
  earlier job in the 30-day window that submit reads; when it has one, it keeps that job's
  `done` or `failed` state and `current.job_id` and still needs `--resubmit`.
  The attempt landed only if `current.job_id` is newer than the one seen
  before the submit.
- A nonzero `sbatch` whose stdout is, trailing newlines aside, one strict job
  id line (`N` or `N;CLUSTER`, a positive decimal integer without a leading
  zero) is exit 0 `submitted`, with the staged files renamed to the job id;
  `sbatch --wait` returns the job's exit code. Before, it was exit 3 with the
  staged provenance deleted. The id is matched as bytes under any login
  locale, so only ASCII digits form one.
- A nonzero `scancel` in `shk cancel` is exit 5 `action: unknown` with
  `scancel exit RC` and the stderr text in `detail`, instead of exit 3.
- When the Slurm client's result is not confirmed, the `detail` of the exit 5
  reply begins `<program> outcome not confirmed` instead of
  `unparsed <program> reply`, where `<program>` is the remote program:
  `submit`, `cancel`, or `control` for `shk release` and `shk requeue`;
  `detail` is evidence text, not a token to match.
- An empty or whitespace-only `--constraint`, `--prefer` or `--signal` is
  refused offline, exit 1 `spec_invalid`. Before, an empty value was dropped
  silently from the `sbatch` request and a whitespace-only one was passed to
  `sbatch`. Omitted options leave the frozen spec and argv byte-identical.
- `shk pull` exits 1 `remote_failed` with `find`'s message when the remote
  enumeration of REMOTE fails (an unreadable subdirectory, for example), and
  promotes nothing. Before, the inventory check could pass with files it had
  not listed.

Fixes:

- `shk cancel --task ""` (an unset shell variable, for example) is refused
  offline, exit 1 `spec_invalid`, like `submit` and `status`. Before, it ended
  as `internal_error`.
- `shk requeue --self` and `shk release --job-id A_k` work for an element of a
  throttled array: `ArrayTaskId` is read as the first word after the key, so a
  following `ArrayTaskThrottle=N` field no longer makes the identity check
  refuse.
- Replies of the remote programs are records separated by LF only. The
  `SHK_SPEC` payload has its LF bytes flattened to spaces remotely and every
  local parser splits on LF alone, so run-directory text or a job name
  containing another line separator cannot add a record or a completion
  marker. A spec file that spans several lines is read as one record, so
  `status` reports its task count.
- On a borrowed partition the submit reply adds
  `request: {tasks, throttle, minutes}` and, when others are running and the
  request has more than 2 tasks, one factual total-work advisory in `warnings`
  (array tasks, throttle or none, minutes). The budget warning keeps its
  trigger and quotes "about 2 jobs x 2 h" as the courtesy budget only for a
  partition whose courtesy text contains it. Nothing is refused; partition
  profiles and courtesy texts are unchanged.
- `allocation --hostlist` and `resources --partition` say `repeatable` in
  their help.
- Tests: a state matrix in `tests/test_slurm.py` covers the pending fallback
  and the terminal Slurm states of name resolution; the packaging fixture
  ignores the global Git configuration (commit signing, for example); the
  two-hop `bin/shk` check runs under the interpreter being tested; an OR
  expression in `--constraint` and `--prefer` is followed through the real
  submit program; `tests/test_records.py` covers the LF-only record
  separation.

Documentation:

- Remove the completed dependency-refactor implementation handoff from release
  documentation and source distributions. Add a documentation index and task
  document lifecycle guidance; retain the Slurm compatibility evidence.
- Name the main checkout's ignored `.state/<task>/` as the durable task
  storage: `git worktree remove` deletes a worktree's ignored files, so
  evidence is copied out first.
- Record limits of the current behavior: do not wrap `shk` in an outer timeout
  shorter than its own (if `shk` itself is killed, a mutation in flight is
  outcome unknown and a transfer may still be running); a failed or timed-out
  push can leave a truncated file under its final name; `already_present`
  means that the receipt beside LOCAL names the manifest, and LOCAL is not
  verified again.
- GPU campaign guidance: a passed pilot admits its configuration over the
  compatible OR set already narrowed by hard requirements; separate validation
  is for a member with a concrete open uncertainty. Borrowed partitions:
  `btrippe` has no blanket daytime prohibition, `possu` keeps its 00:00-07:00
  Pacific window.
- CPU thread budgets: a script that sets only `OMP_NUM_THREADS` stays capped
  by `OMP_THREAD_LIMIT` and the library-specific variables, and a submission
  made from inside a budgeted job inherits its variables, so a nested
  submission passes its own `--cpu-thread-budget N`.
- Write repeatable flags in the repeated form in the command reference, add
  `cpu_threads.py` to the design guide, align the policy provenance with the
  wording of the rules files, correct "snapshots do not guarantee starts" in
  both rules files, and list `--prefer`, OR feature expressions, the
  thread-budget `env` prefix and three source-confirmed Slurm behaviors as not
  observed live.
- Development and packaging changes that shipped between 0.6.0 and 0.7.0
  without an entry: `build` and `hypothesis` joined the `dev` extra;
  `tests/prepare_wheelhouse.py` prepares a per-Python wheelhouse and
  `SHK_TEST_WHEELHOUSE` became required, beside `SHK_BUILD_PYTHON`, for the
  sdist, wheel, editable and source-launcher checks; CI prepares its
  environments and wheelhouse online and then runs the suite offline; bounded
  Hypothesis property tests cover the pure parsers and reducers. A bounded
  Pydantic validation pilot was evaluated and not adopted: the runtime keeps
  no dependencies.

## 0.7.0

- Add `shk submit --prefer` to pass preferred node features to Slurm and record
  them in the frozen spec. Without it, argv and resource fields are unchanged.
  Preferred features are tried independently before the fallback constraint;
  include all hard requirements in both expressions.

- Deliver owner GPU/partition preferences in both rules and the installed skill:
  broad compatible GPU choices, hardware provenance without automatic scientific
  cohort splitting, idempotent owners recovery, parallel ready work and longer
  useful bundles with bounded resource-aware rebalancing. Partition courtesy
  and TOML keys are unchanged.

- Refresh the site-policy comparison, including explicitly requested scratch
  environments and the storage/installation guidance.

- Add a parallel GPU campaign guide and self-contained skill checklist for CPU preparation, independent first pilots, bounded dispatch, explicit work-state counts and safe ownership transfer. Documentation only; no CLI or partition policy changes.

- Document project-pinned standalone installations and project-local agent content,
  including absolute-path commands inside Sherlock allocations. Remove the skill's
  global-launcher fallback; runtime behavior, version and packaged rules stay the same.

- Give the README Quick start a minimal `~/.ssh/config` for the default
  `sherlock-plain` (login node) and `sherlock-dtn` (DTN) aliases, and state that
  `control_host` and `data_host` accept any other OpenSSH aliases as long as the
  two differ. Documentation only.

## 0.6.0

- `shk submit --cpu-thread-budget [N]` (N defaults to 1 and is at most `--cpus`)
  or project `cpu_thread_budget = N` runs sbatch under `env` with the OpenMP,
  MKL, OpenBLAS, BLIS, vecLib and NumExpr thread variables set to N per task,
  also with `APPTAINERENV_` and `SINGULARITYENV_` prefixes, and records
  `cpu_thread_policy` in the spec. They are launch defaults that a script can
  override, not an OS-thread limit; without the option or the key, argv and spec
  are unchanged. See [CPU thread budgets](docs/cpu-threads.md).
- Sherlock's login banner still leaves parsed output, but each banner line is now
  written once per process to stderr: site rule 12 (added 2026-10-19) forbids
  filtering out instruction reminders.
- Both rules files carry site rule 12: read `AGENTS.md` and the task's topic
  files at session start and after any summary or compaction, since memory or an
  earlier read is no substitute, and keep the rule in handoffs. In the cluster
  file `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK` yields to `--cpu-thread-budget`.
  Both files were reviewed against the site files on 2026-10-21; see
  [docs/sherlock-policy.md](docs/sherlock-policy.md) for what made room.

## 0.5.0

- Operational JSON adds `schema_version: 1`; existing result keys and exits,
  help, rules and version text remain compatible.

- `status --details` retains queue, accounting and queued `scontrol` identities,
  including physical IDs, DBIndex, clusters, times, comments, UID and raw lines.
  Repeatable `--job-id` queries only the current UID, supports legacy names and
  lists one entry per selected base job in job id order; detailed ID queries read
  the queue without expired-ID filters while accounting keeps exact ID filters.
  Distinct accounting identities no longer collapse at `(JobID, Restarts)`.

  - Control records come from one `scontrol show job` per queued base job (array
    elements appear once); records outside the selection (siblings, other own
    jobs, other projects under `--all`) are ignored instead of marking the query
    incomplete, and `scontrol` times are UTC like `squeue` and `sacct`.
  - Raw queue and accounting fields are separated by the ASCII unit separator
    (0x1F), so a `|` in a comment or job name no longer poisons every name.
  - Accounting rows of jobs that never had an allocation (held or pending jobs
    cancelled before start, pending array remainders) are complete evidence, and
    a terminal one has a final zero cost; queue rows without nodes report null
    CPUs and start.
  - A failed `squeue` or `sacct` read is exit 1 `malformed_read` like plain
    status, keeping the evidence and `details.returncodes` but no name summaries;
    a failed control read alone exits 0 with `complete: false`. Each entry's
    `current.evidence_complete` covers its own job.
  - A selected name whose rows all fail to parse (for example hetjob ids `N+k`)
    is exit 1 `malformed_read` like plain status, keeping the evidence, instead
    of `new`; a name's unreadable row withdraws only its own final cost. A
    requested ID without readable rows is listed in `missing_job_ids`.
  - `duplicate_live` and `unknown_partition` apply to `shk.` names only, and
    `unexpected_preemption` is judged on the partition of each interrupted
    attempt, so legacy jobs sharing a script name or running on another group's
    partition no longer exit 6.

- Without a spec file, an array's task count is inferred only when its tasks run
  0 to max: 1-based, stepped or partly aged-out arrays have null count and missing
  tasks, a state that follows the observed tasks (no longer `failed`) and a
  nonfinal cost. Explicit task rows outrank pending accounting aggregates.
  `status --since` accepts 1 to 31 days, the widest window Sherlock's slurmdbd
  answers.

- `resources` reports Slurm configured minus allocated node capacity; OS FreeMem
  is never used. Nodes are filtered on Sherlock by their `Partitions=` list, each
  once, and keep their raw line; `complete`, `malformed`, the cadence and one
  top-level `provenance` precede `nodes`. Each node adds `schedulable` from its
  State flags (false for DOWN, DRAIN, FAIL, MAINTENANCE, NOT_RESPONDING, RESERVED
  and similar; PLANNED and COMPLETING stay true): count free capacity only there.
  A failed `scontrol` read is exit 1 `malformed_read`.

- `submit --preflight` runs `sbatch --test-only` without submitting or writing
  provenance; a Slurm refusal is exit 3 and an unreachable or timed-out
  controller is exit 1 `malformed_read` with a retry hint. `--hold` and the public
  `--submission-token` are CLI-owned options frozen with the spec; `--hold` needs
  a token, which the job sees as `SHK_SUBMISSION_TOKEN` (an inherited one is
  unset otherwise). A leading `#SBATCH --comment` is refused only when a token
  owns Comment. Submit, `--preflight` and `--dry-run` refuse `dev` offline (exit 3
  `batch_not_allowed`): Sherlock keeps it for interactive sessions; partition
  profiles gain a `batch` flag.

- Exact `release` checks UID, name, token and user hold and keeps Slurm's error
  text. `requeue --self` requires a local current allocation, Slurm PID membership
  in the physical `SLURM_JOB_ID`, the job's own `scontrol` record (the array meta
  element also returns its siblings), the current UID and name, RUNNING, a
  partition whose profile allows requeue and Requeue=1; `--expect-token` is
  optional and checked when given. An array element is requeued as
  `ArrayJobId_ArrayTaskId`, never as the bare array id; identity and policy fields
  come from the record's leading fields, never from free text such as Comment.
  Lost mutation replies
  remain unknown (exit 5) with Slurm's message; checkpoint/retry decisions stay
  outside the kit.

- `shk --executor local allocation --pid PID --job-id N_OR_N_TASK [--step-id S]`
  verifies a local worker's UID, PID membership (`pidinfo`, `listpids`), host and
  allocation and reports actual resources, topology and hashed raw records. The
  array meta element selects its own record among siblings, an array step is
  queried as `ArrayJobId_ArrayTaskId.STEP` as Slurm prints it, and timeouts or
  controller errors are retryable reads distinct from non-membership. No
  admission policy (hostlist caps, mandatory topology, affinity equality) is
  applied.

- Pull keeps incoming directories owner-writable across large file-list batches
  with the receiver-local `--chmod=Du+rwx`; the restricted DTN rsync server
  command, file bytes and file permissions are unchanged. Before resuming, a stage
  directory lacking owner access gets it back (an ordinary pull issues no chmod;
  a refused repair is exit 1 `stage_unwritable`), a stage directory another user
  owns or a symlinked stage root is refused, and child symlink targets are never
  followed or chmodded.

- Rules distinguish bounded deterministic compute controllers running in a
  Slurm allocation with `--executor local` from prohibited future agent
  sessions. Releasing a job held on a borrowed partition counts as submitting it;
  the reason for a cancellation goes to the user. Array grammar is `N[%M]`
  throughout. The skill documents allocation, release, requeue and resources.

- Both rules files are reviewed against all five site files as of 2026-10-18.
  They carry the site's new ban on SSH servers on any node (rule 5, added
  2026-10-13) and summarize the numbered rules more fully: `sherlock-plain` is a
  login node for light commands, intensive job I/O stays off the home
  filesystems, no help in extending scratch lifetime, no shell polling loops,
  large or shared environments in `$GROUP_HOME`, no probing of shared
  infrastructure, only one's own or shared directories (never merely readable
  ones) and one's own processes, and no `--resubmit` after a failure before its
  cause is found.

- The offline suite stays hermetic inside Slurm jobs: importing `tests/fakes`
  removes inherited `SLURM_*`, `SBATCH_*`, `SQUEUE_*`, `SACCT_*`, `SCANCEL_*`,
  `SHK_*` and `SHERLOCK_KIT_*` variables and exported shell functions, puts
  blocking stubs for every Slurm client on `PATH` behind each test's fakes, and
  refuses a `TMPDIR` that cannot execute files; `tests/run_suite.py` fails a run
  that reached a stub. A run inside a Sherlock job had reached the real `squeue`
  and `scontrol`.

- `docs/validation/observed-slurm.md` records the 2026-10-18 live verification on
  Slurm 25.11.8 and corrects the old DTN note.

- `shk doctor` no longer warns about leftover 0.3 `SHERLOCK_KIT_*` variables, and
  dead or duplicated code is removed. Building needs only `setuptools>=80` (no
  `wheel`) and no longer warns that the packaged data directories are absent from
  `packages`.

## 0.4.1

- Recovery commands after an unknown submit or cancel outcome preserve the
  original project directory and executor, so the status read still selects
  the right task after the caller changes directories.
- SSH master checks distinguish local permission failures, timeouts and other
  check failures from a missing master. Codex permission failures now point to
  per-command approval instead of asking the user to reopen the master.
- The skill and workstation rules explain sandbox failures, the parent
  directory needed for a first push, checking the UTC clock before status,
  malformed-read recovery and the text output of help and usage errors.
- Submission guidance requires status reconciliation after an unknown outcome
  before considering another submit.

## 0.4.0

**Breaking.** A lean rewrite: one small standard-library package, `shk`, with
Slurm as the only source of truth and no local or remote state of its own. Job
names derive from the logical task (`shk.<project>.<task slug>.<hash>`), so a
retry reuses the name and deduplication reads `squeue` and `sacct`. Remote work
runs as fixed POSIX `sh` programs over the existing OpenSSH control masters, and
`shk` never logs in.

- Removed: the attempt registry on Sherlock and its `registry_root`, attempt ids,
  `reconcile` with `--acknowledge-preemption` and `--abandon`, the private
  configuration file, borrowed-partition grants, the guard hooks and their
  adapters, the pin, projection and identity machinery (`shk policy`,
  `build_identity.json`, `SHERLOCK.md`), the frozen launcher and its state root
  (`SHERLOCK_KIT_STATE_ROOT`, `~/.local/share/sherlock-kit`), the authentication
  cooldown, the query cache and the JSON fetch manifest with producer digests.
  0.3.0 names (`shk-<hex>`) cannot collide with 0.4.0 names; 0.4.0 never reads
  the old registry.
- New: `[tool.sherlock-kit]` project configuration in `pyproject.toml` (or
  `sherlock-kit.toml`), found in the nearest directory at or above the working
  directory; `shk submit` with `--task`, `--resubmit REASON` and `--dry-run`;
  `shk status` by task, name or `--all` with `next_check_after`; `shk cancel`
  for one name; `shk push` and `shk pull` with a `SHA256SUMS` manifest
  (`shk manifest DIR`), an optional project validator and a receipt beside the
  destination; `shk rules {workstation,cluster}`; `shk install-skill`; `bin/shk`
  (mode 100755), a stdlib-only entry point that dotfiles links from its
  submodule.
- Transport: `ssh -O check` before every dial; without a usable master, exit 4
  `auth_required` (open the master) or `unavailable` (fix `~/.ssh/config`, or
  install OpenSSH). Dials are mux-only with `-oProxyCommand=false`; a transport
  failure after the check is exit 4 with the hint `run shk doctor`. On Sherlock
  the local executor runs the same programs and `push`/`pull` are refused.
- Partitions: the ten profiles carry over; listing is the authorization fact,
  borrowed partitions need `--courtesy-ack`, and the possu window and btrippe
  budget are courtesy text. `#SBATCH` lines may set anything except the
  directives `shk` owns (account, exclude, GPUs, partition, array, dependency,
  begin, job name) or start a heterogeneous job.
- Rules and skill: `src/sherlock_kit/data/rules/workstation.md` and `cluster.md`,
  rewritten from the Sherlock site files read on 2026-10-10 (purge evasion is
  account termination on the first occurrence; no agent module is named), and
  one `sherlock-kit` skill for Claude Code and Codex.
- Documentation: `docs/design.md`, `docs/sherlock-policy.md` and
  `docs/validation/observed-slurm.md` replace the 0.3.0 guides.
- Development: ruff (lint and format), mypy and pre-commit, run in CI; the
  version is the literal `sherlock_kit.__version__`, read by `pyproject.toml` as
  its dynamic version (build with `setuptools>=80`); `pip install -e '.[dev]'`
  installs the tools.

| 0.3.0 | 0.4.0 |
|---|---|
| `shk policy`, `shk policy --projection` | `shk rules workstation`, `shk rules cluster` |
| `shk policy --identity` | `shk --version` |
| `shk submit --config private.json --spec attempt.json [--apply]` | `shk submit --task T --script FILE --partition P --cpus N --mem SIZE --time DUR [--dry-run]` |
| retry naming `parent_attempt` | `shk submit ... --resubmit "<reason>"` |
| `shk status --attempt ID`, `shk reconcile --attempt ID` | `shk status --task T` or `--name NAME` |
| `shk status --all`, `shk reconcile --all` | `shk status --all` |
| `reconcile --acknowledge-preemption`, `reconcile --abandon` | none: nothing is recorded; report the anomaly, then `--resubmit` |
| `shk occupancy --config private.json --partition P` | `shk occupancy --partition P` |
| `shk fetch ... --manifest M --destination D`, `fetch --local` | `shk pull REMOTE LOCAL [--validate NAME]` |
| JSON manifest with producer digests | `SHA256SUMS` (`shk manifest DIR` or `sha256sum`) |
| private configuration, grants | `[tool.sherlock-kit]`, `--courtesy-ack` |
| `shk guard`, plugin and `sherlock-kit-operate` skills | the `sherlock-kit` skill, `shk install-skill` |
| pinned frozen install and launcher | a pip install in the project environment, or the dotfiles submodule `bin/shk` |

## 0.3.0

Slurm as the source of truth, job arrays and the full partition table. The local
SQLite ledger with its reservations, budgets and cost caps is gone: the only
durable attempt state is an attempt registry on Sherlock (`registry_root`) plus
Slurm accounting, so any POSIX workstation with the frozen toolkit and the
private configuration can operate. Every new path is tested offline by executing
the real registry program against fake `sbatch`/`sacct`/`squeue` binaries. A
live CPU pilot on `normal` on 2026-10-10 (one 4-task array, throttle 2) exercised
the registry runner, `status` through the throttled, partial and terminal phases,
`reconcile --all`, DTN fetch with the attempt sidecar and `fetch --local`; see
the acceptance document. Lost replies, `--abandon` and preemption paths remain
offline evidence. The partition-table change has no live pilot of its own.

- Registry (`sherlock_registry`, standard library, Python 3.11-3.14): the CLI
  ships the module's own source to the login node as a self-decoding
  `python3 -c` program with `submit`, `read`, `event` and `fetch-manifest`
  subcommands, so runner, reader and event writer resolve attempts with the same
  code against a query taken on the login node under the registry lock. Layout
  under `registry_root` (owned, 0700, no symlinks): `.lock`, `tasks/<key>` (the
  latest attempt of a logical task), `attempts/<id>/record.json`,
  `submitted.json`, `not_sent.json`, `ack-<epoch>.json`, `abandoned.json` and
  `resolved.json`. Every file is create-once (private temporary file plus
  `link`), nothing is read-modify-written and timestamps are stamped on the
  login node.
- Private configuration: new required key `registry_root` (absolute canonical
  POSIX path on the control host); `state_root` and `limits` are refused by the
  unknown-field message as the migration signal. Typed commands keep only
  `auth-backoff.json` and the new 60 s query cache `query-cache.json` (0600,
  its own lock, sentinel semantics, expired entries pruned on write, malformed
  or public files refused) under an explicit `transport.backoff_file` or
  `SHERLOCK_KIT_STATE_ROOT`; with neither they refuse to run instead of using
  `$HOME`. Grants keep grantee, evidence reference, validity, partitions and
  scope; a legacy limits member is ignored.
- Submission: `submit --apply` makes one runner call per attempt with a fresh
  id, records `record.json` before `sbatch` and maps the single reply line to
  `submitted` (exit 0), `not_sent` (exit 1, recorded; resubmit naming the id as
  `parent_attempt`), a translated refusal (exit 2, nothing recorded) or `unknown`
  (exit 2, `shk status --attempt ID`). Logical-task deduplication and the retry
  rule live in the registry: a retry names `parent_attempt`, and the parent must
  be `not_sent`, `abandoned` or `terminal` without a `COMPLETED` task.
- Job arrays: `resources.array = {"count": 2..1000, "throttle": 1..count}`
  emits `--array=0-<count-1>[%throttle]` and `slurm-%A_%a` logs; sacct `N_k`
  task rows and `N_[a-b%t]` pending or cancelled aggregates are reconciled per
  task and restart, reported as `tasks {count, terminal, running, pending, missing, by_state, incomplete_history}`, with cost summed over complete task
  and restart groups and `known` only when every task is terminal with complete
  accounting.
- Reconciliation: `status`/`reconcile --attempt ID` and `--all` make one cached
  reader call (one `sacct --name=shk-A,shk-B,...` on the login node; 500 open
  attempts, 50,000 rows); resolutions are `not_sent`, `abandoned`,
  `inconclusive`, `abandonable`, `identified`, `terminal`,
  `unexpected_preemption` and `error`. New `reconcile --attempt ID --abandon [--note T]` closes an attempt that stayed absent from accounting for 900 s
  without a known job id after a fresh `squeue` check on the login node;
  `--acknowledge-preemption [--note T]` now writes a per-task waiver under the
  registry lock from a fresh query and needs the network. `reconcile` writes
  `resolved.json` for fresh terminal attempts, which bounds `--all`; a
  `submitted.json` job id that differs from accounting is an `error`
  (`job_id_conflict`) and never releases. `status --local` and
  `reconcile --local` are removed.
- Fetch: the control-side manifest read is the registry's `fetch-manifest`, and
  an attempt sidecar `.NAME.shk-attempt.json` (attempt, producer, validator
  identity, manifest and its SHA256) is written durably beside the destination
  before the transaction record; `fetch --local` recovers from that sidecar
  alone, so a destination promoted by 0.2.0 needs one remote fetch first.
- Removed: `Coordinator`, SQLite, budgets, reservations, cost floor, quarantine,
  `recover`, `scheduler_cost`, legacy-profile dispatch, evidence helpers,
  `REMOTE_MANIFEST`, `state_root`, `limits`. 0.2.0 ledger attempts are not
  migrated: resolve them with the old installation, then archive
  `coordinator.sqlite3`.
- Full partition table: `sherlock_kit_data/partitions.json` adds the public
  `bigmem`, `dev`, `service` and `gpu` profiles (not preemptible, not borrowed;
  GPUs allowed on `gpu` and `dev`), the department GPU profiles `bioe` and `stat`
  (not borrowed, fairshare courtesy) and the borrowed GPU profile `possu` (grant
  required; its courtesy text confines submission to 00:00-07:00 Pacific).
  `partitions_sha256` changes accordingly; frozen attempts keep the profile they
  were admitted with. Documentation names every profile and the borrowed set
  (`btrippe`, `possu`) and states that a courtesy time window is honoured by the
  operator or agent, not checked by the toolkit.
- Policy: two projection bullets change (a lost mutation reply is resolved by
  `shk status --attempt ID` against the registry and accounting; "budgets" is
  dropped from the explicit-inputs sentence) and the decisions section describes
  the registry model. `policy_sha256` changes, so the dotfiles pin and both
  delivered projections must be regenerated before new managed admissions.
- Tests: `tests/test_registry.py` executes the runner, reader and event writer
  in-process against a temporary registry with fake Slurm binaries, including
  three concurrent runners on one logical task; `tests/fake_remote.py` drives the
  CLI the same way; `tests/test_release_metadata.py` ties the version strings,
  the projection, the documented config keys and the skill adapters, and
  `tests/test_orchestration.py` ties the constants duplicated from the registry.

## 0.2.0

Partition profiles, requeue-aware reconciliation, batch reconciliation and
partition occupancy. Every new path is tested offline against synthetic scheduler
responses, and a live owners/btrippe GPU pilot on 2026-10-09 exercised GPU
admission, one operator-issued requeue with restart-aware accounting, `--all`,
occupancy and DTN fetch (see acceptance evidence); natural preemption remains
offline evidence. All of it is engineering evidence, not scientific acceptance.

- Packaged partition profile table (`sherlock_kit_data/partitions.json`, module
  `sherlock_partitions`): `normal`, `owners` and `btrippe` with boolean
  `preemptible`/`requeue`/`borrowed`/`gpus_allowed` flags and a courtesy sentence,
  no caps. Unknown partitions are refused offline at spec check, GPUs only where
  allowed, `requeue` defaults to the profile and is refused where it forbids it.
- Admission freezes the profile and `partitions_sha256` into the attempt, consults
  a grant only for borrowed profiles (a configured `btrippe` grant no longer blocks
  `normal` submissions) and charges open or uncertified attempts at the larger of
  their estimate and stored lower bound. Dispatch emits `--requeue --open-mode=append`
  or `--no-requeue` from the frozen profile and refuses specs frozen by 0.1.0, so
  such `not_sent` attempts can no longer be dispatched and keep their reservation
  until explicitly resolved.
- Reconciliation groups accounting rows by restart: the highest restart is
  authoritative, `BOOT_FAIL` and `DEADLINE` are terminal, `PREEMPTED` ends only a
  non-requeued preemptible job, costs sum over restarts as a never-decreasing lower
  bound and are certified only with a complete restart history. Preemption or a
  restart on a non-preemptible profile is `unexpected_preemption` (exit 2,
  reservation kept) until `reconcile --attempt ID --acknowledge-preemption`.
  The submit-time tolerance is 300 seconds and the sacct cache key is per attempt.
- `status --all`/`reconcile --all` reconcile every reserved submitting/submitted/unknown
  attempt through one cached `sacct --name` list; a single attempt's contract
  violation is reported as an `error` entry. `occupancy --partition NAME` reports
  per-user running/pending jobs and running GPUs from fixed-width `squeue -O`
  output and gates nothing.
- Transport: the shared authentication cooldown arms only when OpenSSH exits 255
  with an authentication message; `auth_failure`, `record_auth_failure` and
  `cooldown_active` are public. rsync failures and deadlines raise `TransferError`
  with a bounded stderr tail instead of a traceback; `data_transfer` prepends the
  data host, refuses during a cooldown and arms it on a DTN authentication failure.
- Identity: `policy_identity()` reports `partitions_sha256` in both modes, frozen
  installs validate it, doctor and admission compare it only when a pin advertises
  it. `private_config` refuses unknown keys.
- The policy projection lists the packaged profiles verbatim. This changes
  `policy_sha256`, so the dotfiles pin and both delivered projections must be
  regenerated before new managed admissions. The package, Claude plugin and
  changelog versions are tied by `tests/test_release_metadata.py`.
- Offline in-process tests (`tests/test_cli_flows.py`) drive `status`, `--all`,
  `--acknowledge-preemption`, `submit --apply` gate ordering, remote fetch wiring
  and `occupancy` through the CLI against a faked transport.

## 0.1.0

Initial release prepared from the accepted foundation and CPU pilot.

- Canonical source-backed Sherlock policy, provenance identity and verified
  instruction projections for Claude Code and Codex.
- Bounded noninteractive OpenSSH with shared authentication cooldown and
  read-only local/explicit remote doctor.
- Typed single-allocation CPU submission, shared SQLite admission/budgets,
  durable uncertainty and identity-bound reconciliation with cached queries.
- Manifest-bound rsync transfer, frozen workload validators, atomic artifact
  promotion and durable offline recovery across toolkit updates.
- Narrow opt-in guard and namespaced first-party skills, tested with both
  actual clients. Production registration/trust is explicit.
- Explicit runtime state placement, release documentation and full POSIX CI.

Supported limits and real acceptance were in the acceptance document of the
0.1.0 to 0.3.0 trees; its live Slurm observations are now in
[observed Slurm behavior](docs/validation/observed-slurm.md). No research
code/data, borrowed-partition authorization or generic GPU recovery is
distributed.
