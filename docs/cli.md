# Command reference

This is the reference for the `shk` commands, their JSON replies, the project
configuration and the exit codes. `shk <command> --help` is the authoritative
flag list. [Installation and connection](install.md) covers setup; the
[design guide](design.md) explains why the commands behave as they do.

- [Synopsis](#synopsis)
- [Output](#output)
- [Exit codes](#exit-codes)
- [Configuration](#configuration)
- [Sites](#sites): Sherlock and Marlowe
- Jobs: [submit](#submit), [status](#status), [wait](#wait),
  [cancel](#cancel), [update](#update), [occupancy](#occupancy)
- Capacity: [resources](#resources), [resource summary](#resource-summary)
- Inside a job: [allocation](#allocation),
  [release and requeue](#release-and-requeue),
  [controllers inside a job](#controllers-inside-a-job)
- Data: [push](#push), [pull](#pull), [manifest](#manifest)
- Setup: [doctor](#doctor), [rules and install-skill](#rules-and-install-skill)

## Synopsis

```text
shk [--project-dir DIR] [--executor auto|ssh|local] [--site sherlock|marlowe] <command>
shk --version
shk doctor [--remote]
shk rules {workstation,cluster}
shk install-skill --project DIR [--client claude|codex|all] [--rules workstation|cluster] [--check] [--force]
shk occupancy --partition P [--partition P ...]
shk submit --task T [--campaign C] --script FILE --partition P[,P...] --cpus N [--cpu-thread-budget [N]] --mem SIZE --time DUR
           [--gpus N] [--tasks N] [--array N[%M]] [--constraint X] [--prefer X] [--signal S] [--requeue|--no-requeue]
           [--after-ok JOBID ...] [--run-dir PATH] [--courtesy-ack] [--resubmit REASON] [--submission-token TOKEN] [--hold] [--dry-run|--preflight]
shk resources --partition P [--partition P ...]
              [--summary [--by sku|class ...] [--shape NAME:cpus=N,mem=SIZE[,gpus=N] ...]]
shk --executor local allocation --pid PID --job-id N_OR_N_TASK [--step-id NUMERIC_STEP] [--hostlist EXPRESSION [--hostlist EXPRESSION ...]]
shk release --job-id ID --expect-name NAME --expect-token TOKEN
shk requeue --self --expect-name NAME [--expect-token TOKEN]
shk update --job-id N --expect-name NAME [--expect-token TOKEN]
           (--time DUR | --partition P[,P...] | --throttle M | [--constraint X] [--prefer X])
shk status (--task T [--task T ...] [--campaign C [--campaign C ...]] [--run-dir PATH [--run-dir PATH ...]] | --name NAME [--name NAME ...] | --all | --job-id ID [--job-id ID ...])
           [--since DAYS] [--details]
           (--campaign once for every --task, or once per --task; --run-dir once per --task, or not at all)
shk wait (--task T [--task T ...] [--campaign C [--campaign C ...]] [--run-dir PATH [--run-dir PATH ...]] | --name NAME [--name NAME ...] | --all)
         [--since DAYS] --timeout DUR [--interval DUR] [--max-pending DUR]
shk cancel (--task T [--campaign C] | --name NAME)
shk push LOCAL REMOTE [--anywhere]
shk pull REMOTE LOCAL [--validate NAME] [--replace]
shk manifest DIR
```

## Output

- Operational results print one JSON document with `schema_version: 1` on
  stdout; `rules`, `--version` and help print text, and usage errors print
  text on stderr (exit 2).
- `submit`, `status`, `cancel`, `occupancy` and `resources` report
  `next_check_after`, 60 s after they ran; `wait` reports that of its last
  read.
- Errors print `{"ok": false, "error": TOKEN, "message": ..., "hint": ...}`.
- A reply from a site other than Sherlock adds `"site": NAME` directly after
  `executor`; see [replies and hints off Sherlock](#replies-and-hints-off-sherlock).
- Do not wrap `shk` in an outer timeout shorter than its own
  (`command_timeout`, `transfer_timeout`): if `shk` itself is killed, a
  mutation in flight is outcome unknown without an exit 5 reply, and a transfer
  may still be running.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | local error: configuration, spec, verification, validator, malformed reply, failed remote read; this includes `site_mismatch` and a Marlowe submit without `account` (`config_required`) |
| 2 | usage error |
| 3 | refused, and nothing was submitted, cancelled, released, requeued or updated: live duplicate, finished task without `--resubmit`, missing `--courtesy-ack`, an `#SBATCH` directive `shk` owns, an incompatible partition list (`partitions_incompatible`), an `update` to a borrowed partition (`borrowed_not_allowed`), a remote guard that stopped before `sbatch`, `scancel` or `scontrol` ran, a `--preflight` request that Slurm rejects, push or pull on Sherlock; on Marlowe also a plain `submit` that Slurm's test rejects (the preflight reply, nothing staged) and push or pull on Marlowe (`transfer_on_marlowe`) |
| 4 | SSH transport or master check unavailable: `auth_required` (ask the user to open the master); `unavailable` with a local permission denial (check the sandbox; request per-command approval in Codex); `unavailable` with a `~/.ssh/config` hint (fix the client config); other `unavailable` or `timeout`, hint `run shk doctor` (run it, retry once after a minute, then stop), on Marlowe `run shk --site marlowe doctor` |
| 5 | outcome unknown after a dispatched mutation: `sbatch` ran without printing a strict job id (its exit code and stderr are in `detail`, and the staged script, spec and stderr are kept), `scancel` exited nonzero, `scontrol` exited nonzero in `release` or `requeue`, the re-read after `scontrol update` in `update` failed or does not show the requested value on every record (whatever `scontrol` returned), or the reply was lost. Run the `shk status` command in the reply's `next`, not before the time it names (at least 60 s) |
| 6 | status ok, but the newest job of a requested name has an anomaly; `wait` ends there with outcome `anomaly` |

## Configuration

`[tool.sherlock-kit]` in `pyproject.toml`, else the top level of
`sherlock-kit.toml`, found with `--project-dir DIR` (exactly that directory) or
in the nearest directory at or above the working directory that has one.
Unknown keys are errors; nothing secret belongs here.

```toml
[tool.sherlock-kit]
project = "demo"
remote_root = "/scratch/users/USER/demo"

[tool.sherlock-kit.validators]
bundle = ["python", "-m", "demo.validate_bundle"]
```

| Key | Default | Used by |
|---|---|---|
| `site` | `sherlock`; `sherlock` or `marlowe` | every command that reaches a cluster; see [Sites](#sites) |
| `project` | none; `^[a-z0-9][a-z0-9-]{0,23}$` | `submit`, `status --task`, `wait --task`, `cancel --task`; `status --all` and `wait --all` list only this project's names |
| `remote_root` | none; absolute, no `..` | `submit` (default run directories, also for `status` and `wait`), `push` (unless `--anywhere`) |
| `account` | none; Marlowe only, fully matching `marlowe-[a-z]+[0-9]+` | `submit` on Marlowe, required there (`--account=` in the argv) |
| `control_host` | `sherlock-plain`; on Marlowe `marlowe-plain` | every remote call |
| `data_host` | `sherlock-dtn`; on Marlowe `marlowe-plain` | `push`, `pull` |
| `connect_timeout`, `command_timeout`, `transfer_timeout` | 15, 60, 3600 s | ssh and rsync |
| `cpu_thread_budget` | none (off); positive integer, at most `--cpus` | `submit` numerical thread defaults per task, overridden by `--cpu-thread-budget`; see [CPU thread budgets](cpu-threads.md) |
| `[tool.sherlock-kit.validators]` | none; `NAME = ["argv", ...]` | `pull --validate NAME` runs argv plus the stage directory, only from a configuration file you own |

These are `config_invalid` (exit 1):

- a `site` other than `sherlock` or `marlowe`;
- `account` in a Sherlock configuration ("account is set, but site sherlock
  takes no account");
- an `account` that does not fully match `marlowe-[a-z]+[0-9]+`, which
  refuses `marlowe-users` and every suffixed account;
- a `control_host` or `data_host` that is a default alias of the other site,
  such as `sherlock-dtn` in a Marlowe configuration or `marlowe-plain` in a
  Sherlock one: a copied table would otherwise talk to the other cluster;
- on Sherlock, equal `control_host` and `data_host` (Marlowe's two hosts
  default to one alias).

The hint of an unknown key lists every allowed key. A project sets its site,
account, hosts, remote root and the other keys above; partitions, QOS,
account patterns, Slurm paths, the on-cluster test, the rules and the skill
are package data that no configuration changes.

`doctor`, `rules` and `occupancy` work without a configuration. On Sherlock
(`sh<N>-ln<N>` login nodes, `SLURM_CLUSTER_NAME=sherlock`, a compute node inside a
job, or `SHK_EXECUTOR=local`) the same programs run locally and `push` and `pull`
are refused. On Marlowe they run locally where Marlowe's Slurm configuration
file (`/cm/shared/apps/slurm/var/etc/slurm/slurm.conf`) exists, that is on a
Marlowe login or compute node, and `push` and `pull` are refused there (exit 3
`transfer_on_marlowe`). That test chooses the executor for the selected site,
never the site: on a Marlowe login or compute node, a command without
`--site marlowe` and without a Marlowe configuration is a Sherlock command.
With `--executor local` or `SHK_EXECUTOR=local` such a command is exit 1
`site_mismatch`, whose hint names `--site marlowe`: a forced local executor
on a machine of another site would read that site's Slurm. `requeue --self`
and `allocation`, which need the local executor, run that check before they
refuse another executor, so on a Marlowe node without `--site marlowe` they
too are exit 1 `site_mismatch` (on a workstation they stay exit 3
`self_only` and `local_only`). Without a forced local executor every other
such command takes the ssh executor and, with no master on that node, is
exit 4 naming `sherlock-plain`: inside a Marlowe job that means that
`--site marlowe` is missing, not that a login is needed.

## Sites

`shk` ships two sites as package data: `sherlock`, the default, and
`marlowe`. A command that can reach a cluster takes its site from
`--site NAME`, else from the `site` key of the configuration it found, else
`sherlock`. No environment variable selects a site, and one configuration
names one site.

- A `--site` that names another site than the configuration found (one
  without `site` is Sherlock's) is exit 1 `site_mismatch`, before any remote
  call and for a dry run too. The message names that configuration; the hint
  says to pass `--project-dir DIR`, where DIR holds a configuration with the
  flagged site, or to drop `--site`.
- Without a configuration `--site` alone decides, so
  `shk --site marlowe doctor` checks the Marlowe master.
- `rules` and `install-skill` take the site from `--site` alone (default
  `sherlock`) and never read a configuration: in a Marlowe project
  `shk rules cluster` prints the Sherlock rules and
  `shk --site marlowe rules cluster` the Marlowe ones.

What the site sets:

| | Sherlock | Marlowe |
|---|---|---|
| Default `control_host`, `data_host` | `sherlock-plain`, `sherlock-dtn` | `marlowe-plain` for both: rsync goes through a login node |
| `account` | refused | required by `submit`, dry run included |
| Partition table | `data/partitions.json` | `data/partitions-marlowe.json`: `preempt` only |
| Slurm clients | on the login `PATH` | `/cm/shared/apps/slurm/current/bin` appended to `PATH`, `SLURM_CONF` set |
| Local executor | an `sh<N>-ln<N>` login node, `SLURM_CLUSTER_NAME=sherlock`, or an `sh<N>-` compute node inside a job | Marlowe's Slurm configuration file exists on the machine |
| Before a submit is staged | nothing | Slurm's test, `sbatch --test-only` |
| `doctor --remote` | `site_instructions` for `/etc/agents/AGENTS.md` | no instructions file |
| Rules and skill | `workstation.md`, `cluster.md`; `sherlock-kit` | `marlowe-workstation.md`, `marlowe-cluster.md`; `sherlock-kit-marlowe` |

The [design guide](design.md#sites) explains these choices, and
[Installation and connection](install.md#marlowe) covers the `marlowe-plain`
alias, a project on both clusters and `shk` on a Marlowe login node.

### Replies and hints off Sherlock

For a site other than Sherlock:

- Every reply that prints `executor` adds `"site": "marlowe"` directly after
  it: both `status` forms, `wait`, the `submitted` and preflight replies of
  `submit`, `cancel`, `occupancy`, `resources`, `release`, `requeue`,
  `update` and `allocation`; `doctor` adds it after `executor` at the top
  level and in `remote`. A reply that prints `executor` without `site` came
  from Sherlock.
- The `dry_run`, `pushed`, `pulled` and `already_present` replies and the
  exit 5 reply print neither. Error replies carry no `site` either, except
  the errors of `status --details` (a failed read, a selected name none of
  whose rows parse), which keep the read's `executor` and `site`.
- The `shk` commands that hints name carry the selector, as in
  `run shk --site marlowe doctor`,
  `wait; check shk --site marlowe status at most once per 60 s` and
  `wait at least 60 s, then run shk --site marlowe status; never resubmit blind`,
  and messages name the site (`ACTION: Marlowe unreachable.`,
  `REMOTE is not a directory on Marlowe`).
- The exit 5 `next` is one replayable argv that carries the selector:
  `shk --project-dir DIR --site marlowe --executor KIND status SELECTOR (not before T)`.

Sherlock's replies, hints and `next` are those of a kit without sites.

## `submit`

`submit` submits one logical task. Without `--dry-run` or `--preflight` it makes
one read call and then at most one mutation call. On Marlowe a second read
call, Slurm's test of the request, comes between them; see
[submit on Marlowe](#submit-on-marlowe).

### Job names and states

The job name is `shk.<project>.<task slug>.<hash>` of project, campaign and
task, so every retry of a task reuses it. One read call (`squeue` and the last
30 days of `sacct`) decides the state of the name:

- `live`: always refused.
- `done` or `failed`: refused unless `--resubmit REASON`.
- `new`, or `stale` (spec files in the run directory but no Slurm rows):
  submitted; `stale` is submitted like `new`.

Then one mutation call stages the script under `<run-dir>/.shk/` and runs
`sbatch`.

### Acceptance and outcome unknown

The submission is accepted only when the stdout of `sbatch` is, trailing
newlines aside, one line holding a strict job id, `N` or `N;CLUSTER` with N a
positive decimal integer without a leading zero, matched as ASCII digits
whatever the login locale: exit 0 `submitted`, and the staged files are renamed
to the job id, whatever exit code `sbatch` returned (`sbatch --wait` returns
the job's).

Every other result once `sbatch` has started is outcome unknown, never a
refusal:

- exit 5 with `action: unknown`;
- `detail` holds `sbatch exit RC` and Slurm's stderr text when there is any;
- the staged `NAME.STAMP.sh`, `.json` and `.err` are kept under
  `<run-dir>/.shk/`;
- `next` names the `shk status` command and when to run it.

A lost reply is exit 5 too. A name whose submission never landed then reads
`stale` and submits like `new` when it has no earlier job in that 30-day
window; when it has one, it keeps that job's `done` or `failed` state and
`current.job_id` and still needs `--resubmit`. The attempt landed only if
`current.job_id` is newer than the one seen before the submit.

### Arguments and defaults

- Defaults: `--campaign default`, `--run-dir <remote_root>/runs/<name>`.
- `--array N[%M]` takes 2 to 1000 tasks.
- `--after-ok` is repeatable and takes several job ids (`--after-ok 111 222`),
  each a single `N` or `N_TASK`: `shk` writes the `--dependency` string itself,
  so a `,` or `?` list is refused.
- `--constraint`, `--prefer` and `--signal` reach `sbatch` unchanged; one that
  is given but empty or whitespace-only (an unset shell variable, for example)
  is refused offline, exit 1 `spec_invalid`.
- `dev` is refused offline (exit 3 `batch_not_allowed`): Sherlock rejects every
  batch job there and keeps it for interactive `sh_dev` or `salloc` sessions;
  `occupancy` and `resources` still read it.
- Borrowed partitions need `--courtesy-ack`; see [occupancy](#occupancy).

### GPU selection

For GPU selection, use a broad compatible `--constraint` and a `--prefer` subset
for L40/A/H and discovered compatible future B/newer GPUs. The optional
`--prefer` tier is purely a compute-capability ordering, so it applies across
projects. `--prefer` is soft: Slurm tries it first, then the fallback
`--constraint`, and it tries the preference independently of the fallback, so
repeat all hard requirements in both. These are explicit arguments, not TOML
defaults; see [GPU scheduling and bundles](campaign-throughput.md).

### CPU thread budget

Submission can opt in to explicit host/container numerical thread defaults with
`--cpu-thread-budget [N]` or project `cpu_thread_budget = 1`; the default when the
flag has no value is 1. This does not cap OS thread creation or replace Slurm CPU
isolation. See [CPU thread budgets](cpu-threads.md) for provenance, limits and
controller compatibility.

### Partition lists

`--partition` takes one partition, or compatible partitions as one
comma-separated value, Slurm's own syntax (`--partition gpu,bioe,stat`); a
repeated `--partition` flag is not a list (the last one wins). Only `submit`
and `update` (for a pending job) take a list: `resources` and `occupancy` take
one name per `--partition` flag and refuse a value with a comma (exit 3
`unknown_partition`). A list is given on the command line only; it is never a
TOML key.

The members of a list are checked where a single partition's profile is looked
up, before the resource checks, in this order:

1. an unlisted member, the empty one included, is exit 3 `unknown_partition`
   with the usual message for the first such member;
2. a repeated member is exit 1 `spec_invalid`;
3. a member without batch jobs is exit 3 `batch_not_allowed` with the usual
   message for the first such member;
4. an incompatible set is exit 3 `partitions_incompatible`, whose hint says to
   submit incompatible partitions separately.

A set is compatible when no member is borrowed and all members share one
`requeue` and one `preemptible` value: the message names every borrowed
member, or else lists every member with both values, for example
`gpu (preemptible=no, requeue=no), owners (preemptible=yes, requeue=yes)`.

Then the resource checks apply to the set:

- `--gpus` above 0 needs every member to allow GPUs (exit 3
  `gpus_not_allowed`, naming every member without them);
- requeue defaults to the members' common value;
- `--requeue` on a set that does not allow it is exit 3 `requeue_not_allowed`,
  naming the set as given.

So `gpu,bioe,stat` combines, while `owners`, `btrippe` and `possu` stay alone;
by the same profile fields `normal`, `bigmem` and `service` are in the class
too. A set states that the job may run on any member, and every member's
courtesy text applies wherever it starts.

What Slurm does with a partition list, and what `squeue` and `sacct` print for
one, is source-confirmed and
[not observed live](validation/observed-slurm.md#not-observed-live); whether
Sherlock accepts a list depends on site settings that the source does not
determine: try the list with `--preflight` first.

The argv holds `--partition=<value as given>` in the usual position and is
otherwise that of a single partition: the job name does not depend on the
partition, and the read, the live and duplicate guards, `singleton`, the
staging and the exit 5 handling are those of a single partition (no occupancy
is read: no member is borrowed). The frozen spec keeps `resources.partition`
as the value as given and adds `resources.partitions`, the list, only for more
than one member; in the `spec` that `--dry-run` and `--preflight` print it is
the last key of `resources` (after `prefer` when both are present), and the
staged spec file has sorted keys.

### Dry run and preflight

`--dry-run` is offline. `--preflight` is mutually exclusive and sends the same
script/resources to `sbatch --test-only` as a read: no job, run directory or
provenance is created. Its `preflight` object retains returncode, stdout and
stderr; rejection is exit 3, read failure is never unknown. It is the way to
get a definitive Slurm verdict on a request without creating a job.

### Preflight start estimate

The `--preflight` reply adds `estimate` directly after `preflight`:
`{status, start, start_zone, nodes, partition, line, observed_at}`, read from
the lines of `stderr`, then `stdout`, each split on LF only. An estimate line
is a whole line of Slurm's form

```text
Job N to start at TIME [on cluster NAME] [a] using N processors on nodes NODES in partition PARTITION
```

with or without the `sbatch: ` prefix. `status` is one of:

- `parsed` for exactly one estimate line whose TIME is `YYYY-MM-DDTHH:MM:SS`,
  and then `start`, `nodes`, `partition` and `line` are set (other lines that
  merely resemble an estimate, a debug line for example, do not change this).
- `absent` when no line is an estimate line and none holds the words
  `to start at` between spaces, and then those four are null; this covers an
  accepted call without an estimate and a rejected call, which `ok` and the
  exit code tell apart.
- `unparsed` in every other case: several estimate lines, an estimate line
  whose TIME is not an ISO time (`Unknown`, `None`, a custom or relative
  format), or no estimate line while some line holds the words (an unknown
  shape, an empty node list). Then `start`, `nodes` and `partition` are null
  and `line` is the first estimate line, else the first line holding the
  words: nothing is guessed.

The fields:

- `start` is the time as Slurm printed it, without a zone; `start_zone` is the
  constant `sbatch-local`, because Slurm formats the time in the local zone of
  the `sbatch` process on the executor host, which is the site's zone unless
  `TZ` is set in that session, and prints no offset.
- `observed_at` is UTC and equals `queried_at`; the kit converts nothing.
- `nodes` is the node list as printed, a Slurm hostlist expression, not
  expanded.
- The job id that Slurm prints in the line belongs to a temporary record: no
  key of `estimate` has it as its value (it shows only inside `line` and
  `preflight.stderr`).

The estimate is advisory, not a reservation and not a promise. For a request
with several partitions Slurm names the first partition, in its own priority
order, that can run the job, and estimates the start there: it is not the
earliest start over the set. Comparing partitions takes one preflight per
partition.

### Submission tokens and holds

`--submission-token` accepts 1-128 public characters `[A-Za-z0-9_.:-]` (first
alphanumeric), freezes it in the spec, sets Slurm Comment and exports it to the
job as `SHK_SUBMISSION_TOKEN`. `--hold` freezes a user hold and needs a token,
which `release` checks. Tokens correlate attempts and contain no secrets.

### Script directives

The script's leading `#SBATCH` lines may set anything except what `shk` owns
(`-A`/`--account`, `-x`/`--exclude`, `-G`/`--gpus*`/`--gres`,
`-p`/`--partition`, `-a`/`--array`, `-d`/`--dependency`, `-b`/`--begin`,
`-J`/`--job-name`, `-H`/`--hold`, and `--comment` when `--submission-token`
sets Comment) and a `hetjob` or `packjob` separator: heterogeneous jobs are
refused. Matching follows sbatch's own parsing, so abbreviated long options
(`--part=gpu`) and short flags bundled after value-less ones (`-Hp gpu`) count
too.

### Submit on Marlowe

- `account` is required, dry run included, and never guessed. Without it
  `submit` is exit 1 `config_required`, message "PATH: missing account:
  every Marlowe job names its project account, and shk never guesses it",
  with the hint to ask the user which base project account to charge
  (`sacctmgr show assoc user=$USER format=Account%30,QOS` on Marlowe lists
  the user's accounts), then add `account = "..."` to the named
  configuration file.
- The argv adds `--account=<account>` directly after `--partition=` and is
  otherwise built as on Sherlock. The frozen spec gains no key; its `argv`
  holds the account.
- The partition table lists `preempt` alone (preemptible, requeue, GPUs, not
  borrowed): `batch`, `hero` and every other name are exit 3
  `unknown_partition`, with the message `listed: preempt`. Requeue defaults to
  on, so the argv has `--requeue --open-mode=append`. No partition is
  borrowed, so `submit` never reads occupancy and needs no `--courtesy-ack`.
  GPUs are untyped (`-G N`) and the nodes have no GPU model features:
  [GPU selection](#gpu-selection) does not apply.
- After the read and the duplicate guards, before anything is staged, a plain
  `submit` runs Slurm's test, the call that `--preflight` makes
  (`sbatch --test-only`). When Slurm refuses, `submit` returns that preflight
  reply unchanged: `"ok": false`, `"action": "preflight"`,
  `"job_submitted": false`, Slurm's text in `preflight.stderr`, exit 3; no run
  directory is created, no file is staged and no mutation call is made. When
  Slurm accepts, staging and `sbatch` follow as described above. A controller
  transport failure in the test is exit 1 `malformed_read`, as for
  `--preflight`.
- Every Marlowe preflight reply, plain `--preflight` and the refusal of a
  submit alike, ends `estimate` with `note`: "not a wait prediction on
  Marlowe; see the Marlowe rules". The start that `sbatch --test-only` prints
  there is not a queue-wait prediction
  ([observed](validation/observed-marlowe.md#test-only-estimates-and-real-starts)).
- "Requested node configuration is not available" may also mean that no node
  is schedulable right now: read
  `shk --site marlowe resources --partition preempt --summary` before
  changing a request on that message.

## `status`

`status` reads one `squeue` and one `sacct` for all selected names (`--details`
adds one `scontrol show job` per queued base job id).

- Give `--campaign` once for every `--task` or once per `--task`, and
  `--run-dir` once per `--task` or not at all.
- `--since` defaults to 30 days (7 with `--all`) and accepts 1 to 31, because
  Sherlock's slurmdbd refuses wider `sacct` windows. The bound holds on every
  site; Marlowe accepts wider windows, and its hint says that shk reads at most
  31 days of accounting on every site.
- Partitions are looked up in the selected site's table, so on Marlowe a
  preempted and requeued attempt on `preempt` is no anomaly.
- Repeatable `--job-id N` or `N_TASK` is mutually exclusive with task, name and
  all selectors; it includes legacy names and only current UID rows, and lists
  one entry per selected base job.
- Without a spec file (always with `--all`, `--name` and `--job-id`) an array
  whose seen tasks are not exactly 0 to the largest index has null
  `array.count`, `tasks.count` and `tasks.missing`, and `done` or `failed`
  describe the seen tasks only.
- A preemption or restart is unexpected (exit 6) on the non-preemptible
  partition of the attempt it interrupted, and on an interrupted partition list
  whose members are all listed and none preemptible.
- `current.partition` is Slurm's partition text of the job's first row; for a
  job submitted with a partition list, `status --task` adds `current.ran_on`,
  the partitions in which it started (see
  [partition and ran_on](#partition-and-ran_on)).
- `--details` returns the evidence contract described in
  [detailed status contract](#detailed-status-contract).

### Partition and `ran_on`

`current.partition` is the partition text of the first row Slurm printed for
the job, queue rows before accounting rows; the order is Slurm's and `shk` does
not fix it.

- For a single job submitted with a partition list this is the list while
  pending (in an order Slurm may change) and the run partition once started;
  for a job that ended without starting it is one member in `squeue` and the
  list in `sacct`.
- For an array, or a requeued job read from accounting only, it is one row's
  text, can change between reads and says nothing about the other tasks or
  attempts.

`current.ran_on` directly follows `partition` and is present only when status
read the frozen spec of that job (the newest spec file of the name, for that
job id, as for the task count) and its `resources.partitions` lists several
partitions: that is `status --task` with a run directory, plain or with
`--details`.

- Its value is the sorted distinct partitions of the job's started rows, that
  is every partition a task or attempt started in, and `[]` while no row shows
  a start.
- A started row is an explicit row (not an `N_[...]` aggregate) whose
  partition text is one name and which shows a start: an accounting row that
  is not `PENDING` and has AllocCPUS above 0, or a queue row in a state that
  status counts as running.
- A job that finished before accounting recorded its start also reads `[]` on
  that read, with `cost.final` false.

Without a spec (`--all`, `--name`, `--job-id`) there is no `ran_on`:
`current.partition` is the first row's text, which for an array whose tasks
started on different members names only one of them or the pending list. The
per-row partition of `status --details`, or `--task`, tells where each ran.
Every queue and accounting record of `details` keeps its partition text as
Slurm printed it, and the cost arithmetic does not depend on it.

`unknown_partition` (`shk.` names only) means that some row of the job has a
partition text, split on `,`, with a member that is not listed; the verdict
does not depend on the row order. `unexpected_preemption` also counts an
interrupted partition text that is a comma list when every member is listed
and none is preemptible (a compatible set shares one value).

### Detailed status contract

`status --details` adds
`details = {queue, accounting, jobs, complete, queried_at, history_window_days, missing_job_ids, missing_control_job_ids, selection, allocation_evidence_complete, history_complete, control_identities_complete, returncodes, malformed}`.

**Queue and accounting records.** Queue and accounting are lists of full
records, before summary deduplication; every record includes the original `raw`
line, whose fields are separated by the ASCII unit separator (0x1F) so that `|`
in comments and job names stays data. Their normalized fields are `job_id`,
`job_id_raw`, `cluster`, `db_index`, `state`, `partition`, `elapsed_seconds`,
`allocated_cpus`, `allocated_tres`, `submitted_at`, `started_at`, `ended_at`,
`restarts`, `exit_code`, `nodes`, `name`, `comment` and `uid`.

- Missing fields are null, numeric facts are integers, timestamps are UTC
  strings, IDs (including DBIndex) remain strings.
- Queue CPUs and start are null for a job without nodes (pending, or cancelled
  before any allocation), and a live job's end is null, because squeue prints
  requests, placeholders or predictions there.
- Sherlock's accounting `Comment` is empty, so `details` shows a submission
  token only in queue and control records while the controller holds the job;
  the frozen spec keeps it. Slurm also records the sbatch command line, which
  carries the token, as `SubmitLine` (in control records, and in `sacct` where
  slurmdbd stores it).

**Control records.** `jobs` holds current-UID `scontrol` records of queued
physical IDs as `{job_id, name, comment, uid, fields, raw}`, read with one
`show job` per base job id: an array ID prints every element the controller
still holds, records outside the selection are ignored, and each queued
physical ID needs its own record. `fields` retains original Slurm names and
string values (times in UTC), including request TRES, Features, TimeLimit,
Requeue and array identity. All records are restricted to the current UID.
`returncodes` holds each tool's exit status.

**Completeness.** A failed `squeue` or `sacct` read, or a selected name none of
whose rows parse, exits 1 `malformed_read` like plain status, with `details`
and without `names`. A failed control read, missing requested IDs, malformed
rows or missing accounting cost identity make `complete` false and retain
available facts; a failed control read alone still exits 0. Accounting rows of
jobs that never had an allocation (AllocCPUS 0, ElapsedRaw 0, no Start and no
AllocTRES, in any state such as PENDING or `CANCELLED by UID`) are structurally
complete, and a terminal one has a final zero cost. Missing allocation TRES
after a job started and restart-history gaps are incomplete.

**Selection by job id.** `--job-id` lists one entry per selected base job, in
job id order; legacy jobs often share a script name, so `duplicate_live`
(across the selected jobs) and `unknown_partition` apply to `shk.` names only.

- Each entry's `current.evidence_complete` covers its own job (accounting
  identity and allocation, control records, restart history, partial scope)
  and malformed lines that no job can be told apart from; `cost.final`
  requires it, while `complete` is the conjunction over the whole query.
- A requested ID without readable rows (a hetjob's `N+k`, for example) has no
  entry and is listed only in `missing_job_ids`, so a batch's cost is final
  only when that list is empty and every entry has `cost.final`.
- By task or name, an unreadable row withdraws only its own name's final cost.

**Arrays.** An exact array-element selection reports
`selection.partial_array_ids`, entry `scope: partial_array`, unknown
whole-array state/count/missing values, and nonfinal cost; sibling states
cannot be inferred from a selected element. Without a spec file an array's task
count is inferred only when the seen tasks are exactly 0 to the largest index;
otherwise (1-based or stepped legacy arrays, early tasks gone from the window)
`array.count`, `tasks.count` and `tasks.missing` are null, the state follows
the seen tasks and the cost is not final. A cancelled array that never split is
one squeue row without a task suffix, and names-mode `sacct` returned no rows
on Sherlock for jobs held until cancelled, so such a names-mode summary can
count one task; `--job-id` reads the accounting aggregate.

**History bound.** A complete query does not establish unlimited historical
coverage: it is bounded by `history_window_days`, and array cardinality without
provenance is inferred.

## `wait`

`wait` repeats the read that `status` makes for the same selection until the
selected names settle, one of them fails or a bound is reached, and prints one
reply at the end. It lets an agent wait for a job without spending a turn on
each check. It is read-only: it dispatches no mutation and writes nothing,
locally or remotely.

### Options

- The selection (`--task`, `--name` or `--all`), `--campaign`, `--run-dir` and
  `--since` have the meaning, defaults and checks of [status](#status):
  `--since` defaults to 30 days (7 with `--all`) and accepts 1 to 31. `wait`
  has no `--job-id` and no `--details`.
- The global options apply as for `status`. `wait` runs with the ssh and the
  local executor, on Sherlock and on Marlowe (`shk --site marlowe wait`).
- DUR takes the forms of `submit --time`: `2h`, `90m` or `H:MM:SS`. Another
  form, or a value outside its range, is exit 1 `spec_invalid` (the token
  `--time` uses), before the first read:
  - `--timeout DUR` is required, from 1 minute to 168 hours (7 days), both
    included; without it the command is a usage error (exit 2).
  - `--interval DUR` defaults to `5m` and is at least 1 minute, with no upper
    bound.
  - `--max-pending DUR` is optional and off by default; when given it is at
    least 1 minute.

### Loop

The wait takes its start time from the clock and sets `timeout_at` to the
start plus `--timeout`. It then sleeps 60 s, so that its first read comes at
least 60 s after the `shk` reply before it (a submit, a status read, an
earlier wait), and loops:

1. It reads: exactly the read that `shk status` makes for the selection, one
   remote call with one `squeue` and one `sacct`.
2. It decides, in this order; the first case that applies ends the wait:
   1. `anomaly`: the read is exit 6, an anomaly on the newest job of a listed
      name;
   2. `failed`: with `--task` or `--name`, a listed name is `failed` now, at
      any read, the first one included; with `--all`, a name is `failed` now
      and was `live` at an earlier read of this wait;
   3. `settled`: no listed name is `live`;
   4. `max_pending`, only with `--max-pending`: the current job of a live name
      has shown no running task (`current.tasks.running` 0) at every read
      since the first read that showed it so, for at least `--max-pending`
      measured from that read; a read that shows a running task starts that
      name's count again;
   5. `timeout`: less than 60 s remain before `timeout_at`.
3. Otherwise it sleeps for the current delay, cut to what is left before
   `timeout_at` and, with `--max-pending`, to what is left before the earliest
   live name without a running task reaches that bound (but to no less than
   60 s), and reads again.

The delay starts at `--interval`. After a read in which no listed name has a
running task and every name shows the state, current job id and task counts
of the previous read, the delay doubles, up to 30 minutes or `--interval` when
that is longer; after any other read it returns to `--interval`. A sleep cut
short by `timeout_at` or `--max-pending` leaves the delay as it is. With the
default interval, reads of a job that pends unchanged are 5, 10, 20 and then 30
minutes apart, and reads of a running job 5 minutes apart. The wait ends
before it would sleep less than 60 s, so consecutive reads are always at least
60 s apart, and it ends no later than `--timeout` plus one read.

- `settled` means only that no listed name is `live`. A name that is `done`
  is not live, and neither is a `new` or `stale` name, which has no Slurm
  rows: a wait on such names alone settles at the first read, 60 s after the
  start. Start a wait once `submit` has returned the job id; it may start
  right after that reply, or after any other, since its first read comes 60 s
  later.
- With `--task` or `--name`, a listed name whose current job is `failed`
  gives `failed` at any read, so a job that failed before the wait started,
  or in the 60 s before its first read, ends the wait at that read: list only
  the names still waited for.
- With `--all` the names are those the read lists, so an old failed job never
  gives `failed`: only a job that the wait saw live does, and a job that fails
  before the first read does not (read each name's `state` in the reply). An
  anomaly on any listed name, an old one included, ends the wait at the first
  read.
- Pending time before the wait's first read does not count toward
  `--max-pending`, and neither does pending time before the last read that
  showed a running task. The bound is checked at a read, and the wait cuts a
  sleep so that its next read falls on the earliest bound, or 60 s after the
  read before it when the bound falls sooner: with `--interval 5m`,
  `--max-pending 20m` ends at 21 minutes for a job that never starts. The
  reads come at 1, 6 and 16 minutes, the first of them starting the count;
  the delay after the third read is 20 minutes, and the sleep is cut to 5, so
  the fourth read, at 21 minutes, falls on the bound.
- An error of a read (exit 1 `malformed_read` or a configuration error, exit 4
  `auth_required`, `unavailable` or `timeout`) ends the wait with that error
  reply and exit code, without a retry; the error reply has no `wait` object.
- Nothing is printed while it waits: stdout carries one JSON reply at the end.
  `wait` writes nothing to stderr itself; the Sherlock login banner reminder
  goes there once per process, as for every command.

### Reply

The reply is the last read's `status` reply, unchanged (`executor`, the site
fields, `user`, `host`, `queried_at`, `next_check_after`, `names` and
`truncated`), with `wait` added:

```text
"wait": {"outcome": "settled" | "failed" | "anomaly" | "max_pending" | "timeout",
         "names": [NAME, ...], "reads": N, "started_at": ISO-UTC, "timeout_at": ISO-UTC}
```

- `reads` counts the reads of this wait, the last one included.
- `started_at` and `timeout_at` have the format of `queried_at`
  (`YYYY-MM-DDTHH:MM:SSZ`, UTC).
- `next_check_after`, 60 s after the last read, is the earliest time for a
  `status` read by hand. A new wait may start at once: its first read comes
  60 s after it starts.

| Outcome | Ends the wait when | `wait.names` | Exit |
|---|---|---|---|
| `anomaly` | the read is exit 6 | the names whose current job has anomalies | 6 |
| `failed` | a listed name is `failed` (with `--all`, one that the wait saw `live`) | those names | 0 |
| `settled` | no listed name is `live` | `[]` | 0 |
| `max_pending` | a live name has had no running task for `--max-pending` | those names | 0 |
| `timeout` | less than 60 s remain before `timeout_at` | `[]` | 0 |

Since `wait` dispatches nothing, no outcome is ever unknown: it has no exit 5.

### Waiting from an agent

An agent runs one bounded `shk wait` as a background command that its harness
tracks and whose exit it reports (in Claude Code, Bash with
`run_in_background`), so that it spends no turn while the job is queued or
running, and then acts on `wait.outcome`; the skill says what to do after each
outcome. A shell `&` is not such a command: nothing reports its exit, and it
can outlive the session. Where the harness reports no such exit (in Codex, for
example, nothing wakes the agent when a background command ends), the agent
either runs the wait in the foreground with a `--timeout` within the
tool-call limit or ends the turn and checks when the user asks; it neither
starts the wait with `&` nor polls its output. A wait that is killed loses its
reply and nothing else. At most one wait runs at a time, over every name to
watch (repeat `--task`, or `--all`); after a further submit the agent lets the
running wait end, or stops it and starts one wait over all names. A restarted
wait with `--task` reports a name that failed in between at its first read;
one with `--all` does not. A wait belongs to the attended session that started
it. It is never detached so that it outlives the session (`nohup`, `setsid`,
`&`, a `tmux` or `screen` session left running, cron) and never used to prompt
an agent again; see the rules.
Inside a Sherlock allocation a bounded deterministic controller may run
`shk --executor local wait` ([controllers inside a job](#controllers-inside-a-job));
no wait runs inside a Marlowe job, where it would be billed idle time: there
it runs on the workstation or in a login-node session.

## `cancel`

`cancel` cancels the live jobs of one name; a nonzero `scancel` exit is exit 5
with `scancel exit RC` and Slurm's stderr text in `detail`, and a lost reply is
exit 5 too; both are settled by the status read that `next` names.

## `occupancy`

`occupancy` lists current use of partitions. Borrowed partitions (`btrippe`,
`possu`) need it and `--courtesy-ack` before a submission, and such a
submission reports `occupancy`, the partition's `courtesy` text,
`request = {tasks, throttle, minutes}` and `warnings`.

- `tasks` is 1 for a single job and the array count otherwise, `throttle` is
  the `%M` limit or null, and `minutes` is the requested walltime.
- Two advisories can appear in `warnings`, and neither refuses anything: the
  budget warning, when others are running there and you already have 2 jobs
  there or ask for more than 2 h; and a total-work advisory, when others are
  running there and `tasks` is greater than 2, which states the number of array
  tasks, the throttle (or that there is none) and the minutes without calling
  the request within or over any budget.
- The courtesy terms are the profile texts alone: `btrippe` has no blanket
  daytime prohibition, and `possu` keeps its 00:00-07:00 Pacific window.
- Releasing a job held there counts as submitting it: `release` checks neither
  occupancy nor the clock, so check both first.
- Each `--partition` must be listed in the selected site's table; Marlowe's
  has no borrowed partition.

## `resources`

`resources` reads `scontrol show nodes` once; Sherlock ships only the nodes
whose `Partitions=` list names a requested partition, each once.

- A node keeps its state, partitions, active features, configured/allocated CPU
  (CPUEfctv, else CPUTot), memory and GPU, CfgTRES/AllocTRES and its raw line
  (the evidence, so `owners` is megabytes: filter the JSON rather than read it
  whole). `complete`, `malformed`, `next_check_after` and the `provenance` of
  the derived fields come before `nodes`.
- `free_*` is configured minus allocated whatever the state (`free_memory_mb`
  from CfgTRES minus AllocTRES; OS FreeMem never substitutes).
- `schedulable` is false when the `+`-joined state has any of `DOWN`, `DRAIN`,
  `FAIL`, `MAINTENANCE`, `NOT_RESPONDING`, `RESERVED`, `INVALID_REG`,
  `FUTURE`, `UNKNOWN`, `REBOOT_ISSUED`, `REBOOT_REQUESTED`, `POWER_DOWN`,
  `POWERED_DOWN` or `POWERING_DOWN` (or their short sinfo spellings, listed in
  `provenance`), true otherwise (`PLANNED` and `COMPLETING` included) and null
  without a state: count only schedulable nodes.
- Missing facts are null and `complete: false`; a failed `scontrol` read is
  exit 1 `malformed_read` with its rc and stderr.
- Partition and QOS limits, reservations and owners preemption are not
  reflected; availability is a snapshot, not a resource reservation.
- Each `--partition` must be listed in the selected site's table. Marlowe's
  nodes have no `GPU_SKU:` or `CLASS:` feature, so there `--by sku` and
  `--by class` give one entry each, with the key `[]`.

`--summary` reports the same single read as counts instead of `nodes`: per node
class, for the union of the shipped nodes and for each requested partition,
optionally split `--by sku` or `--by class` and with slot counts for each
`--shape`. It ranks nothing; see [resource summary](#resource-summary).

### Resource summary

`resources --summary` is a separate output mode of the same single
`resources.sh` read. Without `--summary` the reply is the per-node reply
described above. In summary mode the reply holds, in this order, `complete`,
`malformed`, `executor`, `queried_at`, `next_check_after`, `provenance` (the
same object) and `summary`; there is no `nodes` key. `complete` and
`malformed` are computed as without `--summary`, and a failed read is exit 1
`malformed_read`.

- `--by` takes one value per flag, `sku` or `class` (`--by sku --by class`);
  another value is a usage error (exit 2), and repeats collapse.
- `--shape NAME:cpus=N,mem=SIZE[,gpus=N]` takes one shape per flag. NAME
  matches `[A-Za-z0-9][A-Za-z0-9_.-]{0,31}` and is unique; the keys may come in
  any order; `cpus` matches `[1-9][0-9]*`; `mem` uses the `submit --mem`
  grammar (`16G`, `16000M`; G = 1024 M); `gpus` matches `0|[1-9][0-9]*` and
  defaults to 0. A shape is one slot on one node. A bad NAME, a repeated NAME,
  an unknown, repeated or missing key or a bad value is exit 1 `spec_invalid`,
  offline.
- `--by` or `--shape` without `--summary` is exit 1 `spec_invalid`, offline,
  before any remote call. The offline checks run in this order: the
  partitions (exit 3 `unknown_partition`), then `--by` or `--shape` without
  `--summary`, then each shape in the order given.

**Node classes.** Every node line the read ships is one node, in exactly one
class. A known node has a non-null `name` and `state` and non-null configured
and allocated CPUs, memory and GPUs (so its three `free_*` values are
non-null). A countable node is known and `schedulable`; an unschedulable node
is known and not `schedulable`; every other node is unknown. A line listed in
`malformed` is an unknown node too: its `partitions` and `features` are read
from the raw line as for any record, and every other fact, `schedulable`
included, is null. It counts in `union`, in the group of every requested
partition it names, in the breakdown entry for its feature key, and in
`overlapping_nodes` when it names more than one requested partition. The
`malformed` list is the same as without `--summary`.

**Groups.** The union, each partition and each breakdown entry are a group
object:

```text
{"nodes": N, "schedulable_nodes": N, "countable_nodes": N,
 "unschedulable_nodes": N, "unknown_nodes": N,
 "cpus":      {"configured": N|null, "allocated": N|null, "free": N|null},
 "memory_mb": {"configured": N|null, "allocated": N|null, "free": N|null},
 "gpus":      {"configured": N|null, "allocated": N|null, "free": N|null},
 "slots": {"<shape NAME>": N|null, ...}}
```

- `nodes` is countable plus unschedulable plus unknown nodes.
  `schedulable_nodes` counts every node whose `schedulable` is true, known or
  not.
- `configured` and `allocated` are sums over known nodes (countable and
  unschedulable); `free` is the sum of `free_*` over countable nodes. Memory is
  in the unit of the node fields (`*_memory_mb`).
- `slots[NAME]` is, over countable nodes, the sum of
  `min(free_cpus // cpus, free_memory_mb // memory_mb, free_gpus // gpus)`, the
  GPU term only when the shape asks for GPUs. `slots` keys follow the shape
  order, and `slots` is `{}` without a shape.
- Unknown stays unknown: a value with nothing known behind it is null,
  never 0. `configured` and `allocated` are null in a group without a known node.
  `free` and every slot are null in a group without a countable node, unless
  every node of the group is a known unschedulable node (then they are 0, a
  known fact). So a group without any node (a requested partition that no
  shipped line names, or the union of an empty read) has `nodes` 0 and null
  sums and slots: the read does not show its capacity. With some known nodes
  and `unknown_nodes` above 0 the numbers are lower bounds, and `complete` is
  false.

**The summary object.** The `summary` object holds these keys in this order:

```text
"summary": {
  "rules": {"snapshot": TEXT, "countable": TEXT, "slots": TEXT, "overlap": TEXT},
  "shapes": {"<NAME>": {"cpus": N, "memory_mb": N, "gpus": N}, ...},
  "by": ["sku", "class"],
  "overlapping_nodes": N,
  "union": GROUP + breakdowns,
  "partitions": {"<partition>": GROUP + breakdowns, ...}
}
```

- `shapes` and `by` are in the order given, `by` deduplicated and `[]` without
  `--by`. `overlapping_nodes` counts the nodes that list more than one
  requested partition. `union` counts every shipped node once. `partitions`
  is in request order, deduplicated.
- A partition's group holds the nodes whose `partitions` list names it, so a
  node in two requested partitions is in both groups and once in `union`. No
  total over partition groups is printed.
- Breakdowns appear on `union` and on every partition, only for the requested
  kinds, after the group fields in the order of `by`:
  `"by_sku": [{"sku": [VALUE, ...], GROUP fields}, ...]` and
  `"by_class": [{"class": [VALUE, ...], GROUP fields}, ...]`. The key of a node
  is the sorted list of its active feature values after the prefix `GPU_SKU:`
  (`sku`) or `CLASS:` (`class`); a node without such a feature has the key
  `[]`. A node is in exactly one entry of a breakdown, the one for its exact
  key, so a node with several SKU features is in the entry for that exact set
  and gives its GPUs to no single-SKU entry. Entries are sorted by key. The
  entries of one breakdown split their group: the five node counts add up to
  the group's; for each capacity value and slot, when the group's value is a
  number the non-null entry values add up to it, and when it is null every
  entry's value is null or 0 (0 only for `free` and slots, in an entry that
  holds only unschedulable known nodes). Without `--by` there is no `by_sku` or
  `by_class` key.
- `rules` holds four fixed texts:
  - `snapshot`: "Snapshot heuristic at queried_at: configured minus allocated
    capacity. It does not model fairshare, QOS or partition limits,
    reservations, preemption or pending work, reserves nothing and predicts no
    start time."
  - `countable`: "A node is countable when its State has none of the flags
    listed in provenance.schedulable and its configured and allocated CPUs,
    memory and GPUs are all known. free and slots count countable nodes only;
    configured and allocated also count unschedulable nodes. A node with a
    missing fact is in unknown_nodes and in no sum: beside a nonzero
    unknown_nodes the numbers are lower bounds. A value with nothing known
    behind it is null, never 0: configured and allocated in a group without a
    known node, free and slots in a group without a countable node unless all
    its nodes are known unschedulable nodes."
  - `slots`: "Per countable node: the smallest of free CPUs, free memory and,
    when the shape asks for GPUs, free GPUs, each divided by the shape's amount
    and rounded down; summed over the group."
  - `overlap`: "A node that lists several partitions is in each of their
    groups, and by_sku and by_class split the same nodes in two ways: never add
    partition groups or the two breakdowns together. union counts every node
    once. Within one breakdown a node is in exactly one entry, the one for its
    exact set of feature values."
- The summary ranks nothing: no recommended partition, shape or GPU model, no
  sorting by capacity, no lowered floor.

## `allocation`

`allocation` verifies an explicit local worker PID, own UID, physical job/array
identity, current host and optional numeric step membership.

- It preserves actual resources, node records, raw records and hashes; missing
  counts remain null and no project resource policy is inferred.
- It refuses SSH, exited/reused PIDs and ambiguous or contradictory evidence; a
  scheduler that does not answer is a retryable read failure, never
  non-membership.
- Its `gpu_sku_candidates` list the `GPU_SKU:` features of each node record as
  candidates for the GPU model, with an `ambiguous` flag and the source fields;
  it never states which device the job got.
- On Marlowe it is `shk --site marlowe --executor local allocation ...`, and
  every entry of `gpu_sku_candidates` has empty `candidates`: Marlowe's nodes
  carry no `GPU_SKU:` feature, and their GRES is untyped.

See [the allocation contract](allocation.md) for the JSON schema.

## `release` and `requeue`

`release` checks one exact ID, current UID, expected name/token and user hold
immediately before `scontrol release`.

`requeue --self` has no arbitrary ID option. It requires:

- the local allocation (`SLURM_JOB_ID`);
- actual Slurm PID membership in that physical job;
- current UID and name, and the token only when `--expect-token` is given;
- RUNNING state;
- a partition whose profile allows requeue (Sherlock's `owners`, Marlowe's
  `preempt`) and Requeue=1.

These are read from Slurm's record of that physical job, which also names the
target: an array element is requeued as its `ArrayJobId_ArrayTaskId`, never the
bare array ID, which would requeue every element; inherited `SLURM_ARRAY_*`
variables are not trusted.

For both commands, a refusal is exit 3 and keeps Slurm's message when a query
failed; a nonzero `scontrol` mutation is exit 5 with `scontrol ACTION exit RC`
and Slurm's message in `detail`, and a lost reply is exit 5 too. Slurm
ownership checks still apply, and concurrent user changes can race the
identity read. Checkpoint durability and retry limits belong to the caller.

## `update`

`update` changes one field group of one own job in place, after identity
checks on every record that Slurm holds for it. The job keeps its job id, so
jobs that depend on it (`--after-ok`) still wait for it.

### Field groups

| Group | Options | Slurm key | Every record | What may change |
|---|---|---|---|---|
| time | `--time DUR` | `TimeLimit` | `PENDING` | a decrease, not below `TimeMin` |
| partition | `--partition P[,P...]` | `Partition` | `PENDING` | a listed, non-borrowed partition or compatible set of the job's requeue and preemptible class |
| features | `--constraint X`, `--prefer X` or both | `Features`, `Prefer` | `PENDING` | a new nonempty value |
| throttle | `--throttle M` | `ArrayTaskThrottle` | any state | an array's run limit, 1 to 1000 |

Give exactly one group per call. Zero or several groups are exit 1
`spec_invalid`, with a hint to run one update per field: Slurm validates and
commits the keys of one `scontrol update` one at a time, without rollback, so a
request with several fields can fail with some of them applied
([source-confirmed](validation/observed-slurm.md#job-updates)). `--constraint`
and `--prefer` are one group and go in one request, because Slurm tries the
preference instead of the constraint, not as their intersection: both must
carry every hard requirement. A `--constraint` alone leaves a stored Prefer as
it is, and Slurm still tries that Prefer first.

- `--job-id` takes a plain job id or an array's ArrayJobId, `N` only. An update
  addresses the whole job or the whole array, every record that Slurm holds for
  it, so `N_TASK` is exit 1 `spec_invalid`.
- `--expect-name` is required and nonempty, any name as for `release`, and must
  equal JobName on every record.
- `--expect-token` is optional, as for `requeue`: when given it must be a valid
  token (exit 1 `spec_invalid` otherwise) and equal the Comment on every
  record.

### Offline checks

These run before any remote call, without network; each failure is exit 1
`spec_invalid` unless stated otherwise:

- `--time` takes the `submit --time` grammar (`2h`, `90m` or `H:MM:SS`, above
  zero), rounded up to whole minutes.
- `--partition` takes the checks of a `submit` value, in the order of
  [partition lists](#partition-lists): an unlisted member is exit 3
  `unknown_partition`, a repeated member exit 1 `spec_invalid`, a member
  without batch jobs exit 3 `batch_not_allowed` and an incompatible set exit 3
  `partitions_incompatible`. A single borrowed partition (`btrippe`, `possu`)
  is exit 3 `borrowed_not_allowed`, with the hint "cancel, run shk occupancy,
  then resubmit with --courtesy-ack"; a set with a borrowed member is already
  `partitions_incompatible`. The table is the selected site's: on Marlowe it
  lists `preempt` alone.
- `--constraint` and `--prefer` take a nonempty value of at most 1024 bytes,
  made of letters, digits and `_ . : + * & | ( ) [ ] -` only: whitespace never
  matches a feature and would break the one-line re-read, Slurm stores `,` as
  `&` (write `&`), and `=` could forge a field of the record. An empty value,
  which would clear the field, is not supported, and neither is the literal
  `(null)`, which Slurm prints for an unset value.
- `--throttle` takes a decimal from 1 to 1000, the per-array task maximum; 0,
  Slurm's "no limit", is not accepted.

### Remote checks

`update.sh` reads `scontrol show job N --oneliner` once and keeps the lines
that start with `JobId=`: the job's record, or every record of the array (the
meta record and every split task still in the controller, finished ones
included). Before any change it refuses with exit 3 `remote_refused`, message
`update: SHK_REFUSED <reason>` with Slurm's text where a read failed; nothing
is dispatched on any refusal. After the read (`query`), `ambiguous` and `id`
look at every record first. Then each record in turn, in the order Slurm
prints them, goes through `name`, `uid`, `token`, `state` and the checks of
the field in the order of the table, so the first record that fails one names
the reason. Last, a partition request reads each requested member, one at a
time, and checks every record's TimeLimit against its MaxTime
(`partition_query`, then `maxtime`):

| Reason | Refused when |
|---|---|
| `query` | the `show job` read failed |
| `ambiguous` | there is no record, or a record's leading fields do not parse |
| `id` | no record has JobId N; N is a split array task's own id; or a job that is not an array has several records |
| `name`, `uid`, `token` | JobName is not NAME, UserId is not the caller, or Comment is not the given token |
| `state` | time, partition, features: a record's JobState is not exactly `PENDING` |
| `time` | TimeLimit is not `[D-]HH:MM:SS` (`UNLIMITED`, `Partition_Limit`), or the requested minutes exceed it |
| `timemin` | the requested minutes are below TimeMin (`N/A` is 0) |
| `requeue` | Requeue differs from the target's `requeue` value |
| `preemptible` | a member of the current Partition is not a listed partition with the target's `preemptible` value |
| `gpus` | the target does not allow GPUs and the job requests them (`gres/gpu` in ReqTRES or a TresPer field) |
| `partition_query` | the `scontrol show partition` read of a target member failed |
| `maxtime` | TimeLimit exceeds a target member's MaxTime (`UNLIMITED` passes) |
| `array` | throttle: the job is not an array |
| `borrowed` | throttle: a member of a record's Partition is borrowed and the throttle would rise (no throttle is no limit, so any value lowers it) |

The partition checks hold a move to the rules of a submission: the job's
requeue and preemptible class, GPUs only where the target allows them, and a
time limit within every member's MaxTime. Slurm does not check the GPUs of a
moved job and, unless partition limits are enforced at update time, accepts a
time limit above MaxTime: such a job would pend instead of being refused. The
current partition is judged by its class alone, so a job pending on a borrowed
partition can move to a non-borrowed partition of the same class.

### Outcome and reply

When every record already shows the requested value (the same minutes, the
same set of partitions in any order, the same Features and Prefer text, the
same throttle) the reply is `unchanged` (exit 0) and nothing is dispatched.
Otherwise `update.sh` runs exactly one `scontrol update JobId=N K=V [K=V]`,
each `K=V` one argv element (`TimeLimit=M` in whole minutes, `Partition=` the
value as given, `Features=`, `Prefer=`, `ArrayTaskThrottle=M`), never retries
it, and re-reads every record. The outcome comes from that re-read alone,
whatever `scontrol` returned:

- `updated` (exit 0) when the re-read has at least one record and every record
  shows the requested value: TimeLimit equal to the minutes, the Partition set
  equal to the requested set, Features and Prefer equal to the given values,
  ArrayTaskThrottle equal to M.
- Otherwise exit 5 with `action: unknown`: `detail` names
  `scontrol update exit RC`, Slurm's text and the records that do not show the
  requested value, or that the re-read failed, and `next` names
  `shk status --job-id N --details`. A lost reply, or one without exactly one
  terminal marker, is exit 5 too.

The exit status of `scontrol update` does not say whether the change happened
([source-confirmed](validation/observed-slurm.md#job-updates)). A throttle
stored on an array exits 1 while finished split records are still in the
controller (`Job has already finished`), and reads as `updated` with that text
in `slurm_messages`; a partition can be committed before a later check returns
an error; a job_submit plugin can reject or rewrite the request; and a lost
reply reads the same whether or not the request reached the controller. A value
that a plugin rewrote, or a drained or inactive member that Slurm replaced by
its Alternate or dropped, therefore reads as exit 5, settled by the status read
in `next`.

The reply, after `schema_version` and `ok`:

```text
{"action": "updated" | "unchanged", "job_id": "N", "name": NAME,
 "submission_token": TOKEN | null, "field": "time" | "partition" | "features" | "throttle",
 "requested": {SLURM_KEY: VALUE, ...}, "before": [RECORD, ...], "after": [RECORD, ...],
 "slurm_messages": TEXT | null, "executor": ..., "queried_at": ..., "next_check_after": ...}
```

- `requested` holds the Slurm keys and values sent: `TimeLimit` (minutes),
  `Partition`, `Features` and `Prefer` as given, or `ArrayTaskThrottle`.
- `before` holds every record of the first read and `after` every record of
  the re-read. A RECORD is `{job_id, array_task_id, state}` plus the field's
  keys as Slurm printed them: `TimeLimit` and `TimeMin`; `Partition` and
  `Requeue`; `Features` and `Prefer`; or `ArrayTaskThrottle`, null when the
  record prints none.
- For `unchanged`, `after` is `[]` and `slurm_messages` is null. For `updated`,
  `slurm_messages` keeps Slurm's stderr and stdout text when there was any: a
  nonzero exit with every record applied, or a plugin message.

### Effects and limits

- A held job stays held: an update neither releases nor sets a hold, so
  release it with `shk release` when ready (on a borrowed partition, releasing
  counts as submitting). For a job that is not held, a successful update can
  reset Reason to None until the scheduler sets it again.
- The job keeps its job id, its dependencies (`singleton`, `afterok`), its
  begin time, its QOS and its requeue flag.
- A partition move can lower the job's priority for a while: under
  `AccountingStorageEnforce=limits` without `PriorityFlags=ACCRUE_ALWAYS`, the
  move zeroes the job's accrue time, so its age factor dips until the next
  scheduler pass and restarts from the move where accrue limits
  (`MaxJobsAccrue`, `GrpJobsAccrue`) apply. Sherlock's settings are not known.
- Features are not checked per partition: Slurm checks feature names against
  the union of all node features in the cluster, so a feature that no node of
  the job's partitions has is accepted and the job never starts. Check the
  names on the job's partitions with `shk resources --partition P` first.
- A shorter time limit must still fit the work or its checkpoint scheme. Slurm
  has no "only if pending" condition: a job that starts between the read and
  the dispatch still gets the shorter limit (`after` then shows it running, and
  Slurm ends it if the limit is below its elapsed time), while a partition or
  features change then fails and reads as exit 5.
- `update` writes no file: the frozen spec under `<run-dir>/.shk/` stays the
  request as submitted, Slurm's records carry the current one, and the reply
  carries the records before and after. `status --task` therefore decides
  `current.ran_on` from the spec: a job moved from one partition to a set
  shows no `ran_on`, and a job submitted to a set keeps it.

Not supported, by design: CPUs, memory, GPUs, tasks, the array size, the
script, the signal, dependencies, the requeue flag, the begin time, the comment
or token, nice, clearing a constraint or preference, a throttle of 0, an
element `N_TASK`, and the time, partition or features of a job with a record
that is not pending (a running job, or an array with a started task). The CPU
thread budget environment is fixed at submission, a change of GPUs or memory
changes what the script can use, and a change of several fields can apply
partially. For these, cancel the job (`shk cancel`) and submit it again with
`--resubmit REASON`.

## Controllers inside a job

On Sherlock a bounded deterministic workflow manager may run inside its
allocated Slurm job and call `shk --executor local` to submit/poll authorized
later compute stages at intervals of at least 60 seconds, with no workstation
dependency; `shk --executor local wait` is such a poll. This does not
authorize scheduling future agent or LLM sessions. Controllers own their
bounded retries, checkpointing, scientific validation and accounting.
Call the project's pinned install by its absolute path, as
[inside jobs and controllers](install.md#inside-jobs-and-controllers) shows.

Inside a Marlowe job, where every job and every attempt is billed, the
toolkit is stricter. `shk` calls are steps of the job's own work, written
`shk --site marlowe --executor local ...` and called by the absolute path of
an environment with Python 3.11 or newer: `requeue --self` before the
walltime, `allocation`, `manifest`, `status --job-id` of the job itself, and
at most a `submit` of a fixed next stage. The user's authorized bound covers
every job the script can submit and every attempt; the stage list is fixed
and finite, and no job submits its own kind again. No `shk wait` and no
status loop run inside a Marlowe job, since a job that waits is billed idle
time: stages chain with `--after-ok`, and the wait runs on the workstation or
in a login-node session. No job starts or schedules an agent or LLM session.
`requeue --self` there needs `preempt`, the one Marlowe partition whose
profile allows requeue. The Marlowe skill
(`src/sherlock_kit/data/skills/sherlock-kit-marlowe/SKILL.md`, section
"Inside a job") holds a job-script template for the walltime, and
[inside a Marlowe job](install.md#inside-a-marlowe-job) shows the commands
and which of them need `--project-dir`. One CPU-only job on Marlowe ran
`allocation`, `status --job-id`, `submit --preflight`, the template's
`requeue --self` with a second attempt, and `manifest`
([observed](validation/observed-marlowe.md#shk-inside-a-batch-job)); a
`submit` that creates a job from inside one, array elements and GPU jobs are
tested offline against Marlowe-shaped records only.

## `push`

`push` rsyncs LOCAL/ through the DTN into an absolute REMOTE under
`remote_root` (`--anywhere` lifts that) and never deletes.

- Pushed files keep their local modification times (`rsync -t`, so a re-push
  skips unchanged files instead of rewriting them); inputs that are old
  locally can therefore be purge-eligible on scratch soon after a push.
- REMOTE's parent must already exist. For a new remote root, push a staging
  directory containing `inputs/` to the root itself, for example
  `shk push staging/ /scratch/users/USER/demo` when `/scratch/users/USER`
  exists.
- Check for exit 0 before submitting a job that needs those inputs: a failed or
  timed-out push can leave a truncated file under its final name, also where a
  complete older copy was, so rerun it to exit 0 before any job reads REMOTE.
- On Marlowe the data host is `marlowe-plain`, the login-node alias: Marlowe
  has no transfer node for rsync, and large data goes through Globus, outside
  `shk`. A push whose REMOTE has no parent fails with rsync exit 11
  (`transfer_failed`): create `remote_root` once before the first push, as
  [remote root on Marlowe](install.md#remote-root-on-marlowe) shows.

## `pull`

`pull` is payload-only: the stage, and so LOCAL, holds exactly the regular
files that `REMOTE/SHA256SUMS` lists, and the directories they imply.

- `SHA256SUMS` itself is not staged and not promoted; the receipt keeps the
  sha256 of the `SHA256SUMS` text that pull read (`manifest_sha256`).
- A manifest lists files only, so an empty remote directory is not created
  locally: a validator must not require one, and must not create one.

`pull` copies exactly the listed files into a private stage beside LOCAL,
verifies them, runs the optional validator and renames the stage onto LOCAL.
With NAME the last component of LOCAL:

- the stage is `.NAME.shk-stage` (a rerun resumes it);
- `.NAME.shk-lock` serializes concurrent pulls;
- the receipt is `.NAME.shk-receipt.json`;
- `--replace` keeps the previous copy as `.NAME.shk-old-<UTC>`.

Projects can gitignore `.*.shk-*`. LOCAL's parent must exist: pull creates
LOCAL, never its parent.

The validator runs from the project root (the directory of the configuration
file) with the stage directory as its last argument, passes with exit 0 and
must treat the stage as read-only: nothing re-verifies the stage after the
validator, and whatever it writes is promoted with the data. The kit does not
enforce this obligation.

An error while the remote side enumerates REMOTE (an unreadable subdirectory,
for example) is exit 1 `remote_failed` with `find`'s message, and nothing is
promoted.

`already_present` means that the receipt beside LOCAL names this manifest;
LOCAL itself is not verified again: the pull reads `SHA256SUMS` once more,
compares its digest with the receipt, reads no file of LOCAL and runs no
validator, so that reply rests on the receipt of the earlier pull.

The `pulled` and `already_present` replies end with the constant
`payload_contract`:

```json
{"name": "payload-only", "version": 1, "verified_against": "SHA256SUMS", "manifest_in_payload": false, "empty_directories_preserved": false, "validator_must_not_write_stage": true}
```

It describes what a pull promotes, and `validator_must_not_write_stage` states
the validator's obligation.

## `manifest`

The job writes `out/SHA256SUMS` as its last step (`shk manifest out`, or the
`sha256sum` one-liner in the skill), over its output directory, never the run
directory itself; `shk pull` verifies every file against it before the copy
appears under LOCAL. `manifest` runs inside jobs and never uses the network.

## `doctor`

`doctor` is read-only and offline: it checks Python, ssh, rsync, the
configuration and both SSH masters. `--remote` also runs one read-only program
on Sherlock.

- It checks the site of the command. A `site_mismatch` is a problem (exit 1)
  with `executor` null, `masters` `{}` and no remote run; with an invalid
  configuration it checks the defaults of the site that `--site` names.
- On Marlowe, where both hosts are `marlowe-plain`, it runs one
  `ssh -O check` and reports that state for both `masters` keys. `--remote`
  runs its program through the site's Slurm environment, and the reply has no
  `site_instructions` key and no `/etc/agents` warning.

## `rules` and `install-skill`

- Rules: `src/sherlock_kit/data/rules/workstation.md` (agents on a workstation)
  and `cluster.md` (agents on Sherlock), at most 7 KiB each, each ending with
  the partition profiles generated from `data/partitions.json`. Read them
  directly with `shk rules workstation` or `shk rules cluster` when they are
  not already loaded.
- Skill: `src/sherlock_kit/data/skills/sherlock-kit/SKILL.md`, for Claude Code
  and Codex alike.
- `install-skill` copies the agent skill into a project (`.claude/skills/` and
  `.agents/skills/`) and, with `--rules`, the rules into `.claude/rules/`
  (Codex gets a hint instead); `--check` exits 1 when a copy is missing or
  stale. Use the pinned environment's `shk install-skill --project .`
  (optionally `--rules workstation` or `--rules cluster`).
- When `~/.claude/rules/sherlock-kit.md` exists, `install-skill --rules` skips
  the project copy, because Claude Code already loads that file; `--force`
  copies it anyway.
- Marlowe: `shk --site marlowe rules workstation` and
  `shk --site marlowe rules cluster` print
  `src/sherlock_kit/data/rules/marlowe-workstation.md` (agents that drive
  Marlowe from a workstation) and `marlowe-cluster.md` (agents on a Marlowe
  login node), at most 7 KiB each, each ending with the partition profiles
  generated from `data/partitions-marlowe.json`. The skill is
  `src/sherlock_kit/data/skills/sherlock-kit-marlowe/SKILL.md`.
- `shk --site marlowe install-skill --project DIR` copies that skill to
  `.claude/skills/sherlock-kit-marlowe/SKILL.md` and
  `.agents/skills/sherlock-kit-marlowe/SKILL.md` and, with `--rules`, the
  Marlowe rules to `.claude/rules/sherlock-kit-marlowe.md`; that copy is
  skipped when `~/.claude/rules/sherlock-kit-marlowe.md` exists (`--force`
  copies it anyway), and the Codex hint names `shk --site marlowe rules KIND`.
  The copies of both sites live side by side in one project.
  `install-skill --help` names the Sherlock paths.
- The Marlowe paths are provisional: unlike the Sherlock rules and skill, they
  are not contract paths yet.

Sherlock's own `/etc/agents/AGENTS.md` takes precedence over the packaged
rules; [Sherlock policy](sherlock-policy.md) records where each site rule lands.
Marlowe has no such file: its documentation is the site policy, and
[Marlowe policy](marlowe-policy.md) records where each Marlowe rules sentence
comes from.
