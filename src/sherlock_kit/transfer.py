"""`shk push`, `shk pull`, `shk manifest`: rsync through the DTN, bound by a SHA256SUMS manifest.

push: rsync -rlt LOCAL/ to data_host:REMOTE/ (below remote_root unless --anywhere), never deleting. pull (flock
.NAME.shk-lock): SHA256SUMS via manifest.sh (it must list every regular file), rsync into the resumable 0700 stage
.NAME.shk-stage, verify, re-read, validators[NAME] + [stage] (cwd project root, exit 0), rename onto LOCAL (--replace
keeps the old one as .NAME.shk-old-*), .NAME.shk-receipt.json. The DTN refuses rsync forms that change its remote
command line: add no options (--files-from=- sends the same command as 0.3.0's DTN-proven list file).
"""
from __future__ import annotations

import fcntl
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

from sherlock_kit import __version__, executor
from sherlock_kit.config import DEFAULTS
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, EXIT_UNREACHABLE, Outcome, ShkError

LINE, MAX_FILES, MAX_BYTES = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)"), 10_000, 4 << 20
PUSH = ("rsync", "-rlt", "--protect-args", "--partial")
PULL = ("rsync", "-r", "--protect-args", "--checksum", "--partial", "--from0", "--files-from=-")
_fail = partial(ShkError, EXIT_LOCAL)  # _fail(token, message, hint=None)


def _digest(path: Path) -> str:
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _parents(names) -> set:
    return {"/".join(parts[:i]) for parts in (name.split("/") for name in names) for i in range(1, len(parts))}


def parse_manifest(text: str) -> dict:
    """{relative path: sha256} from SHA256SUMS text, else ShkError(1, "manifest_invalid")."""
    entries, size = {}, len(text.encode("utf-8", "surrogateescape"))
    for number, line in ((n, x) for n, x in enumerate(text.split("\n") if size <= MAX_BYTES else (), 1) if x):
        match = LINE.fullmatch(line)
        name = match[2].removeprefix("./") if match else ""
        parts = name.split("/")
        if (any(p in ("", ".", "..") or p.startswith(".shk") for p in parts) or parts[-1] == "SHA256SUMS"
                or not name.isprintable() or "\\" in name or name in entries):
            raise _fail("manifest_invalid", f"SHA256SUMS line {number} {line[:99]!r}: want '<sha256>  <path>', a "
                        "new relative path free of '.', '..', .shk*, SHA256SUMS, escapes and control characters")
        entries[name] = match[1].lower()
    clash = sorted(_parents(entries) & entries.keys())
    if clash or not 0 < len(entries) <= MAX_FILES or size > MAX_BYTES:
        raise _fail("manifest_invalid", f"SHA256SUMS lists {clash[0]!r} as a file and a directory" if clash
                    else f"SHA256SUMS must list 1-{MAX_FILES} files in at most {MAX_BYTES} bytes")
    return entries


def _walk(root: Path, prune: bool, rel: str = ""):
    with os.scandir(root / rel) as scan:  # symlinks are never followed; prune skips .shk* names
        for entry in (e for e in scan if not (prune and e.name.startswith(".shk"))):
            path = f"{rel}/{entry.name}" if rel else entry.name
            yield path, entry
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(root, prune, path)


def _verify(stage: Path, entries: dict) -> int:
    walked = [(path, entry.is_file(follow_symlinks=False)) for path, entry in _walk(stage, False)]
    files, other = {path for path, regular in walked if regular}, {path for path, regular in walked if not regular}
    odd = sorted(entries.keys() ^ files) + sorted(other - _parents(entries))
    odd = odd or [name for name, digest in entries.items() if _digest(stage / name) != digest]
    if odd:
        raise _fail("verify_failed", f"{stage} differs from SHA256SUMS at {odd[:3]}; nothing was promoted",
                    f"rerun once SHA256SUMS is final; if the stage is stale, first rm -r {shlex.quote(str(stage))}")
    return sum((stage / name).stat().st_size for name in entries)


