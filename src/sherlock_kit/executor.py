r"""Run argv on a site: OpenSSH from a workstation, or locally on the cluster (frozen 0.4.0).

Result.status is complete, failed (a read exited nonzero), unknown (a mutation exited nonzero
or timed out after dispatch: never retry it blind), auth_required or unavailable (ssh exit 255
with or without authentication text, or no ssh) or timeout (a read). Banner lines leave the
parsed output and go to stderr once each (site rule 12: never filter out instruction reminders),
and a timeout or an interrupt kills the child's whole process group.

Every ssh dial follows `ssh -O check -oBatchMode=yes HOST`, which only asks the local control
socket: no master is ShkError(4, "auth_required") with the hint `ssh HOST` (`ssh -fN HOST` for
a data host other than the control host), a client config without a usable ControlPath is
ShkError(4, "unavailable") with a ~/.ssh/config hint, and -oProxyCommand=false makes a master
lost after the check fail instead of logging in. An ssh run sends ONE shlex-joined remote
command.

Each executor knows its site (sites.Site, Sherlock by default). run_program() sends
["sh", "-c", <program>, "shk-NAME", *args] for a site whose Slurm clients are on the login PATH
(Sherlock), and ["sh", "-c", <site-env.sh> + <program>, "shk-NAME", BIN, CONF, *args] for a site
with a Slurm client environment (Marlowe); the Result names the site, whose display name and
selector check() puts in its messages and hints.

detect() picks the executor for the site of the configuration (Sherlock without one):
--executor or SHK_EXECUTOR; else local where the site's on_cluster(hostname, env) holds; else
ssh to the control host (or with data=True the data host). On the cluster the data executor is
always local, and its rsync_args() refuses: transfers start outside the cluster, and on
Sherlock never ssh to a DTN from Sherlock. A forced local executor where the site's on_cluster
fails and another packaged site's holds is ShkError(1, "site_mismatch"): this machine belongs
to that other site, whose Slurm would answer for the selected one.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from importlib import resources
from typing import TypeAlias

from sherlock_kit import sites
from sherlock_kit.config import DEFAULTS, ProjectConfig
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, EXIT_UNKNOWN, EXIT_UNREACHABLE, ShkError

BANNER = "Automated tools: read and follow /etc/agents/AGENTS.md"
_reminded: set[str] = set()  # banner lines this process already wrote to stderr
AUTH_TEXT = (
    "permission denied",
    "authentication failed",
    "no supported authentication",
    "too many authentication failures",
    "credentials cache",
    "ticket expired",
)
DIAL = (
    "-T",
    "-oBatchMode=yes",
    "-oRemoteCommand=none",
    "-oConnectTimeout={}",
    "-oConnectionAttempts=1",
    "-oStrictHostKeyChecking=yes",
    "-oUpdateHostKeys=no",
    "-oControlMaster=no",
    "-oPermitLocalCommand=no",
    "-oClearAllForwardings=yes",
    "-oForwardAgent=no",
    "-oProxyCommand=false",
)
UNCONFIGURED = re.compile(
    r"no controlpath specified|controlpath too long|bad configuration option", re.I
)
LOCAL_DENIAL = re.compile(r"permission denied|operation not permitted", re.I)
NO_MASTER = re.compile(r"control socket connect\(.*\): (?:no such file|connection refused)", re.I)


@dataclass(frozen=True)
class Result:
    """What one run of a program produced; status is described in the module docstring."""

    status: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    site: sites.Site = sites.SHERLOCK  # where the program ran; run_program() sets it


def _text(value: bytes | str | None) -> str:
    """Decoded output without Sherlock's login banner lines, each written once to stderr."""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
    kept = []
    for line in re.split(r"(?<=\n)", text):  # LF ends a line; a VT or a NEL is ordinary text
        if not line.startswith(BANNER):
            kept.append(line)
        elif (reminder := line.rstrip("\r\n")) not in _reminded:
            _reminded.add(reminder)
            print(reminder, file=sys.stderr)
    return "".join(kept)


def records(reply: str) -> list[str]:
    """The records of a remote program's reply, which LF alone separates.

    str.splitlines() also breaks at CR, VT, FF, FS, GS, RS, NEL, LS and PS, so a job name, a
    comment or a spec file holding one of them could add records such as SHK_END to a reply.
    """
    return reply.removesuffix("\n").split("\n") if reply else []


def _spawn(command: Sequence[str], data: bytes | None, timeout: float) -> tuple[int, bytes, bytes]:
    """(returncode, stdout, stderr); a timeout or an interrupt kills the process group first."""
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as child:
        try:
            out, err = child.communicate(data, timeout=timeout)
        except BaseException:
            with suppress(ProcessLookupError):  # the group is already gone, which is the goal
                os.killpg(child.pid, signal.SIGKILL)
            raise
    return child.returncode, out, err


