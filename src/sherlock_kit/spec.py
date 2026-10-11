"""Offline submit checks and the frozen job spec; nothing here touches the network.

build(args, config, now) -> Spec. Exit 3: unlisted partition, GPUs or requeue its profile
forbids, a denylisted directive in the leading #SBATCH block (read as sbatch reads it),
borrowed partition without --courtesy-ack. Exit 1 (spec_invalid): bad grammar, script
over 1 MiB. Name: shk.<project>.<slug(task)[:20]>.<sha256(canonical([project, campaign,
task]))[:10]>, at most 60 chars of [a-z0-9.-]; retries reuse it.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

from sherlock_kit import partitions, version_line
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, ShkError

SCRIPT_LIMIT = 1 << 20
ARRAY_MAX_TASKS = 1000  # Sherlock max_array_tasks (2026-10-10); indices are 0..count-1
NAME = re.compile(r"shk\.[a-z0-9][a-z0-9-]{0,23}\.[a-z0-9-]{1,20}\.[0-9a-f]{10}")
TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,128}")
RUN_DIR = re.compile(r"/[A-Za-z0-9_./-]+")
GRAMMAR = {"mem": re.compile(r"([1-9][0-9]*)([MG])", re.I),
           "time": re.compile(r"([1-9][0-9]*)([hm])|([0-9]+):([0-5][0-9]):([0-5][0-9])"),
           "array": re.compile(r"([0-9]+)(?:%([0-9]+))?"),
           "constraint": re.compile(r"[A-Za-z0-9_.:-]+(?:[&|][A-Za-z0-9_.:-]+)*"),
           "signal": re.compile(r"(?:B:)?(?:USR1|USR2|TERM)@[1-9][0-9]*"),
           "after_ok": re.compile(r"[1-9][0-9]*(?:_[0-9]+)?")}
DENIED_LONG = ("account", "exclude", "gres", "gpus", "partition", "array", "dependency", "begin", "job-name")
DENIED_SHORT, NO_VALUE = "AxGpadJ", "HhOQsuvVW"  # short flags; NO_VALUE ones may be bundled before others


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bad(message: str) -> ShkError:
    return ShkError(EXIT_LOCAL, "spec_invalid", message)


def checked_text(label: str, value) -> str:
    if not isinstance(value, str) or not TEXT.fullmatch(value):
        raise _bad(f"--{label} must be 1-128 printable characters")
    return value


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:20].rstrip("-") or "x"


def job_name(project: str, campaign: str, task: str) -> str:
    digest = hashlib.sha256(canonical([project, campaign, task]).encode()).hexdigest()[:10]
    return f"shk.{project}.{slug(task)}.{digest}"


def memory_mb(text) -> int:
    match = GRAMMAR["mem"].fullmatch(text or "")
    if not match:
        raise _bad(f"--mem {text!r}: use 16G or 16000M")
    return int(match[1]) * (1024 if match[2] in "Gg" else 1)


def minutes(text) -> int:
    match = GRAMMAR["time"].fullmatch(text or "")
    seconds = (int(match[1]) * (3600 if match[2] == "h" else 60) if match[1] else
               int(match[3]) * 3600 + int(match[4]) * 60 + int(match[5])) if match else 0
    if not seconds:
        raise _bad(f"--time {text!r}: use 2h, 90m or H:MM:SS (above zero)")
    return -(-seconds // 60)


def array(text) -> dict:
    match = GRAMMAR["array"].fullmatch(text or "")
    count, throttle = (int(match[1]), int(match[2]) if match[2] else None) if match else (0, None)
    if not 2 <= count <= ARRAY_MAX_TASKS or (throttle is not None and not 1 <= throttle <= count):
        raise _bad(f"--array {text!r}: N[%M] with 2 <= N <= {ARRAY_MAX_TASKS} and 1 <= M <= N")
    return {"count": count, "throttle": throttle}


def run_directory(value, config, name: str) -> str:
    run = (value or f"{config.remote_root}/runs/{name}").rstrip("/")
    if not RUN_DIR.fullmatch(run) or ".." in run.split("/"):
        raise _bad(f"--run-dir {run!r} must be absolute, [A-Za-z0-9_./-] only, without '..'")
    return run


def directives(script: bytes) -> list[str]:
    """Words of the leading #SBATCH lines; like sbatch, stop at the first non-comment line."""
    words = []
    for line in script.decode("utf-8", "replace").splitlines():
        if line.startswith("#SBATCH"):
            try:
                words += shlex.split(line[7:], comments=True)
            except ValueError:
                words += line[7:].split()
        elif line.strip() and not line.lstrip().startswith("#"):
            break
    return words


def denied(word: str) -> bool:
    if word.startswith("--"):  # getopt_long accepts unambiguous prefixes such as --part
        option = word[2:].partition("=")[0]
        return bool(option) and (option.startswith("gpus") or any(d.startswith(option) for d in DENIED_LONG))
    for flag in word[1:] if word.startswith("-") else "":
        if flag in DENIED_SHORT or flag not in NO_VALUE:
            return flag in DENIED_SHORT
    return False


