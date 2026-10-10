"""Manifest-bound bundle transfer with same-filesystem recoverable promotion."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile

from sherlock_orchestration import DIGEST, SafetyError, canonical, digest


IDENTITY_FIELDS = ("attempt", "cluster", "principal", "code_digest", "input_digest", "runtime_digest", "policy_digest")
STDERR_TAIL_LIMIT = 500
VALIDATOR_FIELDS = ("validator_path", "validator_digest", "validator_function")
ATTEMPT_RECORD_FIELDS = ("schema_version", "attempt", "producer", *VALIDATOR_FIELDS, "manifest", "manifest_sha256")
# A remote manifest is at most 4 MiB; the sidecar embeds it plus a few short fields.
ATTEMPT_SIDECAR_LIMIT = 4 * 1024 * 1024 + 64 * 1024
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class TransferError(SafetyError):
    """rsync did not complete; the private stage keeps partial bytes for a later resume.

    returncode is rsync's exit status, or None when the whole-transfer deadline
    passed. stderr_tail is bounded, single-line, free of control characters and of
    the toolkit's own temporary file-list path, so it is safe to print.
    """

    def __init__(self, message, *, returncode=None, stderr_tail=""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


def _stderr_tail(raw, *, redact=(), limit=STDERR_TAIL_LIMIT):
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
    for secret in redact:
        text = text.replace(secret, "<files-from>")
    text = "".join(ch for ch in text if ch.isspace() or (ord(ch) >= 32 and ord(ch) != 127))
    return " ".join(text.split())[-limit:]


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


def checked_producer(identity):
    if not isinstance(identity, dict) or not all(isinstance(identity.get(key), str) and identity[key] for key in IDENTITY_FIELDS):
        raise SafetyError("producer identity missing")
    for key in IDENTITY_FIELDS[3:]:
        if not DIGEST.fullmatch(identity[key]):
            raise SafetyError("producer digest malformed")
    return identity


def checked_manifest(manifest, *, expected_identity=None, max_items=10000, max_bytes=64 * 1024 * 1024):
    if manifest.get("schema_version") != 1:
        raise SafetyError("unsupported manifest schema")
    identity = checked_producer(manifest.get("producer", {}))
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


def _clean_text(value):
    return isinstance(value, str) and bool(value) and not any(ch in value for ch in "\x00\n\r")


def _normalized_json(value):
    try:
        return json.loads(canonical(value))
    except (TypeError, ValueError):
        raise SafetyError("attempt sidecar malformed") from None


def checked_attempt_record(record):
    """Shape and internal consistency of an attempt sidecar; the manifest itself is checked by fetch_bundle."""
    record = _normalized_json(record)
    if not isinstance(record, Mapping) or sorted(record) != sorted(ATTEMPT_RECORD_FIELDS) or record["schema_version"] != 1:
        raise SafetyError("attempt sidecar malformed")
    if not _clean_text(record["attempt"]):
        raise SafetyError("attempt sidecar malformed")
    try:
        checked_producer(record["producer"])
    except SafetyError:
        raise SafetyError("attempt sidecar malformed") from None
    path, validator_digest, function = (record[key] for key in VALIDATOR_FIELDS)
    if not _clean_text(path) or not path.startswith("/"):
        raise SafetyError("attempt sidecar malformed")
    if not isinstance(validator_digest, str) or not DIGEST.fullmatch(validator_digest):
        raise SafetyError("attempt sidecar malformed")
    if not isinstance(function, str) or not IDENTIFIER.fullmatch(function):
        raise SafetyError("attempt sidecar malformed")
    manifest, manifest_digest = record["manifest"], record["manifest_sha256"]
    if not isinstance(manifest, Mapping) or not isinstance(manifest_digest, str) or not DIGEST.fullmatch(manifest_digest):
        raise SafetyError("attempt sidecar malformed")
    if manifest_digest != digest(manifest) or record["producer"] != manifest.get("producer") or record["attempt"] != record["producer"]["attempt"]:
        raise SafetyError("attempt sidecar disagrees with manifest")
    return record


def attempt_record(attempt_id, producer, validator, manifest):
    """The durable attempt identity kept next to a fetched bundle for network-free local recovery."""
    if not isinstance(validator, Mapping) or not all(key in validator for key in VALIDATOR_FIELDS):
        raise SafetyError("attempt sidecar malformed")
    record = {"schema_version": 1, "attempt": attempt_id, "producer": producer,
              **{key: validator[key] for key in VALIDATOR_FIELDS},
              "manifest": manifest, "manifest_sha256": digest(_normalized_json(manifest))}
    return checked_attempt_record(record)


def attempt_sidecar_path(destination):
    destination = no_symlink_ancestors(destination)
    return destination.parent / ("." + destination.name + ".shk-attempt.json")


def read_attempt_sidecar(destination):
    """Only a private regular file owned by the caller; a missing sidecar raises FileNotFoundError."""
    path = attempt_sidecar_path(destination)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SafetyError("attempt sidecar symlink refused") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SafetyError("attempt sidecar is not a regular file")
        if info.st_uid != os.geteuid():
            raise SafetyError("attempt sidecar owner mismatch")
        if info.st_mode & 0o077:
            raise SafetyError("attempt sidecar must remain private")
        if info.st_size > ATTEMPT_SIDECAR_LIMIT:
            raise SafetyError("attempt sidecar exceeds size limit")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(ATTEMPT_SIDECAR_LIMIT + 1)
    finally:
        os.close(fd)
    if len(raw) > ATTEMPT_SIDECAR_LIMIT:
        raise SafetyError("attempt sidecar exceeds size limit")
    try:
        record = json.loads(raw)
    except ValueError:
        raise SafetyError("attempt sidecar malformed") from None
    return checked_attempt_record(record)


def _agreeing_sidecar(record, manifest):
    """The caller's sidecar must describe exactly the manifest being fetched."""
    record = checked_attempt_record(record)
    producer = manifest["producer"]
    if record["manifest_sha256"] != digest(manifest) or record["producer"] != producer or record["attempt"] != producer["attempt"]:
        raise SafetyError("attempt sidecar disagrees with manifest")
    return record


