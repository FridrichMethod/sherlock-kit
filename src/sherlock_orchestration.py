"""Attempt specifications frozen for admission; pure functions, no local ledger.

An attempt is checked against the packaged partition profile, frozen into the
record that the registry on Sherlock stores (``sherlock_registry``), and turned
into the exact ``sbatch`` option list. Nothing here touches a filesystem, a
database or the network: Slurm accounting and the registry are the only durable
attempt state, so any workstation can operate.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import time

from sherlock_partitions import PartitionError, partition_profile, partitions_sha256

SCHEMA = 1
# Duplicated on purpose in sherlock_registry (standalone remote program); a
# release-metadata test asserts the two copies are equal.
SUBMIT_TIME_TOLERANCE_SECONDS = 300
BASE_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE"})
PREEMPTION_STATES = frozenset({"PREEMPTED", "REQUEUED"})
PROFILE_FLAGS = ("preemptible", "requeue", "borrowed", "gpus_allowed")
IDENTITY_KEYS = ("cluster", "principal", "code_digest", "input_digest", "runtime_digest", "policy_digest")
ARRAY_MAX_TASKS = 1000  # Sherlock scontrol max_array_tasks (2026-10-10); task indices are 0..count-1
RESOURCE_FIELDS = frozenset({"partition", "cpus", "memory_mb", "walltime_seconds", "gpus", "tasks", "constraint", "signal", "requeue", "array"})
DIGEST = re.compile(r"[0-9a-f]{64}")
IDENT = re.compile(r"[A-Za-z0-9_.-]{1,128}")
ATTEMPT = re.compile(r"[0-9a-f]{32}")
CONSTRAINT = re.compile(r"[A-Za-z0-9_.-]+(?:[&|][A-Za-z0-9_.-]+)*")
SIGNAL = re.compile(r"(?:B:)?(?:USR1|USR2|TERM)@[1-9][0-9]*")
LEGACY_MESSAGE = "frozen spec lacks partition_profile/requeue; attempts admitted before partition profiles cannot be dispatched by this toolkit"


class SafetyError(ValueError):
    """A contract cannot be established; preserve existing evidence."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def packaged_profile(partition):
    """The packaged profile of a partition; anything outside the table is refused."""
    try:
        return partition_profile(partition)
    except PartitionError as exc:
        raise SafetyError(str(exc)) from None


def checked_profile(profile):
    if not isinstance(profile, Mapping) or any(type(profile.get(flag)) is not bool for flag in PROFILE_FLAGS):
        raise SafetyError("partition profile must carry boolean preemptible/requeue/borrowed/gpus_allowed flags")
    return profile


def terminal_states(resources, profile):
    """States that release an attempt: PREEMPTED only ends a preemptible job that is not requeued."""
    if profile["preemptible"] and not resources.get("requeue", False):
        return BASE_TERMINAL | {"PREEMPTED"}
    return BASE_TERMINAL


def _positive_int(value):
    return type(value) is int and value > 0


def checked_array(array):
    """``{"count": N, "throttle": M}`` with 2 <= N <= ARRAY_MAX_TASKS and 1 <= M <= N; nothing else."""
    if not isinstance(array, Mapping) or "count" not in array or set(array) - {"count", "throttle"}:
        raise SafetyError("array must be a mapping with exactly count and an optional throttle")
    count = array["count"]
    if type(count) is not int or not 2 <= count <= ARRAY_MAX_TASKS:
        raise SafetyError(f"array count must be an integer from 2 to {ARRAY_MAX_TASKS}")
    checked = {"count": count}
    if "throttle" in array:
        throttle = array["throttle"]
        if type(throttle) is not int or not 1 <= throttle <= count:
            raise SafetyError("array throttle must be an integer from 1 to count")
        checked["throttle"] = throttle
    return checked


