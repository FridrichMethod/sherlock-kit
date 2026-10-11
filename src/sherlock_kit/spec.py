"""Offline submit checks, the job name, the sbatch argv and the frozen job spec.

Nothing here touches the network. build() raises ShkError with exit 3 for an unlisted
partition, GPUs or requeue its profile forbids, a denylisted option in the leading #SBATCH
block, or a borrowed partition without --courtesy-ack; exit 1 (spec_invalid,
script_unreadable) for bad grammar, an unreadable script or one over 1 MiB.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

from sherlock_kit import partitions, version_line
from sherlock_kit.config import ProjectConfig
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, ShkError

SCRIPT_LIMIT = 1 << 20  # bytes
ARRAY_MAX_TASKS = 1000  # Sherlock max_array_tasks (2026-10-10); indices are 0..count-1
NAME = re.compile(r"shk\.[a-z0-9][a-z0-9-]{0,23}\.[a-z0-9-]{1,20}\.[0-9a-f]{10}")
TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,128}")  # --task and --campaign
RUN_DIR = re.compile(r"/[A-Za-z0-9_./-]+")
MEMORY = re.compile(r"([1-9][0-9]*)([MG])", re.IGNORECASE)
DURATION = re.compile(r"([1-9][0-9]*)([hm])")
CLOCK_TIME = re.compile(r"([0-9]+):([0-5][0-9]):([0-5][0-9])")
ARRAY = re.compile(r"([0-9]+)(?:%([0-9]+))?")
OPTION_GRAMMAR = {
    "constraint": re.compile(r"[A-Za-z0-9_.:-]+(?:[&|][A-Za-z0-9_.:-]+)*"),
    "signal": re.compile(r"(?:B:)?(?:USR1|USR2|TERM)@[1-9][0-9]*"),
    "after-ok": re.compile(r"[1-9][0-9]*(?:_[0-9]+)?"),
}
# #SBATCH options that shk sets on the command line or that would bypass the profile.
DENIED_LONG = (
    "account", "exclude", "gres", "gpus", "partition", "array", "dependency", "begin", "job-name",
)
DENIED_SHORT = "AxGpadJ"
FLAGS_WITHOUT_VALUE = "HhOQsuvVW"  # sbatch short flags that may be bundled before another one


@dataclass(frozen=True)
class Array:
    count: int
    throttle: int | None


@dataclass(frozen=True)
class Resources:
    partition: str
    cpus: int
    tasks: int
    gpus: int
    mem_mb: int
    minutes: int
    requeue: bool
    array: Array | None
    constraint: str | None
    signal: str | None
    after_ok: tuple[str, ...]


@dataclass(frozen=True)
class Spec:
    name: str
    run_dir: str
    argv: list[str]
    script: bytes
    sha256: str
    profile: partitions.Profile
    document: dict[str, Any]

    @property
    def text(self) -> str:
        """The frozen spec as one canonical JSON line, staged beside the script."""
        return canonical(self.document)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _invalid(message: str) -> ShkError:
    return ShkError(EXIT_LOCAL, "spec_invalid", message)


def checked_text(label: str, value: str) -> str:
    """A --task or --campaign value: 1-128 printable characters."""
    if not TEXT.fullmatch(value):
        raise _invalid(f"--{label} must be 1-128 printable characters")
    return value


def slug(text: str) -> str:
    """Lowercase [a-z0-9-] form of a task, at most 20 characters; "x" when nothing is left."""
    dashed = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return dashed[:20].rstrip("-") or "x"


def job_name(project: str, campaign: str, task: str) -> str:
    """shk.<project>.<slug(task)>.<10 hex of sha256([project, campaign, task])>, <= 60 chars."""
    digest = hashlib.sha256(canonical([project, campaign, task]).encode()).hexdigest()
    return f"shk.{project}.{slug(task)}.{digest[:10]}"


def memory_mb(text: str) -> int:
    """--mem 16G or 16000M in megabytes (G = 1024 M)."""
    match = MEMORY.fullmatch(text)
    if match is None:
        raise _invalid(f"--mem {text!r}: use 16G or 16000M")
    return int(match[1]) * (1024 if match[2].upper() == "G" else 1)


def _seconds(text: str) -> int:
    if duration := DURATION.fullmatch(text):
        return int(duration[1]) * (3600 if duration[2] == "h" else 60)
    if clock := CLOCK_TIME.fullmatch(text):
        hours, mins, secs = (int(part) for part in clock.groups())
        return hours * 3600 + mins * 60 + secs
    return 0


def minutes(text: str) -> int:
    """--time 2h, 90m or H:MM:SS, rounded up to whole minutes."""
    seconds = _seconds(text)
    if seconds == 0:
        raise _invalid(f"--time {text!r}: use 2h, 90m or H:MM:SS (above zero)")
    return -(-seconds // 60)


def array(text: str) -> Array:
    """--array N[%M] with 2 <= N <= 1000 tasks and a throttle 1 <= M <= N."""
    match = ARRAY.fullmatch(text)
    count = int(match[1]) if match else 0
    throttle = int(match[2]) if match and match[2] else None
    if not 2 <= count <= ARRAY_MAX_TASKS or (throttle is not None and not 1 <= throttle <= count):
        raise _invalid(f"--array {text!r}: N[%M] with 2 <= N <= {ARRAY_MAX_TASKS} and 1 <= M <= N")
    return Array(count, throttle)


def run_directory(value: str | None, config: ProjectConfig, name: str) -> str:
    """--run-dir, else <remote_root>/runs/<name>: absolute, [A-Za-z0-9_./-] only, no '..'."""
    run = (value or f"{config.remote_root}/runs/{name}").rstrip("/")
    if not RUN_DIR.fullmatch(run) or ".." in run.split("/"):
        raise _invalid(f"--run-dir {run!r} must be absolute, [A-Za-z0-9_./-] only, without '..'")
    return run


def _directive_words(text: str) -> list[str]:
    try:
        return shlex.split(text, comments=True)
    except ValueError:  # an unbalanced quote: plain words can only refuse more, never less
        return text.split()


def directives(script: bytes) -> list[str]:
    """Words of the leading #SBATCH block; like sbatch, stop at the first non-comment line."""
    words: list[str] = []
    for line in script.decode("utf-8", "replace").splitlines():
        if line.startswith("#SBATCH"):
            words += _directive_words(line[len("#SBATCH"):])
        elif line.strip() and not line.lstrip().startswith("#"):
            break
    return words