def _execute(
    command: Sequence[str],
    data: bytes | None,
    *,
    mutation: bool,
    timeout: float,
    transport: bool,
) -> Result:
    """Run command and map what happened to a Result status."""
    try:
        code, out_bytes, err_bytes = _spawn(command, data, timeout)
    except subprocess.TimeoutExpired as exc:
        return Result("unknown" if mutation else "timeout", _text(exc.stdout), _text(exc.stderr))
    except OSError as exc:
        return Result("unavailable", stderr=f"cannot start {command[0]}: {exc.strerror or exc}")
    out, err = _text(out_bytes), _text(err_bytes)
    if code == 0:
        return Result("complete", out, err, code)
    if mutation:  # a nonzero exit cannot prove that the mutation did not happen
        return Result("unknown", out, err, code)
    if transport and code == 255:
        authentication = any(text in err.lower() for text in AUTH_TEXT)
        return Result("auth_required" if authentication else "unavailable", out, err, code)
    return Result("failed", out, err, code)


def master_status(host: str, ssh: str = "ssh", timeout: float = 10) -> tuple[str, str]:
    """(state, detail) from the local control socket, never a dial; logging in cures only "none".

    The state is running, none, not_configured (no usable ControlPath), ssh_missing,
    inaccessible (local permission denial), timeout or unavailable (another check failure).
    """
    try:
        done = subprocess.run(
            [ssh, "-O", "check", "-oBatchMode=yes", host],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except PermissionError as exc:
        return "inaccessible", f"cannot run {ssh}: {exc.strerror or exc}"
    except FileNotFoundError as exc:
        return "ssh_missing", f"cannot run {ssh}: {exc.strerror or exc}"
    except OSError as exc:
        return "unavailable", f"cannot run {ssh}: {exc.strerror or exc}"
    except subprocess.TimeoutExpired:
        return "timeout", f"ssh -O check {host} timed out"
    detail = _text(done.stderr).strip()[-300:]
    if done.returncode == 0:
        return "running", detail
    if LOCAL_DENIAL.search(detail):
        return "inaccessible", detail
    if UNCONFIGURED.search(detail):
        return "not_configured", detail
    return ("none" if NO_MASTER.search(detail) else "unavailable"), detail


def master_hint(
    state: str, host: str, data: bool = False, site: sites.Site = sites.SHERLOCK
) -> str:
    """What the user should do about a master state other than running."""
    login = f"ssh -fN {host}" if data else f"ssh {host}"
    if state == "none":
        return login
    if state == "ssh_missing":
        return "install OpenSSH"
    if state == "inaccessible":
        return (
            "local SSH access is blocked; check sandbox and filesystem permissions; "
            "in Codex request per-command approval for this shk command, then retry once; "
            "do not disable the sandbox or reopen the master solely for this error"
        )
    if state in {"timeout", "unavailable"}:
        return (
            f"run {sites.command(site)} doctor; inspect the local SSH check failure before "
            "requesting a new login"
        )
    persist = "ControlPersist yes" if site.name == sites.SHERLOCK.name else "ControlPersist"
    return (  # off Sherlock no value: Marlowe's Connecting page suggests a finite one
        f"set ControlMaster auto, {persist} and ControlPath ~/.ssh/%C for {host} "
        f"in ~/.ssh/config, then run: {login}"
    )


class SshExecutor:
    """Runs argv on a host of a site through the user's OpenSSH control master; never logs in."""

    kind = "ssh"

    def __init__(
        self,
        host: str,
        *,
        data: bool = False,
        connect_timeout: int = 15,
        command_timeout: float = 60,
        ssh: str = "ssh",
        site: sites.Site = sites.SHERLOCK,
    ) -> None:
        """Use the OpenSSH alias host; data marks the data host, whose login hint is ssh -fN."""
        self.host = host
        self.data = data
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.ssh = ssh
        self.site = site

    def preflight(self) -> None:
        """Raise ShkError 4 unless `ssh -O check` finds a running master; nothing is dialled."""
        state, detail = master_status(self.host, self.ssh)
        if state != "running":
            instruction = (
                " Ask the user to run the hint in a terminal, then retry once."
                if state in {"none", "not_configured"}
                else ""
            )
            raise ShkError(
                EXIT_UNREACHABLE,
                "auth_required" if state == "none" else "unavailable",
                f"no usable SSH master for {self.host} ({state}: {detail}); shk never logs in."
                f"{instruction}",
                hint=master_hint(state, self.host, self.data, self.site),
            )

    def _prefix(self) -> list[str]:
        return [self.ssh, *(option.format(self.connect_timeout) for option in DIAL)]

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        mutation: bool = False,
        timeout: float | None = None,
    ) -> Result:
        """Run argv on the host as one shlex-joined remote command."""
        self.preflight()
        command = [*self._prefix(), self.host, shlex.join(argv)]
        timeout = timeout or self.command_timeout
        return _execute(command, stdin, mutation=mutation, timeout=timeout, transport=True)

    def rsync_args(self, remote_path: str) -> tuple[list[str], str]:
        """(["-e", ssh command], "host:remote_path") for rsync, after the master preflight."""
        self.preflight()
        return ["-e", shlex.join(self._prefix())], f"{self.host}:{remote_path}"