def _checked_requeue(r, profile):
    if "requeue" in r and type(r["requeue"]) is not bool:
        raise SafetyError("requeue must be boolean")
    requeue = r.get("requeue", profile["requeue"])
    if requeue and not profile["requeue"]:
        raise SafetyError(f"automatic requeue is not permitted on partition {r['partition']!r}; its profile forbids requeue")
    return requeue


def resources_checked(resources, profile=None):
    """Site-supported resources only; GPUs and requeue follow the (packaged or frozen) profile."""
    if not isinstance(resources, Mapping) or set(resources) - RESOURCE_FIELDS:
        raise SafetyError("unknown/site-unsupported resource fields (account and exclude are forbidden)")
    r = dict(resources)
    for name in ("cpus", "memory_mb", "walltime_seconds"):
        if not _positive_int(r.get(name)):
            raise SafetyError(f"positive integer {name} required")
    for name, default, lower in (("gpus", 0, 0), ("tasks", 1, 1)):
        r.setdefault(name, default)
        if type(r[name]) is not int or r[name] < lower:
            raise SafetyError(f"invalid {name}")
    if not isinstance(r.get("partition"), str) or not IDENT.fullmatch(r["partition"]):
        raise SafetyError("explicit discovered partition required")
    profile = packaged_profile(r["partition"]) if profile is None else checked_profile(profile)
    if r["gpus"] and not profile["gpus_allowed"]:
        raise SafetyError(f"GPU resources require an eligible GPU partition; the {r['partition']!r} profile forbids GPUs")
    r["walltime_seconds"] = ((r["walltime_seconds"] + 59) // 60) * 60
    if "constraint" in r and not (isinstance(r["constraint"], str) and CONSTRAINT.fullmatch(r["constraint"])):
        raise SafetyError("invalid feature constraint grammar")
    if "signal" in r and not (isinstance(r["signal"], str) and SIGNAL.fullmatch(r["signal"])):
        raise SafetyError("invalid signal grammar")
    r["requeue"] = _checked_requeue(r, profile)
    if "array" in r:
        r["array"] = checked_array(r["array"])
    return r


def checked_grant(grant, spec, partition, now):
    """Borrowed scope must be backed by an actual external grant, not visibility or old script comments.

    Only grantee, evidence_reference, validity, partitions and scope are read; a
    legacy ``limits`` member (or any other extra key) is ignored.
    """
    if not isinstance(grant, Mapping) or grant.get("grantee") != spec.principal or not grant.get("evidence_reference"):
        raise SafetyError("borrowed admission disabled without identity-bound grant")
    if not grant.get("valid_from", math.inf) <= now < grant.get("valid_until", -math.inf):
        raise SafetyError("grant inactive/expired")
    if partition not in grant.get("partitions", []) or grant.get("scope") != spec.resource_scope:
        raise SafetyError("grant does not authorize this partition/scope")


@dataclass(frozen=True)
class AttemptSpec:
    project: str
    campaign: str
    task: str
    cluster: str
    principal: str
    resource_scope: str
    code_digest: str
    input_digest: str
    runtime_digest: str
    policy_digest: str
    resources: dict
    remote_script: str
    script_digest: str
    parent_attempt: str | None = None
    validator_path: str | None = None
    validator_digest: str | None = None
    validator_function: str | None = None
    toolkit_revision: str | None = None
    remote_run_directory: str | None = None

    def _checked_validator(self):
        fields = (self.validator_path, self.validator_digest, self.validator_function)
        if all(value is None for value in fields):
            return
        if not all(isinstance(value, str) and value for value in fields):
            raise SafetyError("complete frozen workload validator contract required")
        if not self.validator_path.startswith("/") or not DIGEST.fullmatch(self.validator_digest) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.validator_function):
            raise SafetyError("invalid workload validator identity")

    def _checked_run_directory(self):
        if self.remote_run_directory is None:
            return
        path = Path(self.remote_run_directory)
        if not path.is_absolute() or ".." in path.parts or any(c in self.remote_run_directory for c in "\x00\n\r%"):
            raise SafetyError("resolved isolated remote run directory required")

    def checked(self):
        for name in ("project", "campaign", "task", "cluster", "principal", "resource_scope"):
            if not isinstance(getattr(self, name), str) or not IDENT.fullmatch(getattr(self, name)):
                raise SafetyError(f"invalid identity field {name}")
        for name in ("code_digest", "input_digest", "runtime_digest", "policy_digest", "script_digest"):
            if not isinstance(getattr(self, name), str) or not DIGEST.fullmatch(getattr(self, name)):
                raise SafetyError(f"invalid digest {name}")
        if not isinstance(self.remote_script, str) or not self.remote_script.startswith("/") or any(c in self.remote_script for c in "\x00\n\r"):
            raise SafetyError("resolved absolute immutable script path required")
        self._checked_validator()
        if self.toolkit_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", self.toolkit_revision):
            raise SafetyError("toolkit revision must be immutable")
        self._checked_run_directory()
        return resources_checked(self.resources)


