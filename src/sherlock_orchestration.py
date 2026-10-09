"""Single-workstation Slurm transactions; uncertain dispatch is never replayed.

SQLite coordinates independent repositories/worktrees on one authoritative host.
It is not a distributed coordinator and Slurm tokens are not idempotency keys.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import sqlite3
import time
from types import MappingProxyType
import uuid

from sherlock_partitions import PartitionError, partition_profile, partitions_sha256

SCHEMA = 1
SUBMIT_TIME_TOLERANCE_SECONDS = 300
BASE_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE"})
TERMINAL = BASE_TERMINAL  # import compatibility; reconcile() uses terminal_states()
PREEMPTION_STATES = frozenset({"PREEMPTED", "REQUEUED"})
ACK_KINDS = frozenset({"transport_ack", "operator_ack"})
PROFILE_FLAGS = ("preemptible", "requeue", "borrowed", "gpus_allowed")
# Attempts frozen before partition profiles existed were all consumer CPU work.
LEGACY_PROFILE = MappingProxyType({"preemptible": False, "requeue": False, "borrowed": False, "gpus_allowed": False, "courtesy": ""})
IDENTITY_KEYS = ("cluster", "principal", "code_digest", "input_digest", "runtime_digest", "policy_digest")
DIGEST = re.compile(r"[0-9a-f]{64}")
IDENT = re.compile(r"[A-Za-z0-9_.-]{1,128}")
JOB = re.compile(r"[1-9][0-9]*(?:_[0-9]+)?")


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


def frozen_profile(spec):
    """Profile frozen at admission; legacy attempts behave as normal/no-requeue."""
    profile = spec.get("partition_profile")
    return LEGACY_PROFILE if profile is None else checked_profile(profile)


def terminal_states(resources, profile):
    """States that release an attempt: PREEMPTED only ends a preemptible job that is not requeued."""
    if profile["preemptible"] and not resources.get("requeue", False):
        return BASE_TERMINAL | {"PREEMPTED"}
    return BASE_TERMINAL


def resources_checked(resources, profile=None):
    allowed = {"partition", "cpus", "memory_mb", "walltime_seconds", "gpus", "tasks", "constraint", "signal", "requeue"}
    if set(resources) - allowed:
        raise SafetyError("unknown/site-unsupported resource fields (account and exclude are forbidden)")
    r = dict(resources)
    for name in ("cpus", "memory_mb", "walltime_seconds"):
        if type(r.get(name)) is not int or r[name] <= 0:
            raise SafetyError(f"positive integer {name} required")
    for name, default, lower in (("gpus", 0, 0), ("tasks", 1, 1)):
        r.setdefault(name, default)
        if type(r[name]) is not int or r[name] < lower:
            raise SafetyError(f"invalid {name}")
    if not IDENT.fullmatch(r.get("partition", "")):
        raise SafetyError("explicit discovered partition required")
    profile = packaged_profile(r["partition"]) if profile is None else checked_profile(profile)
    if r["gpus"] and not profile["gpus_allowed"]:
        raise SafetyError(f"GPU resources require an eligible GPU partition; the {r['partition']!r} profile forbids GPUs")
    r["walltime_seconds"] = ((r["walltime_seconds"] + 59) // 60) * 60
    if "constraint" in r and not re.fullmatch(r"[A-Za-z0-9_.-]+(?:[&|][A-Za-z0-9_.-]+)*", r["constraint"]):
        raise SafetyError("invalid feature constraint grammar")
    if "signal" in r and not re.fullmatch(r"(?:B:)?(?:USR1|USR2|TERM)@[1-9][0-9]*", r["signal"]):
        raise SafetyError("invalid signal grammar")
    if "requeue" in r and type(r["requeue"]) is not bool:
        raise SafetyError("requeue must be boolean")
    r.setdefault("requeue", profile["requeue"])
    if r["requeue"] and not profile["requeue"]:
        raise SafetyError(f"automatic requeue is not permitted on partition {r['partition']!r}; its profile forbids requeue")
    return r


def checked_grant(grant, spec, partition, limits, now):
    """Borrowed scope must be backed by an actual external grant, not visibility or old script comments."""
    if not grant or grant.get("grantee") != spec.principal or not grant.get("evidence_reference"):
        raise SafetyError("borrowed admission disabled without identity-bound grant")
    if not grant.get("valid_from", math.inf) <= now < grant.get("valid_until", -math.inf):
        raise SafetyError("grant inactive/expired")
    if partition not in grant.get("partitions", []) or grant.get("scope") != spec.resource_scope:
        raise SafetyError("grant does not authorize this partition/scope")
    # Limits cannot be loosened by a per-project profile.
    if limits != grant.get("limits"):
        raise SafetyError("all borrowed consumers must use grant's exact limits")


def budget_charge(row, field):
    """Final cost once certified; otherwise the larger of the frozen estimate and the stored lower bound."""
    if row["cost_known"] and not row["reserved"]:
        return row[field]
    resources = json.loads(row["spec"])["resources"]
    unit = resources["cpus"] * resources["tasks"] if field == "cpu_seconds" else resources["gpus"]
    return max(unit * resources["walltime_seconds"], row[field])


def restart_of(item):
    restart = item.get("restart", 0)
    if isinstance(restart, bool) or type(restart) is not int or restart < 0:
        raise SafetyError("invalid restart count in scheduler evidence")
    return restart


def valid_cost(pair):
    return all(not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x) and x >= 0 for x in pair)


def matching_evidence(current, evidence):
    """Evidence bound to this attempt's frozen identity, job and submission window."""
    spec, attempt = current["spec"], current["id"]
    matches = []
    for item in evidence:
        if not isinstance(item, dict):
            raise SafetyError("malformed evidence")
        if item.get("attempt") != attempt:
            continue
        if any(item.get(key) != spec[key] for key in IDENTITY_KEYS):
            raise SafetyError("identity conflict retained; cannot adopt evidence")
        if item.get("submitted_at", 0) < current["created"] - SUBMIT_TIME_TOLERANCE_SECONDS:
            raise SafetyError("stale/job-id reuse evidence")
        if not JOB.fullmatch(str(item.get("job_id", ""))):
            raise SafetyError("invalid scheduler identity")
        if current["job_id"] and item["job_id"] != current["job_id"]:
            raise SafetyError("scheduler job identity conflict")
        matches.append(item)
    return matches


