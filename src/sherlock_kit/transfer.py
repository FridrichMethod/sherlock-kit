"""`shk push`, `shk pull`, `shk manifest`: rsync through the DTN, bound by a SHA256SUMS manifest.

push: rsync -rlt LOCAL/ into data_host:REMOTE/ (below remote_root unless --anywhere); it never
deletes. -t keeps the local modification times, so a re-push skips unchanged files instead of
rewriting them (and refreshing their scratch purge clock); pushed files that are old locally can
therefore be purge-eligible on scratch soon after the push.

pull: read REMOTE/SHA256SUMS through the control host (data/remote/manifest.sh); it must list every
regular file under REMOTE except SHA256SUMS and .shk* entries. Under a flock on .NAME.shk-lock, an
existing LOCAL with a matching receipt is already_present (no DTN dial); otherwise rsync fetches
exactly the listed files into the private stage .NAME.shk-stage. A rerun resumes the stage: what an
earlier attempt fetched is kept, anything this manifest does not list (temp files of a killed rsync,
files of an older manifest) is pruned first. Then: verify the exact inventory and every sha256,
re-read the manifest, run validators[NAME] + [stage] from the project root (exit 0 = pass; validators
treat the stage as read-only, whatever they write is promoted with it), rename the stage onto LOCAL
(--replace keeps the old copy as .NAME.shk-old-<UTC>) and write .NAME.shk-receipt.json.

manifest: write DIR/SHA256SUMS locally (inside jobs, no network), sha256sum-compatible and sorted.

The DTN refuses options that alter its restricted sender command. Pull's --chmod=Du+rwx is
receiver-local: it keeps incoming directories writable without changing file permissions or the
remote sender command. --files-from=- sends the DTN-proven list-file command.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sherlock_kit import __version__, executor
from sherlock_kit.config import DEFAULTS, ProjectConfig
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, EXIT_UNREACHABLE, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context

SUMS = "SHA256SUMS"
RESERVED_PREFIX = ".shk"
SUMS_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)")
TAIL_CHARS = 500
LINK_LOST = (12, 255)  # rsync: protocol stream broken; ssh: connection failed
PUSH = ("rsync", "-rlt", "--protect-args", "--partial")
PULL = (
    "rsync",
    "-r",
    "--protect-args",
    "--checksum",
    "--partial",
    "--chmod=Du+rwx",
    "--from0",
    "--files-from=-",
)
MANIFEST_HINT = (
    "write SHA256SUMS last in the job: shk manifest DIR (or the documented sha256sum one-liner)"
)
RERUN_HINT = "push is idempotent and pull resumes from its stage"


# SHA256SUMS ------------------------------------------------------------------------------------


def _invalid(message: str) -> ShkError:
    return ShkError(
        EXIT_LOCAL, "manifest_invalid", message, hint="fix the named file or line; " + MANIFEST_HINT
    )


def _check_name(where: str, name: str) -> None:
    parts = name.split("/")
    if name.startswith("/"):
        problem = "is an absolute path; list paths relative to the manifest's directory"
    elif any(part in ("", ".", "..") for part in parts):
        problem = "has an empty, '.' or '..' component"
    elif any(part.startswith(RESERVED_PREFIX) for part in parts):
        problem = "starts with .shk, a name reserved for shk"
    elif parts[-1] == SUMS:
        problem = "is named SHA256SUMS, a name reserved for the manifest"
    else:
        return
    raise _invalid(f"{where}: {name!r} {problem}")


def _parse_line(where: str, line: str) -> tuple[str, str]:
    if line.startswith("\\"):  # sha256sum's escaped form: the fix is the file name, not the line
        raise _invalid(
            f"{where}: sha256sum escaped this name because it holds a backslash or a newline; "
            "rename the file"
        )
    match = SUMS_LINE.fullmatch(line)
    if match is None:
        raise _invalid(f"{where}: want '<64 hex digits>  <path>' as sha256sum writes it")
    name = match[2].removeprefix("./")
    _check_name(where, name)
    return match[1].lower(), name


def _parents(names: Iterable[str]) -> set[str]:
    """Every directory the relative paths imply: "a/b/c" gives "a" and "a/b"."""
    parents: set[str] = set()
    for name in names:
        parts = name.split("/")
        parents.update("/".join(parts[:end]) for end in range(1, len(parts)))
    return parents


def parse_manifest(text: str) -> dict[str, str]:
    """{relative path: lowercase sha256} from SHA256SUMS text, else ShkError(1, "manifest_invalid")."""
    entries: dict[str, str] = {}
    for number, line in enumerate(text.split("\n"), 1):
        if not line:
            continue
        digest, name = _parse_line(f"SHA256SUMS line {number} {line[:100]!r}", line)
        if name in entries:
            raise _invalid(f"SHA256SUMS line {number}: {name!r} is listed twice")
        entries[name] = digest
    if not entries:
        raise _invalid("SHA256SUMS lists no files")
    return entries


def _digest(path: Path) -> str:
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _walk(
    root: Path, skip_reserved: bool, prefix: str = ""
) -> Iterator[tuple[str, os.DirEntry[str]]]:
    """(relative path, entry) for everything below root, depth first; symlinks are never followed."""
    with os.scandir(root / prefix) as scan:
        found = list(scan)
    for entry in found:
        if skip_reserved and entry.name.startswith(RESERVED_PREFIX):
            continue
        path = prefix + entry.name
        yield path, entry
        if entry.is_dir(follow_symlinks=False):
            yield from _walk(root, skip_reserved, path + "/")


# Reading the remote manifest -------------------------------------------------------------------


def _malformed(remote: str, problem: str) -> ShkError:
    return ShkError(
        EXIT_LOCAL,
        "malformed_reply",
        f"reading {remote}/SHA256SUMS: {problem}",
        hint="run shk doctor --remote",
    )


def _manifest_error(remote: str, error: str) -> ShkError:
    if error == "no_directory":
        return ShkError(
            EXIT_LOCAL,
            "manifest_unavailable",
            f"{remote} is not a directory on Sherlock",
            hint="check REMOTE: the absolute path of the directory the job wrote",
        )
    if error == "no_manifest":
        return ShkError(
            EXIT_LOCAL,
            "manifest_unavailable",
            f"{remote}/SHA256SUMS is missing or unreadable",
            hint=MANIFEST_HINT,
        )
    return _malformed(remote, f"unknown error {error!r}")


def _read_manifest(control: executor.Executor, remote: str) -> tuple[str, int]:
    """(SHA256SUMS text, regular files under REMOTE that a complete manifest lists) via manifest.sh."""
    reply = executor.check(executor.run_program(control, "manifest", [remote]), "read SHA256SUMS")
    lines = reply.stdout.split("\n")
    if "SHK_END" not in lines:
        raise _malformed(remote, "the reply ends before SHK_END")
    fields = dict(line.split(" ", 1) for line in lines if re.match(r"SHK_[A-Z]+ ", line))
    if "SHK_ERROR" in fields:
        raise _manifest_error(remote, fields["SHK_ERROR"])
    count = fields.get("SHK_COUNT", "")
    if not count.isdigit():
        raise _malformed(remote, "the reply has no file count")
    text = "".join(
        line.removeprefix("SHK_M|") + "\n" for line in lines if line.startswith("SHK_M|")
    )
    return text, int(count)


def _check_complete(remote: str, count: int, listed: int) -> None:
    if count == listed:
        return
    raise ShkError(
        EXIT_LOCAL,
        "manifest_incomplete",
        f"{remote} holds {count} regular files; SHA256SUMS lists {listed}",
        hint="list every regular file except SHA256SUMS and .shk* entries; " + MANIFEST_HINT,
    )


# Running rsync and validators ------------------------------------------------------------------


def _failure(label: str, result: executor.Result, timeout: int) -> str:
    """The message "<label> <what happened>: <tail>", with the output's tail on one line."""
    if result.status == "timeout":
        what = f"timed out after {timeout} s"
    elif result.status == "failed":
        what = f"exit {result.returncode}"
    else:  # unavailable: the program could not start
        what = result.status
    tail = " ".join((result.stdout + result.stderr).split())[-TAIL_CHARS:]  # banner already gone
    return f"{label} {what}: {tail}" if tail else f"{label} {what}"