def _run(argv, timeout: int, label: str = "rsync", stdin=None) -> None:
    """rsync or a validator with the executor's group-kill timeout; a failure reports a clean, bounded tail."""
    done = executor.LocalExecutor().run(list(argv), stdin=stdin, timeout=timeout)  # banner lines already dropped
    if done.status != "complete":
        tail = " ".join("".join(c for c in done.stdout + done.stderr if c.isprintable() or c.isspace()).split())
        what = {"failed": f"exit {done.returncode}", "timeout": f"timed out after {timeout} s"}.get(done.status)
        token = "transfer_failed" if label == "rsync" else "validator_failed"
        raise ShkError(EXIT_UNREACHABLE if token == "transfer_failed" and done.returncode in (12, 255) else EXIT_LOCAL,
                       token, f"{label} {what or done.status}" + (f": {tail[-500:]}" if tail else ""),
                       "fix the cause, then rerun: push is idempotent; pull resumes its stage and promotes nothing")


def _remote(path: str) -> str:
    if not path.startswith("/") or ".." in path.split("/") or set(path) & set("\0\n\r"):
        raise _fail("invalid_remote_path", f"REMOTE {path!r} must be one absolute path without '..'")
    return path.rstrip("/") or "/"


def push(args, ctx) -> Outcome:
    remote, local = _remote(args.remote), Path(args.local).expanduser().resolve()
    root = None if args.anywhere else ctx.require("remote_root").remote_root
    if root and remote != root and not remote.startswith(root + "/"):
        raise ShkError(EXIT_REFUSED, "outside_remote_root", f"{remote} is outside remote_root {root}", "--anywhere")
    if not local.is_dir():
        raise _fail("local_missing", f"LOCAL {args.local} is not a directory")
    options, target = ctx.data_executor().rsync_args(remote)
    _run([*PUSH, *options, "--", f"{local}/", f"{target}/"], (ctx.config or DEFAULTS).transfer_timeout)
    return Outcome({"action": "pushed", "local": str(local), "remote": remote, "target": target})


def _read_manifest(control, remote: str) -> tuple[str, int]:
    lines = executor.check(executor.run_program(control, "manifest", [remote]), "read SHA256SUMS").stdout.split("\n")
    fields = dict(line.split(" ", 1) for line in lines if re.match(r"SHK_[A-Z]+ ", line))
    error = fields.get("SHK_ERROR")
    if error or "SHK_END" not in lines or not fields.get("SHK_COUNT", "").isdigit():
        raise _fail("manifest_unavailable" if error else "malformed_reply", f"{remote}/SHA256SUMS: "
                    f"{(error or 'incomplete reply').replace('_', ' ')}", "write it last in the job: shk manifest DIR "
                    "(or the sha256sum one-liner)" if error else "run shk doctor --remote")
    return "".join(line[6:] + "\n" for line in lines if line.startswith("SHK_M|")), int(fields["SHK_COUNT"])


def _promote(stage: Path, dest: Path, old: Path) -> Path | None:
    old = old if os.path.lexists(dest) else None
    try:  # an existing dest moves to old first and comes back if the second rename fails
        if old:
            os.rename(dest, old)
        os.rename(stage, dest)
    except OSError as exc:
        if old and not os.path.lexists(dest):
            os.rename(old, dest)
        raise _fail("promote_failed", f"cannot move the stage onto {dest} ({exc}); {dest} is unchanged") from None
    return old