def group_states(items, terminal):
    states = {item.get("state") for item in items}
    if states <= terminal and len(states) != 1:
        raise SafetyError("terminal evidence conflicts")
    if states & terminal and not states <= terminal:
        raise SafetyError("scheduler/receipt state conflict; preserve unresolved")
    return states


def group_cost(items, terminal):
    """Agreed (cpu, gpu, complete) of one restart's measured rows, or None."""
    measured = [item for item in items if item.get("cost_known") is True
                and (item.get("accounting_complete") is True or item.get("state") not in terminal)]
    costs = {(item.get("cpu_seconds"), item.get("gpu_seconds")) for item in measured}
    if len(costs) > 1:
        raise SafetyError("accounting evidence conflicts")
    if not costs:
        return None
    cost = costs.pop()
    if not valid_cost(cost):
        raise SafetyError("invalid accounting cost")
    return (*cost, any(item.get("accounting_complete") is True for item in measured))


def history_complete(groups, top):
    """Restarts 0..top are all present and the authoritative rows claim no missing restart."""
    if set(groups) != set(range(top + 1)):
        return False
    return all(item.get("restart_history_complete", top == 0) is True for item in groups[top])


@dataclass(frozen=True)
class Observation:
    """One scheduler response about one job, read per restart; the highest restart is authoritative."""
    top: int
    states: tuple
    terminal: bool
    preempted: bool
    measured: bool
    known: bool
    cpu_seconds: float
    gpu_seconds: float


