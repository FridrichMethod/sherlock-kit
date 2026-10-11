"""`shk push`, `shk pull`, `shk manifest`: rsync through the DTN, bound by a SHA256SUMS manifest.

push LOCAL REMOTE: rsync -rlt --protect-args --partial LOCAL/ data_host:REMOTE/ (absolute, no "..",
under remote_root unless --anywhere); never deletes. pull REMOTE LOCAL, under flock .NAME.shk-lock
beside LOCAL: read REMOTE/SHA256SUMS via the control host (manifest.sh; it must list every regular
file), rsync exactly those into the resumable 0700 stage .NAME.shk-stage, verify the exact inventory
and each sha256, re-read the manifest, run validators[NAME] + [stage] (cwd: project root, exit 0
passes, then re-verified), rename onto LOCAL (an existing one needs --replace and stays as
.NAME.shk-old-<UTC>), write .NAME.shk-receipt.json (a matching receipt: already_present).
manifest DIR: DIR/SHA256SUMS ("<sha256>  <path>", sorted; no SHA256SUMS, .shk* or symlinks).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

from sherlock_kit import __version__, executor
from sherlock_kit.config import DEFAULTS
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, EXIT_UNREACHABLE, Outcome, ShkError

LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)")
MAX_FILES, MAX_BYTES = 10_000, 4 << 20
PUSH = ("rsync", "-rlt", "--protect-args", "--partial")
PULL = ("rsync", "-r", "--protect-args", "--checksum", "--partial", "--from0")  # DTN-proven: add no options


class TransferError(ShkError):
    """rsync failed (returncode) or timed out (None); stderr_tail is filtered, redacted and bounded."""

    def __init__(self, code: int, message: str, returncode=None, stderr_tail=""):
        super().__init__(code, "transfer_failed", message, hint="rerun the same command; a pull resumes its stage")
        self.returncode, self.stderr_tail = returncode, stderr_tail


def _tail(raw, redact=(), limit=500) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")
    text = " ".join(line for line in text.splitlines() if not line.startswith(executor.BANNER))
    for secret in redact:
        text = text.replace(secret, "<files-from>")
    return " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())[-limit:]


def _digest(path: Path) -> str:
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _parents(names) -> set:
    return {"/".join(parts[:i]) for parts in (name.split("/") for name in names) for i in range(1, len(parts))}


def parse_manifest(text: str) -> dict:
    """{relative path: sha256} from SHA256SUMS text, else ShkError(1, "manifest_invalid")."""
    entries, size = {}, len(text.encode("utf-8", "surrogateescape"))
    for number, line in enumerate(text.split("\n") if size <= MAX_BYTES else (), 1):
        match = LINE.fullmatch(line)
        name = match[2].removeprefix("./") if match else ""
        parts = name.split("/")
        if line and (any(p in ("", ".", "..") or p.startswith(".shk") for p in parts) or parts[-1] == "SHA256SUMS"
                     or not name.isprintable() or "\\" in name or name in entries):
            raise ShkError(EXIT_LOCAL, "manifest_invalid", f"SHA256SUMS line {number} {line[:100]!r}: want "
                           "'<sha256>  <path>' with a new relative path free of '.', '..', .shk* and SHA256SUMS "
                           "parts, backslash escapes and control characters")
        if line:
            entries[name] = match[1].lower()
    clash = sorted(_parents(entries) & entries.keys())
    if clash or not 0 < len(entries) <= MAX_FILES or size > MAX_BYTES:
        raise ShkError(EXIT_LOCAL, "manifest_invalid", f"SHA256SUMS lists {clash[0]!r} as a file and a directory"
                       if clash else f"SHA256SUMS must list 1-{MAX_FILES} files in at most {MAX_BYTES} bytes")
    return entries


def _walk(root: Path, prune: bool, rel: str = ""):
    """(relative path, DirEntry) below root, symlinks not followed; prune skips .shk* names."""
    with os.scandir(root / rel) as scan:
        for entry in scan:
            path = f"{rel}/{entry.name}" if rel else entry.name
            if not (prune and entry.name.startswith(".shk")):
                yield path, entry
                if entry.is_dir(follow_symlinks=False):
                    yield from _walk(root, prune, path)


def _verify(stage: Path, entries: dict) -> int:
    """Total bytes when stage holds exactly the listed regular files with their sha256, else verify_failed."""
    files, other = set(), set()
    for path, entry in _walk(stage, False):
        (files if entry.is_file(follow_symlinks=False) else other).add(path)
    odd = sorted(entries.keys() ^ files) + sorted(other - _parents(entries))
    if odd:
        raise ShkError(EXIT_LOCAL, "verify_failed", f"{stage} does not hold exactly the SHA256SUMS files: {odd[:3]}",
                       hint=f"if the stage is stale: rm -r {shlex.quote(str(stage))}, then rerun")
    changed = [name for name, digest in entries.items() if _digest(stage / name) != digest]
    if changed:
        raise ShkError(EXIT_LOCAL, "verify_failed", f"sha256 differs from SHA256SUMS for {changed[:3]}; nothing "
                       "was promoted", hint="write SHA256SUMS last in the job, then rerun (the stage resumes)")
    return sum((stage / name).stat().st_size for name in entries)


def _rsync(argv, timeout: int, redact=()) -> None:
    """rsync with the executor's group-kill timeout; TransferError on failure (exit 4 if the link broke)."""
    try:
        code, _, err = executor._spawn(list(argv), None, timeout)
    except subprocess.TimeoutExpired as exc:
        code, err = None, exc.stderr
    except OSError as exc:
        raise ShkError(EXIT_LOCAL, "rsync_missing", f"cannot run rsync: {exc.strerror or exc}") from None
    tail = _tail(err, redact)
    if code != 0:
        what = f"rsync exceeded transfer_timeout ({timeout} s)" if code is None else f"rsync exit {code}"
        raise TransferError(EXIT_UNREACHABLE if code in (12, 255) else EXIT_LOCAL, what + (f": {tail}" if tail
                            else ""), code, tail)


