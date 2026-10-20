"""Explicit numerical thread defaults; not an OS-thread or kernel CPU sandbox."""

from __future__ import annotations

from typing import Any

from sherlock_kit.errors import EXIT_LOCAL, ShkError

MODE = "numerical_cpu_thread_budget_v1"
NUMERICAL = (
    "OMP_NUM_THREADS",
    "OMP_THREAD_LIMIT",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMEXPR_MAX_THREADS",
)


def policy(budget: int | None, cpus: int) -> dict[str, Any] | None:
    """Freeze opt-in per-task environment defaults within the requested CPUs."""
    if budget is None:
        return None
    if type(budget) is not int or not 1 <= budget <= cpus:
        raise ShkError(
            EXIT_LOCAL,
            "spec_invalid",
            "the CPU thread budget (--cpu-thread-budget or cpu_thread_budget) must be an integer"
            " from 1 through --cpus (per task)",
        )
    values = dict.fromkeys(NUMERICAL, str(budget))
    values.update(OMP_MAX_ACTIVE_LEVELS="1", OMP_DYNAMIC="FALSE", MKL_DYNAMIC="FALSE")
    environment = {
        prefix + name: value
        for prefix in ("", "APPTAINERENV_", "SINGULARITYENV_")
        for name, value in values.items()
    }
    return {
        "mode": MODE,
        "budget_per_task": budget,
        "environment": environment,
        "enforcement": "launch_environment_defaults_not_os_thread_limit",
    }


def command(argv: list[str], value: dict[str, Any] | None) -> list[str]:
    """Set the actual sbatch process environment without rewriting user scripts."""
    if value is None:
        return argv
    return ["env", *(f"{k}={v}" for k, v in value["environment"].items()), *argv]
