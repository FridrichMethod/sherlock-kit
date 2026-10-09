"""Shared policy, literal OpenSSH argv, and a fixed read-only diagnostic inventory.

Remote argv requires a POSIX-compatible login shell. This transport does not certify
the supplied program as read-only, authorize a mutation, or retry an unknown result.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
from importlib import resources
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import time

from sherlock_partitions import partitions_sha256

SCHEMA_VERSION = 1
BEGIN = "<!-- SHERLOCK-KIT:BEGIN -->"
END = "<!-- SHERLOCK-KIT:END -->"
_SOURCE_BEGIN = "<!-- SHERLOCK-KIT:PROJECTION:BEGIN -->"
_SOURCE_END = "<!-- SHERLOCK-KIT:PROJECTION:END -->"
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*\Z")


@dataclass(frozen=True)
class TransportConfig:
    control_host: str = "sherlock-plain"
    data_host: str = "sherlock-dtn"
    connect_timeout_seconds: int = 15
    command_timeout_seconds: float = 60
    ssh_binary: str = "ssh"
    auth_backoff_seconds: float = 300
    backoff_file: str | Path | None = None

    def __post_init__(self):
        for host in (self.control_host, self.data_host):
            if not isinstance(host, str) or not _HOST.fullmatch(host):
                raise ValueError("Host must be an OpenSSH alias or user@hostname without options")
        if self.control_host == self.data_host:
            raise ValueError("Control and data endpoints must be distinct")
        for name in ("connect_timeout_seconds", "command_timeout_seconds", "auth_backoff_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not isinstance(self.connect_timeout_seconds, int):
            raise ValueError("connect_timeout_seconds must be an integer")
        if not isinstance(self.ssh_binary, str) or not self.ssh_binary or "\0" in self.ssh_binary:
            raise ValueError("ssh_binary must name an executable")
        # Validate without creating state, including an explicitly set empty root.
        _backoff_path(self)


@dataclass(frozen=True)
class RemoteResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    dispatched: bool = False
    retry_performed: bool = False


def _ssh_prefix(config):
    return [config.ssh_binary, "-T", "-oBatchMode=yes", "-oRemoteCommand=none",
            f"-oConnectTimeout={config.connect_timeout_seconds}", "-oConnectionAttempts=1",
            "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no", "-oControlMaster=no",
            "-oPermitLocalCommand=no", "-oClearAllForwardings=yes", "-oForwardAgent=no"]


def ssh_argv(config: TransportConfig, argv: list[str] | tuple[str, ...]) -> list[str]:
    """Serialize literal remote arguments; never interpolate shell fragments."""
    if not isinstance(config, TransportConfig):
        raise ValueError("config must be TransportConfig")
    if (not isinstance(argv, (list, tuple)) or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not argv[0] or argv[0].startswith("-")):
        raise ValueError("Remote command requires nonempty literal argv without NUL")
    return [*_ssh_prefix(config), config.control_host, shlex.join(argv)]


def _text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


def _execute(config, command, *, mutation=False):
    try:
        process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=config.command_timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        return RemoteResult("unknown" if mutation else "timeout", _text(exc.stdout),
                            _text(exc.stderr), dispatched=True)
    except OSError:
        return RemoteResult("not_sent", stderr="OpenSSH could not be started")
    stdout, stderr = _text(process.stdout), _text(process.stderr)
    if process.returncode == 0:
        status = "complete"
    elif mutation:
        # Nonzero transport exit cannot prove remote mutation was rejected.
        status = "unknown"
    elif process.returncode == 255:
        status = "auth_required" if auth_failure(stderr) else "unavailable"
    else:
        status = "failed"
    return RemoteResult(status, stdout, stderr, process.returncode, dispatched=True)


def auth_failure(stderr: str) -> bool:
    """Whether diagnostic text names an authentication failure.

    Text alone is not proof: a remote program's own "Permission denied" is relayed
    verbatim by OpenSSH. Callers arm the shared cooldown only together with the
    transport exit status 255.
    """
    return any(fragment in stderr.lower() for fragment in (
        "permission denied", "authentication failed", "no supported authentication",
        "too many authentication failures", "credentials cache", "ticket expired"))


def _backoff_path(config):
    """Explicit file, environment root, then XDG default; never create state."""
    if config.backoff_file is not None:
        path = Path(config.backoff_file).expanduser()
    elif "SHERLOCK_KIT_STATE_ROOT" in os.environ:
        value = os.environ["SHERLOCK_KIT_STATE_ROOT"]
        if (not value or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or not Path(value).is_absolute() or ".." in Path(value).parts):
            raise ValueError("SHERLOCK_KIT_STATE_ROOT must be a nonempty absolute path")
        path = Path(value) / "auth-backoff.json"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        path = root / "sherlock-kit/auth-backoff.json"
    return _checked_backoff_path(path)


def _checked_backoff_path(path):
    if any(ord(c) < 32 or ord(c) == 127 for c in str(path)) or ".." in path.parts:
        raise ValueError("Authentication state path must not contain control characters or parent traversal")
    path = path.absolute()
    # Refuse links before mkdir/read, including a linked ancestor above the
    # immediate parent. Do not resolve them and silently select another root.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Authentication state path and ancestors must not be symlinks")
    try:
        info = path.parent.lstat()
    except FileNotFoundError:
        return path
    # Read-only local diagnostics also run on Windows, where getuid and POSIX
    # mode/ownership guarantees are unavailable. Mutating locks remain POSIX.
    getuid = getattr(os, "getuid", None)
    if (not stat.S_ISDIR(info.st_mode) or (getuid is not None
            and (info.st_uid != getuid() or stat.S_IMODE(info.st_mode) & 0o077))):
        raise ValueError("Authentication state parent must be an owned private directory")
    return path


def _read_backoff(path):
    path = _checked_backoff_path(path)
    try:
        if path.is_symlink():
            raise ValueError("Backoff file must not be a symlink")
        info = path.stat()
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise ValueError("Reading authentication state requires POSIX ownership metadata")
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("Backoff state must be an owned private regular file")
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return 0.0
    value = payload.get("retry_after") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1 or isinstance(value, bool)
            or not isinstance(value, (int, float)) or not math.isfinite(value)):
        raise ValueError("Malformed shared authentication backoff")
    return value


@contextmanager
def _backoff_lock(path):
    # One private state/lock per controller, shared across repositories and aliases.
    import fcntl
    path = _checked_backoff_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _checked_backoff_path(path)
    lock = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Authentication lock must be an owned private regular file")
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Shared authentication transport is busy")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def run_remote(config: TransportConfig, argv, mutation=False) -> RemoteResult:
    """Dispatch at most once, with a shared authentication-failure cooldown.