def _remote(path: str) -> str:
    if not path.startswith("/") or ".." in path.split("/") or set(path) & set("\0\n\r"):
        raise ShkError(EXIT_LOCAL, "invalid_remote_path", f"REMOTE {path!r} must be one absolute path without '..'")
    return path.rstrip("/") or "/"


def push(args, ctx) -> Outcome:
    remote, local = _remote(args.remote), Path(args.local).expanduser().resolve()
    root = None if args.anywhere else ctx.require("remote_root").remote_root
    if root and remote != root and not remote.startswith(root + "/"):
        raise ShkError(EXIT_REFUSED, "outside_remote_root", f"{remote} is outside remote_root {root}",
                       hint="push below remote_root, or pass --anywhere")
    if not local.is_dir():
        raise ShkError(EXIT_LOCAL, "local_missing", f"LOCAL {args.local} is not a directory")
    options, target = ctx.data_executor().rsync_args(remote)
    _rsync([*PUSH, *options, "--", f"{local}/", f"{target}/"], (ctx.config or DEFAULTS).transfer_timeout)
    return Outcome({"action": "pushed", "local": str(local), "remote": remote, "target": target})


def _read_manifest(control, remote: str) -> tuple[str, str, int]:
    """(text, sha256 of the file, regular files the directory holds) from manifest.sh."""
    lines = executor.check(executor.run_program(control, "manifest", [remote]), "read SHA256SUMS").stdout.split("\n")
    fields = dict(line.split(" ", 1) for line in lines if re.match(r"SHK_[A-Z0-9]+ ", line))
    body = "".join(line[6:] + "\n" for line in lines if line.startswith("SHK_M|"))
    if "SHK_ERROR" in fields:
        raise ShkError(EXIT_LOCAL, "manifest_missing", f"{remote}/SHA256SUMS: {fields['SHK_ERROR'].replace('_', ' ')}",
                       hint="write it last in the job: shk manifest DIR (or the sha256sum one-liner)")
    if "SHK_END" not in lines or not fields.get("SHK_COUNT", "").isdigit():
        raise ShkError(EXIT_LOCAL, "malformed_reply", f"manifest.sh gave an incomplete reply for {remote}")
    if fields.get("SHK_SHA256") not in {hashlib.sha256(text.encode()).hexdigest() for text in (body, body[:-1])}:
        raise ShkError(EXIT_LOCAL, "manifest_invalid", f"{remote}/SHA256SUMS is not UTF-8 text or changed while read")
    return body, fields["SHK_SHA256"], int(fields["SHK_COUNT"])