def observe(matches, terminal):
    groups = {}
    for item in matches:
        groups.setdefault(restart_of(item), []).append(item)
    top = max(groups)
    states = {restart: group_states(items, terminal) for restart, items in groups.items()}
    costs = {restart: cost for restart, cost in ((restart, group_cost(items, terminal)) for restart, items in groups.items()) if cost is not None}
    is_terminal = states[top] <= terminal and history_complete(groups, top)
    return Observation(top=top, states=tuple(sorted(states[top], key=str)), terminal=is_terminal,
                       preempted=top > 0 or any(item.get("state") in PREEMPTION_STATES for item in matches),
                       measured=bool(costs), known=is_terminal and all(restart in costs and costs[restart][2] for restart in range(top + 1)),
                       cpu_seconds=sum(cost[0] for cost in costs.values()), gpu_seconds=sum(cost[1] for cost in costs.values()))


def cost_floor(latest, history):
    """Stored and certified costs never decrease."""
    complete = [(e.get("cpu_seconds", 0), e.get("gpu_seconds", 0)) for e in history if e.get("cost_known") and e.get("accounting_complete")]
    return (max([latest["cpu_seconds"], *(c[0] for c in complete)]), max([latest["gpu_seconds"], *(c[1] for c in complete)]))


def recorded_cost(history):
    """Lower-bound (cpu, gpu, known) over the recorded restarts of one job, or None."""
    per_restart, top = {}, -1
    for item in history:
        restart = restart_of(item)
        top = max(top, restart)
        cost = (item.get("cpu_seconds"), item.get("gpu_seconds"))
        if item.get("cost_known") is not True or not valid_cost(cost):
            continue
        cpu, gpu, complete = per_restart.get(restart, (0, 0, False))
        per_restart[restart] = (max(cpu, cost[0]), max(gpu, cost[1]), complete or item.get("accounting_complete") is True)
    if not per_restart:
        return None
    known = all(restart in per_restart and per_restart[restart][2] for restart in range(top + 1))
    return (sum(c[0] for c in per_restart.values()), sum(c[1] for c in per_restart.values()), known)


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

    def checked(self):
        for name in ("project", "campaign", "task", "cluster", "principal", "resource_scope"):
            if not IDENT.fullmatch(getattr(self, name)):
                raise SafetyError(f"invalid identity field {name}")
        for name in ("code_digest", "input_digest", "runtime_digest", "policy_digest", "script_digest"):
            if not DIGEST.fullmatch(getattr(self, name)):
                raise SafetyError(f"invalid digest {name}")
        if not self.remote_script.startswith("/") or any(c in self.remote_script for c in "\x00\n\r"):
            raise SafetyError("resolved absolute immutable script path required")
        validator_fields = (self.validator_path, self.validator_digest, self.validator_function)
        if any(value is not None for value in validator_fields):
            if not all(isinstance(value, str) and value for value in validator_fields):
                raise SafetyError("complete frozen workload validator contract required")
            if not self.validator_path.startswith("/") or not DIGEST.fullmatch(self.validator_digest) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.validator_function):
                raise SafetyError("invalid workload validator identity")
        if self.toolkit_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", self.toolkit_revision):
            raise SafetyError("toolkit revision must be immutable")
        if self.remote_run_directory is not None:
            path = Path(self.remote_run_directory)
            if not path.is_absolute() or '..' in path.parts or any(c in self.remote_run_directory for c in '\x00\n\r%'):
                raise SafetyError("resolved isolated remote run directory required")
        return resources_checked(self.resources)


