# Design (0.13.0)

`shk` is a thin, stateless interface between an agent and Sherlock's Slurm and
filesystems. It answers three questions with as little machinery as possible:
is this logical task already in Slurm, what is its state, and did this data
arrive intact. Everything else (authorization, scientific validity, what a job
script does) stays with the user, the site rules and the project.

No command runs `srun` or `salloc`. A run on Sherlock's `dev` partition, which
takes interactive jobs only, stays outside `shk`: the rules and the skill carry
the raw `srun` command, and a run whose call was lost is ended with `scancel`
and its exact job id, because `shk cancel` addresses only `shk` job names.

Stanford's Marlowe cluster is a second packaged site, selected per command
([Sites](#sites)). Where this guide names Sherlock alone it describes the
default site.

## Pieces

| Module | Role |
|---|---|
| `cli.py` | argparse frame, `Context` (lazy config, site, executors, clock, sleep), JSON output, exit codes |
| `errors.py` | exit codes, `ShkError(code, token, message, hint)`, `Outcome(payload, code)` |
| `config.py` | `[tool.sherlock-kit]` discovery and validation |
| `sites.py` | the packaged site records (Sherlock, Marlowe) and the helpers that add a site to replies, hints and `next` |
| `executor.py` | `SshExecutor` and `LocalExecutor`, the master preflight, detection, `run_program` |
| `partitions.py` | the packaged partition table of each site and the generated rules block |
| `rates.py` | the packaged rate table of a site with a recharge (checked once on load, `firm_until`), the split of a usage range into segments, the site-local date, and cost and rounding |
| `doctor.py` | read-only checks |
| `spec.py`, `cpu_threads.py`, `slurm.py`, `details.py`, `node_resources.py`, `control.py` | resources (one partition or a compatible partition list), job name, sbatch argv and its optional thread-budget `env` prefix; `submit` (with the `--preflight` estimate), `status`, `cancel`, `occupancy`, `resources` (per node or `--summary`), `release`, `requeue`, `update` |
| `wait.py` | `wait`: a bounded loop over the `status` read, with the outcome decision and the backoff |
| `usage.py` | `usage`: the one `usage.sh` read, its record parsing and association check, the sharing of the caller's runs among segments, and the priced reply |
| `allocation.py` | local worker PID and allocation/step evidence, strict field parsing, actual resources and GPU SKU candidates of the node records |
| `transfer.py` | `push`, payload-only `pull`, `manifest` |
| `install.py` | `rules`, `install-skill` |
| `data/remote/*.sh` | the fixed POSIX `sh` programs run on the login node, and `site-env.sh`, the prelude for a site whose Slurm clients are not on the login `PATH` |
| `data/partitions.json`, `data/partitions-marlowe.json` | the partition table of each site |
| `data/rates-marlowe.json` | Marlowe's rate table, read by `rates.py` |
| `data/rules/*.md`, `data/skills/*/SKILL.md` | agent content of each site, delivered by `install-skill` or read with `shk rules` |

`bin/shk` is a stdlib-only entry point: it follows symlinks to the repository,
puts its `src/` first on `sys.path`, disables bytecode to keep the source checkout
clean and refuses Python older than 3.11. The version is the literal
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
  script as submitted and its frozen spec) as provenance; a submission whose
  `sbatch` printed no strict job id leaves its staged
  `.shk/<name>.<stamp>.sh`, `.json` and `.err` there instead, one set per such
  submission, and each `.json` under `.shk/`, staged or renamed, counts toward
  the name's `spec_files` in `status`. On scratch the directory is subject to
  the 90-day purge like everything else there (Marlowe's scratch "may be
  purged", with no period stated).
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
or on an `sh<N>-` compute node inside a job; else ssh. For Sherlock it never
probes system configuration and never infers Sherlock from `sbatch` being on
`PATH`. For Marlowe, whose host names do not identify it, the local test is
whether its Slurm configuration file exists ([on the cluster](#on-the-cluster)).

Every ssh dial is preceded by `ssh -O check -oBatchMode=yes HOST`, which only
asks the local control socket:

- no master: exit 4 `auth_required`, hint `ssh sherlock-plain` (or
  `ssh -fN sherlock-dtn` for the data host). Only the `-O check` ran;
- no usable `ControlPath`: exit 4 `unavailable` with a `~/.ssh/config` hint,
  because logging in again would not help;
- no `ssh`: exit 4 `unavailable`, hint `install OpenSSH`.
- local socket or process permission denial: exit 4 `unavailable`, state
  `inaccessible`; check local permissions and, in Codex, request per-command
  approval. This does not prove that the master is absent;
- a timed-out or otherwise failed check: exit 4 `unavailable`, state `timeout`
  or `unavailable`; inspect the failure before requesting a new login.

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
hint `run shk doctor`. The Sherlock login banner leaves the parsed output and
goes to stderr once per process, since site rule 12 forbids filtering out
instruction reminders. A timeout or
interrupt kills the child's whole process group. The local executor runs the
same programs with `sh -c` and no preflight; the data executor on any Sherlock
host refuses (exit 3), because transfers start outside Sherlock and the site
forbids ssh to a DTN from Sherlock.

## Sites

`sites.py` holds one frozen record per cluster, `SHERLOCK` and `MARLOWE`, as
typed literals: no JSON and no loader. A record carries stable facts only,
never a limit, duration, price or node shape, because the sites change those
with demand:

- the default control and data host aliases: `sherlock-plain` and
  `sherlock-dtn`; `marlowe-plain` for both, since rsync goes through the
  Marlowe login nodes;
- the Slurm client environment: none for Sherlock, whose clients are on the
  login `PATH`; for Marlowe the bin directory to append to `PATH` and the
  `SLURM_CONF` path;
- the account rule: none for Sherlock; for Marlowe the pattern
  `marlowe-[a-z]+[0-9]+` that a configured account must fully match (base
  project accounts only);
- the on-cluster test, the site instructions file that `doctor` reads
  (Sherlock's `/etc/agents/AGENTS.md`, none on Marlowe), whether `submit`
  tests a request before staging, the partition table file, the rate table
  file (none for Sherlock), the rules files by kind, the skill name, and the
  note a preflight estimate carries (Marlowe only).

Functions without a `Context` (partition lookups, spec helpers) take the
site as a parameter that defaults to Sherlock, so the Sherlock paths and their
tests are those of a kit without sites.

### Selection

The site of a command is `--site`, else the found configuration's `site`,
else Sherlock (`Context.site`); `rules` and `install-skill` use `--site` alone
(`Context.flag_site`) and read no configuration. One configuration names one
site: a project on both clusters keeps two configurations in two directories,
and distinct `project` values give the clusters distinct job names, so a
command aimed at the wrong cluster finds nothing instead of the other
cluster's job. A `--site` that differs from the found configuration is
`site_mismatch`, not an override: the configuration's project, remote root
and hosts belong to its site, and a Marlowe command run from the root of a
two-cluster repository would otherwise act with the Sherlock table. The
Marlowe rules therefore write `shk --site marlowe` on every Marlowe command.

No environment variable selects the site, so nothing ambient can redirect a
command to another cluster. An inherited variable would follow a shell into
every later command and into jobs (`--export=ALL` carries the submitting
shell's environment), while a flag stays visible in the command line, in
hints and in the exit 5 `next`. `SHK_EXECUTOR` chooses the executor, never
the site.

A configuration cannot set what belongs to the site: partitions, QOS,
account patterns, Slurm paths, the on-cluster test, rules and skill. Its
`control_host` and `data_host` must not be a default alias of another
packaged site, because consumers' Sherlock tables name both hosts explicitly
and a copied table would talk to Sherlock. The rule that the two hosts differ
applies only where the site's defaults differ. A pinned release without site
support reads a Marlowe configuration as `config_invalid` (unknown keys), so
it stops before any remote call.

### Remote environment

On Marlowe the Slurm clients are not on the `PATH` of the non-login `sh` that
ssh runs, and `module` is not defined there; the clients work once their bin
directory is on `PATH` and `SLURM_CONF` is set
([observed](validation/observed-marlowe.md#slurm-client-environment)). For a
site with a Slurm client environment, `run_program` puts the fixed prelude
`data/remote/site-env.sh` in front of the program text:

```sh
PATH=$PATH:$1
SLURM_CONF=$2
export PATH SLURM_CONF
shift 2
```

and runs `["sh", "-c", PRELUDE + PROGRAM, "shk-NAME", BIN, CONF, *ARGS]`,
where Sherlock keeps `["sh", "-c", PROGRAM, "shk-NAME", *ARGS]`. The shift
leaves exactly ARGS as the program's positional parameters, and every
program's scrub of `SLURM_*` keeps `SLURM_CONF`. The bin directory is
appended, never prepended, so earlier `PATH` entries keep winning: the test
suite's blocking stubs stay ahead of every client, and should the site put
Slurm on the default `PATH`, the prelude changes nothing. The prelude serves
both executors and `doctor --remote`, and is linted and formatted with the
other programs.

### On the cluster

`detect()` asks the selected site's `on_cluster(hostname, env)` whether this
machine belongs to that site: the test chooses the executor, never the site.
Sherlock's test is the host name and environment check above. Marlowe's host
names (`login-0N`, `nNN`, the `cm.cluster` domain) and its Slurm ClusterName
(`slurm`) are vendor defaults that do not identify it, so its test is whether
its Slurm configuration file exists on the machine. That path is a vendor
default too, so the test would also hold on another cluster managed the same
way, which is why it never selects the site. On a Marlowe login or compute
node, inside a job too, a Marlowe command (by `--site` or by configuration)
runs locally by itself, while a command with neither stays a Sherlock command.
A forced local executor (`--executor local`, `SHK_EXECUTOR=local`) where the
selected site's test fails and another site's holds is exit 1
`site_mismatch`, whose hint names `--site` of that site: the Slurm of this
machine would otherwise answer for the selected site. `requeue --self` and
`allocation` work only with the local executor; with another one they run
that forced-local check before they refuse, so on a machine of another
packaged site they answer `site_mismatch` with that hint, and only elsewhere,
on a workstation, exit 3 `self_only` or `local_only`. Inside a Marlowe job
every retry is billed time, so the first answer names the missing flag.
Where the test holds,
the data executor is local and refuses push and pull; on Marlowe that is
`transfer_on_marlowe`, a toolkit choice: transfers start outside Marlowe.

### Partitions, accounts and the test before submit

Each site names its own partition table (`partitions.json`,
`partitions-marlowe.json`, one schema); the tables never merge, and every
lookup, anomaly and update check uses the selected site's. Marlowe's lists
`preempt` alone, the only partition that the QOS of a base project account
reaches; `batch` and `hero` need suffixed accounts and other QOS. The
generated rules block prints its borrowed-partition sentence only for a table
with a borrowed profile.

Marlowe's submit plugin refuses a job without an explicit account, even for a
user with a default account, so `submit` takes the account from the
configuration, puts `--account=` directly after `--partition=` and never
guesses it (`$PROJECT_ID` is not read). The frozen spec gains no key, and the
`--export` value is Sherlock's.

On Marlowe every deterministic refusal (account, QOS, the shared submit
limit, the time limit, features, `--tmp`, typed GRES) comes at test time
([observed](validation/observed-marlowe.md#submission-verdicts)). Without a
test each would surface as a failed `sbatch`, which `shk` never reads as a
refusal: exit 5, then `stale`, then a resubmit. So for a site whose record
sets `test_before_submit`, `submit` calls `control.preflight` after the read
and the duplicate guards and before staging, and returns its reply unchanged
when Slurm refuses: exit 3 with nothing staged. The test also stands in for
any packaged array or time cap. It costs one round trip per submit, and it can
refuse while no node is schedulable, since a full drain gives the same
"Requested node configuration is not available".

### Replies and hints

A reply from a site other than Sherlock carries `"site": NAME` after
`executor`, from one helper (`sites.reply_fields`), and the `shk` commands of
its hints and exit 5 `next` carry `--site NAME` (`sites.selector`,
`sites.command`). Error replies carry no `site`, except the errors of
`status --details`, which keep the read's `executor` and `site`. Sherlock adds
nothing, so its replies, hints and `next` are those of a kit without sites.

### Inside a Marlowe job

A job's working directory, its run directory on scratch, holds no
configuration, and no environment variable selects a site, so a command
inside a Marlowe job names its site like every other Marlowe command:
`shk --site marlowe --executor local ...`, called by the absolute path of an
environment with Python 3.11 or newer. On a compute node the executor is
local by itself, since the Slurm configuration file exists there too; the
explicit `--executor local` makes a command on the wrong machine fail instead
of dialing. `requeue --self`, `allocation`, `status --job-id`,
`status --name` and `manifest` need no configuration; `submit`,
`status --task` and `cancel --task` need `--project-dir` naming a Marlowe
configuration on Marlowe. On either site a job carries `SHK_NAME`, the
submission token and the thread defaults of a CPU thread budget
([CPU thread budgets](cpu-threads.md)); a Marlowe job also carries the Slurm
`PATH` entry and `SLURM_CONF` that `sbatch` ran with, since `--export=ALL`
passes the prelude's environment on
([remote environment](#remote-environment)). None of them names the site. In
one CPU-only job on Marlowe the automatic executor was local, and
`allocation`, `status --job-id`, `submit --preflight`, `requeue --self` with
its second attempt and `manifest` ran as described
([observed](validation/observed-marlowe.md#shk-inside-a-batch-job)); a
`submit` that creates a job from inside one, array elements and GPU jobs are
tested offline against Marlowe-shaped records only.

On Sherlock a controller job may submit and poll its later stages. On
Marlowe every job and every attempt is billed, CPU-only ones included, and a
job that waits is billed idle time, so the toolkit is stricter there: `shk`
calls inside a job are steps of the job's own work (`requeue --self` before
the walltime, `allocation`, `manifest`, `status --job-id` of itself, and at
most a `submit` of a fixed next stage); the authorized bound covers every job
the script can submit and every attempt; and no job waits or polls: stages
chain with `--after-ok`, and the wait runs on the workstation or in a
login-node session. The site's documentation states no rule on such jobs;
this is the stricter reading of the owner's rule that CPU-only work which
neither prepares nor checks a Marlowe GPU job runs off Marlowe.

The Marlowe skill carries one job-script template, for the walltime:
`--signal=B:USR1@T` before the time limit, the workload stopped with a
checkpoint, then `requeue --self`. Its restart bound is on
`SLURM_RESTART_COUNT`, which by Slurm's source counts every restart,
preemptions and self-requeues alike (unset, then 1 after one self-requeue, is
[observed](validation/observed-marlowe.md#shk-inside-a-batch-job)): the user's
authorization counts attempts of every kind (1 + retries), and a count of
self-requeues alone would let preemptions add attempts without limit. The
guard stops the work at the authorized bound; an attempt past it still starts
and is billed for the seconds before the guard. The template sets no TERM
trap: periodic checkpoints are what survive a preemption, a time limit or a
cancel, and a trap that forwards SIGTERM to the workload would rest on a
signal sequence not observed on Marlowe. It covers one workload that the batch
shell starts directly. Through `srun`, the SIGTERM that should stop the
workload becomes SIGKILL of its tasks, and a preemption ends the step at the
start of its grace window, so the script exits by itself inside the window,
which by Slurm's source counts toward the hold that MaxBatchRequeue sets
([source-confirmed](validation/observed-marlowe.md#signals-and-requeues-in-slurms-source)).

### Deferred

Marlowe support covers commands from a workstation, from a login node and
inside a job. Left out on purpose:

- a job template that reacts to a preemption signal. By Slurm's source (not
  observed on Marlowe) the batch shell gets no signal at the start of a
  preemption's grace window and `--signal` does not fire there, so its only
  notice is the SIGTERM at the end of the window, and the signal sequence of
  a real preemption is not observed
  ([not observed](validation/observed-marlowe.md#not-observed)). Such a
  template waits for a consumer that has seen one;
- an export of the site or the executor into jobs, and selection of the site
  by an environment variable (`SHK_SITE` or another): every Marlowe command
  writes `--site marlowe`, inside a job too, so none needs one; an exported
  value would read as a selector that `shk` does not honor, and it can be
  added later without breaking a script. Also a configuration layout with
  several sites;
- partitions `batch` and `hero`, suffixed accounts and `--qos`;
- detection of Marlowe by host name;
- a `site` key in the frozen spec or in error replies;
- a requeue-hold anomaly token;
- dollar figures, numeric caps, and a per-site array cap or accounting
  window;
- a `marlowe-dtn` alias, Globus integration and size warnings;
- a neutral rewording of the Sherlock texts, and a separate distribution.

## Job names

```text
shk.<project>.<slug(task)[:20]>.<sha256(canonical([project, campaign, task]))[:10]>
```

At most 60 characters of `[a-z0-9.-]`. Retries reuse the name; the newest base
job id under a name is the current job and older ones are history. The `.`
separator makes `shk.<project>.` an unambiguous prefix for `status --all`.

## Submit

1. **Offline checks.** The partition is listed in `partitions.json` and takes
   batch jobs (`batch: false` for `dev`, where Sherlock rejects every sbatch
   job; `occupancy` and `resources` still read it); GPUs only
   where the profile allows them; requeue only where the profile allows it,
   alone or in a set (Sherlock's `owners`, Marlowe's `preempt`), else
   `--no-requeue`. A
   `--partition` value with a comma is a partition list, Slurm's own syntax
   (`gpu,bioe,stat`), on `submit` and on `update` (for a pending job) only:
   `resources` and `occupancy` take one name per flag and refuse a comma as
   exit 3 `unknown_partition`, and a repeated `--partition` flag is not a list
   (argparse keeps the last one). A value without a comma takes the
   single-partition path. The members of a list are checked where the
   single partition's profile is looked up and `batch` tested, before the
   resource checks, in this order: an unlisted member, the empty one included
   (exit 3 `unknown_partition`, the usual message for the first such member);
   a repeated member (exit 1 `spec_invalid`); a member without batch jobs
   (exit 3 `batch_not_allowed`, the usual message for the first such member);
   a borrowed member (exit 3 `partitions_incompatible`). Compatibility is
   derived from the existing profile fields alone: no member `borrowed`. The
   message names every borrowed member, and the hint says to submit
   incompatible partitions separately. The combined profile is named by the
   value as given and has `preemptible` and `requeue` when any member has
   them, `gpus_allowed` only when every member has it, `borrowed` false,
   `batch` true and an empty courtesy text. The resource checks then apply to
   the set: `--gpus` above 0 needs `gpus_allowed` on every member
   (`gpus_not_allowed` names every member without GPUs), requeue defaults to
   on when a member allows it (`--requeue --open-mode=append`; `--no-requeue`
   is accepted), and `--requeue` on a set without such a member is
   `requeue_not_allowed`, naming the set as given. So `owners`, `gpu`, `bioe`
   and `stat` combine, `normal`, `bigmem` and `service` combine too, and
   `btrippe` and `possu` stay alone. A set states that the job may run on any
   member, so it follows every member's rules and courtesy text wherever it
   starts; a set with `owners` is preemptible and requeues by default. A list
   is explicit on the command line only (`submit`, or `update` for a pending
   job), never a TOML key. The other checks: the `--mem`, `--time` and `--array`
   grammar, at least one CPU and task; a script of at most 1 MiB whose leading
   `#SBATCH` lines do not set a directive `shk` owns (`-A`/`--account`,
   `-x`/`--exclude`, `-G`/`--gpus*`/`--gres`, `-p`/`--partition`,
   `-a`/`--array`, `-d`/`--dependency`, `-b`/`--begin`, `-J`/`--job-name`,
   `-H`/`--hold`, and `--comment` when a submission token sets Comment) or
   start a heterogeneous job (`hetjob`, `packjob`); a borrowed partition needs
   `--courtesy-ack`. The denylist follows sbatch's getopt parsing: abbreviated
   long options (`--part`) and short flags bundled after value-less ones
   (`-Hp`) count. `--constraint`, `--prefer` and `--signal` go to sbatch
   unchanged, which checks them itself; one that is given but empty or
   whitespace-only is refused here (exit 1 `spec_invalid`, for example
   `--constraint must not be empty`), while an omitted one leaves the frozen
   spec and argv as they are. Each `--after-ok` value (repeatable) must be one
   job id, `N` or `N_TASK`, because `shk` joins them into its own `--dependency`
   string, where a `,` or `?` would add clauses. Build the sbatch argv
   (explicit name, partition, CPUs, memory, minutes, run directory, log paths,
   `--export=ALL,SHK_NAME=<name>`, `singleton` for single jobs; a submission
   token adds `,SHK_SUBMISSION_TOKEN=<token>` to the export and
   `--comment=<token>`, and `--hold` is accepted only with a token) and the
   frozen spec. A partition list reaches the argv as
   `--partition=<value as given>` in the same position, and the argv is
   otherwise that of a single partition; the job name does not depend on the
   partition. The frozen
   spec keeps the value as given in `resources.partition` and adds the list as
   `resources.partitions`, only for more than one member; in the `spec` that
   `--dry-run` and `--preflight` print it is the last key of `resources` (after
   `prefer` when both are present), and the staged file has sorted keys. A
   single partition leaves the frozen spec and argv as they are. An explicit `--prefer` is also recorded in `resources.prefer`;
   omitting it leaves the resource document and argv unchanged. Slurm tries
   preferred features independently before the fallback constraint; callers must
   include every hard requirement in both. With `--cpu-thread-budget` or the
   project's `cpu_thread_budget`, `cpu_threads.py` puts an
   `env NAME=VALUE ...` prefix in front of the sbatch argv and records
   `cpu_thread_policy` in the spec; without either, both are unchanged (see
   [CPU thread budgets](cpu-threads.md)). `--dry-run` stops here, with no
   network at all.

2. **Read call** (`query.sh`, shared with `status`): one `squeue -t all` and one
   `sacct --duplicates` over the last 30 days for the user and name, plus the
   newest spec file; for a borrowed partition also its occupancy (never for a
   partition list, which has no borrowed member). The read, the live and
   duplicate guards, `singleton`, the staging and the exit 5 handling are the
   same for a list as for one partition. Only
   `SHK_`-prefixed lines count, and the read is complete only with `SHK_END`
   and every tool returning 0 (else exit 1 `malformed_read`). Every remote
   program that calls a Slurm client first unsets `SBATCH_*`, `SQUEUE_*`,
   `SACCT_*` and `SLURM_*` (except `SLURM_CONF`); `doctor.sh` and
   `manifest.sh` call none. A reply of any remote program is a sequence of
   records separated by LF only: the `SHK_SPEC` payload, the one file content
   embedded inside a single record, has its LF bytes flattened to spaces on the
   remote side, and every local parser splits a reply on LF alone, never with
   `str.splitlines()`, so no other separator inside a field (CR, VT, FF, FS,
   GS, RS or a Unicode line separator) can start a record or forge a marker.

3. **Local decision** (pure): `new` (no rows) submits; `live` (any task pending,
   running, suspended or completing, including throttle-held array tasks that
   `squeue` reports only as an aggregate) is refused even with `--resubmit`;
   `done` and `failed` are refused unless `--resubmit REASON`; `stale` (spec files
   but no Slurm rows) is informational and treated as `new`. On a borrowed
   partition (`btrippe`, `possu`) the reply carries `occupancy`, the
   partition's `courtesy` text, `request = {tasks, throttle, minutes}` (`tasks`
   is 1 for a single job and the array count otherwise, `throttle` is the `%M`
   limit or null) and `warnings`. The budget warning appears when others are
   running there and you already have 2 jobs there or ask for more than 2 h.
   It quotes "about 2 jobs x 2 h" as the courtesy budget only when the
   partition's own courtesy text contains that phrase; otherwise it names the
   advisory threshold and points at the `courtesy` text in the same reply. A
   total-work advisory appears when others are running there and `tasks` is
   greater than 2: it states the number of array tasks, the throttle (or that
   there is none) and the minutes, and does not call the request within or
   over any budget. Both are advisory and nothing is refused. The courtesy
   terms are the profile texts alone: `btrippe` has no blanket daytime
   prohibition, and `possu` keeps its 00:00-07:00 Pacific window.

4. **Mutation call** (`submit.sh`, the script on stdin): a live guard, staging of
   the script and spec as `<run-dir>/.shk/NAME.STAMP.sh` and `.json`, a digest
   check, `sbatch` on the staged script with its stderr in `NAME.STAMP.err`,
   then exactly one terminal marker:

   - `SHK_LIVE` (token `live`) or `SHK_REFUSED <why>` (token `remote_refused`):
     a guard stopped before `sbatch` ran, so nothing was dispatched; exit 3.
   - `SHK_SUBMITTED <jobid>` (exit 0, action `submitted`): the stdout of
     `sbatch` is, trailing newlines aside, one line holding a strict job id,
     `N` or `N;CLUSTER`, where N is a positive decimal integer without a
     leading zero and CLUSTER is a non-empty run of letters, digits,
     underscore, dot or dash. `submit.sh` sets `LC_ALL=C` once `sbatch` has
     returned, so both are matched as ASCII bytes whatever the login locale
     (digits or letters of another script are never an id). The id reaches
     the client only in the controller's success response, so it is positive
     evidence of acceptance whatever exit code `sbatch` returned
     (`sbatch --wait` returns the job's exit code). The staged files are
     renamed to the job id. Nothing else is ever read as a job id.
   - `SHK_UNPARSED`, with the `sbatch` exit code and Slurm's stderr as
     evidence: every other result once `sbatch` has started, whether it exited
     0 or not. The outcome is unknown, exit 5, with the reply
     `{ok: false, action: unknown, name, detail, next}`; `detail` holds
     `sbatch exit RC` followed by the stderr text when there is any, and the
     staged `.sh`, `.json` and `.err` are all kept as evidence.

   No stderr text is classified as a definitive refusal: a nonzero `sbatch`
   can follow an accepted submission
   ([source-confirmed](validation/observed-slurm.md#not-observed-live)), so a
   remembered error string proves nothing. `shk submit --preflight`
   (`sbatch --test-only`) is the way to get a definitive Slurm verdict
   (exit 3) without creating a job. A transport failure after dispatch or a
   missing or duplicate marker is exit 5 as well. In every exit 5 case the
   reply's `next` names the `shk status` command and the time before which not
   to run it (60 s later); settle the outcome with that status read before
   considering another submit. A name whose submission never landed then reads
   `stale` (spec files, no Slurm rows), which submits like `new`, when it has
   no earlier job in the 30-day window that submit reads. When it has one, status still reads
   that job's `done` or `failed` state and `current.job_id`, with `spec_files`
   one higher, and the name still needs `--resubmit`. The attempt landed only
   if `current.job_id` is newer than the one seen before the submit. A
   deliberate rerun must follow the duplicate and resubmission guards above;
   the check-then-submit race described below prevents an exactly-once
   guarantee.

## Status, wait, cancel and occupancy

`status` batches every selected name into one read call. Per name it reports the
state, the current job (partition, array size, task counts by state, exit codes,
restarts, CPU and GPU seconds) and the history, plus `queried_at` and
`next_check_after` (now + 60 s). Anomalies are evaluated on the current job only:
`unexpected_preemption` (a `PREEMPTED` attempt on a listed non-preemptible
partition, or on a comma list whose members are all such; a `NODE_FAIL` or
`REQUEUED` attempt and a restart whose earlier attempt is not visible are not
anomalies, and show in `max_restart`, `tasks.by_state` and
`tasks.incomplete_history`), `duplicate_live`,
`unknown_partition` (some row of the job has a partition text, split on `,`,
with a member that is not listed; the verdict does not depend on the row
order), `malformed_rows`; any of them
gives exit 6. `duplicate_live` and `unknown_partition` apply to `shk.` names only,
and `--job-id` entries are per base job. Nothing is acknowledged or stored,
so nothing needs closing: a deliberate `--resubmit` makes a new current job.

Status reports what Slurm prints. `current.partition` is the partition text of
the first row Slurm printed for the job, queue rows before accounting rows, in
Slurm's order, which `shk` does not fix. The rows of a started job do not show
that it was submitted to a list: clients get the run partition for a job that
is not pending and the list only while it pends, and a job that
ended without starting shows one member in `squeue` and keeps the list in
`sacct` ([source-confirmed](validation/observed-slurm.md#jobs-submitted-to-several-partitions)).
For an array, or a requeued job read from accounting only, the text is one
row's, can change between reads and says nothing about the other tasks or
attempts. Whether a job used a list is therefore decided by its frozen spec:
`current.ran_on`, directly after `partition`, appears only when status read the
spec of that job (the newest spec file of the name, for that job id, as for the
task count) and its `resources.partitions` lists several partitions, that is
`status --task` with a run directory, plain or with `--details`. Its value is
the sorted distinct partitions of the job's started rows, `[]` while no row
shows a start. A started row is an explicit row (not an `N_[...]` aggregate) whose
partition text is one name and which shows a start: an accounting row that is
not `PENDING` and has AllocCPUS above 0, or a queue row whose state is in
`slurm.RUNNING`
(`RUNNING`, `COMPLETING`, `SUSPENDED`, `CONFIGURING`, `STAGE_OUT`,
`SIGNALING`, `RESIZING`, `STOPPED`). Without a spec (`--all`, `--name`,
`--job-id`) there is no `ran_on`: `current.partition` is the first row's text,
which for an array whose tasks started on different members names only one of
them or the pending list. The per-row partition of `status --details`, or
`--task`, tells where each ran. The cost arithmetic is the same for a list, and
every detail row keeps its partition text as Slurm printed it.

`wait` is a bounded loop over that read, not a second reader: each iteration
is the read that `status` makes for the same selection, and the last read's
status reply, with a `wait` object added, is its one reply
([command reference](cli.md#wait)). It exists so that an agent can wait for a
job without spending a turn on each check: the harness runs the wait in the
background and reports its exit, and the agent acts once, on the outcome. It
keeps no state beyond one run: in memory it holds the names it saw live, since
when each live name has shown no running task, the previous read's state,
current job id and task counts per name, and the current delay; nothing is
written. The loop ends by itself, at the first of an anomaly (exit 6), a
failure of a name it saw live, no live name, the optional `--max-pending`
bound and the required `--timeout` of at most 168 hours. Its reads are at
least 60 s apart by construction, and its first read comes at least 60 s
after the reply before it: it sleeps 60 s before that read, the delay starts
at `--interval` (5 minutes by default, at least 1 minute) and doubles, up to
30 minutes or the interval, while nothing runs and nothing changes, a sleep
cut short so that the next read falls on a `--max-pending` bound is still at
least 60 s, and the wait ends before a sleep shorter than 60 s. The decision
order wakes the agent as soon as it is
needed: an anomaly first, then a failure, which ends the wait even while other
names are still live, then the settled case and the two bounds. A read error
ends the wait with that error and exit code, never retried, as for
`status`. The clock and the sleep come from `Context`, so tests drive the loop
without waiting.

`cancel` resolves one name and cancels only its live jobs (`scancel` scoped by
user and name), reporting the job ids and `next_check_after`. A guard that
stops before `scancel` runs is exit 3; any nonzero `scancel` exit is outcome
unknown, exit 5 with `scancel exit RC` and the stderr text in `detail`, and so
is a lost reply, each with a `next` like submit's. `occupancy` reads the
partition's current use with the fixed-width `squeue -O` layout
([verified live with 0.2.0](validation/observed-slurm.md#occupancy)).

## Controller primitives

Every operational JSON document, errors included, carries `schema_version: 1`;
text output (help, `rules`, `--version` and usage errors) carries none.
`details.py` keeps full accounting identities (Cluster, DBIndex, physical
JobIDRaw, Start/End and Restarts) and keeps the raw rows alongside the summaries
derived from them. `status --job-id` selects current
UID jobs including legacy names and reports one entry per base job; full queued
`scontrol` request facts (read once per base job, times in UTC) support held job
verification. A failed queue or accounting read is exit 1; a failed control read
only marks the evidence incomplete. Partial evidence is explicit and cannot claim
final cost.

`node_resources.py` subtracts Slurm allocations from configured capacity; OS
FreeMem does not drive admission. `resources.sh` keeps only nodes whose
`Partitions=` list names a wanted partition, `schedulable` comes from the State
flags while `free_*` stays configured minus allocated, and a failed `scontrol`
read is exit 1. `resources --summary` is a separate output mode of that same
single read: the reply replaces `nodes` with `summary`, and without
`--summary` it is the per-node reply. The summary puts every shipped node in
exactly one class (countable: all facts known and schedulable; unschedulable: all
facts known and not schedulable; unknown: any fact missing, a malformed line
included) and sums groups for the union, for each requested partition and for
the `--by sku` and `--by class` breakdowns of the `GPU_SKU:` and `CLASS:`
features, with optional `--shape` slot counts
([command reference](cli.md#resource-summary)). Unknown stays unknown: a sum with
nothing known behind it is null, never 0. The reply carries its rules as four
fixed texts and ranks nothing. `--by` and `--shape` need `--summary`; they and
the shapes are checked offline, after the partitions and before any remote
call. `control.py` implements read-only `sbatch --test-only` (a
controller transport failure is a retryable exit 1, not a refusal), exact
identity-verified user-hold release and local allocation-only self requeue: PID
membership through `listpids` and `show job` both use the physical
`SLURM_JOB_ID`, the job's own record is kept out of any sibling records, and an
array element is requeued as that record's `ArrayJobId_ArrayTaskId`. The run
partition must be a listed, non-borrowed partition of the site (else refused
`partition`) and the record must show Requeue=1 (else refused `policy`,
wherever the job runs), which `submit` sets by default only for a
`requeue=yes` partition, alone or in a set. `control.py` also implements
`update`, one field group per call (`--time`, `--partition`, `--throttle`, or
`--constraint` and `--prefer` together): offline checks of the value, then
`update.sh`, a fixed program beside `control.sh`, which reads every record of
the job once (`show job N`: one record, or the meta record and every split
record of an array), checks name, UID and the optional token on every record
and the field's own rules, prints `SHK_UNCHANGED` when every record already
shows the value, and otherwise dispatches one `scontrol update` and re-reads
every record. The outcome is the re-read's: `updated` only when every record
shows the requested value, whatever `scontrol` returned, and exit 5 otherwise
([command reference](cli.md#update)). After a mutation is
issued an ambiguous reply stays unknown, with no retry. Slurm identity reads
and mutations are not atomic; no exactly-once guarantee is claimed. The public
submission token is correlation data stored in Slurm Comment, the job's
`SHK_SUBMISSION_TOKEN` and the frozen spec, not a credential; a held job needs
one, because release verifies it. No project ledger, checkpoint or retry
policy enters the kit.

The `--preflight` reply adds `estimate` directly after `preflight`; the
`preflight` object, the exit codes (0 accepted, 3 rejected by Slurm, 1
`malformed_read`) and `preflight.sh` are those of a plain preflight.
`control.py` splits `stderr`, then `stdout`, on LF only, and an estimate line
is a line that matches this pattern from its first character to its last:

```text
(?:sbatch: )?Job [0-9]+ to start at (?P<start>\S+)(?: on cluster \S+)?(?: a)? using [0-9]+ processors on nodes (?P<nodes>\S+) in partition (?P<partition>\S+)
```

`status` is `parsed` when there is exactly one estimate line and its `start`
also matches `[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}`; `start`,
`nodes`, `partition` and `line` are then set, and lines that merely resemble an
estimate do not change this. `absent` means no estimate line and no line
holding the words `to start at` between spaces, with those four null; it
covers an accepted call without an estimate and a rejected call. `unparsed`
is every other case (several estimate lines, an estimate line whose time is
not an ISO time, or no estimate line while some line holds the words, as for
an unknown shape or an empty node list): `start`, `nodes` and `partition` are
null and `line` is the first estimate line, else the first line holding the
words. `start` is the text Slurm printed, the local time of the `sbatch`
process on the executor host without a zone, which the constant
`start_zone: "sbatch-local"` names; `observed_at` is UTC and equals
`queried_at`. The kit converts nothing and guesses nothing, and no key of
`estimate` holds the job id of the line, which belongs to a temporary record
([source-confirmed](validation/observed-slurm.md#test-only-estimates)).

## Transfer

`push` checks the DTN master, then runs `rsync -rlt --protect-args --partial`
into an absolute REMOTE under `remote_root` (unless `--anywhere`) and never
deletes. `-t` keeps the local modification times: a re-push compares size and
time and skips unchanged files, where rewriting them would refresh their purge
clock on scratch, which looks like purge evasion. The cost is stated plainly:
pushed files keep their local modification times, so inputs that are old
locally can be purge-eligible soon after a push. With plain `--partial`, a
push that fails or times out can leave a truncated file under its final name,
also where a complete older copy was: rerun the push to exit 0 before any job
reads REMOTE.

`pull` reads `REMOTE/SHA256SUMS` (GNU `sha256sum` format) through the control
host and needs it to list every regular file under REMOTE except `SHA256SUMS`
and `.shk*` entries, so the job writes it over its own output subdirectory, not
the run directory with its `.shk/` and logs. `manifest.sh` counts those files
with one `find` and keeps its exit status: an enumeration error (an unreadable
subdirectory, for example) makes the program exit 1, which `pull` reports as
exit 1 `remote_failed` with `find`'s message, and nothing is promoted. `pull`
refuses unsafe manifest entries
(absolute paths, empty, `.` or `..` components, `.shk` components, duplicates).
With NAME the last component of LOCAL, it takes a lock on `.NAME.shk-lock`,
rsyncs exactly the listed files into the private stage `.NAME.shk-stage`
(created 0700) beside LOCAL, pruning anything the manifest does not list so a
rerun resumes it, and uses receiver-local `--chmod=Du+rwx` to keep incoming
directories writable when a readonly source spans multiple file-list batches.
Before pruning it gives owner rwx back only to real stage directories that lack
it (left by an interrupted pull of a readonly source); an ordinary pull issues
no chmod, so filesystems that refuse chmod (CIFS) work, and a needed repair that
fails is exit 1 `stage_unwritable`. Because the stage is promoted onto LOCAL, a
stage that already exists and has a directory another user owns (planted in a
shared parent) is refused with exit 3 `stage_untrusted`; a stage shk has just
created is not checked, so on an id-mapped filesystem that shows another owner a
refused resume works again once the stage is removed. File permissions are unchanged, a symlinked
stage root is refused and a child symlink target is never followed or
chmodded. It verifies the inventory and every hash, re-reads the manifest, then
runs the validator named by `--validate`, if any: its argv from
`[tool.sherlock-kit.validators]` plus the stage directory as the last argument,
from the project root (the directory of the configuration file); exit 0 passes,
and the validator treats the stage as read-only because whatever it writes is
promoted. Validators are commands, so they run only from a configuration file the
current user owns (otherwise exit 3 `validator_untrusted`); discovery itself walks up
freely so group-owned project trees on Sherlock keep working. Then it renames the stage onto LOCAL and writes
`.NAME.shk-receipt.json`. An existing LOCAL with a receipt for the same manifest
is `already_present`: the receipt beside LOCAL names this manifest, and LOCAL
itself is not verified again. Any other existing LOCAL is kept unless
`--replace`, which renames it to `.NAME.shk-old-<UTC>` (never deletes). `manifest DIR`
writes `DIR/SHA256SUMS` inside jobs; the skill's `sha256sum` one-liner writes
the same.

The pull contract is payload-only:

- The stage, and so LOCAL, holds exactly the regular files that
  `REMOTE/SHA256SUMS` lists, and the directories they imply. `SHA256SUMS`
  itself is not staged and not promoted; the receipt keeps the sha256 of the
  `SHA256SUMS` text that pull read (`manifest_sha256`).
- A manifest lists files only, so an empty remote directory is not created
  locally. A validator must not require one, and must not create one.
- The validator gets the stage as its last argument and treats it as
  read-only: nothing re-verifies the stage after the validator, and whatever
  it writes is promoted with the data. The kit does not enforce this.
- The `pulled` and `already_present` replies end with the constant
  `payload_contract`:
  `{"name": "payload-only", "version": 1, "verified_against": "SHA256SUMS", "manifest_in_payload": false, "empty_directories_preserved": false, "validator_must_not_write_stage": true}`.
  It describes what a pull promotes; `validator_must_not_write_stage` states
  the validator's obligation. On `already_present` LOCAL is not verified
  again: the pull reads `SHA256SUMS` once more, compares its digest with the
  receipt, reads no file of LOCAL and runs no validator, so that reply rests on
  the receipt of the earlier pull.

## Weaker guarantees, accepted

- Deduplication uses only `squeue -t all` and `sacct` over the last 30 days
  (`status` takes `--since`, 1 to 31 days: Sherlock's slurmdbd refuses wider
  windows, and the bound holds on Marlowe too, which accepts them). Spec files are reported as `stale` but never block:
  a task with no Slurm rows in that window is submitted again as new, without
  `--resubmit`, even when its run directory still exists, and the new job runs
  in the same run directory.
- Sherlock's windowed `sacct` omits jobs that were never eligible
  ([observed live](validation/observed-slurm.md#accounting)): a job held until
  it was cancelled leaves no row for a name query, so once the controller
  purges it the name reads as `new` or `stale`.
  `sacct --jobs` still finds it. Sherlock also refuses `sacct` windows much
  wider than 30 days (`Too wide of a date range in query` at 45 days) and keeps
  no job Comment in accounting, so a submission token is visible as a Comment
  only while the job is in the controller; the frozen spec keeps it, and so does
  the control record's `SubmitLine` (the sbatch command line).
- Check-then-submit is a race: two submitters can both pass the guard. For a
  single job `singleton` keeps the two from running at once; arrays have no
  backstop; `status` flags `duplicate_live`. There is no exactly-once claim.
- `--all` lists names by prefix within a time window; there is no ledger of
  everything ever submitted.
- Cost is informational: no budget, cap or reservation.
- The 60 s cadence is advisory (`next_check_after`), enforced by the rules, not
  by a file. `shk wait` keeps it by construction within one run and toward
  the reply before it, since its first read comes 60 s after it starts, but
  nothing stops two waits running at once, or reads by hand during a wait,
  from reading the same names more often; the skills ask for at most one wait
  at a time.
- `wait` decides from its reads alone. A task that ran only between two reads
  counts as never running for `--max-pending`; with `--all`, a name that failed
  before the first read does not give `failed` (with `--task` or `--name` any
  listed failed name does); and a name that failed and was resubmitted between
  two reads reads as live.
- A master can die between the check and the dial; `-oProxyCommand=false` makes
  that dial fail instead of logging in, at most once per command.
- The remote side trusts `sh`, `sha256sum` and the Slurm clients (`squeue`,
  `sacct`, `sbatch`, `scancel`, `scontrol`) on the login shell's `PATH`; on
  Marlowe, on that `PATH` with the site's bin directory appended, and the
  configuration that `SLURM_CONF` names.
- Provenance lives in the run directory and is purged with scratch.
- Reply records are separated by LF only: the `SHK_SPEC` payload is flattened
  remotely, but an LF inside a job name or comment that `squeue`, `sacct` or
  `scontrol` prints raw is Slurm's own output and still starts a new line.
- `shk` bounds its children with its own timeouts and kills their process
  group on a timeout or interrupt, but it installs no handler for SIGTERM or
  SIGHUP. Do not wrap it in an outer timeout shorter than its own: if `shk`
  itself is killed, a mutation in flight is outcome unknown without an exit 5
  reply, and a transfer may still be running.
- `already_present` trusts the pull receipt, and a failed push can leave a
  truncated remote file (see Transfer). Nothing re-verifies the pull stage
  after the validator, and the kit does not enforce that a validator leaves it
  unwritten.
- `resources --summary` is a snapshot heuristic at `queried_at`: configured
  minus allocated capacity. It does not model fairshare, QOS or partition
  limits, reservations, preemption or pending work, reserves nothing and
  predicts no start time. Beside a nonzero `unknown_nodes` its numbers are
  lower bounds.
- The `--preflight` estimate is advisory: not a reservation and not a promise,
  and its start is the local time of the `sbatch` process, which the kit does
  not convert. For a request with several partitions Slurm names the first
  partition, in its own priority order, that can run the job, and estimates
  the start there: it is not the earliest start over the set, and the named
  partition can be the Alternate of a listed one
  ([source-confirmed](validation/observed-slurm.md#test-only-estimates)).
  Comparing partitions takes one preflight per partition.
- On Marlowe the start that `sbatch --test-only` prints is not a queue-wait
  prediction at all: an estimate more than a day out preceded a start within
  a minute ([observed](validation/observed-marlowe.md#test-only-estimates-and-real-starts)).
  The reply says so in `estimate.note`; only the verdict is usable.
- The test before a Marlowe submit and the submit are separate calls: a
  shared limit can fill in between, and an `sbatch` refused then is outcome
  unknown (exit 5) as on Sherlock. The test can also refuse a request while
  no node is schedulable.
- A Marlowe job on `preempt` can be preempted, requeued and restarted from
  the top many times; `status` counts this as no anomaly. A job that ends in
  REQUEUE_HOLD shows that state in `current.tasks.by_state`, with no anomaly
  token of its own.
- The partition text of a job submitted with a partition list is what Slurm
  prints: the list while it pends, in an order Slurm may change, and the run
  partition once it has started; one member in `squeue` and the list in
  `sacct` for a job that ended without starting; one row's text for an array
  or a requeued job read from accounting only. `current.partition` therefore
  does not say where every task ran.
- `current.ran_on` needs the frozen spec: it appears only for `status --task`,
  for a job whose spec lists several partitions. `--all`, `--name` and
  `--job-id` read no spec and show Slurm's partition text alone, so they
  cannot tell that a started job was submitted to a list. A job that finished
  before accounting recorded its start reads `[]` on that read, with
  `cost.final` false: a finished queue row alone cannot be told from one
  cancelled while pending.
- What Slurm does with a partition list, and what `squeue` and `sacct` print
  for one, is source-confirmed and
  [not observed live](validation/observed-slurm.md#not-observed-live); whether
  Sherlock accepts a list depends on site settings that the source does not
  determine, and a `--preflight` of the list is the first check.
- A job submitted with a partition list must be read with a kit version that
  knows lists: an older one reports `unknown_partition` (exit 6) while Slurm
  prints the list.
- One `scontrol update` that carries several keys is not atomic: the
  controller validates and commits one key at a time (throttle, partition,
  time limit, features, prefer) with no rollback, and it updates an array
  record by record, the meta record and then every split record, finished
  ones included
  ([source-confirmed](validation/observed-slurm.md#job-updates)). `update`
  therefore takes one field group per call. The features group sends Features
  and Prefer in one request, because Slurm tries the preference instead of the
  constraint and both must carry every hard requirement; such a request can
  still leave Features applied and Prefer not, which the re-read reports as
  exit 5.
- The outcome of an update is the re-read's, never the exit status of
  `scontrol`: a stored throttle exits 1 while finished split records remain in
  the controller, a partition commit can be followed by an error, a job_submit
  plugin can reject or rewrite the request, and a lost reply looks the same
  whether or not the request reached the controller. A re-read that does not
  show the requested value on every record, or that fails, is exit 5 and is
  never retried. The identity read and the dispatch are not atomic either: a
  job that starts in between still gets a shorter time limit, because Slurm
  allows a decrease on a running job.
- The frozen spec under `<run-dir>/.shk/` stays the request as submitted:
  `update` writes no file and keeps no receipt, because no run directory is
  named by `--job-id`, `shk` keeps no state, and a second side effect after a
  dispatch would add a failure mode. Slurm's records carry the current request
  and the reply carries the records before and after. `current.ran_on`
  therefore still follows the spec's partition list.
- Sherlock's job_submit modify hook let time, features and throttle updates
  pass with the requested values
  ([observed live](validation/observed-slurm.md#job-updates-in-place)); what it
  does with a partition move is unknown. The hook runs on every update, once
  per array record, and can reject the update or rewrite the values; a
  rewritten value reads as exit 5.
- Slurm validates feature names against the union of the features of every
  node in the cluster, not per partition, so a `--constraint` or `--prefer`
  that no node of the job's partitions has is accepted and the job never
  starts. `update` adds no per-partition check; `shk resources` shows the
  features of a partition's nodes.
- Under `AccountingStorageEnforce=limits` without
  `PriorityFlags=ACCRUE_ALWAYS`, a partition move zeroes the job's accrue
  time: the age factor of its priority dips until the next scheduler pass, and
  restarts from the move where accrue limits (`MaxJobsAccrue`,
  `GrpJobsAccrue`) apply. Sherlock's settings are not known.
- The update facts above are source-confirmed at Slurm 25.11.9. On Sherlock,
  with 25.11.9, time, features and throttle updates of held pending jobs, the
  `unchanged` replies and the refusals were observed live, and so were two
  updates that Slurm rejected (a partition set, an unknown feature): each kept
  the record and read as exit 5
  ([observed live](validation/observed-slurm.md#job-updates-in-place)). A
  partition move that Slurm accepts, with its effect on the age factor, and an
  update of an array with a started task are not observed live.

## Rules, skill and delivery

The rules files and the skill are package data at fixed paths (contracts C1-C3,
with `bin/shk` as C4). Package consumers may reference these paths directly.
The rules start with an H2 and carry no front matter, HTML comments or imports,
so consumers can load them as instructions. Projects copy the skill with
`shk install-skill`, which follows links inside the project (a shared
`.agents/skills -> ../.claude/skills`) and refuses one that leads out of it, so
it never writes through a link into `HOME` or another source checkout. When
`~/.claude/rules/sherlock-kit.md` exists, Claude Code already loads it, so
`install-skill --rules` skips the project rules copy unless `--force` is given;
the check needs no global `shk` executable.
Codex has no rules import: with `--rules` it gets a hint to append `shk rules KIND` to the
project's `AGENTS.md`. The partition block in both rules files is generated by
`partitions.markdown()` and a test keeps it byte-identical to the table.

The Marlowe rules (`data/rules/marlowe-workstation.md`, `marlowe-cluster.md`)
and skill (`data/skills/sherlock-kit-marlowe/SKILL.md`) keep the same format
and cap and stand alone: the Sherlock rules are not assumed to be loaded.
Their paths are provisional, not contract paths. `install-skill` derives every
copy path and both HOME checks from the site's skill name, so the copies of
both sites live side by side in one project, and the Codex hint names
`shk --site marlowe rules KIND`. Their partition block is generated by
`partitions.markdown(sites.MARLOWE)`, and the same kind of test keeps it
identical. [Marlowe policy](marlowe-policy.md) records the source of every
Marlowe rules sentence.