def _validate(ctx, name: str, stage: Path) -> None:
    argv, timeout = [*ctx.config.validators[name], str(stage)], ctx.config.transfer_timeout
    try:
        code, out, err = executor._spawn(["sh", "-c", 'cd "$1" && shift && exec "$@"', "shk-validate",
                                          str(ctx.config.root), *argv], None, timeout)
    except subprocess.TimeoutExpired:
        code, out, err = f"timeout after {timeout} s", b"", b""
    if code != 0:
        raise ShkError(EXIT_LOCAL, "validator_failed", f"validator {name} ({code}): {_tail(out + err)}; nothing "
                       "was promoted", hint="the stage is kept; fix the outputs or the validator, then rerun")


def _promote(stage: Path, dest: Path, stamp: str) -> Path | None:
    """Rename stage onto dest; an existing dest moves to .NAME.shk-old-<stamp> first and back on failure."""
    old = dest.with_name(f".{dest.name}.shk-old-{stamp}") if os.path.lexists(dest) else None
    try:
        if old and os.path.lexists(old):
            raise FileExistsError(f"{old} exists; retry in a second")
        if old:
            os.rename(dest, old)
        try:
            os.rename(stage, dest)
        except OSError:
            if old:
                os.rename(old, dest)
            raise
    except OSError as exc:
        raise ShkError(EXIT_LOCAL, "promote_failed", f"cannot move the verified stage onto {dest} ({exc}); "
                       f"{dest} is unchanged and the stage is kept") from None
    return old


