# Opt-in CPU numerical thread budget

`shk submit --cpu-thread-budget [N]` enables explicit numerical thread defaults
for the submitted task. Omitting N after the flag selects 1. N must be a positive
integer no greater than `--cpus` (CPUs per task). In a multi-task job the budget
is per task, not an additional allocation or a limit for the whole process tree.
Projects may opt in by adding `cpu_thread_budget = 1` to `[tool.sherlock-kit]` or
`sherlock-kit.toml`. An explicit CLI value overrides this configured budget.
Without either setting, the prior script/argv/spec behavior is unchanged.

The policy is intentionally explicit: enabling it replaces inherited numerical
thread settings, including conflicting container-prefixed values. If a scientific
runtime requires a different numerical thread budget, freeze that explicit CLI
or configuration value and validate the runtime; do not silently depend on the
submitting shell's ambient values.

## Effective command and provenance

The frozen `argv` becomes `env NAME=VALUE ... sbatch ...`. Both packaged POSIX
submit and preflight programs execute this command directly. `env` sets these
values in the actual sbatch process; existing `--export=ALL` passes them to the
job before its script begins. The user script remains byte-identical, with its
original interpreter/shebang and SHA. No shell syntax is inserted into arbitrary
Python, shell or other scripts. No additional remote protocol or wrapper file is
created. The staged spec includes `cpu_thread_policy`, its versioned mode,
budget_per_task, complete environment and explicit enforcement scope. Dry-run
and preflight expose the same command and policy.

The numerical values are set to N for:

- OMP_NUM_THREADS and OMP_THREAD_LIMIT
- MKL_NUM_THREADS and OPENBLAS_NUM_THREADS
- BLIS_NUM_THREADS and VECLIB_MAXIMUM_THREADS
- NUMEXPR_NUM_THREADS and NUMEXPR_MAX_THREADS

OMP_MAX_ACTIVE_LEVELS is 1, and OMP_DYNAMIC and MKL_DYNAMIC are FALSE.
Every value is set identically in its host name, APPTAINERENV\_ name and
SINGULARITYENV\_ name. Explicit container prefixes permit normal Apptainer or
compatible Singularity clean-environment launchers to propagate the values. An
actual container runtime must still be verified by the project; toolkit tests
include a hermetic clean-environment handoff simulation, not a site container
installation or model qualification.

## Honest limits

This is a launch-environment guard, **not a hard OS-thread limit** or a proof of
CPU cpuset enforcement. A user script can override the variables; an application
may ignore them or use additional runtime setters. PyTorch intra-op/inter-op
settings, DataLoader/preprocessing worker counts and numerical library settings
after import remain application responsibilities. CUDA, I/O, container and FUSE
helper threads may legitimately exist even when numerical computation uses 1 CPU.
A 52-thread process is not proof of 52 CPUs executing.

Kernel CPU isolation remains the site's Slurm cgroup/cpuset responsibility.
Requesting 2 CPUs does not by itself prove which cgroup constraints are enabled.
Use verified job/step membership, affinity/cgroup evidence and aggregate actual
CPU-time deltas to assess CPU usage. Do not set a per-UID RLIMIT_NPROC or a 2-PID
cap as a substitute for CPU isolation. The toolkit neither changes site cgroups
nor monitors/kills processes based on their OS thread count.

## Controller compatibility

Guarded argv deliberately starts with `env`, rather than `sbatch`. A project
controller or frozen native/SubmitLine verifier that insists on argv[0] being
sbatch must explicitly accept and validate this exact frozen environment prefix
before enabling the option. Existing deployed toolkit copies do not gain this
feature automatically; CLI/config/source hashes must be upgraded through their
normal reviewed deployment. Do not point a live job at a development checkout or
change a frozen scientific runtime to bypass its source/argv gates.

Tests run actual packaged submit/preflight shell programs against fake Slurm,
verify the effective payload/container environment despite inherited 52-thread
settings, validate config/CLI boundaries and preserve unguarded legacy behavior.
No remote job, container installation or GPU is used by these tests.