def denied(word: str) -> bool:
    """Whether one #SBATCH word sets a denylisted option (-pgpu, -Hp, --part=gpu, --gpus=1)."""
    if word.startswith("--"):  # getopt_long also accepts unambiguous prefixes such as --part
        option = word[2:].partition("=")[0]
        if not option:
            return False
        return option.startswith("gpus") or any(name.startswith(option) for name in DENIED_LONG)
    if word.startswith("-"):
        flags = word[1:].lstrip(FLAGS_WITHOUT_VALUE)
        return bool(flags) and flags[0] in DENIED_SHORT
    return False


def read_script(path: str) -> bytes:
    """The job script bytes: at most 1 MiB, without a denylisted #SBATCH option."""
    try:
        with open(path, "rb") as stream:
            data = stream.read(SCRIPT_LIMIT + 1)
    except OSError as exc:
        raise ShkError(EXIT_LOCAL, "script_unreadable", f"{path}: {exc.strerror or exc}") from None
    if len(data) > SCRIPT_LIMIT:
        raise _invalid(f"{path} is larger than 1 MiB")
    refused = [word for word in directives(data) if denied(word)]
    if refused:
        raise ShkError(EXIT_REFUSED, "sbatch_directive",
                       f"{path}: #SBATCH {' '.join(refused)} is set by shk",
                       hint="remove it from the script and use the shk submit command line instead")
    return data


def _check_option_grammar(args: argparse.Namespace) -> None:
    values = [("constraint", args.constraint), ("signal", args.signal)]
    values += [("after-ok", job) for job in args.after_ok]
    for flag, value in values:
        if value is not None and not OPTION_GRAMMAR[flag].fullmatch(value):
            raise _invalid(f"--{flag} {value!r} does not match {OPTION_GRAMMAR[flag].pattern}")