def _receipt_digest(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("manifest_sha256")
    except (OSError, ValueError, AttributeError):  # absent, torn or foreign: LOCAL counts as unrelated
        return None


def pull(args, ctx) -> Outcome:
    remote, settings, now = _remote(args.remote), ctx.config or DEFAULTS, ctx.clock()
    if args.validate is not None and args.validate not in settings.validators:
        raise ShkError(EXIT_LOCAL, "validator_unknown", f"no validator {args.validate!r} in the project config",
                       hint="add it under [tool.sherlock-kit.validators] as NAME = [argv ...]")
    local = Path(os.path.abspath(Path(args.local).expanduser()))
    parent, base = local.parent.resolve(), f".{local.name}.shk-"
    if local.name in ("", "..") or not parent.is_dir():
        raise ShkError(EXIT_LOCAL, "local_parent_missing", f"LOCAL {args.local} must name a new entry in an "
                       "existing directory", hint="create the parent directory first")
    dest, stage, receipt_path, listed = (parent / name for name in (local.name, base + "stage", base + "receipt.json",
                                                                    base + "files"))
    options, target = ctx.data_executor().rsync_args(remote)
    with open(parent / (base + "lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a concurrent pull waits here, then finds the receipt
        text, digest, count = _read_manifest(ctx.executor(), remote)
        entries = parse_manifest(text)
        if count != len(entries):
            raise ShkError(EXIT_LOCAL, "manifest_incomplete", f"{remote} holds {count} regular files but SHA256SUMS "
                           f"lists {len(entries)}", hint="regenerate SHA256SUMS after the outputs are final")
        present = os.path.lexists(dest)
        if present and _receipt_digest(receipt_path) == digest:
            return Outcome({"action": "already_present", "local": str(dest), "remote": remote,
                            "manifest_sha256": digest, "receipt": str(receipt_path)})
        if present and not args.replace:
            raise ShkError(EXIT_REFUSED, "destination_exists", f"{dest} exists and its receipt does not match "
                           f"{remote}/SHA256SUMS", hint="pass --replace to swap it (the old copy is kept beside it)")
        stage.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(stage)
        if stage.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ShkError(EXIT_LOCAL, "stage_invalid", f"{stage} must be a directory only you can access (0700)",
                           hint=f"inspect it, then rm -r {shlex.quote(str(stage))}")
        listed.write_bytes(b"".join(name.encode() + b"\0" for name in entries))
        try:
            _rsync([*PULL, f"--files-from={listed}", *options, "--", f"{target}/", f"{stage}/"],
                   settings.transfer_timeout, (str(listed),))
        finally:
            listed.unlink()
        total = _verify(stage, entries)
        if _read_manifest(ctx.executor(), remote)[1:] != (digest, count):
            raise ShkError(EXIT_LOCAL, "manifest_changed", f"{remote} changed during the transfer; nothing was "
                           "promoted", hint="rerun once the job has finished writing (the stage resumes)")
        if args.validate is not None:
            _validate(ctx, args.validate, stage)
            total = _verify(stage, entries)
        old = _promote(stage, dest, now.strftime("%Y%m%dT%H%M%SZ"))
        receipt = {"manifest_sha256": digest, "remote": remote, "data_host": settings.data_host,
                   "files": len(entries), "bytes": total, "validator": args.validate,
                   "shk_version": __version__, "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return Outcome({"action": "pulled", "local": str(dest), **receipt, "receipt": str(receipt_path),
                    "replaced": str(old) if old else None})


def manifest(args, ctx) -> Outcome:
    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        raise ShkError(EXIT_LOCAL, "bundle_missing", f"{args.directory} is not a directory")
    files, skipped = [], []
    for path, entry in _walk(root, True):
        if entry.is_file(follow_symlinks=False) and entry.name != "SHA256SUMS":
            files.append(path)
        elif not entry.is_dir(follow_symlinks=False) and entry.name != "SHA256SUMS":
            skipped.append(path)
    parse_manifest("".join(f"{'0' * 64}  {name}\n" for name in files))  # refuse bad names before hashing
    text = "".join(f"{_digest(root / name)}  {name}\n" for name in sorted(files))
    (root / ".shk-SHA256SUMS").write_text(text, encoding="utf-8")
    os.replace(root / ".shk-SHA256SUMS", root / "SHA256SUMS")
    return Outcome({"action": "manifest_written", "path": str(root / "SHA256SUMS"), "files": len(files),
                    "sha256": hashlib.sha256(text.encode()).hexdigest(), "skipped": sorted(skipped)})


def register(subparsers) -> None:
    sub = subparsers.add_parser("push", help="rsync LOCAL/ into data_host:REMOTE/ (never deletes)")
    sub.add_argument("local", metavar="LOCAL", help="local directory")
    sub.add_argument("remote", metavar="REMOTE", help="absolute remote directory under remote_root")
    sub.add_argument("--anywhere", action="store_true", help="allow REMOTE outside remote_root")
    sub.set_defaults(handler=push)
    sub = subparsers.add_parser("pull", help="fetch the files REMOTE/SHA256SUMS lists, verify, then promote")
    sub.add_argument("remote", metavar="REMOTE", help="absolute remote directory holding SHA256SUMS")
    sub.add_argument("local", metavar="LOCAL", help="directory to create; its parent must exist")
    sub.add_argument("--validate", metavar="NAME", help="run validators.NAME from the config on the stage first")
    sub.add_argument("--replace", action="store_true", help="swap an existing LOCAL (kept as .LOCAL.shk-old-*)")
    sub.set_defaults(handler=pull)
    sub = subparsers.add_parser("manifest", help="write DIR/SHA256SUMS (inside jobs; never uses the network)")
    sub.add_argument("directory", metavar="DIR", help="directory whose regular files to list")
    sub.set_defaults(handler=manifest)