def _ensure_sidecar(destination, record):
    """Under the destination lock: an existing sidecar must equal the new one, else write it durably."""
    path = attempt_sidecar_path(destination)
    if path.is_symlink() or path.exists():
        if canonical(read_attempt_sidecar(destination)) != canonical(record):
            raise SafetyError("existing attempt sidecar conflicts")
        return
    durable_json(path, record)


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
        try:
            subprocess.run(argv, check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            tail = _stderr_tail(exc.stderr, redact=(name,))
            raise TransferError(f"rsync transfer failed with exit status {exc.returncode}" + (f": {tail}" if tail else ""),
                                returncode=exc.returncode, stderr_tail=tail) from None
        except subprocess.TimeoutExpired as exc:
            tail = _stderr_tail(exc.stderr, redact=(name,))
            raise TransferError(f"rsync transfer exceeded its {timeout} s deadline" + (f": {tail}" if tail else ""),
                                returncode=None, stderr_tail=tail) from None
    finally:
        os.unlink(name)


def fetch_bundle(manifest, destination, transfer, *, source_manifest, validator,
                 validator_digest, expected_identity=None, max_items=10000,
                 max_bytes=64 * 1024 * 1024, fault=None, attempt_record=None):
    """Recover after either side of rename/receipt without exposing partial bytes.

    transfer(stage, manifest) transfers bytes only. source_manifest() rereads the
    immutable manifest through the control endpoint, never through a DTN shell.
    validator(stage) is the workload's scientific validator, not a generic regex.
    attempt_record, when given, is written as .NAME.shk-attempt.json immediately
    before the transaction record (or on recovery, before the receipt) so a later
    local recovery needs no network; an existing sidecar must equal it.
    """
    manifest = json.loads(canonical(manifest))
    total = checked_manifest(manifest, expected_identity=expected_identity, max_items=max_items, max_bytes=max_bytes)
    if not DIGEST.fullmatch(validator_digest):
        raise SafetyError("validator identity must be pinned")
    sidecar = None if attempt_record is None else _agreeing_sidecar(attempt_record, manifest)
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
            if sidecar is not None:
                _ensure_sidecar(destination, sidecar)
            durable_json(receipt_path, metadata)
            return {"destination": str(destination), "receipt": metadata, "recovered": True}
        stable()
        if sidecar is not None:
            _ensure_sidecar(destination, sidecar)
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