Callers must authorize mutations and durably record their own intent/reservation.
Doctor uses a separate fixed mux-only path and never writes this state.
"""
    command = ssh_argv(config, argv)
    try:
        path = _backoff_path(config)
        with _backoff_lock(path):
            if _read_backoff(path) > time.time():
                return RemoteResult("auth_required", stderr="Shared authentication cooldown active; authenticate manually")
            result = _execute(config, command, mutation=mutation)
            # Only the transport's own exit status proves an authentication failure.
            if result.returncode == 255 and auth_failure(result.stderr):
                _arm_backoff(path, config.auth_backoff_seconds)
            return result
    except (OSError, ValueError, ImportError):
        # State failure after dispatch must not convert an ambiguous mutation to not-sent.
        if "result" in locals():
            return result
        return RemoteResult("not_sent", stderr="Shared authentication state unavailable, unsafe, malformed, or busy")


def _arm_backoff(path, seconds):
    data = json.dumps({"schema_version": 1, "retry_after": time.time() + seconds}) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def record_auth_failure(config: TransportConfig) -> bool:
    """Arm the shared cooldown after an endpoint rejected authentication.

    Returns False when the shared state is unavailable, unsafe or busy; the caller's
    own failure still propagates and nothing else is written.
    """
    try:
        path = _backoff_path(config)
        with _backoff_lock(path):
            _arm_backoff(path, config.auth_backoff_seconds)
    except (OSError, ValueError, ImportError):
        return False
    return True


def cooldown_active(config: TransportConfig) -> bool:
    """True while the shared cooldown holds, or when its state cannot be trusted (fail closed)."""
    try:
        return _read_backoff(_backoff_path(config)) > time.time()
    except (OSError, ValueError):
        return True


def data_transfer(config: TransportConfig, source: str, stage, manifest, *, timeout=300):
    """Manifest-selected rsync from the data endpoint, sharing the control cooldown.

    source is the host-less absolute remote directory; the data host is prepended
    here so callers never assemble host:path strings. An active cooldown refuses the
    transfer before rsync starts. A data-endpoint authentication failure (rsync exit
    255 with an authentication message) arms the same shared cooldown as run_remote.
    """
    from sherlock_artifacts import TransferError, rsync_transfer
    from sherlock_orchestration import SafetyError
    if not isinstance(config, TransportConfig):
        raise SafetyError("data transfer requires a TransportConfig")
    if not isinstance(source, str) or not source.startswith("/") or any(ch in source for ch in "\x00\n\r"):
        raise SafetyError("data transfer source must be an absolute remote path without control characters")
    if cooldown_active(config):
        raise SafetyError("Shared authentication cooldown active or its state is unavailable; data transfer refused before dispatch")
    try:
        rsync_transfer(config.data_host + ":" + source, stage, manifest,
                       ssh_command=shlex.join(_ssh_prefix(config)), timeout=timeout)
    except TransferError as exc:
        if exc.returncode == 255 and auth_failure(exc.stderr_tail):
            record_auth_failure(config)
        raise


def _source_root():
    root = Path(__file__).resolve().parent.parent
    return root if (root / "SHERLOCK.md").is_file() and (root / "pyproject.toml").is_file() else None


def policy_text() -> str:
    root = _source_root()
    if root is not None:
        return (root / "SHERLOCK.md").read_bytes().decode("utf-8")
    return resources.files("sherlock_kit_data").joinpath("SHERLOCK.md").read_bytes().decode("utf-8")


def policy_identity() -> dict:
    """Policy, code and packaged partition-profile identity; frozen installs are verified."""
    digest = hashlib.sha256(policy_text().encode("utf-8")).hexdigest()
    profiles = partitions_sha256()
    root = _source_root()
    if root is not None:
        try:
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                               text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            revision = "unversioned"
        return {"schema_version": SCHEMA_VERSION, "code_revision": revision,
                "policy_sha256": digest, "install_mode": "development", "partitions_sha256": profiles}
    identity = json.loads(resources.files("sherlock_kit_data").joinpath("build_identity.json").read_text())
    if identity.get("schema_version") != SCHEMA_VERSION or identity.get("policy_sha256") != digest:
        raise ValueError("Installed policy identity mismatch")
    if identity.get("partitions_sha256") != profiles:
        raise ValueError("Installed partition profile identity mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", identity.get("code_revision", "")):
        raise ValueError("Installed package does not have a frozen revision")
    return identity


def policy_projection() -> str:
    text = policy_text()
    if text.count(_SOURCE_BEGIN) != 1 or text.count(_SOURCE_END) != 1:
        raise ValueError("Canonical policy projection markers are malformed")
    start, end = text.index(_SOURCE_BEGIN) + len(_SOURCE_BEGIN), text.index(_SOURCE_END)
    if end <= start:
        raise ValueError("Canonical policy projection markers are reversed")
    identity = policy_identity()
    metadata = f"<!-- source: SHERLOCK.md; schema_version: {SCHEMA_VERSION}; policy_sha256: {identity['policy_sha256']} -->"
    return f"{BEGIN}\n{metadata}\n{text[start:end].strip()}\n{END}\n"


def _pin_matches(identity, expected):
    """Compare an advertised pin; partitions_sha256 only when the pin advertises it."""
    if not isinstance(expected, dict) or expected.get("schema_version") != SCHEMA_VERSION:
        return False
    keys = ["code_revision", "policy_sha256"]
    if "partitions_sha256" in expected:
        keys.append("partitions_sha256")
    return all(isinstance(expected.get(key), str) and identity[key] == expected[key] for key in keys)


def _projection_matches(path):
    try:
        text = Path(path).read_text()
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            return "configuration_mismatch"
        start, end = text.index(BEGIN), text.index(END)
        return "complete" if text[start:end + len(END)] + "\n" == policy_projection() else "configuration_mismatch"
    except OSError:
        return "unavailable"


# Fixed inventory: no scheduler polls, arbitrary environment dumps, or DTN shells.
DOCTOR_REMOTE_INVENTORY = (
    ("context", ("sh", "-c", 'hostname; printf "SLURM_JOB_ID=%s\\n" "${SLURM_JOB_ID-}"')),
    ("site_instructions", ("cat", "/etc/agents/AGENTS.md", "/etc/agents/slurm.md",
                           "/etc/agents/storage.md", "/etc/agents/software.md", "/etc/agents/policy.md")),
    ("slurm_version", ("scontrol", "--version")),
)


def doctor(config=None, *, remote=False, advertised_identity=None,
           claude_instructions=None, codex_instructions=None):
    """Read-only report; remote uses existing masters and forbids cold connections."""
    config = config or TransportConfig()
    identity = policy_identity()
    report = {"identity": identity, "mode": "remote" if remote else "local",
              "ssh": "available" if shutil.which(config.ssh_binary) else "unavailable",
              "advertised_identity": "unverified", "instructions": {},
              "control": {"status": "unverified"},
              "data": {"status": "unverified", "remote_capability": "unverified"}}
    report["agent_integration"] = {
        "guard_runtime": "available" if find_spec("sherlock_guard") else "unavailable",
        "claude_registration": "unverified", "codex_registration": "unverified",
        "codex_current_hash_trust": "unverified", "blocking_smoke": "unverified",
        "enforcement": "unverified",
    }
    # Disk presence does not establish client registration, current trust or execution.
    if identity["install_mode"] == "frozen":
        bundle = resources.files("sherlock_kit_data") / "adapters"
    else:
        bundle = Path(__file__).resolve().parents[1] / "adapters"
    report["agent_integration"]["adapter_bundle"] = "available" if all(
        (bundle / path).is_file() for path in (
            "claude/.claude-plugin/plugin.json", "claude/skills/sherlock-kit-operate/SKILL.md",
            "codex/skills/sherlock-kit-operate/SKILL.md")) else "unavailable"
    advertised_identity = advertised_identity or os.environ.get("SHERLOCK_KIT_PIN")
    if advertised_identity:
        try:
            expected = json.loads(Path(advertised_identity).read_text())
            same = _pin_matches(identity, expected)
            report["advertised_identity"] = "complete" if same and identity["install_mode"] == "frozen" else "configuration_mismatch"
        except (OSError, ValueError):
            report["advertised_identity"] = "configuration_mismatch"
    for tool, path in (("claude", claude_instructions or os.environ.get("SHERLOCK_KIT_CLAUDE_INSTRUCTIONS")),
                       ("codex", codex_instructions or os.environ.get("SHERLOCK_KIT_CODEX_INSTRUCTIONS"))):
        report["instructions"][tool] = _projection_matches(path) if path else "unverified"
    try:
        backoff_path = _backoff_path(config)
    except (OSError, ValueError):
        report["control"]["status"] = "configuration_mismatch"
        return report
    if not remote or report["ssh"] == "unavailable":
        return report
    try:
        if _read_backoff(backoff_path) > time.time():
            report["control"]["status"] = "auth_required"
            report["data"]["status"] = "auth_required"
            return report
    except (OSError, ValueError):
        report["control"]["status"] = "configuration_mismatch"
        return report
    for endpoint, host in (("control", config.control_host), ("data", config.data_host)):
        result = _execute(config, [*_ssh_prefix(config), "-oProxyCommand=false", "-O", "check", host])
        report[endpoint]["status"] = "master_available" if result.status == "complete" else "auth_required"
    if report["control"]["status"] != "master_available":
        report["manual_authentication"] = ["ssh", config.control_host]
        return report
    checks = {}
    for name, argv in DOCTOR_REMOTE_INVENTORY:
        command = ssh_argv(config, argv)
        command.insert(-2, "-oProxyCommand=false")
        result = _execute(config, command)
        # Never include site/config output or transport diagnostics in public reports.
        status = result.status
        if status == "complete":
            if name == "slurm_version" and not re.fullmatch(r"slurm \d+\.\d+(?:\.\d+)?\s*", result.stdout):
                status = "unsupported_capability"
            elif name == "context" and not re.fullmatch(r"[A-Za-z0-9_.-]+\nSLURM_JOB_ID=[0-9]*\n", result.stdout):
                status = "unsupported_capability"
            elif name == "site_instructions" and not result.stdout.strip():
                status = "unsupported_capability"
        checks[name] = {"status": status, "returncode": result.returncode}
        if status != "complete":
            break
    report["control"]["checks"] = checks
    report["control"]["status"] = "complete" if len(checks) == len(DOCTOR_REMOTE_INVENTORY) and all(v["status"] == "complete" for v in checks.values()) else "unavailable"
    return report


def main(argv=None):
    import sys
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"submit", "status", "reconcile", "fetch", "occupancy"}:
        from sherlock_commands import main as operations
        return operations(arguments)
    if arguments and arguments[0] == "guard":
        from sherlock_guard import main as guard
        return guard(arguments[1:])
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Consumer commands: submit, status, reconcile, fetch, occupancy. "
               "Opt-in client adapter: guard. Run shk COMMAND --help for its contract.")
    sub = parser.add_subparsers(dest="operation", required=True)
    policy = sub.add_parser("policy", help="Print canonical policy or installed provenance")
    mode = policy.add_mutually_exclusive_group()
    mode.add_argument("--identity", action="store_true")
    mode.add_argument("--projection", action="store_true")
    diagnostic = sub.add_parser("doctor", help="Read-only local checks; --remote explicitly opts in")
    diagnostic.add_argument("--remote", action="store_true")
    diagnostic.add_argument("--control-host", default="sherlock-plain")
    diagnostic.add_argument("--data-host", default="sherlock-dtn")
    diagnostic.add_argument("--advertised-identity")
    diagnostic.add_argument("--claude-instructions")
    diagnostic.add_argument("--codex-instructions")
    args = parser.parse_args(argv)
    try:
        if args.operation == "policy":
            print(json.dumps(policy_identity(), sort_keys=True) if args.identity
                  else policy_projection() if args.projection else policy_text(), end="\n" if args.identity else "")
            return 0
        report = doctor(TransportConfig(control_host=args.control_host, data_host=args.data_host),
                        remote=args.remote, advertised_identity=args.advertised_identity,
                        claude_instructions=args.claude_instructions, codex_instructions=args.codex_instructions)
        print(json.dumps(report, sort_keys=True, indent=2))
        failed = report["ssh"] == "unavailable" or report["advertised_identity"] == "configuration_mismatch" or any(v != "complete" and v != "unverified" for v in report["instructions"].values())
        return 1 if failed or (args.remote and report["control"]["status"] != "complete") else 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f"shk: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
