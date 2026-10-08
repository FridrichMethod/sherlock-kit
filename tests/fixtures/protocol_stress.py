"""Bounded offline protocol soak, using real processes, SQLite, locks and rsync.

Scheduler acknowledgements/accounting are synthetic fixtures. This program never
executes submission_argv, SSH, sbatch, or a scheduler status command. Its success
is engineering evidence, not proof of a live Slurm/scientific result.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import dataclasses
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import random
import resource
import shutil
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

from sherlock_artifacts import build_manifest, fetch_bundle, rsync_transfer, verify_bundle
from sherlock_kit import policy_identity
from sherlock_orchestration import AttemptSpec, Coordinator, SafetyError, canonical, digest, scheduler_cost

FAULTS = ("intent_committed", "transferred", "verified", "promoted", "receipt_committed")
FAMILIES = ("admission", "dispatch_death", "ack_race", "query", "fetch_death", "fetch_race", "fetch_reject", "accounting")
THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
CRASH = 73
MAX_CASES = 40000
MAX_LOG_BYTES = 64 * 1024 * 1024
CONTEXT = mp.get_context("fork")  # Sherlock/Linux; at most three workers per case.


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def blocked(call):
    try:
        call()
    except SafetyError:
        return
    raise AssertionError("unsafe operation was accepted")


def spec(config, task="task"):
    d = digest([config["seed"], config["index"], task])
    return AttemptSpec(project="stress-fixture", campaign="offline", task=f"case-{config['index']}-{config['seed']:x}-{task}",
        cluster="fixture-cluster", principal="fixture-principal", resource_scope="fixture-scope",
        code_digest=d, input_digest=digest([d, "input"]), runtime_digest=digest([d, "runtime"]),
        policy_digest=digest([d, "policy"]), remote_script="/synthetic/never-executed/job.sh",
        script_digest=digest([d, "script"]), resources={"partition": "normal", "cpus": config["cpus"],
        "gpus": 0, "tasks": 1, "memory_mb": config["memory_mb"], "walltime_seconds": config["walltime"]})


def limits(config):
    return {"cpus": config["capacity"] * config["cpus"], "gpus": 0, "tasks": config["capacity"], "cpu_seconds": 1000000}


def evidence(coordinator, attempt, config, state="COMPLETED", **updates):
    row = coordinator.get(attempt)
    item = {k: row["spec"][k] for k in ("cluster", "principal", "code_digest", "input_digest", "runtime_digest", "policy_digest")}
    item.update(attempt=attempt, submitted_at=row["created"], job_id=str(config["job"]),
        state=state, cost_known=True, accounting_complete=state != "RUNNING", cpu_seconds=config["cost"], gpu_seconds=0)
    item.update(updates)
    return item


def emit(path, value):
    # Each worker owns one file. Atomicity of a shared log is not assumed.
    with open(path, "x", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def admission_worker(root, start, output, config, task):
    c = Coordinator(Path(root))
    require(start.wait(8), "start barrier timed out")
    try:
        row = c.admit(spec(config, task), limits(config))
        emit(output, {"accepted": row["id"]})
    except SafetyError as exc:
        emit(output, {"blocked": str(exc)})


def dispatch_worker(root, attempt, output, config, claimed=None, release=None):
    c = Coordinator(Path(root))
    def fake_transport(_argv):
        # Deliberately ignore the real submission argv. No child Slurm work.
        emit(output, {"synthetic_transport_invocation": attempt, "job": config["job"]})
        if claimed is not None:
            claimed.set()
            require(release.wait(8), "ack barrier timed out")
        if config["dispatch_mode"] == "death":
            os._exit(CRASH)
        if config["dispatch_mode"] == "malformed":
            return SimpleNamespace(dispatched=True, returncode=0, stdout="ambiguous-" + str(config["job"]))
        if config["dispatch_mode"] == "disconnect":
            return SimpleNamespace(dispatched=True, returncode=255, stdout="")
        return SimpleNamespace(dispatched=True, returncode=0, stdout=str(config["job"]), stderr="")
    try:
        c.dispatch(attempt, fake_transport)
    except SafetyError:
        if not config.get("ack_conflict"):
            raise


def query_worker(root, start, output, invocation, config, now):
    c = Coordinator(Path(root))
    require(start.wait(8), "query barrier timed out")
    def fake_query():
        emit(invocation, {"synthetic_query": config["seed"], "now": now})
        if config["query_failure"]:
            raise TimeoutError("synthetic transport failure")
        return {"seed": config["seed"], "jobs": [config["job"]]}
    try:
        result = c.cached_query("equivalent-synthetic-scope", fake_query, now=now)
        emit(output, {"result": result})
    except TimeoutError:
        emit(output, {"expected_failure": True})


def fetch_worker(source, destination, manifest, output, config, start=None):
    if start is not None:
        require(start.wait(8), "fetch barrier timed out")
    def fault(point):
        if point == config.get("fetch_fault"):
            os._exit(CRASH)
    def transfer(stage, m):
        rsync_transfer(source, stage, m, timeout=8)
        if config.get("fetch_fault") == "partial_transfer":
            target = stage / m["files"][0]["path"]
            target.write_bytes(target.read_bytes()[: config["truncate"]])
            os._exit(CRASH)
    def validate(root):
        if config.get("reject") == "validator_false":
            return False
        if config.get("reject") == "validator_mutation":
            (root / "result.json").write_bytes(b"mutated")
        return (root / "result.json").read_text() == canonical({"seed": config["seed"], "job": config["job"]}) + "\n" or config.get("reject") == "validator_mutation"
    def source_manifest():
        if config.get("reject") == "changed_manifest":
            changed = json.loads(canonical(manifest))
            changed["producer"]["input_digest"] = "f" * 64
            return changed
        return manifest
    try:
        result = fetch_bundle(manifest, Path(destination), transfer, source_manifest=source_manifest,
            validator=validate, validator_digest=digest([config["seed"], "validator"]),
            expected_identity=manifest["producer"], fault=fault)
        emit(output, {"ok": True, "recovered": result["recovered"], "receipt": result["receipt"]})
    except SafetyError as exc:
        if not config.get("reject"):
            raise
        emit(output, {"blocked": str(exc)})


def launch(target, args):
    process = CONTEXT.Process(target=target, args=args)
    process.start()
    return process


def join(processes, expected=None):
    # A hung protocol is a counterexample, never a reason to consume the budget.
    expected = expected or [0] * len(processes)
    try:
        for process, code in zip(processes, expected):
            process.join(10)
            require(not process.is_alive(), "worker deadline exceeded")
            require(process.exitcode == code, f"worker exited {process.exitcode}, expected {code}")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
            process.close()


def read(path):
    return json.loads(Path(path).read_text())


def check_database(c, config):
    with closing(c.connect()) as db:
        require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity")
        rows = db.execute("SELECT * FROM attempts").fetchall()
        active = [json.loads(r["spec"])["resources"] for r in rows if r["reserved"]]
        require(sum(r["cpus"] * r["tasks"] for r in active) <= limits(config)["cpus"], "CPU capacity exceeded")
        require(sum(r["tasks"] for r in active) <= limits(config)["tasks"], "task capacity exceeded")
        require(all(r["reserved"] == 1 for r in rows if r["state"] in {"unknown", "submitting", "submitted"}), "uncertainty lost reservation")
        require(all(r["reserved"] == 0 for r in rows if r["state"] == "terminal"), "terminal reservation retained")
    require((c.root / "coordinator.sqlite3").stat().st_mode & 0o077 == 0, "public state file")
    return [{k: row[k] for k in ("id", "logical", "state", "reserved", "job_id", "cpu_seconds", "cost_known")} for row in rows]


def protocol_case(root, config):
    c = Coordinator(root / "state")
    family = config["family"]
    observed = []
    if family == "admission":
        start = CONTEXT.Event()
        tasks = ["same" if config["same_task"] else "task-" + str(i) for i in range(3)]
        outputs = [root / f"admit-{i}.json" for i in range(3)]
        workers = [launch(admission_worker, (str(c.root), start, str(outputs[i]), config, tasks[i])) for i in config["worker_order"]]
        start.set()
        join(workers)
        answers = [read(out) for out in outputs]
        count = sum("accepted" in a for a in answers)
        require(count == (1 if config["same_task"] else min(3, config["capacity"])), "race admission winner count")
        observed = ["accepted" if "accepted" in a else "blocked" for a in answers]
    elif family in {"dispatch_death", "ack_race"}:
        attempt = c.admit(spec(config), limits(config))["id"]
        invocation = root / "invocation.json"
        if family == "dispatch_death":
            process = launch(dispatch_worker, (str(c.root), attempt, str(invocation), config))
            join([process], [CRASH if config["dispatch_mode"] == "death" else 0])
            c.recover()
            require((c.get(attempt)["state"], c.get(attempt)["reserved"]) == ("unknown", 1), "uncertain dispatch released")
            require(c.reconcile(attempt, [])["resolution"] == "inconclusive", "absence proved non-submission")
            blocked(lambda: c.dispatch(attempt, lambda _: require(False, "duplicate transport")))
            blocked(lambda: c.admit(spec(config), limits(config)))
            wrong = evidence(c, attempt, config, principal="foreign")
            blocked(lambda: c.reconcile(attempt, [wrong]))
            c.reconcile(attempt, [evidence(c, attempt, config)])
            stale = c.reconcile(attempt, [evidence(c, attempt, config, state="RUNNING", accounting_complete=False)])
            require(stale["resolution"] == "stale_observation_ignored", "terminal state regressed")
        else:
            claimed, release = CONTEXT.Event(), CONTEXT.Event()
            process = launch(dispatch_worker, (str(c.root), attempt, str(invocation), config, claimed, release))
            try:
                require(claimed.wait(8), "dispatch did not reach claim")
                if config["recover_race"]:
                    c.recover()
                expected_job = config["job"] + (1 if config["ack_conflict"] else 0)
                item = evidence(c, attempt, config, state=config["race_state"], job_id=str(expected_job))
                c.reconcile(attempt, [item])
            finally:
                release.set()
                join([process])
            row = c.get(attempt)
            require(row["job_id"] == str(expected_job), "late ack replaced reconciled identity")
            require(row["state"] == ("terminal" if config["race_state"] == "COMPLETED" else "submitted"), "late ack replaced state")
            if config["ack_conflict"]:
                blocked(lambda: c.admit(spec(config, "later"), limits(config)))
        require(read(invocation)["synthetic_transport_invocation"] == attempt, "missing dispatch marker")
        observed = [c.get(attempt)["state"], "quarantined" if config.get("ack_conflict") else "identity_bound"]
    elif family == "query":
        start = CONTEXT.Event()
        outputs = [root / f"query-{i}.json" for i in range(3)]
        invocations = [root / f"query-invocation-{i}.json" for i in range(3)]
        now = 1000 + config["index"] * 100
        workers = [launch(query_worker, (str(c.root), start, str(outputs[i]), str(invocations[i]), config, now)) for i in config["worker_order"]]
        start.set()
        join(workers)
        require(sum(p.exists() for p in invocations) == 1, "equivalent query duplicated")
        calls = []
        c.cached_query("equivalent-synthetic-scope", lambda: calls.append(1), now=now + config["cache_delta"])
        require(not calls, "query cadence bypassed")
        c.cached_query("equivalent-synthetic-scope", lambda: calls.append(1) or {}, now=now + 60)
        require(calls == [1], "expired query cache did not refresh")
        observed = [read(p) for p in outputs]
    elif family == "accounting":
        attempt = c.admit(spec(config), limits(config))["id"]
        incomplete = evidence(c, attempt, config, state="FAILED", cost_known=False, accounting_complete=False)
        c.reconcile(attempt, [incomplete])
        require(c.get(attempt)["cost_known"] == 0, "incomplete accounting charged final")
        complete = evidence(c, attempt, config, state="FAILED")
        c.reconcile(attempt, [complete, complete])
        c.reconcile(attempt, [complete])
        require(c.get(attempt)["cpu_seconds"] == config["cost"], "duplicate accounting double charged")
        blocked(lambda: c.reconcile(attempt, [evidence(c, attempt, config, state="FAILED", cpu_seconds=config["cost"] - 1)]))
        retry = c.admit(dataclasses.replace(spec(config), parent_attempt=attempt), limits(config))
        require(retry["spec"]["parent_attempt"] == attempt, "retry lost parent identity")
        rows, expected = [], 0
        for i, amount in enumerate(config["array_costs"]):
            allocation = {"job_id": str(config["job"]) + "_" + str(i), "cpu_seconds": amount, "gpu_seconds": 0, "start_time": i}
            rows.extend([allocation, allocation.copy(), {**allocation, "job_id": allocation["job_id"] + ".batch"}])
            expected += amount
        rows.append({"job_id": str(config["job"]), "array_parent": True, "cpu_seconds": 999999, "gpu_seconds": 0})
        require(scheduler_cost(rows)["cpu_seconds"] == expected, "array/step costs incorrect")
        require(not scheduler_cost([{**rows[0], "restart": 1, "restart_history_complete": False}])["known"], "restart history invented")
        observed = ["identity_linked_retry", len(config["array_costs"]), expected]
    attempts = check_database(c, config)
    return {"observed": observed, "attempts": attempts, "crashes": int(family == "dispatch_death" and config["dispatch_mode"] == "death"),
        "invariants": ["sqlite_integrity", "private_state", "capacity", "reservation", family]}


def artifact_case(root, config):
    source, destination = root / "source", root / "destination"
    source.mkdir()
    (source / "result.json").write_text(canonical({"seed": config["seed"], "job": config["job"]}) + "\n")
    rng = random.Random(config["seed"])
    for i in range(config["files"]):
        path = source / ("nested " + str(i % config["depth"])) / ("payload-'" + str(i) + "-λ.bin")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(rng.randbytes(config["bytes"] + i))
    producer = {k: getattr(spec(config), k) for k in ("cluster", "principal", "code_digest", "input_digest", "runtime_digest", "policy_digest")}
    producer["attempt"] = digest([config["seed"], "attempt"])[:32]
    manifest = build_manifest(source, producer)
    output = root / "fetch.json"
    family = config["family"]
    crashes = 0
    if family == "fetch_death":
        join([launch(fetch_worker, (str(source), str(destination), manifest, str(output), config))], [CRASH])
        crashes = 1
        require(destination.exists() == (config["fetch_fault"] in {"promoted", "receipt_committed"}), "partial final exposed")
        if config["fetch_fault"] in {"transferred", "partial_transfer"}:
            stage = root / (".destination.shk-stage-" + digest(manifest))
            target = stage / manifest["files"][0]["path"]
            original = source / manifest["files"][0]["path"]
            data = original.read_bytes()
            target.write_bytes(bytes(b ^ 0x55 for b in data))
            os.utime(target, ns=(original.stat().st_atime_ns, original.stat().st_mtime_ns))
        clean = {**config, "fetch_fault": None}
        join([launch(fetch_worker, (str(source), str(destination), manifest, str(output), clean))])
    elif family == "fetch_race":
        start = CONTEXT.Event()
        outputs = [root / f"fetch-{i}.json" for i in range(2)]
        workers = [launch(fetch_worker, (str(source), str(destination), manifest, str(outputs[i]), config, start)) for i in config["worker_order"] if i < 2]
        start.set()
        join(workers)
        answers = [read(p) for p in outputs]
        require(sorted(a["recovered"] for a in answers) == [False, True], "promotion not serialized")
        output = outputs[0]
    else:
        if config["reject"] == "extra_stage":
            stage = root / (".destination.shk-stage-" + digest(manifest))
            stage.mkdir(mode=0o700)
            (stage / "unrelated").write_bytes(b"preserve")
        elif config["reject"] == "conflicting_final":
            destination.mkdir(mode=0o700)
            (destination / "unrelated").write_bytes(b"preserve")
        elif config["reject"] == "symlink_stage":
            stage = root / (".destination.shk-stage-" + digest(manifest))
            stage.mkdir(mode=0o700)
            (stage / "escape").symlink_to(source / "result.json")
        join([launch(fetch_worker, (str(source), str(destination), manifest, str(output), config))])
        require("blocked" in read(output), "invalid bundle accepted")
        if config["reject"] == "conflicting_final":
            require((destination / "unrelated").read_bytes() == b"preserve", "conflicting final overwritten")
        else:
            require(not destination.exists(), "rejected bundle promoted")
        require(digest(build_manifest(source, producer)) == digest(manifest), "source changed")
        return {"observed": [config["reject"], "blocked"], "crashes": 0,
            "invariants": ["no_invalid_promotion", "source_immutable", "preserve_unrelated"]}
    verify_bundle(destination, manifest)
    require(digest(build_manifest(source, producer)) == digest(manifest), "source changed")
    receipt = read(root / ".destination.shk-receipt.json")
    require(receipt == read(root / ".destination.shk-transaction.json"), "receipt/intent disagree")
    require(receipt["manifest_sha256"] == digest(manifest) and receipt["producer"] == producer, "receipt identity mismatch")
    require(read(output)["receipt"] == receipt, "worker receipt mismatch")
    return {"observed": [read(output)["recovered"], digest(manifest)], "producer": producer,
        "validator_sha256": receipt["validator_sha256"], "crashes": crashes,
        "invariants": ["exact_inventory_hashes", "source_immutable", "receipt_identity", "atomic_promotion", family]}


def configuration(seed, index):
    # Reproducible case-level generation; a failing index does not require replaying
    # thousands of preceding cases or preserving their private SQLite files.
    case_seed = int(hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()[:16], 16)
    rng = random.Random(case_seed)
    family = FAMILIES[index % len(FAMILIES)]
    config = {"seed": case_seed, "index": index, "family": family, "cpus": rng.randint(1, 3),
        "capacity": rng.randint(1, 3), "memory_mb": rng.randint(128, 2048), "walltime": rng.randint(61, 900),
        "job": rng.randint(10000, 99999999), "cost": rng.randint(1, 200), "same_task": bool(rng.getrandbits(1)),
        "dispatch_mode": rng.choice(("death", "malformed", "disconnect")), "ack_conflict": bool(rng.getrandbits(1)),
        "recover_race": bool(rng.getrandbits(1)), "race_state": rng.choice(("RUNNING", "COMPLETED")),
        "query_failure": bool(rng.getrandbits(1)), "cache_delta": rng.randrange(60),
        "fetch_fault": rng.choice((*FAULTS, "partial_transfer")), "truncate": rng.randrange(16),
        "files": rng.randint(1, 6), "bytes": rng.randint(32, 32768), "depth": rng.randint(1, 3),
        "reject": rng.choice(("validator_false", "validator_mutation", "changed_manifest", "extra_stage", "conflicting_final", "symlink_stage")),
        "array_costs": [rng.randint(1, 1000) for _ in range(rng.randint(1, 12))], "worker_order": rng.sample(range(3), 3)}
    if family == "ack_race":
        config["dispatch_mode"] = "ack"
    if family != "fetch_death":
        config["fetch_fault"] = None
    if family != "fetch_reject":
        config["reject"] = None
    return config


def atomic_report(path, report):
    temporary = path.with_suffix(".pending")
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(output: Path, *, duration_seconds=600, seed=20261007, max_cases=MAX_CASES, replay_index=None):
    require(0 < duration_seconds <= 600, "duration must be in (0, 600]")
    require(1 <= max_cases <= MAX_CASES, "case count outside bounded limit")
    require(replay_index is None or 0 <= replay_index < MAX_CASES, "replay index outside bounded limit")
    for name in THREAD_VARS:
        os.environ[name] = "1"
    # The caller supplies node-local storage inside an allocation. No default home
    # or shared/private production coordinator paths are used.
    output = output.absolute()
    require(not output.exists() and not output.is_symlink(), "output must be fresh")
    require(output.parent.is_dir(), "output parent must exist")
    require(all(not p.is_symlink() for p in (output.parent, *output.parent.parents)), "output ancestor is a symlink")
    output.mkdir(mode=0o700)
    started = time.monotonic()
    cpu_start = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    report = {"schema_version": 1, "status": "running", "seed": seed, "duration_budget_seconds": duration_seconds,
        "scheduler_evidence": "synthetic; no child Slurm jobs", "live_integration": False, "scientific_acceptance": False,
        "max_workers": 3, "max_live_payload_bytes": 6 * 32773, "max_case_log_bytes": MAX_LOG_BYTES,
        "thread_limits": {name: os.environ[name] for name in THREAD_VARS}, "policy_identity": policy_identity(),
        "python": sys.version, "rsync": subprocess.run(["rsync", "--version"], capture_output=True, text=True, check=True, timeout=5).stdout.splitlines()[0],
        "fixture_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "module_sha256": {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (sys.modules["sherlock_orchestration"], sys.modules["sherlock_artifacts"])},
        "case_count": 0, "crash_count": 0, "coverage": {}, "invariant_counts": {}, "case_chain_sha256": "0" * 64,
        "novel_semantic_buckets": 0, "revisited_semantic_buckets": 0}
    report_path = output / "report.json"
    atomic_report(report_path, report)
    coverage, invariants, semantic = Counter(), Counter(), set()
    log = output / "cases.jsonl"
    case_root = None
    try:
        with open(log, "x", encoding="utf-8") as stream:
            for offset in range(max_cases):
                # Cases finish before another starts; the last bounded case can
                # overrun the generation budget by its worker/rsync deadline.
                if offset and time.monotonic() - started >= duration_seconds:
                    report["stop_reason"] = "generation_deadline"
                    break
                index = replay_index if replay_index is not None else offset
                config = configuration(seed, index)
                case_root = output / "active-case"
                case_root.mkdir(mode=0o700)
                case_start = time.monotonic()
                outcome = artifact_case(case_root, config) if config["family"].startswith("fetch_") else protocol_case(case_root, config)
                bucket = (config["family"], config["same_task"] if config["family"] == "admission" else config["dispatch_mode"] if config["family"] == "dispatch_death" else (config["ack_conflict"], config["recover_race"], config["race_state"]) if config["family"] == "ack_race" else config["query_failure"] if config["family"] == "query" else config["fetch_fault"] if config["family"] == "fetch_death" else config["reject"] if config["family"] == "fetch_reject" else "default")
                key = canonical(bucket)
                semantic.add(key)
                coverage[key] += 1
                invariants.update(outcome["invariants"])
                record = {"config": config, "outcome": outcome, "elapsed_seconds": time.monotonic() - case_start}
                body = canonical(record)
                require(stream.tell() + len(body.encode()) + 1 <= MAX_LOG_BYTES, "case log limit reached")
                stream.write(body + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                report["case_chain_sha256"] = hashlib.sha256((report["case_chain_sha256"] + body).encode()).hexdigest()
                report["case_count"] += 1
                report["crash_count"] += outcome["crashes"]
                report["coverage"] = dict(coverage)
                report["invariant_counts"] = dict(invariants)
                report["novel_semantic_buckets"] = len(semantic)
                report["revisited_semantic_buckets"] = report["case_count"] - len(semantic)
                report["elapsed_seconds"] = time.monotonic() - started
                atomic_report(report_path, report)
                shutil.rmtree(case_root)  # Exact, fixture-owned bounded outputs only.
                case_root = None
                if replay_index is not None:
                    report["stop_reason"] = "counterexample_replay"
                    break
            else:
                report["stop_reason"] = "case_limit"
        report["status"] = "passed"
        report["coverage_limit"] = "Fixed semantic buckets saturate; subsequent unique parameter draws measure race/failure robustness, not new semantic coverage."
    except BaseException as exc:
        report["status"] = "failed"
        report["counterexample"] = {"config": config if "config" in locals() else None,
            "error": repr(exc), "traceback": traceback.format_exc(), "retained_fixture": str(case_root) if case_root else None}
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        for label, who, baseline in zip(("parent", "children"), (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN), cpu_start):
            usage = resource.getrusage(who)
            report[label + "_cpu_seconds"] = usage.ru_utime + usage.ru_stime - baseline.ru_utime - baseline.ru_stime
            report[label + "_maxrss_kib"] = usage.ru_maxrss  # Linux units; fork workers include rsync descendants.
        atomic_report(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--max-cases", type=int, default=MAX_CASES)
    parser.add_argument("--replay-index", type=int)
    args = parser.parse_args()
    report = run(args.output, duration_seconds=args.duration_seconds, seed=args.seed,
        max_cases=args.max_cases, replay_index=args.replay_index)
    print(canonical({k: report[k] for k in ("status", "case_count", "crash_count", "elapsed_seconds", "novel_semantic_buckets", "revisited_semantic_buckets", "stop_reason")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