def frozen_record(spec: AttemptSpec, *, grant=None, advertised_policy=None, now=None):
    """The admission body stored as ``record.json['spec']`` on Sherlock.

    The packaged profile, never a partition name, decides what is borrowed; a
    consumer partition ignores any configured grant and records ``grant: None``.
    """
    r = spec.checked()
    profile = packaged_profile(r["partition"])
    if advertised_policy is not None and advertised_policy != spec.policy_digest:
        raise SafetyError("installed/advertised policy mismatch; new admission blocked")
    borrowed = profile["borrowed"]
    if borrowed:
        checked_grant(grant, spec, r["partition"], time.time() if now is None else now)
    frozen = asdict(spec)
    frozen["resources"] = r
    frozen["schema_version"] = SCHEMA
    frozen["grant"] = dict(grant) if borrowed else None
    frozen["partition_profile"] = dict(profile)
    frozen["partitions_sha256"] = partitions_sha256()
    return frozen


def _array_option(array):
    option = f"--array=0-{array['count'] - 1}"
    return option + f"%{array['throttle']}" if "throttle" in array else option


def sbatch_options(attempt_id, frozen):
    """The exact ``sbatch`` argv for a frozen record; only the frozen profile is consulted.

    Arrays add ``--array=0-<count-1>[%throttle]`` right after the partition and
    write ``slurm-%A_%a`` logs; a single job keeps ``slurm-%j``. Records frozen
    before partition profiles existed are refused before anything is claimed.
    """
    if not isinstance(attempt_id, str) or not ATTEMPT.fullmatch(attempt_id):
        raise SafetyError("attempt id must be 32 lowercase hex characters")
    if not isinstance(frozen, Mapping):
        raise SafetyError("frozen record must be a mapping")
    profile, resources = frozen.get("partition_profile"), frozen.get("resources")
    if profile is None or not isinstance(resources, Mapping) or "requeue" not in resources:
        raise SafetyError(LEGACY_MESSAGE)
    r = resources_checked(resources, profile)
    array = r.get("array")
    requeue = ["--requeue", "--open-mode=append"] if r["requeue"] else ["--no-requeue"]
    options = ["sbatch", "--parsable", "--job-name=shk-" + attempt_id, "--comment=shk:" + attempt_id, "--partition=" + r["partition"]]
    if array:
        options.append(_array_option(array))
    options += ["--cpus-per-task=" + str(r["cpus"]), *requeue, "--ntasks=" + str(r["tasks"]), "--mem=" + str(r["memory_mb"]) + "M",
                "--time=" + str(r["walltime_seconds"] // 60)]
    run_directory = frozen.get("remote_run_directory")
    if run_directory:
        log = "slurm-%A_%a" if array else "slurm-%j"
        options += ["--chdir=" + run_directory, "--output=" + run_directory + "/" + log + ".out", "--error=" + run_directory + "/" + log + ".err"]
    if r["gpus"]:
        options += ["-G", str(r["gpus"])]
    for field in ("constraint", "signal"):
        if field in r:
            options += ["--" + field + "=" + r[field]]
    return options