class LocalExecutor:
    """Runs argv directly on the cluster of a site (a login node or inside a job)."""

    kind = "local"

    def __init__(self, *, command_timeout: float = 60, site: sites.Site = sites.SHERLOCK) -> None:
        """Use command_timeout seconds unless a run passes its own timeout."""
        self.command_timeout = command_timeout
        self.site = site

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        mutation: bool = False,
        timeout: float | None = None,
    ) -> Result:
        """Run argv here."""
        timeout = timeout or self.command_timeout
        return _execute(argv, stdin, mutation=mutation, timeout=timeout, transport=False)

    def rsync_args(self, remote_path: str) -> tuple[list[str], str]:
        """Always refuses (exit 3): transfers start outside the cluster."""
        if self.site.name == sites.SHERLOCK.name:
            raise ShkError(
                EXIT_REFUSED,
                "transfer_on_sherlock",
                f"push and pull of {remote_path} start from the workstation; never ssh to a DTN "
                "from Sherlock",
                hint="the data is already here; copy large trees in a Slurm job",
            )
        display = self.site.display
        raise ShkError(
            EXIT_REFUSED,
            f"transfer_on_{self.site.name}",
            f"push and pull of {remote_path}: shk transfers to and from {display} start outside "
            f"{display} (a toolkit choice)",
            hint=f"the data is already on {display}: copy it there with cp or rsync, without shk",
        )


Executor: TypeAlias = SshExecutor | LocalExecutor


def program_text(name: str) -> str:
    """The text of the packaged remote program data/remote/<name>.sh."""
    return (resources.files("sherlock_kit") / "data" / "remote" / f"{name}.sh").read_text(
        encoding="utf-8"
    )


def run_program(
    ex: Executor,
    name: str,
    args: Sequence[str] = (),
    *,
    stdin: bytes | None = None,
    mutation: bool = False,
    timeout: float | None = None,
) -> Result:
    """Run the packaged program on the executor's site; the module docstring gives the argv."""
    slurm = ex.site.slurm
    if slurm is None:
        argv = ["sh", "-c", program_text(name), f"shk-{name}", *args]
    else:
        text = program_text("site-env") + program_text(name)
        argv = ["sh", "-c", text, f"shk-{name}", slurm.bin, slurm.conf, *args]
    result = ex.run(argv, stdin=stdin, mutation=mutation, timeout=timeout)
    return replace(result, site=ex.site)


def check(result: Result, action: str) -> Result:
    """The result when complete; else ShkError 5 (unknown), 1 (failed) or 4 (unreachable)."""
    if result.status == "complete":
        return result
    tail, shk = result.stderr.strip()[-2000:], sites.command(result.site)
    if result.status == "unknown":
        raise ShkError(
            EXIT_UNKNOWN,
            "unknown_outcome",
            f"{action}: dispatched, outcome unknown. {tail}".strip(),
            hint=f"wait at least 60 s, then run {shk} status; never resubmit blind",
        )
    if result.status == "failed":
        raise ShkError(
            EXIT_LOCAL, "remote_failed", f"{action}: exit {result.returncode}. {tail}".strip()
        )
    raise ShkError(
        EXIT_UNREACHABLE,
        result.status,
        f"{action}: {result.site.display} unreachable. {tail}".strip(),
        hint=f"run {shk} doctor",
    )


def detect(
    config: ProjectConfig | None = None,
    override: str | None = None,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
    *,
    data: bool = False,
) -> Executor:
    """The executor for the control host (or with data=True the data host); see the module."""
    env = os.environ if env is None else env
    settings = config or DEFAULTS
    site = sites.site(settings.site)
    choice = override if override not in (None, "auto") else env.get("SHK_EXECUTOR", "")
    choice = (choice or "").strip().lower() or "auto"
    if choice not in ("auto", "local", "ssh"):
        raise ShkError(
            EXIT_LOCAL,
            "executor_invalid",
            f"executor {choice!r} (--executor or SHK_EXECUTOR) must be auto, local or ssh",
        )
    machine = socket.gethostname() if hostname is None else hostname
    here = site.on_cluster(machine, env)
    if choice == "local" and not here:
        for other in sites.SITES.values():
            if other.name != site.name and other.on_cluster(machine, env):
                raise ShkError(
                    EXIT_LOCAL,
                    "site_mismatch",
                    f"this machine belongs to {other.display}, but the site is {site.display}: "
                    f"--executor local (or SHK_EXECUTOR=local) would treat {other.display}'s "
                    f"Slurm as {site.display}'s",
                    hint=f"pass --site {other.name}, or --project-dir DIR where DIR holds the "
                    f"{other.display} configuration",
                )
    if choice == "local" or (here and (data or choice == "auto")):  # transfers start outside
        return LocalExecutor(command_timeout=settings.command_timeout, site=site)
    return SshExecutor(
        settings.data_host if data else settings.control_host,
        data=data and settings.data_host != settings.control_host,
        connect_timeout=settings.connect_timeout,
        command_timeout=settings.command_timeout,
        site=site,
    )
