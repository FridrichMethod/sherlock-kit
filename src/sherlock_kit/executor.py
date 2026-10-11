"""Run literal argv on Sherlock: over OpenSSH from a workstation, or locally on Sherlock (frozen 0.4.0).

Result(status, stdout, stderr, returncode), status in STATUSES: complete; failed (a
read exited nonzero); unknown (a mutation exited nonzero or timed out after
dispatch: never retry blind); auth_required / unavailable (ssh exit 255 with /
without an auth message, or no ssh); timeout (a read). Text drops the login banner.
SshExecutor(host, *, data=False, connect_timeout, command_timeout, ssh="ssh") and
LocalExecutor(*, command_timeout) share .kind ("ssh"/"local"), .host (None locally),
.run(argv, *, stdin=None, mutation=False, timeout=None): argv is literal (ssh sends
shlex.join(argv) as ONE remote command), stdin bytes/str/None; and
.rsync_args(remote_path) -> (["-e", ssh_command], "host:path"), which locally raises
ShkError(3) (never ssh to a DTN from Sherlock). Before EVERY dial ssh runs
`ssh -O check -oBatchMode=yes HOST` (local socket query); no live master raises
ShkError(4, "auth_required", hint "ssh HOST" / "ssh -fN HOST") and dials nothing.
run_program(ex, name, args=()) runs data/remote/<name>.sh as
["sh", "-c", text, "shk-<name>", *args]; keep single quotes out of programs.
check(result, action) returns a complete result or raises ShkError 4, 5
(unknown_outcome) or 1 (remote_failed). detect(config, override, env, hostname, *,
data=False): --executor / SHK_EXECUTOR first; local on a login node (^sh\\d+-ln\\d+$),
with SLURM_CLUSTER_NAME=sherlock, or on ^sh\\d+- inside a job; else ssh. Never by
"sbatch on PATH" (marlowe has Slurm).
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import os
import re
import shlex
import socket
import subprocess

from sherlock_kit.config import DEFAULTS, HOST
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, EXIT_UNKNOWN, EXIT_UNREACHABLE, ShkError

STATUSES = frozenset({"complete", "failed", "unknown", "auth_required", "unavailable", "timeout"})
BANNER = "Automated tools: read and follow /etc/agents/AGENTS.md"
AUTH_TEXT = ("permission denied", "authentication failed", "no supported authentication",
             "too many authentication failures", "credentials cache", "ticket expired")
DIAL = ("-T", "-oBatchMode=yes", "-oRemoteCommand=none", "-oConnectTimeout={}", "-oConnectionAttempts=1",
        "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no", "-oControlMaster=no",
        "-oPermitLocalCommand=no", "-oClearAllForwardings=yes", "-oForwardAgent=no")


@dataclass(frozen=True)
class Result:
    status: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown result status {self.status!r}")


def _text(value) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
    return "".join(line for line in text.splitlines(keepends=True) if not line.startswith(BANNER))


def _literal(argv) -> list[str]:
    if (not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str) or not argv[0]
            or argv[0].startswith("-") or any(not isinstance(a, str) or "\0" in a for a in argv)):
        raise ValueError("argv must be a non-empty list of literal strings without NUL")
    return list(argv)


def _stream(stdin) -> dict:
    if not isinstance(stdin, (bytes, str, type(None))):
        raise ValueError("stdin must be bytes, str or None")
    return {"stdin": subprocess.DEVNULL} if stdin is None else {
        "input": stdin.encode("utf-8") if isinstance(stdin, str) else stdin}


def _execute(command, stream, *, mutation, timeout, transport) -> Result:
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout, check=False, **stream)
    except subprocess.TimeoutExpired as exc:
        return Result("unknown" if mutation else "timeout", _text(exc.stdout), _text(exc.stderr))
    except OSError as exc:
        return Result("unavailable", stderr=f"cannot start {command[0]}: {exc.strerror or exc}")
    out, err, code = _text(done.stdout), _text(done.stderr), done.returncode
    if code == 0:
        return Result("complete", out, err, code)
    if mutation:  # a nonzero exit cannot prove that the mutation did not happen
        return Result("unknown", out, err, code)
    if transport and code == 255:
        return Result("auth_required" if any(t in err.lower() for t in AUTH_TEXT) else "unavailable", out, err, code)
    return Result("failed", out, err, code)


def master_status(host: str, ssh: str = "ssh", timeout: float = 10) -> str:
    """"running" | "none" | "ssh_missing" from the local control socket; never dials."""
    try:
        done = subprocess.run([ssh, "-O", "check", "-oBatchMode=yes", host], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, check=False)
    except OSError:
        return "ssh_missing"
    except subprocess.TimeoutExpired:
        return "none"
    return "running" if done.returncode == 0 else "none"


class SshExecutor:
    kind = "ssh"

    def __init__(self, host: str, *, data: bool = False, connect_timeout: int = 15,
                 command_timeout: float = 60, ssh: str = "ssh"):
        if not isinstance(host, str) or not HOST.fullmatch(host):
            raise ValueError(f"host {host!r} must be an OpenSSH alias without options")
        self.host, self.data, self.ssh = host, data, ssh
        self.connect_timeout, self.command_timeout = connect_timeout, command_timeout

    def preflight(self) -> None:
        state = master_status(self.host, self.ssh)
        if state == "ssh_missing":
            raise ShkError(EXIT_UNREACHABLE, "unavailable", f"cannot run {self.ssh}", hint="install OpenSSH")
        if state != "running":
            raise ShkError(EXIT_UNREACHABLE, "auth_required", f"no live SSH master for {self.host}; shk never "
                           "logs in. Ask the user to run the hint in a terminal, then retry once.",
                           hint=f"ssh -fN {self.host}" if self.data else f"ssh {self.host}")

    def _prefix(self) -> list[str]:
        return [self.ssh, *(option.format(self.connect_timeout) for option in DIAL)]

    def run(self, argv, *, stdin=None, mutation=False, timeout=None) -> Result:
        command, stream = [*self._prefix(), self.host, shlex.join(_literal(argv))], _stream(stdin)
        self.preflight()
        return _execute(command, stream, mutation=mutation, timeout=timeout or self.command_timeout, transport=True)

    def rsync_args(self, remote_path: str) -> tuple[list[str], str]:
        if not isinstance(remote_path, str) or not remote_path.startswith("/") or any(
                c in remote_path for c in "\0\n\r"):
            raise ShkError(EXIT_LOCAL, "invalid_remote_path", f"{remote_path!r} must be one absolute path")
        self.preflight()
        return ["-e", shlex.join(self._prefix())], f"{self.host}:{remote_path}"


class LocalExecutor:
    kind, host = "local", None

    def __init__(self, *, command_timeout: float = 60):
        self.command_timeout = command_timeout

    def run(self, argv, *, stdin=None, mutation=False, timeout=None) -> Result:
        return _execute(_literal(argv), _stream(stdin), mutation=mutation, timeout=timeout or self.command_timeout,
                        transport=False)

    def rsync_args(self, remote_path: str):
        raise ShkError(EXIT_REFUSED, "transfer_on_sherlock", "push/pull start from the workstation; never ssh "
                       "to a DTN from Sherlock", hint="the data is already here; copy large trees in a Slurm job")


def program_text(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise ValueError(f"bad remote program name {name!r}")
    return (resources.files("sherlock_kit") / "data" / "remote" / f"{name}.sh").read_text(encoding="utf-8")


def run_program(ex, name: str, args=(), *, stdin=None, mutation=False, timeout=None) -> Result:
    return ex.run(["sh", "-c", program_text(name), f"shk-{name}", *args], stdin=stdin, mutation=mutation,
                  timeout=timeout)


def check(result: Result, action: str) -> Result:
    tail = result.stderr.strip()[-2000:]
    if result.status == "complete":
        return result
    if result.status == "unknown":
        raise ShkError(EXIT_UNKNOWN, "unknown_outcome", f"{action}: dispatched, outcome unknown. {tail}".strip(),
                       hint="wait at least 60 s, then run shk status; never resubmit blind")
    if result.status == "failed":
        raise ShkError(EXIT_LOCAL, "remote_failed", f"{action}: exit {result.returncode}. {tail}".strip())
    raise ShkError(EXIT_UNREACHABLE, result.status, f"{action}: Sherlock unreachable. {tail}".strip(),
                   hint="run shk doctor")


def detect(config=None, override=None, env=None, hostname=None, *, data=False):
    env, settings = os.environ if env is None else env, config or DEFAULTS
    choice = override if override not in (None, "auto") else (env.get("SHK_EXECUTOR") or "").strip().lower()
    choice = choice or "auto"
    if choice not in ("auto", "local", "ssh"):
        raise ShkError(EXIT_LOCAL, "executor_invalid", f"executor {choice!r} (--executor or SHK_EXECUTOR) "
                       "must be auto, local or ssh")
    short = (socket.gethostname() if hostname is None else hostname).split(".")[0].lower()
    on_sherlock = (re.fullmatch(r"sh\d+-ln\d+", short) or env.get("SLURM_CLUSTER_NAME") == "sherlock"
                   or (re.match(r"sh\d+-", short) and env.get("SLURM_JOB_ID")))
    if choice == "local" or (choice == "auto" and on_sherlock):
        return LocalExecutor(command_timeout=settings.command_timeout)
    return SshExecutor(settings.data_host if data else settings.control_host, data=data,
                       connect_timeout=settings.connect_timeout, command_timeout=settings.command_timeout)