def _script(path: str) -> bytes:
    try:
        with open(path, "rb") as stream:
            data = stream.read(SCRIPT_LIMIT + 1)
    except OSError as exc:
        raise ShkError(EXIT_LOCAL, "script_unreadable", f"{path}: {exc.strerror or exc}") from None
    if len(data) > SCRIPT_LIMIT:
        raise _bad(f"{path} is larger than 1 MiB")
    refused = [word for word in directives(data) if denied(word)]
    if refused:
        raise ShkError(EXIT_REFUSED, "sbatch_directive", f"{path}: #SBATCH {' '.join(refused)} is set by shk",
                       hint="remove it from the script and use the shk submit command line instead")
    return data


def _resources(args, profile) -> dict:
    if args.cpus < 1 or args.tasks < 1 or args.gpus < 0:
        raise _bad("--cpus and --tasks must be at least 1 and --gpus at least 0")
    if args.gpus and not profile.gpus_allowed:
        raise ShkError(EXIT_REFUSED, "gpus_not_allowed", f"partition {profile.name} does not allow GPUs")
    requeue = profile.requeue if args.requeue is None else args.requeue
    if requeue and not profile.requeue:
        raise ShkError(EXIT_REFUSED, "requeue_not_allowed", f"partition {profile.name} does not allow requeue")
    checks = [("constraint", args.constraint), ("signal", args.signal), *(("after_ok", j) for j in args.after_ok)]
    for key, value in checks:
        if value is not None and not GRAMMAR[key].fullmatch(value):
            raise _bad(f"--{key.replace('_', '-')} {value!r} does not match {GRAMMAR[key].pattern}")
    return {"partition": profile.name, "cpus": args.cpus, "tasks": args.tasks, "gpus": args.gpus,
            "mem_mb": memory_mb(args.mem), "minutes": minutes(args.time), "requeue": requeue,
            "array": None if args.array is None else array(args.array), "constraint": args.constraint,
            "signal": args.signal, "after_ok": list(args.after_ok)}


def sbatch_argv(name: str, run: str, r: dict) -> list[str]:
    log = f"{run}/slurm-{'%A_%a' if r['array'] else '%j'}"
    argv = ["sbatch", "--parsable", f"--job-name={name}", f"--partition={r['partition']}"]
    if r["array"]:
        throttle = r["array"]["throttle"]
        argv.append(f"--array=0-{r['array']['count'] - 1}" + (f"%{throttle}" if throttle else ""))
    argv += [f"--cpus-per-task={r['cpus']}", *(["--requeue", "--open-mode=append"] if r["requeue"] else ["--no-requeue"]),
             f"--ntasks={r['tasks']}", f"--mem={r['mem_mb']}M", f"--time={r['minutes']}", f"--chdir={run}",
             f"--output={log}.out", f"--error={log}.err", *(["-G", str(r["gpus"])] if r["gpus"] else [])]
    argv += [f"--{key}={r[key]}" for key in ("constraint", "signal") if r[key]] + [f"--export=ALL,SHK_NAME={name}"]
    after = [f"afterok:{':'.join(r['after_ok'])}"] if r["after_ok"] else []
    dependency = after + ([] if r["array"] else ["singleton"])  # singleton would serialize an array's own tasks
    return argv + ([f"--dependency={','.join(dependency)}"] if dependency else [])


def git_state(root) -> dict | None:
    def git(*argv):
        return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *argv], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20, check=False)
    try:
        head = git("rev-parse", "--verify", "HEAD") if root else None
        if head is None or head.returncode:
            return None
        dirty = git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"head": head.stdout.strip(), "dirty": bool(dirty.stdout.strip()) or dirty.returncode != 0}


@dataclass(frozen=True)
class Spec:
    name: str
    run_dir: str
    argv: list
    script: bytes
    sha256: str
    profile: partitions.Profile
    document: dict

    @property
    def text(self) -> str:
        return canonical(self.document)


def build(args, config, now) -> Spec:
    campaign, task = checked_text("campaign", args.campaign), checked_text("task", args.task)
    name, profile = job_name(config.project, campaign, task), partitions.profile(args.partition)
    resources, run = _resources(args, profile), run_directory(args.run_dir, config, name)
    script = _script(args.script)
    if profile.borrowed and not args.courtesy_ack:
        raise ShkError(EXIT_REFUSED, "courtesy_ack_required", f"{profile.name} is borrowed: {profile.courtesy}",
                       hint=f"run shk occupancy --partition {profile.name}, then add --courtesy-ack")
    argv, digest = sbatch_argv(name, run, resources), hashlib.sha256(script).hexdigest()
    document = {"schema": 1, "name": name, "project": config.project, "campaign": campaign, "task": task,
                "run_dir": run, "resources": resources, "argv": argv, "resubmit_reason": args.resubmit,
                "script": {"name": Path(args.script).name, "sha256": digest}, "shk": version_line(),
                "git": git_state(config.root), "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return Spec(name, run, argv, script, digest, profile, document)