def _rsync(argv: list[str], timeout: int, stdin: bytes | None = None) -> None:
    """Rsync with the executor's timeout (which kills the whole process group); else transfer_failed."""
    result = executor.LocalExecutor().run(argv, stdin=stdin, timeout=timeout)
    if result.status == "complete":
        return
    code = EXIT_UNREACHABLE if result.returncode in LINK_LOST else EXIT_LOCAL
    if result.status == "timeout":
        hint = (
            f"raise transfer_timeout ({timeout} s) in the project config, then rerun: {RERUN_HINT}"
        )
    else:
        hint = f"fix the cause, then rerun: {RERUN_HINT}"
    raise ShkError(code, "transfer_failed", _failure("rsync", result, timeout), hint=hint)


def _validate(settings: ProjectConfig, name: str, stage: Path) -> None:
    """validators[name] + [stage], run from the project root; only exit 0 lets pull promote."""
    argv = [
        "sh",
        "-c",
        'cd "$1" && shift && exec "$@"',
        "shk-validate",
        str(settings.root),
        *settings.validators[name],
        str(stage),
    ]
    result = executor.LocalExecutor().run(argv, timeout=settings.transfer_timeout)
    if result.status != "complete":
        raise ShkError(
            EXIT_LOCAL,
            "validator_failed",
            _failure(f"validator {name!r}", result, settings.transfer_timeout),
            hint="nothing was promoted and the stage is kept; fix the data or the validator, "
            "then rerun pull",
        )


