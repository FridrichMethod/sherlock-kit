"""Manifest-bound bundle transfer with same-filesystem recoverable promotion."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile

from sherlock_orchestration import DIGEST, SafetyError, canonical, digest


IDENTITY_FIELDS = ("attempt", "cluster", "principal", "code_digest", "input_digest", "runtime_digest", "policy_digest")


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise SafetyError("metadata symlink refused")
    fd, name = tempfile.mkstemp(prefix=".shk-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def no_symlink_ancestors(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SafetyError("symlink path/ancestor refused")
    return path


@contextmanager
def destination_lock(destination):
    destination = no_symlink_ancestors(destination)
    parent = destination.parent
    if not parent.is_dir():
        raise SafetyError("destination parent must already exist")
    lock = parent / ("." + destination.name + ".shk-lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SafetyError("invalid lock file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield destination
    finally:
        os.close(fd)


def checked_manifest(manifest, *, expected_identity=None, max_items=10000, max_bytes=64 * 1024 * 1024):
    if manifest.get("schema_version") != 1:
        raise SafetyError("unsupported manifest schema")
    identity = manifest.get("producer", {})
    if not isinstance(identity, dict) or not all(isinstance(identity.get(key), str) and identity[key] for key in IDENTITY_FIELDS):
        raise SafetyError("producer identity missing")
    for key in IDENTITY_FIELDS[3:]:
        if not DIGEST.fullmatch(identity[key]):
            raise SafetyError("producer digest malformed")
    if expected_identity is not None and identity != expected_identity:
        raise SafetyError("manifest producer does not match admitted attempt")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > max_items:
        raise SafetyError("manifest item count invalid")
    seen, total = set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise SafetyError("manifest item schema invalid")
        name = item["path"]
        if not isinstance(name, str) or "\x00" in name or "\n" in name or "\r" in name or "\\" in name:
            raise SafetyError("invalid relative artifact path")
        parts = PurePosixPath(name)
        if not parts.parts or parts.is_absolute() or name != str(parts) or any(p in {".", ".."} or p.startswith(".shk") for p in parts.parts):
            raise SafetyError("artifact path escape/reserved metadata name")
        if name in seen or any(name.startswith(other + "/") or other.startswith(name + "/") for other in seen):
            raise SafetyError("duplicate/path-prefix artifact inventory")
        seen.add(name)
        if type(item["size"]) is not int or item["size"] < 0 or not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"]):
            raise SafetyError("invalid artifact size/digest")
        total += item["size"]
    if total > max_bytes:
        raise SafetyError("bundle exceeds real byte ceiling")
    return total


def inventory(root):
    """Only regular files/directories; do not follow any links or special files."""
    root = no_symlink_ancestors(root)
    if not root.is_dir():
        raise SafetyError("bundle is not a directory")
    files = set()
    for base, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(base) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise SafetyError("symlink/special file in artifact bundle")
            if stat.S_ISREG(mode):
                files.add(path.relative_to(root).as_posix())
    return files


def file_hash(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SafetyError("artifact is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            h = hashlib.file_digest(stream, "sha256").hexdigest()
        os.fsync(fd)
        return h
    finally:
        os.close(fd)


def verify_bundle(root, manifest):
    expected = {item["path"] for item in manifest["files"]}
    if inventory(root) != expected:
        raise SafetyError("artifact inventory mismatch")
    expected_directories = {str(parent) for name in expected for parent in PurePosixPath(name).parents if str(parent) != "."}
    actual_directories = {Path(base).relative_to(root).as_posix() for base, _, _ in os.walk(root) if Path(base) != Path(root)}
    if actual_directories != expected_directories:
        raise SafetyError("artifact directory inventory mismatch")
    for item in manifest["files"]:
        path = Path(root) / item["path"]
        if path.stat().st_size != item["size"] or file_hash(path) != item["sha256"]:
            raise SafetyError("artifact size/checksum mismatch")
    # Durability is recursive, including nested directory entries.
    directories = [Path(base) for base, _, _ in os.walk(root)]
    for path in reversed(directories):
        fsync_dir(path)


def build_manifest(root, producer):
    root = Path(root)
    names = sorted(inventory(root))
    manifest = {"schema_version": 1, "producer": producer,
                "files": [{"path": name, "size": (root / name).stat().st_size, "sha256": file_hash(root / name)} for name in names]}
    checked_manifest(manifest, max_bytes=2**63 - 1)
    return manifest


def rsync_transfer(source, stage, manifest, *, ssh_command=None, timeout=300):
    """Exact file selection, resumable private stage, then independent verification."""
    if not isinstance(source, str) or any(ch in source for ch in "\x00\n\r"):
        raise SafetyError("invalid transfer source")
    stage = Path(stage)
    fd, name = tempfile.mkstemp(prefix=".shk-files-", dir=stage.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            for item in manifest["files"]:
                stream.write(item["path"].encode() + b"\0")
        argv = ["rsync", "-r", "--protect-args", "--checksum", "--partial", "--from0", "--files-from=" + name]
        if ssh_command is not None:
            argv += ["-e", ssh_command]
        argv += ["--", source.rstrip("/") + "/", str(stage) + "/"]
        subprocess.run(argv, check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    finally:
        os.unlink(name)


def fetch_bundle(manifest, destination, transfer, *, source_manifest, validator,
                 validator_digest, expected_identity=None, max_items=10000,
                 max_bytes=64 * 1024 * 1024, fault=None):
    """Recover after either side of rename/receipt without exposing partial bytes.

    transfer(stage, manifest) transfers bytes only. source_manifest() rereads the
    immutable manifest through the control endpoint, never through a DTN shell.
    validator(stage) is the workload's scientific validator, not a generic regex.
    """
    manifest = json.loads(canonical(manifest))
    total = checked_manifest(manifest, expected_identity=expected_identity, max_items=max_items, max_bytes=max_bytes)
    if not DIGEST.fullmatch(validator_digest):
        raise SafetyError("validator identity must be pinned")
    manifest_digest = digest(manifest)
    def crash(point):
        if fault:
            fault(point)
    def stable():
        if digest(source_manifest()) != manifest_digest:
            raise SafetyError("source manifest changed during transfer")
    with destination_lock(destination) as destination:
        transaction_path = destination.parent / ("." + destination.name + ".shk-transaction.json")
        receipt_path = destination.parent / ("." + destination.name + ".shk-receipt.json")
        stage = destination.parent / ("." + destination.name + ".shk-stage-" + manifest_digest)
        metadata = {"schema_version": 1, "manifest_sha256": manifest_digest,
                    "producer": manifest["producer"], "validator_sha256": validator_digest}
        if transaction_path.exists():
            if transaction_path.is_symlink() or json.loads(transaction_path.read_text()) != metadata:
                raise SafetyError("existing transfer transaction conflicts")
        if receipt_path.exists():
            if receipt_path.is_symlink() or json.loads(receipt_path.read_text()) != metadata:
                raise SafetyError("existing artifact receipt conflicts")
        if destination.exists():
            verify_bundle(destination, manifest)
            if validator(destination) is not True:
                raise SafetyError("scientific validation failed for promoted bundle")
            verify_bundle(destination, manifest)
            durable_json(receipt_path, metadata)
            return {"destination": str(destination), "receipt": metadata, "recovered": True}
        stable()
        durable_json(transaction_path, metadata)
        crash("intent_committed")
        no_symlink_ancestors(stage)
        stage.mkdir(mode=0o700, exist_ok=True)
        if stage.stat().st_mode & 0o077:
            raise SafetyError("stage must remain private")
        if shutil.disk_usage(stage).free < total + 1024 * 1024:
            raise SafetyError("insufficient local capacity")
        inventory(stage)
        transfer(stage, json.loads(canonical(manifest)))
        crash("transferred")
        verify_bundle(stage, manifest)
        stable()
        if validator(stage) is not True:
            raise SafetyError("scientific validation failed")
        verify_bundle(stage, manifest)
        stable()
        crash("verified")
        if destination.exists() or destination.is_symlink():
            raise SafetyError("destination conflict; never overwrite unrelated data")
        os.rename(stage, destination)
        fsync_dir(destination.parent)
        crash("promoted")
        durable_json(receipt_path, metadata)
        crash("receipt_committed")
        # Keep transaction evidence permanently; no deletion before verification.
        return {"destination": str(destination), "receipt": metadata, "recovered": False}