class Coordinator:
    """Private non-purged state shared by every consumer on one workstation."""

    def __init__(self, root: Path, *, authority=None):
        root = Path(root)
        if not root.is_absolute() or root.is_symlink():
            raise SafetyError("state root must be an absolute private directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise SafetyError("state root must have mode 0700")
        self.root = root
        self.path = root / "coordinator.sqlite3"
        if self.path.is_symlink():
            raise SafetyError("state database may not be a symlink")
        for ancestor in root.parents:
            if ancestor.is_symlink():
                raise SafetyError("state ancestor may not be a symlink")
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        if self.path.stat().st_mode & 0o077:
            raise SafetyError("database must remain private")
        self.authority = authority or socket.gethostname()
        try:
            with closing(self.connect()) as db:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                  id TEXT PRIMARY KEY, logical TEXT NOT NULL, scope TEXT NOT NULL,
                  spec TEXT NOT NULL, limits TEXT NOT NULL, created REAL NOT NULL,
                  state TEXT NOT NULL, job_id TEXT, reserved INTEGER NOT NULL,
                  cpu_seconds REAL NOT NULL DEFAULT 0, gpu_seconds REAL NOT NULL DEFAULT 0,
                  cost_known INTEGER NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS active_logical ON attempts(logical) WHERE reserved=1;
                CREATE TABLE IF NOT EXISTS evidence (
                  attempt TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
                  PRIMARY KEY(attempt,digest));
                CREATE TABLE IF NOT EXISTS manifests (attempt TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queries (key TEXT PRIMARY KEY, observed REAL NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quarantines (scope TEXT PRIMARY KEY, reason TEXT NOT NULL);
                """)
                db.execute("INSERT OR IGNORE INTO meta VALUES ('schema',?)", (str(SCHEMA),))
                db.execute("INSERT OR IGNORE INTO meta VALUES ('authority',?)", (self.authority,))
                if dict(db.execute("SELECT key,value FROM meta")) != {"schema": str(SCHEMA), "authority": self.authority}:
                    raise SafetyError("state schema/authoritative workstation mismatch")
                db.commit()
            os.chmod(self.path, 0o600)
        except sqlite3.DatabaseError as exc:
            raise SafetyError("malformed coordinator state preserved; quarantine requires explicit recovery") from exc

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=EXTRA")
        db.execute("PRAGMA journal_mode=DELETE")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _record(row):
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["limits"] = json.loads(result["limits"])
        return result

    def get(self, attempt):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
        if row is None:
            raise SafetyError("unknown attempt")
        return self._record(row)

    def unresolved(self):
        """Reserved attempts whose scheduler outcome is still open, oldest first.

        A claim abandoned in `submitting` (dispatcher died after its durable claim) is
        treated exactly like `unknown`: listed here, never re-dispatched.
        """
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM attempts WHERE reserved=1 AND state IN ('submitting','submitted','unknown') ORDER BY created, rowid").fetchall()
        return [self._record(row) for row in rows]

    def admit(self, spec: AttemptSpec, limits: dict, *, grant=None, advertised_policy=None, now=None):
        r = spec.checked()
        profile = packaged_profile(r["partition"])
        now = time.time() if now is None else now
        if advertised_policy is not None and advertised_policy != spec.policy_digest:
            raise SafetyError("installed/advertised policy mismatch; new admission blocked")
        # Consumer partitions use consumer-scoped limits and ignore any configured grant;
        # the packaged profile, never a partition name, decides what is borrowed.
        borrowed = profile["borrowed"]
        if borrowed:
            checked_grant(grant, spec, r["partition"], limits, now)
        allowed = {"cpus", "gpus", "tasks", "cpu_seconds", "gpu_seconds"}
        if set(limits) - allowed or not {"cpus", "gpus", "tasks"} <= set(limits):
            raise SafetyError("explicit concurrency limits required")
        for value in limits.values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise SafetyError("invalid limit")
        logical = canonical([spec.project, spec.campaign, spec.task])
        scope = canonical([spec.cluster, spec.principal, spec.resource_scope])
        attempt = uuid.uuid4().hex
        frozen = asdict(spec)
        frozen["resources"] = r
        frozen["schema_version"] = SCHEMA
        frozen["grant"] = grant if borrowed else None
        frozen["partition_profile"] = dict(profile)
        frozen["partitions_sha256"] = partitions_sha256()
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM quarantines WHERE scope=?", (scope,)).fetchone():
                raise SafetyError("scope quarantined by conflicting scheduler identity; explicit recovery required")
            previous = db.execute("SELECT * FROM attempts WHERE logical=? ORDER BY rowid DESC", (logical,)).fetchall()
            if previous:
                latest = previous[0]
                if latest["reserved"] or latest["state"] not in {"not_sent", "rejected", "terminal"}:
                    raise SafetyError("logical task has unresolved/active attempt; automatic duplicate forbidden")
                if spec.parent_attempt != latest["id"]:
                    raise SafetyError("new attempt must explicitly link previous resolved attempt")
                old_evidence = list(db.execute("SELECT body FROM evidence WHERE attempt=?", (latest["id"],)))
                if latest["state"] == "terminal" and any(json.loads(e[0]).get("state") == "COMPLETED" for e in old_evidence):
                    raise SafetyError("completed task cannot be retried")
            rows = db.execute("SELECT * FROM attempts WHERE scope=?", (scope,)).fetchall()
            for row in rows:
                if json.loads(row["limits"]) != limits:
                    raise SafetyError("shared scope limits changed; explicit migration required")
            active = [json.loads(row["spec"])["resources"] for row in rows if row["reserved"]]
            for field in ("cpus", "gpus", "tasks"):
                if sum(item[field] * (item["tasks"] if field == "cpus" else 1) for item in active) + r[field] * (r["tasks"] if field == "cpus" else 1) > limits[field]:
                    raise SafetyError(f"shared {field} concurrency exhausted")
            charged = [row for row in rows if row["reserved"] or row["state"] == "terminal"]
            for field, amount in (("cpu_seconds", r["cpus"] * r["tasks"]), ("gpu_seconds", r["gpus"])):
                consumed = sum(budget_charge(row, field) for row in charged)
                if field in limits and consumed + amount * r["walltime_seconds"] > limits[field]:
                    raise SafetyError(f"shared {field} budget exhausted")
            db.execute("INSERT INTO attempts(id,logical,scope,spec,limits,created,state,reserved) VALUES (?,?,?,?,?,?,?,1)",
                       (attempt, logical, scope, canonical(frozen), canonical(limits), now, "not_sent"))
        return self.get(attempt)

    def dispatch(self, attempt, runner):
        """Claim once, commit before process launch, release lock before transport."""
        spec = self.get(attempt)["spec"]
        # A frozen spec this toolkit cannot dispatch is refused before any claim, so
        # the attempt stays exactly as admitted instead of becoming unknown.
        command = submission_argv(attempt, spec)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            if row is None or row["state"] != "not_sent" or not row["reserved"]:
                raise SafetyError("attempt is not dispatchable; unresolved attempts cannot replay")
            if db.execute("SELECT 1 FROM quarantines WHERE scope=?", (row["scope"],)).fetchone():
                raise SafetyError("scope quarantined; no further dispatch")
            db.execute("UPDATE attempts SET state='submitting' WHERE id=?", (attempt,))
        try:
            result = runner(command)
            output = result.stdout.strip()
            job_id = None
            if not result.dispatched:
                state = "not_sent"
                reserved = 0
            elif result.returncode == 0 and re.fullmatch(r"[1-9][0-9]*(?:;[A-Za-z0-9_.-]+)?", output):
                if ";" in output and output.split(";")[1] != spec["cluster"]:
                    state, reserved = "unknown", 1
                else:
                    state, reserved, job_id = "submitted", 1, output.split(";")[0]
            elif result.returncode == 0 and output == "SHK_NOT_SENT:" + attempt + ":digest_mismatch":
                state, reserved = "not_sent", 0
            else:
                state, reserved = "unknown", 1
        except BaseException:
            with self.transaction() as db:
                db.execute("UPDATE attempts SET state='unknown' WHERE id=? AND state='submitting'", (attempt,))
            raise
        ack_conflict = None
        with self.transaction() as db:
            # Reconciliation may have raced while SSH was in flight. Preserve an
            # already identity-resolved result instead of overwriting it.
            current_row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            ack = {"kind": "transport_ack", "attempt": attempt, "state": state,
                   "job_id": job_id, "stdout": result.stdout, "stderr": getattr(result, "stderr", ""),
                   "returncode": result.returncode}
            db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?)", (attempt, digest(ack), canonical(ack)))
            current = current_row["state"]
            if job_id and current_row["job_id"] and current_row["job_id"] != job_id:
                ack_conflict = "acknowledgement conflicts with concurrent reconciliation"
                db.execute("INSERT OR IGNORE INTO quarantines VALUES (?,?)", (current_row["scope"], canonical(ack)))
            if not ack_conflict and current in {"submitting", "unknown"} and not current_row["job_id"]:
                db.execute("UPDATE attempts SET state=?,job_id=?,reserved=? WHERE id=?", (state, job_id, reserved, attempt))
        if ack_conflict:
            raise SafetyError(ack_conflict)
        return self.get(attempt)

    def recover(self):
        with self.transaction() as db:
            db.execute("UPDATE attempts SET state='unknown' WHERE state='submitting'")

    def reconcile(self, attempt, records, *, receipts=()):
        current = self.get(attempt)
        profile = frozen_profile(current["spec"])
        terminal = terminal_states(current["spec"]["resources"], profile)
        matches = matching_evidence(current, [*records, *receipts])
        if not matches:
            return {"attempt": current, "resolution": "inconclusive", "reason": "absence/lag/retention never proves non-submission"}
        if len({item["job_id"] for item in matches}) != 1:
            raise SafetyError("multiple scheduler identities for one attempt")
        job_id = matches[0]["job_id"]
        observed = observe(matches, terminal)
        with self.transaction() as db:
            latest = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            if latest["job_id"] and latest["job_id"] != job_id:
                raise SafetyError("concurrent scheduler job identity conflict")
            history = self._scheduler_history(db, attempt)
            if latest["state"] == "terminal":
                if not observed.terminal:
                    return {"attempt": self.get(attempt), "resolution": "stale_observation_ignored", "scientific_validation": "unverified"}
                previous = {e.get("state") for e in history if e.get("state") in terminal and restart_of(e) == observed.top}
                if previous and previous != set(observed.states):
                    raise SafetyError("terminal evidence conflicts")
                anomaly = False
            else:
                # Preemption or a restart where the profile forbids both is an anomaly to
                # investigate: the evidence is kept, the reservation is not released.
                anomaly = observed.preempted and not profile["preemptible"]
            if observed.measured:
                cpu_floor, gpu_floor = cost_floor(latest, history)
                if observed.cpu_seconds < cpu_floor or observed.gpu_seconds < gpu_floor:
                    raise SafetyError("accounting costs may not decrease")
            for item in matches:
                db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?)", (attempt, digest(item), canonical(item)))
            released = observed.terminal and not anomaly
            db.execute("UPDATE attempts SET state=?,job_id=?,reserved=? WHERE id=?", ("terminal" if released else "submitted", job_id, 0 if released else 1, attempt))
            if observed.measured:
                known = observed.known and not anomaly
                db.execute("UPDATE attempts SET cpu_seconds=?,gpu_seconds=?,cost_known=? WHERE id=?", (observed.cpu_seconds, observed.gpu_seconds, int(known), attempt))
            elif released and not any(e.get("cost_known") and e.get("accounting_complete") for e in history):
                db.execute("UPDATE attempts SET cost_known=0 WHERE id=?", (attempt,))
        resolution = "unexpected_preemption" if anomaly else "terminal" if released else "identified"
        return {"attempt": self.get(attempt), "resolution": resolution, "scientific_validation": "unverified"}

    def acknowledge_preemption(self, attempt, *, now=None):
        """Release an investigated preemption on a non-preemptible partition; all evidence is kept."""
        current = self.get(attempt)
        if frozen_profile(current["spec"])["preemptible"]:
            raise SafetyError("acknowledgement applies only to non-preemptible partitions; preemptible attempts resolve from accounting")
        now = time.time() if now is None else now
        with self.transaction() as db:
            latest = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            if latest["state"] == "terminal" or not latest["reserved"]:
                raise SafetyError("attempt already resolved; nothing to acknowledge")
            history = self._scheduler_history(db, attempt)
            preempted = [e for e in history if e.get("state") in PREEMPTION_STATES or restart_of(e) > 0]
            if not preempted:
                raise SafetyError("no recorded preemption evidence for this attempt; reconcile first")
            ack = {"kind": "operator_ack", "attempt": attempt, "job_id": latest["job_id"], "acknowledged_at": now,
                   "resolution": "acknowledged_preemption", "states": sorted({str(e.get("state")) for e in preempted})}
            db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?)", (attempt, digest(ack), canonical(ack)))
            db.execute("UPDATE attempts SET state='terminal',reserved=0 WHERE id=?", (attempt,))
            recorded = recorded_cost(history)
            if recorded is not None:
                cpu, gpu, known = recorded
                db.execute("UPDATE attempts SET cpu_seconds=?,gpu_seconds=?,cost_known=? WHERE id=?",
                           (max(cpu, latest["cpu_seconds"]), max(gpu, latest["gpu_seconds"]), int(known), attempt))
        return self.get(attempt)

    @staticmethod
    def _scheduler_history(db, attempt):
        rows = [json.loads(row[0]) for row in db.execute("SELECT body FROM evidence WHERE attempt=?", (attempt,))]
        return [item for item in rows if item.get("kind") not in ACK_KINDS]

    def pin_manifest(self, attempt, manifest):
        self.get(attempt)
        body, identity = canonical(manifest), digest(manifest)
        with self.transaction() as db:
            previous = db.execute("SELECT digest FROM manifests WHERE attempt=?", (attempt,)).fetchone()
            if previous and previous[0] != identity:
                raise SafetyError("attempt's pinned artifact manifest changed")
            db.execute("INSERT OR IGNORE INTO manifests VALUES (?,?,?)", (attempt, identity, body))
        return manifest

    def pinned_manifest(self, attempt):
        with closing(self.connect()) as db:
            row = db.execute("SELECT body,digest FROM manifests WHERE attempt=?", (attempt,)).fetchone()
        if row is None:
            raise SafetyError("no durable manifest for local receipt recovery")
        manifest = json.loads(row[0])
        if digest(manifest) != row[1]:
            raise SafetyError("durable artifact manifest corrupted")
        return manifest

    def cached_query(self, key, query, *, now=None):
        """Cache equivalent bounded query scopes and reserve cadence before network."""
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM queries WHERE key=?", (key,)).fetchone()
            if row and now - row["observed"] < 60:
                return json.loads(row["body"])
            db.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?)", (key, now, canonical({"status": "query_pending_or_failed"})))
        result = query()
        with self.transaction() as db:
            db.execute("UPDATE queries SET body=? WHERE key=? AND observed=?", (canonical(result), key, now))
        return result


def submission_argv(attempt, spec):
    """Snapshot script bytes before dispatch, suppress unowned SBATCH_* overrides.

    A nonzero sbatch result remains unknown: it can follow an accepted dispatch.
    Only a proven pre-sbatch validation failure is reported as not_sent. Only the
    profile frozen at admission is consulted; the packaged table is not re-read.
    """
    profile = spec.get("partition_profile")
    if profile is None or "requeue" not in spec.get("resources", {}):
        raise SafetyError("frozen spec lacks partition_profile/requeue; attempts admitted before partition profiles cannot be dispatched by this toolkit")
    r = resources_checked(spec["resources"], profile)
    requeue = ["--requeue", "--open-mode=append"] if r["requeue"] else ["--no-requeue"]
    options = ["sbatch", "--parsable", "--job-name=shk-" + attempt, "--comment=shk:" + attempt,
               "--partition=" + r["partition"], "--cpus-per-task=" + str(r["cpus"]),
               *requeue, "--ntasks=" + str(r["tasks"]), "--mem=" + str(r["memory_mb"]) + "M",
               "--time=" + str(r["walltime_seconds"] // 60)]
    run_directory = spec.get("remote_run_directory")
    if run_directory:
        options += ["--chdir=" + run_directory, "--output=" + run_directory + "/slurm-%j.out", "--error=" + run_directory + "/slurm-%j.err"]
    if r["gpus"]:
        options += ["-G", str(r["gpus"])]
    for field in ("constraint", "signal"):
        if field in r:
            options += ["--" + field + "=" + r[field]]
    # The tiny remote runner is our independently authored stdlib protocol.
    # All input parameters are argv; bytes passed to sbatch are the hashed snapshot.
    runner = """import hashlib,json,os,pathlib,re,subprocess,sys
attempt,path,expected,options,run_directory=sys.argv[1:]
try:
 if run_directory and (pathlib.Path(run_directory).resolve(strict=True)!=pathlib.Path(run_directory) or not pathlib.Path(run_directory).is_dir()):
  raise OSError('isolated run directory invalid')
 payload=pathlib.Path(path).read_bytes()
 if hashlib.sha256(payload).hexdigest()!=expected or re.search(rb'^\\s*#SBATCH',payload,re.M):
  print('SHK_NOT_SENT:'+attempt+':digest_mismatch');sys.exit(0)
except OSError:
 print('SHK_NOT_SENT:'+attempt+':digest_mismatch');sys.exit(0)
env={k:v for k,v in os.environ.items() if not k.startswith('SBATCH_')}
try:
 result=subprocess.run(json.loads(options),input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
except OSError:
 print('SHK_NOT_SENT:'+attempt+':digest_mismatch');sys.exit(0)
sys.stderr.buffer.write(result.stderr)
if result.returncode==0: sys.stdout.buffer.write(result.stdout)
else: print('SHK_UNKNOWN:'+attempt+':'+str(result.returncode))
"""
    return ["python3", "-c", runner, attempt, spec["remote_script"], spec["script_digest"], canonical(options), run_directory or ""]


def scheduler_cost(rows):
    """Charge distinct allocation/restart roots, excluding array parents and steps."""
    allocations = {}
    for row in rows:
        job_id = str(row["job_id"])
        if "." in job_id or row.get("array_parent"):
            continue
        if not JOB.fullmatch(job_id):
            raise SafetyError("invalid allocation ID")
        if row.get("restart", 0) and not row.get("restart_history_complete", False):
            return {"known": False, "cpu_seconds": None, "gpu_seconds": None}
        key = (row.get("cluster"), row.get("db_index"), job_id, row.get("restart", 0), row.get("start_time"))
        cost = (row.get("cpu_seconds"), row.get("gpu_seconds"))
        if key in allocations and allocations[key] != cost:
            raise SafetyError("duplicate accounting conflicts")
        allocations[key] = cost
    if any(any(not isinstance(x, (float, int)) or isinstance(x, bool) or not math.isfinite(x) or x < 0 for x in pair) for pair in allocations.values()):
        return {"known": False, "cpu_seconds": None, "gpu_seconds": None}
    return {"known": bool(allocations), "cpu_seconds": sum(c[0] for c in allocations.values()), "gpu_seconds": sum(c[1] for c in allocations.values())}