def pull(args, ctx) -> Outcome:
    remote, settings, now = _remote(args.remote), ctx.config or DEFAULTS, ctx.clock()
    if args.validate is not None and args.validate not in settings.validators:
        raise _fail("validator_unknown", f"no validator {args.validate!r} in [tool.sherlock-kit.validators]")
    local = Path(os.path.abspath(Path(args.local).expanduser()))
    if not local.name or not local.parent.is_dir():
        raise _fail("local_parent_missing", f"LOCAL {args.local} must name an entry in an existing directory")
    dest = local.parent.resolve() / local.name
    stage, receipt_path = (dest.with_name(f".{dest.name}.shk-{kind}") for kind in ("stage", "receipt.json"))
    options, target = ctx.data_executor().rsync_args(remote)
    with open(dest.with_name(f".{dest.name}.shk-lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a concurrent pull waits here, then finds the receipt
        text, count = _read_manifest(ctx.executor(), remote)
        entries, digest = parse_manifest(text), hashlib.sha256(text.encode()).hexdigest()
        if count != len(entries):
            raise _fail("manifest_incomplete", f"{remote} holds {count} files; SHA256SUMS lists {len(entries)}")
        mine = receipt_path.is_file() and digest in receipt_path.read_text("utf-8", "replace")  # our own JSON
        if mine and os.path.lexists(dest):
            return Outcome({"action": "already_present", "local": str(dest), "manifest_sha256": digest})
        if os.path.lexists(dest) and not args.replace:
            raise ShkError(EXIT_REFUSED, "destination_exists", f"{dest} exists without a receipt for this "
                           "SHA256SUMS", "pass --replace to swap it in; the old copy is kept as .NAME.shk-old-*")
        stage.mkdir(exist_ok=True)
        stage.chmod(0o700)
        _run([*PULL, *options, "--", f"{target}/", f"{stage}/"], settings.transfer_timeout,
             stdin=b"".join(name.encode() + b"\0" for name in entries))
        total = _verify(stage, entries)
        if _read_manifest(ctx.executor(), remote) != (text, count):
            raise _fail("manifest_changed", f"{remote} changed during the transfer; nothing was promoted")
        if args.validate is not None:
            _run(["sh", "-c", 'cd "$1" && shift && exec "$@"', "shk-validate", str(settings.root),
                  *settings.validators[args.validate], str(stage)], settings.transfer_timeout, "validator")
            total = _verify(stage, entries)
        old = _promote(stage, dest, dest.with_name(f".{dest.name}.shk-old-{now:%Y%m%dT%H%M%S%fZ}"))
        receipt = {"manifest_sha256": digest, "remote": remote, "files": len(entries), "bytes": total,
                   "validator": args.validate, "shk_version": __version__, "created_utc": f"{now:%Y-%m-%dT%H:%M:%SZ}"}
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return Outcome({"action": "pulled", "local": str(dest), **receipt, "replaced": str(old) if old else None})


def manifest(args, ctx) -> Outcome:
    root, files, skipped = Path(args.directory).expanduser().resolve(), [], []
    if not root.is_dir():
        raise _fail("bundle_missing", f"{args.directory} is not a directory")
    for path, entry in _walk(root, True):
        if entry.name != "SHA256SUMS" and not entry.is_dir(follow_symlinks=False):
            (files if entry.is_file(follow_symlinks=False) else skipped).append(path)
    parse_manifest("".join(f"{'0' * 64}  {name}\n" for name in files))  # refuse bad names before hashing
    text = "".join(f"{_digest(root / name)}  {name}\n" for name in sorted(files))
    (root / ".shk-SHA256SUMS").write_text(text, encoding="utf-8")
    os.replace(root / ".shk-SHA256SUMS", root / "SHA256SUMS")
    return Outcome({"action": "manifest_written", "path": str(root / "SHA256SUMS"), "files": len(files),
                    "sha256": hashlib.sha256(text.encode()).hexdigest(), "skipped": sorted(skipped)})


def register(subparsers) -> None:
    to = subparsers.add_parser("push", help="rsync LOCAL/ into data_host:REMOTE/ (never deletes)")
    to.add_argument("local", metavar="LOCAL")
    to.add_argument("remote", metavar="REMOTE", help="absolute; below remote_root unless --anywhere")
    to.add_argument("--anywhere", action="store_true", help="allow REMOTE outside remote_root")
    back = subparsers.add_parser("pull", help="fetch what REMOTE/SHA256SUMS lists, verify it, then create LOCAL")
    back.add_argument("remote", metavar="REMOTE")
    back.add_argument("local", metavar="LOCAL", help="new directory; its parent must exist")
    back.add_argument("--validate", metavar="NAME", help="first run validators.NAME from the config on the stage")
    back.add_argument("--replace", action="store_true", help="swap an existing LOCAL (kept as .LOCAL.shk-old-*)")
    sums = subparsers.add_parser("manifest", help="write DIR/SHA256SUMS (inside jobs; never uses the network)")
    sums.add_argument("directory", metavar="DIR")
    for parser, handler in ((to, push), (back, pull), (sums, manifest)):
        parser.set_defaults(handler=handler)
