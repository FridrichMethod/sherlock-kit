"""Run literal argv on Sherlock: OpenSSH from a workstation, or locally on Sherlock (frozen 0.4.0).

Result.status: complete | failed (read exit != 0) | unknown (mutation exit != 0 or timeout
after dispatch: never retry blind) | auth_required / unavailable (ssh 255 with / without
auth text, or no ssh) | timeout (read). Banner lines are dropped; a timeout kills the
child's process group. SshExecutor / LocalExecutor: .kind, .host, .run(argv, *, stdin,
mutation, timeout) with literal argv (ssh: ONE shlex.join'ed remote command) and
.rsync_args(path) -> (["-e", cmd], "host:path") (local: ShkError 3, never ssh to a DTN
from Sherlock). Every dial follows `ssh -O check -oBatchMode=yes HOST` (local socket):
no master -> ShkError(4, "auth_required", hint "ssh HOST" / "ssh -fN HOST"), no usable
ControlPath -> ShkError(4, "unavailable"); -oProxyCommand=false makes a master lost
after the check fail instead of a cold login. master_status() -> (state, detail).
run_program(ex, name, args) runs ["sh", "-c", data/remote/<name>.sh, "shk-<name>", *args];
check(result, action) raises ShkError 4, 5 (unknown_outcome) or 1 unless complete.
detect(): --executor / SHK_EXECUTOR, else local on ^sh\\d+-ln\\d+$, SLURM_CLUSTER_NAME=sherlock
or ^sh\\d+- in a job (never "sbatch on PATH"), else ssh; data=True on Sherlock always refuses.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from importlib import resources
import os
import re
import shlex
import signal
import socket
import subprocess

from sherlock_kit.config import DEFAULTS, HOST
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, EXIT_UNKNOWN, EXIT_UNREACHABLE, ShkError

BANNER = "Automated tools: read and follow /etc/agents/AGENTS.md"
AUTH_TEXT = ("permission denied", "authentication failed", "no supported authentication",
             "too many authentication failures", "credentials cache", "ticket expired")
DIAL = ("-T", "-oBatchMode=yes", "-oRemoteCommand=none", "-oConnectTimeout={}", "-oConnectionAttempts=1",
        "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no", "-oControlMaster=no",
        "-oPermitLocalCommand=no", "-oClearAllForwardings=yes", "-oForwardAgent=no", "-oProxyCommand=false")
UNCONFIGURED = re.compile(r"no controlpath specified|controlpath too long|bad configuration option", re.I)


@dataclass(frozen=True)
class Result:
    status: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None


def _text(value) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
    return "".join(line for line in text.splitlines(keepends=True) if not line.startswith(BANNER))


def _literal(argv) -> list[str]:
    if (not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str) or not argv[0]
            or argv[0].startswith("-") or any(not isinstance(a, str) or "\0" in a for a in argv)):
        raise ValueError("argv must be a non-empty list of literal strings without NUL")
    return list(argv)


def _input(stdin) -> bytes | None:
    if not isinstance(stdin, (bytes, str, type(None))):
        raise ValueError("stdin must be bytes, str or None")
    return stdin.encode("utf-8") if isinstance(stdin, str) else stdin


def _spawn(command, data, timeout):
    """(returncode, stdout, stderr); on a timeout or interrupt the whole process group is killed first."""
    with subprocess.Popen(command, stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as child:
        try:
            out, err = child.communicate(data, timeout=timeout)
        except BaseException:
            with suppress(ProcessLookupError):  # the group is already gone, which is the goal
                os.killpg(child.pid, signal.SIGKILL)
            raise
    return child.returncode, out, err


def _execute(command, data, *, mutation, timeout, transport) -> Result:
    try:
        code, out, err = _spawn(command, data, timeout)
    except subprocess.TimeoutExpired as exc:
        return Result("unknown" if mutation else "timeout", _text(exc.stdout), _text(exc.stderr))
    except OSError as exc:
        return Result("unavailable", stderr=f"cannot start {command[0]}: {exc.strerror or exc}")
    out, err = _text(out), _text(err)
    if code == 0:
        return Result("complete", out, err, code)
    if mutation:  # a nonzero exit cannot prove that the mutation did not happen
        return Result("unknown", out, err, code)
    if transport and code == 255:
        return Result("auth_required" if any(t in err.lower() for t in AUTH_TEXT) else "unavailable", out, err, code)
    return Result("failed", out, err, code)


def master_status(host: str, ssh: str = "ssh", timeout: float = 10) -> tuple[str, str]:
    """(state, detail) from the local control socket, never a dial; logging in cures only "none"."""
    try:
        done = subprocess.run([ssh, "-O", "check", "-oBatchMode=yes", host], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, check=False)
    except OSError as exc:
        return "ssh_missing", f"cannot run {ssh}: {exc.strerror or exc}"
    except subprocess.TimeoutExpired:
        return "none", f"ssh -O check {host} timed out"
    detail = _text(done.stderr).strip()[-300:]
    if done.returncode == 0:
        return "running", detail
    return ("not_configured" if UNCONFIGURED.search(detail) else "none"), detail


def master_hint(state: str, host: str, data: bool = False) -> str:
    login = f"ssh -fN {host}" if data else f"ssh {host}"
    fix = f"set ControlMaster auto, ControlPersist yes and ControlPath ~/.ssh/%C for {host} in ~/.ssh/config, then "
    return {"none": login, "ssh_missing": "install OpenSSH"}.get(state, fix + f"run: {login}")


class SshExecutor:
    kind = "ssh"

    def __init__(self, host: str, *, data: bool = False, connect_timeout: int = 15,
                 command_timeout: float = 60, ssh: str = "ssh"):
        if not isinstance(host, str) or not HOST.fullmatch(host):
            raise ValueError(f"host {host!r} must be an OpenSSH alias without options")
        self.host, self.data, self.ssh = host, data, ssh
        self.connect_timeout, self.command_timeout = connect_timeout, command_timeout

    def preflight(self) -> None:
        state, detail = master_status(self.host, self.ssh)
        if state != "running":
            raise ShkError(EXIT_UNREACHABLE, "auth_required" if state == "none" else "unavailable",
                           f"no usable SSH master for {self.host} ({state}: {detail}); shk never logs in. "
                           "Ask the user to run the hint in a terminal, then retry once.",
                           hint=master_hint(state, self.host, self.data))

    def _prefix(self) -> list[str]:
        return [self.ssh, *(option.format(self.connect_timeout) for option in DIAL)]

    def run(self, argv, *, stdin=None, mutation=False, timeout=None) -> Result:
        command, data = [*self._prefix(), self.host, shlex.join(_literal(argv))], _input(stdin)
        self.preflight()
        return _execute(command, data, mutation=mutation, timeout=timeout or self.command_timeout, transport=True)

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
        return _execute(_literal(argv), _input(stdin), mutation=mutation, timeout=timeout or self.command_timeout,
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
    if choice == "local" or (on_sherlock and (data or choice == "auto")):  # never ssh to a DTN from Sherlock
        return LocalExecutor(command_timeout=settings.command_timeout)
    return SshExecutor(settings.data_host if data else settings.control_host, data=data,
                       connect_timeout=settings.connect_timeout, command_timeout=settings.command_timeout)