# Paths -------------------------------------------------------------------------------------------


def _settings(ctx: Context) -> ProjectConfig:
    return ctx.config or DEFAULTS


def _remote(path: str) -> str:
    """REMOTE without its trailing "/"; absolute and free of "..".

    Nothing else about its characters is checked: rsync runs with --protect-args, which sends the
    path inside its own protocol instead of on the remote shell's command line, and manifest.sh
    gets it as one argument of the executor's shlex-joined remote command.
    """
    if not path.startswith("/") or ".." in path.split("/"):
        raise ShkError(
            EXIT_LOCAL,
            "invalid_remote_path",
            f"REMOTE {path!r} must be an absolute path without '..'",
        )
    return path.rstrip("/") or "/"


def _destination(local: str) -> Path:
    """LOCAL as an absolute path with its existing parent resolved (LOCAL itself may not exist yet)."""
    path = Path(os.path.abspath(Path(local).expanduser()))
    if not path.name or not path.parent.is_dir():
        raise ShkError(
            EXIT_LOCAL,
            "local_parent_missing",
            f"LOCAL {local} must name an entry in an existing directory",
        )
    return path.parent.resolve() / path.name


@dataclass(frozen=True)
class Places:
    """LOCAL and the shk files beside it: .NAME.shk-lock, .NAME.shk-stage, .NAME.shk-receipt.json."""

    dest: Path
    lock: Path
    stage: Path
    receipt: Path

    @classmethod
    def beside(cls, dest: Path) -> Places:
        """The shk files of LOCAL dest, which may not exist yet."""

        def sibling(kind: str) -> Path:
            return dest.with_name(f".{dest.name}.shk-{kind}")

        return cls(dest, sibling("lock"), sibling("stage"), sibling("receipt.json"))

    def old_copy(self, now: datetime) -> Path:
        """Where --replace keeps the previous LOCAL."""
        return self.dest.with_name(f".{self.dest.name}.shk-old-{now:%Y%m%dT%H%M%S%fZ}")


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    with open(path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes
        yield


# Pull steps --------------------------------------------------------------------------------------


def _read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    receipt: dict[str, Any] = json.loads(text)  # written by _write_receipt, atomically
    return receipt


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def _existing(places: Places, digest: str, validator: str | None, replace: bool) -> Outcome | None:
    """already_present when LOCAL holds this manifest's data (validated as asked); None to pull."""
    if not os.path.lexists(places.dest):
        return None
    receipt = _read_receipt(places.receipt)
    if receipt is not None and receipt.get("manifest_sha256") == digest:
        if validator is None or receipt.get("validator") == validator:
            return Outcome({
                "action": "already_present",
                "local": str(places.dest),
                "manifest_sha256": digest,
                "validator": receipt.get("validator"),
            })
        reason = f"holds this SHA256SUMS but was pulled without validator {validator!r}"
    else:
        reason = "exists without a receipt for this SHA256SUMS"
    if not replace:
        raise ShkError(
            EXIT_REFUSED,
            "destination_exists",
            f"{places.dest} {reason}",
            hint="pass --replace to pull again and swap it in; the old copy is kept as .NAME.shk-old-*",
        )
    return None


def _prune(directory: Path, files: set[str], directories: set[str], prefix: str = "") -> None:
    """Delete every entry below directory that is neither a listed file nor a directory leading to one."""
    with os.scandir(directory) as scan:
        found = list(scan)
    for entry in found:
        path = prefix + entry.name
        if entry.is_dir(follow_symlinks=False):
            if path in directories:
                _prune(Path(entry.path), files, directories, path + "/")
            else:
                shutil.rmtree(entry.path)
        elif path not in files or not entry.is_file(follow_symlinks=False):
            os.unlink(entry.path)


def _writable_directories(directory: Path, parent_fd: int | None = None) -> None:
    """Give owner rwx back to the stage directories that lack it, and touch nothing else.

    An interrupted pull of a readonly source by an older shk (without --chmod=Du+rwx) leaves
    them; an ordinary pull issues no chmod, so filesystems that refuse chmod or map owners work.
    Opening each child relative to its parent's descriptor with O_NOFOLLOW avoids chmodding a
    symlink target; files are never opened or chmodded by this pass.
    """
    try:
        fd = os.open(
            directory if parent_fd is None else directory.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ShkError(
            EXIT_REFUSED,
            "stage_untrusted",
            f"Cannot open private stage directory {directory}: {error}",
        ) from None
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode & 0o700 != 0o700:
            os.fchmod(fd, mode | 0o700)
        with os.scandir(fd) as scan:
            found = list(scan)
        for entry in found:
            if entry.is_dir(follow_symlinks=False):
                _writable_directories(directory / entry.name, fd)
    finally:
        os.close(fd)


def _prepare_stage(stage: Path, entries: dict[str, str]) -> None:
    """The private stage, keeping what an earlier attempt fetched for this manifest and nothing else.

    A killed rsync leaves temp files (.NAME.XXXXXX) and an older manifest leaves unlisted files; both
    would fail verification forever. The stage belongs to shk, so they go before rsync resumes.
    """
    stage.mkdir(mode=0o700, exist_ok=True)
    _writable_directories(stage)
    _prune(stage, set(entries), _parents(entries))


def _verify(stage: Path, entries: dict[str, str]) -> int:
    """Total bytes when the stage holds exactly the listed files with their sha256; else verify_failed."""
    walked = list(_walk(stage, skip_reserved=False))
    files = {path for path, entry in walked if entry.is_file(follow_symlinks=False)}
    others = {path for path, entry in walked if not entry.is_file(follow_symlinks=False)}
    problems = sorted(entries.keys() ^ files) + sorted(others - _parents(entries))
    if not problems:
        problems = [name for name, digest in entries.items() if _digest(stage / name) != digest]
    if problems:
        raise ShkError(
            EXIT_LOCAL,
            "verify_failed",
            f"{stage} differs from SHA256SUMS at {problems[:3]}; nothing was promoted",
            hint="the remote files do not match SHA256SUMS: rewrite it once the job's "
            "outputs are final, then rerun pull (it keeps the stage and refetches what differs)",
        )
    return sum((stage / name).stat().st_size for name in entries)


def _promote(stage: Path, dest: Path, old_copy: Path) -> Path | None:
    """Rename the stage onto dest. An existing dest moves to old_copy first and comes back if that fails."""
    kept = old_copy if os.path.lexists(dest) else None
    try:
        if kept is not None:
            os.rename(dest, kept)
        os.rename(stage, dest)
    except OSError as exc:
        if kept is not None and not os.path.lexists(dest):
            os.rename(kept, dest)
        raise ShkError(
            EXIT_LOCAL,
            "promote_failed",
            f"cannot move the stage onto {dest} ({exc}); {dest} is unchanged",
        ) from None
    return kept


# Commands ----------------------------------------------------------------------------------------


def push(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk push LOCAL REMOTE`: rsync LOCAL/ into REMOTE/ through the DTN; never deletes."""
    remote = _remote(args.remote)
    local = Path(args.local).expanduser().resolve()
    if not args.anywhere:
        root = ctx.require("remote_root").remote_root
        if remote != root and not remote.startswith(root + "/"):
            raise ShkError(
                EXIT_REFUSED,
                "outside_remote_root",
                f"{remote} is outside remote_root {root}",
                hint="pass --anywhere to push there on purpose",
            )
    if not local.is_dir():
        raise ShkError(EXIT_LOCAL, "local_missing", f"LOCAL {args.local} is not a directory")
    options, target = ctx.data_executor().rsync_args(remote)  # DTN preflight; refused on Sherlock
    _rsync([*PUSH, *options, "--", f"{local}/", f"{target}/"], _settings(ctx).transfer_timeout)
    return Outcome({"action": "pushed", "local": str(local), "remote": remote, "target": target})


def _owned_by_me(path: Path) -> bool:
    """Whether the current user owns path: validators are commands, never taken from another user."""
    return path.stat().st_uid == os.geteuid()


def pull(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk pull REMOTE LOCAL`: fetch, verify, validate and promote what SHA256SUMS lists."""
    remote, settings = _remote(args.remote), _settings(ctx)
    if args.validate is not None and args.validate not in settings.validators:
        raise ShkError(
            EXIT_LOCAL,
            "validator_unknown",
            f"no validator {args.validate!r} in [tool.sherlock-kit.validators]",
        )
    if args.validate is not None and settings.path and not _owned_by_me(settings.path):
        raise ShkError(
            EXIT_REFUSED,
            "validator_untrusted",
            f"{settings.path} belongs to another user; shk runs validators only from a "
            "configuration you own",
            hint="copy the configuration into a project you own, or pull without --validate",
        )
    places = Places.beside(_destination(args.local))
    data = ctx.data_executor()
    if data.kind == "local":  # on Sherlock: refuse before any work, the data is already here
        data.rsync_args(remote)
    text, count = _read_manifest(ctx.executor(), remote)
    entries = parse_manifest(text)
    _check_complete(remote, count, len(entries))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with _locked(places.lock):  # a concurrent pull waits here, then finds the receipt
        present = _existing(places, digest, args.validate, args.replace)
        if present is not None:
            return present
        options, target = data.rsync_args(remote)  # DTN preflight, only once a transfer is needed
        _prepare_stage(places.stage, entries)
        _rsync(
            [*PULL, *options, "--", f"{target}/", f"{places.stage}/"],
            settings.transfer_timeout,
            stdin=b"".join(name.encode("utf-8") + b"\0" for name in entries),
        )
        total = _verify(places.stage, entries)
        if _read_manifest(ctx.executor(), remote) != (text, count):
            raise ShkError(
                EXIT_LOCAL,
                "manifest_changed",
                f"{remote} changed during the transfer; nothing was promoted",
                hint="rerun pull once the job has finished writing",
            )
        if args.validate is not None:
            _validate(settings, args.validate, places.stage)
        now = ctx.clock()
        old = _promote(places.stage, places.dest, places.old_copy(now))
        receipt = {
            "manifest_sha256": digest,
            "remote": remote,
            "files": len(entries),
            "bytes": total,
            "validator": args.validate,
            "shk_version": __version__,
            "created_utc": f"{now:%Y-%m-%dT%H:%M:%SZ}",
        }
        _write_receipt(places.receipt, receipt)
    return Outcome({
        "action": "pulled",
        "local": str(places.dest),
        **receipt,
        "replaced": str(old) if old else None,
    })


def manifest(args: argparse.Namespace, _ctx: Context) -> Outcome:
    """`shk manifest DIR`: write DIR/SHA256SUMS over its regular files (inside jobs, offline)."""
    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        raise ShkError(EXIT_LOCAL, "bundle_missing", f"{args.directory} is not a directory")
    files: list[str] = []
    skipped: list[str] = []
    for path, entry in _walk(root, skip_reserved=True):
        if entry.is_dir(follow_symlinks=False) or entry.name == SUMS:
            continue
        if entry.is_file(follow_symlinks=False):
            files.append(path)
        else:
            skipped.append(path)  # symlinks and special files, as `find -type f` leaves them out
    names = sorted(files)
    text = "".join(f"{_digest(root / name)}  {name}\n" for name in names)
    temporary = root / f"{RESERVED_PREFIX}-{SUMS}"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, root / SUMS)
    return Outcome({
        "action": "manifest_written",
        "path": str(root / SUMS),
        "files": len(names),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "skipped": sorted(skipped),
    })


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the push, pull and manifest commands."""
    to = subparsers.add_parser("push", help="rsync LOCAL/ into data_host:REMOTE/ (never deletes)")
    to.add_argument("local", metavar="LOCAL")
    to.add_argument(
        "remote", metavar="REMOTE", help="absolute; below remote_root unless --anywhere"
    )
    to.add_argument("--anywhere", action="store_true", help="allow REMOTE outside remote_root")
    to.set_defaults(handler=push)
    back = subparsers.add_parser(
        "pull", help="fetch what REMOTE/SHA256SUMS lists, verify it, then create LOCAL"
    )
    back.add_argument("remote", metavar="REMOTE")
    back.add_argument("local", metavar="LOCAL", help="new directory; its parent must exist")
    back.add_argument(
        "--validate",
        metavar="NAME",
        help="first run validators.NAME from the config on the stage (treat it as read-only)",
    )
    back.add_argument(
        "--replace", action="store_true", help="swap an existing LOCAL (kept as .LOCAL.shk-old-*)"
    )
    back.set_defaults(handler=pull)
    sums = subparsers.add_parser(
        "manifest", help="write DIR/SHA256SUMS (inside jobs; never uses the network)"
    )
    sums.add_argument("directory", metavar="DIR")
    sums.set_defaults(handler=manifest)