def resources(args: argparse.Namespace, profile: partitions.Profile) -> Resources:
    """Checked resources; GPUs and requeue follow the profile (requeue defaults to it)."""
    if args.cpus < 1 or args.tasks < 1 or args.gpus < 0:
        raise _invalid("--cpus and --tasks must be at least 1 and --gpus at least 0")
    if args.gpus and not profile.gpus_allowed:
        raise ShkError(EXIT_REFUSED, "gpus_not_allowed",
                       f"partition {profile.name} does not allow GPUs")
    requeue = profile.requeue if args.requeue is None else args.requeue
    if requeue and not profile.requeue:
        raise ShkError(EXIT_REFUSED, "requeue_not_allowed",
                       f"partition {profile.name} does not allow requeue")
    _check_option_grammar(args)
    return Resources(
        partition=profile.name,
        cpus=args.cpus,
        tasks=args.tasks,
        gpus=args.gpus,
        mem_mb=memory_mb(args.mem),
        minutes=minutes(args.time),
        requeue=requeue,
        array=None if args.array is None else array(args.array),
        constraint=args.constraint,
        signal=args.signal,
        after_ok=tuple(args.after_ok),
    )


def sbatch_argv(name: str, run: str, wanted: Resources) -> list[str]:
    """sbatch options in the fixed 0.4.0 order; submit.sh appends the staged script path."""
    log = f"{run}/slurm-{'%A_%a' if wanted.array else '%j'}"
    argv = ["sbatch", "--parsable", f"--job-name={name}", f"--partition={wanted.partition}"]
    if wanted.array:
        throttle = f"%{wanted.array.throttle}" if wanted.array.throttle else ""
        argv.append(f"--array=0-{wanted.array.count - 1}{throttle}")
    argv.append(f"--cpus-per-task={wanted.cpus}")
    argv += ["--requeue", "--open-mode=append"] if wanted.requeue else ["--no-requeue"]
    argv += [
        f"--ntasks={wanted.tasks}",
        f"--mem={wanted.mem_mb}M",
        f"--time={wanted.minutes}",
        f"--chdir={run}",
        f"--output={log}.out",
        f"--error={log}.err",
    ]
    if wanted.gpus:
        argv += ["-G", str(wanted.gpus)]
    if wanted.constraint:
        argv.append(f"--constraint={wanted.constraint}")
    if wanted.signal:
        argv.append(f"--signal={wanted.signal}")
    argv.append(f"--export=ALL,SHK_NAME={name}")
    dependency = [f"afterok:{':'.join(wanted.after_ok)}"] if wanted.after_ok else []
    if wanted.array is None:  # singleton is for single jobs only; arrays have no backstop
        dependency.append("singleton")
    if dependency:
        argv.append(f"--dependency={','.join(dependency)}")
    return argv


def _git(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *argv],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
                          check=False)


def git_state(root: Path | None) -> dict[str, Any] | None:
    """HEAD and a dirty flag (tracked files) of the project checkout; None outside git."""
    if root is None:
        return None
    try:
        head = _git(root, "rev-parse", "--verify", "HEAD")
        if head.returncode != 0:
            return None
        status = _git(root, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.TimeoutExpired):  # no git binary, or a very slow checkout
        return None
    return {"head": head.stdout.strip(),
            "dirty": status.returncode != 0 or bool(status.stdout.strip())}


def build(args: argparse.Namespace, config: ProjectConfig, now: datetime) -> Spec:
    """All offline checks of shk submit, then the sbatch argv and the frozen spec document."""
    project = config.project
    assert project is not None, "the submit handler requires project before building"
    campaign, task = checked_text("campaign", args.campaign), checked_text("task", args.task)
    name = job_name(project, campaign, task)
    profile = partitions.profile(args.partition)
    wanted = resources(args, profile)
    run = run_directory(args.run_dir, config, name)
    script = read_script(args.script)
    if profile.borrowed and not args.courtesy_ack:
        raise ShkError(EXIT_REFUSED, "courtesy_ack_required",
                       f"{profile.name} is borrowed: {profile.courtesy}",
                       hint=f"run shk occupancy --partition {profile.name}, "
                       "then add --courtesy-ack")
    argv, digest = sbatch_argv(name, run, wanted), hashlib.sha256(script).hexdigest()
    document = {
        "schema": 1,
        "name": name,
        "project": project,
        "campaign": campaign,
        "task": task,
        "run_dir": run,
        "resources": asdict(wanted),
        "argv": argv,
        "script": {"name": Path(args.script).name, "sha256": digest},
        "resubmit_reason": args.resubmit,
        "shk": version_line(),
        "git": git_state(config.root),
        "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return Spec(name, run, argv, script, digest, profile, document)
