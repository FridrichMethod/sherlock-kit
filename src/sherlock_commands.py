"""Minimal CLI for explicit typed Slurm admission, bounded reconciliation, partition occupancy and fetch.

The attempt registry on Sherlock (``sherlock_registry``) and Slurm accounting are
the only durable attempt state. Every typed command ships the registry program to
the login node; the workstation keeps nothing but the shared authentication
cooldown and a 60 s query cache under one explicit private state root, so any
POSIX workstation can operate.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
import time
from types import ModuleType
from uuid import uuid4

from sherlock_artifacts import attempt_record, checked_manifest, durable_json, fetch_bundle, read_attempt_sidecar
from sherlock_orchestration import AttemptSpec, IDENTITY_KEYS, SafetyError, canonical, digest, frozen_record, packaged_profile, sbatch_options
from sherlock_registry import (ABANDON_SECONDS, HEX32, MAX_OPEN, RegistryError, parse_event_reply, parse_fetch_reply, parse_read, parse_submit_reply,
                               program_argv, program_sha256, resolve, task_key)


REQUIRED_CONFIG = frozenset({'schema_version', 'registry_root', 'principal', 'cluster', 'transport'})
OPTIONAL_CONFIG = frozenset({'fetch_root', 'validator_roots', 'remote_roots', 'grant'})
COUNT = re.compile('[0-9]+')
SQUEUE_COLUMNS = (('UserName', 64), ('State', 24), ('tres-alloc', 128), ('TimeUsed', 24), ('TimeLimit', 24))
RUNNING_STATES = frozenset({'RUNNING', 'COMPLETING'})
PENDING_STATES = frozenset({'PENDING'})
PREEMPTION_MESSAGE = 'unexpected preemption on a non-preemptible partition; investigate, then `reconcile --acknowledge-preemption`'
EXPLICIT_ROOT_MESSAGE = 'typed commands need an explicit local state location: set transport.backoff_file or SHERLOCK_KIT_STATE_ROOT'
CACHE_MESSAGE = 'query cache malformed or unsafe; remove query-cache.json explicitly'
QUERY_CACHE_SECONDS = 60
QUERY_SENTINEL = {'status': 'query_pending_or_failed'}
QUERY_BODY_LIMIT = 8 * 1024 * 1024
QUERY_FILE_LIMIT = 64 * 1024 * 1024
REMOTE_ARGV_LIMIT = 100_000
STDERR_TAIL = 500
# Runner refusal tokens (sherlock_registry) translated for the operator; unknown tokens pass through verbatim.
REFUSAL_TEXT = {
    'record_invalid': 'registry runner refused the attempt record as invalid',
    'principal_mismatch': 'login principal differs from the spec principal',
    'registry_unsafe': 'registry_root is not an owned private directory (0700, no symlinks) on Sherlock',
    'run_directory_invalid': 'remote_run_directory must be an existing canonical directory on Sherlock',
    'digest_mismatch': 'script bytes on Sherlock differ from script_digest or carry #SBATCH directives',
    'script_unreadable': 'remote_script could not be read on Sherlock',
    'registry_busy': 'registry is busy; retry later',
    'parent_unknown': 'parent_attempt names a logical task that has no attempt yet',
    'parent_unverifiable': 'parent attempt could not be verified against Slurm accounting',
    'attempt_exists': 'attempt id already exists in the registry',
    'record_write_failed': 'registry could not write the attempt record',
    'duplicate_logical_task': 'logical task already has attempt {detail}; name it as parent_attempt to retry',
    'parent_mismatch': 'parent_attempt differs from the latest attempt {detail} of this logical task',
    'parent_not_released': 'parent attempt is not released ({detail}); a retry waits for not_sent, abandoned or a terminal result without COMPLETED',
    'parent_conflict': 'parent attempt record conflicts ({detail})',
}


@dataclass(frozen=True)
class Outcome:
    """What an operation prints, its exit code and bounded stderr notes (printed with the ``shk:`` prefix)."""
    result: object
    code: int = 0
    notes: tuple = ()


def load_validator(path, expected_digest, function):
    """Execute precisely the verified source snapshot, never a stale .pyc."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SafetyError('validator source must be a regular file')
        source = stream.read()
    if hashlib.sha256(source).hexdigest() != expected_digest:
        raise SafetyError('workload validator code identity mismatch')
    module = ModuleType('shk_validator_' + expected_digest)
    module.__file__ = str(path)
    # Some adapters use dataclasses, which resolve their defining module by name.
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(path), 'exec'), module.__dict__)
        validator = getattr(module, function)
        if not callable(validator):
            raise SafetyError('workload validator must be callable')
        return validator
    finally:
        sys.modules.pop(module.__name__, None)


def checked_registry_root(value):
    """An absolute canonical POSIX path: the runner compares it byte for byte on every call."""
    if not isinstance(value, str) or not value.startswith('/') or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SafetyError('registry_root must be an absolute POSIX path without control characters')
    # PurePosixPath keeps a leading double slash (an implementation-defined root), so it is refused explicitly.
    if any(part in {'.', '..'} for part in PurePosixPath(value).parts) or value != str(PurePosixPath(value)) or value.startswith('//'):
        raise SafetyError('registry_root must be canonical: no ., .. or repeated/trailing slashes')
    return value


def private_config(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SafetyError('configuration must be an owned private regular file (0600)')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not REQUIRED_CONFIG <= value.keys() or value['schema_version'] != 1:
        raise SafetyError('private config schema mismatch')
    unknown = sorted(value.keys() - (REQUIRED_CONFIG | OPTIONAL_CONFIG))
    if unknown:
        raise SafetyError('unknown private config field(s): ' + ', '.join(unknown) + '; filesystem and partition access are not configurable')
    checked_registry_root(value['registry_root'])
    if not isinstance(value['transport'], dict):
        raise SafetyError('private config transport must be a mapping')
    return value


def transport_options(config):
    """The transport mapping as configured; typed commands never fall back to $HOME or XDG for local state."""
    options = {**config['transport']}
    if options.get('backoff_file') is None and 'SHERLOCK_KIT_STATE_ROOT' not in os.environ:
        raise SafetyError(EXPLICIT_ROOT_MESSAGE)
    return options


def checked_attempt_id(attempt):
    if not isinstance(attempt, str) or not HEX32.fullmatch(attempt):
        raise SafetyError('attempt id must be 32 lowercase hex characters')
    return attempt


def refusal_error(token):
    """SafetyError for one runner refusal token such as ``duplicate_logical_task:<attempt>``."""
    head, _, detail = token.partition(':')
    text = REFUSAL_TEXT.get(head)
    if text is None:
        return SafetyError(token)
    return SafetyError(text.format(detail=detail or '?'))


def stderr_tail(text):
    return ' '.join((text or '').split())[-STDERR_TAIL:]


# ---------------------------------------------------------------- query cache

def _valid_entry(entry):
    return (isinstance(entry, dict) and set(entry) == {'observed', 'body'}
            and type(entry['observed']) in (int, float) and entry['observed'] == entry['observed'])


def _read_cache(path):
    """Entries of an owned private regular cache file; anything else fails closed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > QUERY_FILE_LIMIT:
        raise SafetyError(CACHE_MESSAGE)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        data = stream.read(QUERY_FILE_LIMIT + 1)
    if len(data) > QUERY_FILE_LIMIT:
        raise SafetyError(CACHE_MESSAGE)
    try:
        document = json.loads(data)
    except ValueError:
        raise SafetyError(CACHE_MESSAGE) from None
    if not isinstance(document, dict) or document.get('schema_version') != 1:
        raise SafetyError(CACHE_MESSAGE)
    entries = document.get('entries')
    if not isinstance(entries, dict) or not all(_valid_entry(entry) for entry in entries.values()):
        raise SafetyError(CACHE_MESSAGE)
    return entries


def _write_cache(path, entries, now):
    """Durable 0600 rewrite with expired entries pruned; the caller holds the cache lock."""
    live = {key: entry for key, entry in entries.items() if now - entry['observed'] < QUERY_CACHE_SECONDS}
    durable_json(path, {'schema_version': 1, 'entries': live})


def _checked_body(result):
    try:
        text = canonical(result)
    except (TypeError, ValueError):
        raise SafetyError('query result is not JSON-serialisable') from None
    if len(text.encode()) > QUERY_BODY_LIMIT:
        raise SafetyError(f'query result exceeds the {QUERY_BODY_LIMIT} byte cache bound')
    return json.loads(text)


def cached_query(path, key, query, now=None):
    """Cache equivalent bounded query scopes and reserve cadence before the network.

    The slot is reserved with a sentinel under the cache lock, the query runs
    unlocked, and the result replaces the sentinel only when the slot's ``observed``
    stamp is still ours. A failed query leaves the sentinel, so a retry within the
    cadence gets ``{"status": "query_pending_or_failed"}`` instead of a new call.
    """
    from sherlock_kit import state_lock
    now = time.time() if now is None else now
    path = Path(path)
    if path.is_symlink():
        raise SafetyError(CACHE_MESSAGE)
    with state_lock(path):
        entries = _read_cache(path)
        entry = entries.get(key)
        if entry is not None and now - entry['observed'] < QUERY_CACHE_SECONDS:
            return entry['body']
        _write_cache(path, {**entries, key: {'observed': now, 'body': dict(QUERY_SENTINEL)}}, now)
    result = query()
    body = _checked_body(result)
    with state_lock(path):
        entries = _read_cache(path)
        if entries.get(key, {}).get('observed') == now:
            _write_cache(path, {**entries, key: {'observed': now, 'body': body}}, now)
    return result


def forget_queries(path, keys):
    """Drop the cached scopes a mutation of ours just changed, so the next read within the cadence is fresh.

    Returns stderr notes instead of raising: the mutation is already durable on
    Sherlock, so a cache problem here must not misreport it.
    """
    from sherlock_kit import state_lock
    path, dropped = Path(path), set(keys)
    try:
        if path.is_symlink():
            raise SafetyError(CACHE_MESSAGE)
        with state_lock(path):
            entries = _read_cache(path)
            kept = {key: entry for key, entry in entries.items() if key not in dropped}
            if len(kept) != len(entries):
                _write_cache(path, kept, time.time())
    except (SafetyError, OSError, ValueError) as exc:
        return (f'query cache not refreshed after the write ({exc}); the next read within {QUERY_CACHE_SECONDS} s may be stale',)
    return ()


# ---------------------------------------------------------------- registry reads and resolution

def controller_identity(config):
    return (config['principal'], config['cluster'])


def record_identity(record):
    spec = record.get('spec') if isinstance(record, dict) else None
    return (spec.get('principal'), spec.get('cluster')) if isinstance(spec, dict) else None


def controller_matches(record, config):
    return controller_identity(config) == record_identity(record)


def controller_owns(record, config):
    if not controller_matches(record, config):
        raise SafetyError('controller cannot query another principal/cluster')


def query_key(config, transport, *suffix):
    """The cache key of one bounded query scope: controller identity, control host, then the scope."""
    return canonical([config['cluster'], config['principal'], transport.control_host, *suffix])


def read_keys(config, transport, attempts):
    """Every cached read scope that a closure of ``attempts`` changes: the listing of open attempts and each attempt's own read."""
    return [query_key(config, transport, 'read-all'), *(query_key(config, transport, 'read', attempt) for attempt in attempts)]


def forget_reads(config, transport, attempts):
    from sherlock_kit import query_cache_path
    return forget_queries(query_cache_path(transport), read_keys(config, transport, attempts))


def read_registry(config, transport, selection, key_suffix):
    """One reader call per cache key and cadence: ``{'status', 'document'}``; malformed reader output raises."""
    from sherlock_kit import query_cache_path, run_remote
    argv = program_argv('read', config['registry_root'], *selection)
    key = query_key(config, transport, *key_suffix)

    def query():
        result = run_remote(transport, argv)
        if result.status != 'complete':
            return {'status': result.status, 'document': None}
        return {'status': 'complete', 'document': parse_read(result.stdout)}

    return cached_query(query_cache_path(transport), key, query)


def inconclusive_result(attempt, transport_status, **extra):
    return {'attempt': attempt, 'resolution': 'inconclusive', 'transport': transport_status, 'scientific_validation': 'unverified', **extra}


def event_kinds(entry):
    return sorted(event['kind'] for event in entry['events'])


def attempt_result(entry, resolution):
    result = {'attempt': entry['record'] or {'attempt': entry['attempt']}, 'resolution': resolution['resolution'], 'job_id': resolution['job_id'],
              'tasks': resolution['tasks'], 'cost': resolution['cost'], 'anomalies': resolution['anomalies'], 'events': event_kinds(entry),
              'scientific_validation': 'unverified'}
    if resolution['reason'] is not None:
        result['reason'] = resolution['reason']
    if resolution['resolution'] == 'error':
        result['error'] = resolution['reason']
    return result


def entry_error(entry, reason):
    return {'attempt': entry['record'] or {'attempt': entry['attempt']}, 'resolution': 'error', 'error': reason, 'reason': reason,
            'events': event_kinds(entry), 'scientific_validation': 'unverified'}


def closed_by_event(entry):
    """A not_sent or abandoned event decides the resolution without accounting rows (D4: the event wins)."""
    return any(event['kind'] in {'not_sent', 'abandoned'} for event in entry['events'])


def resolved_entry(entry, document):
    """resolve() for one reader entry; a registry-level problem is an isolated error, a sacct problem inconclusive.

    A sacct outage cannot reopen an attempt closed by its own event, so such an
    attempt resolves from its events alone and carries the outage in ``transport``.
    """
    if entry['errors']:
        return entry_error(entry, 'registry_entry_errors:' + ';'.join(entry['errors']))
    sacct = document.get('sacct')
    if sacct is None or sacct['status'] != 'complete' or sacct['truncated']:
        status = 'absent' if sacct is None else ('truncated' if sacct['status'] == 'complete' else sacct['status'])
        if closed_by_event(entry):
            return {**attempt_result(entry, resolve(entry['record'], entry['events'], [], document['now'])), 'transport': 'sacct:' + status}
        return inconclusive_result(entry['record'], 'sacct:' + status)
    return attempt_result(entry, resolve(entry['record'], entry['events'], sacct['rows'], document['now']))


def selected_entry(document, attempt, config):
    """The reader entry of one attempt; absence is a refusal and the record must belong to this controller."""
    entries = [entry for entry in document['attempts'] if entry.get('attempt') == attempt]
    if not entries or not entries[0]['exists']:
        raise SafetyError('unknown attempt')
    if entries[0]['record'] is not None:
        controller_owns(entries[0]['record'], config)
    return entries[0]


def with_resolved_events(results, config, transport):
    """reconcile only: one ``event resolved`` call for the fresh terminal attempts; refusals are non-fatal.

    Returns ``(results, notes)``; a written closure forgets the cached reads it changed.
    """
    from sherlock_kit import run_remote
    fresh = [result for result in results if result.get('resolution') == 'terminal' and 'resolved' not in result['events']]
    if not fresh:
        return results, ()
    ids = [result['attempt']['attempt'] for result in fresh]
    reply = run_remote(transport, program_argv('event', config['registry_root'], 'resolved', '{}', *ids), mutation=True)
    replies = {}
    if reply.status == 'complete':
        try:
            replies = {attempt: (outcome, value) for attempt, outcome, value in parse_event_reply(reply.stdout)}
        except RegistryError:
            replies = {}

    def annotated(result):
        if result not in fresh:
            return result
        if reply.status != 'complete':
            return {**result, 'resolved_event': 'transport:' + reply.status}
        outcome, value = replies.get(result['attempt']['attempt'], ('unknown', 'missing_reply'))
        if outcome == 'written':
            return {**result, 'events': [*result['events'], 'resolved']}
        return {**result, 'resolved_event': outcome + ':' + value}

    written = [attempt for attempt, (outcome, _) in replies.items() if outcome == 'written' and attempt in ids]
    notes = forget_reads(config, transport, written) if written else ()
    return [annotated(result) for result in results], notes


def abandon_hint(count):
    return (f'{count} attempt(s) abandonable: no accounting rows after {ABANDON_SECONDS} s and no known job id; '
            'after your own check, `shk reconcile --attempt ID --abandon` releases one')


def check_outcome(result, notes=()):
    """Exit code and stderr notes for one attempt result or a batch."""
    items = result if isinstance(result, list) else [result]
    resolutions = [item.get('resolution') for item in items if isinstance(item, dict)]
    failed = resolutions.count('error')
    extra = [f'{failed} attempt(s) could not be reconciled; see the "error" entries'] if failed else []
    abandonable = resolutions.count('abandonable')
    if abandonable:
        extra.append(abandon_hint(abandonable))
    preempted = 'unexpected_preemption' in resolutions
    if preempted:
        extra.append(PREEMPTION_MESSAGE)
    return Outcome(result, 2 if failed or preempted else 0, (*notes, *extra))


def status_one(args, config, transport, *, reconcile):
    attempt = checked_attempt_id(args.attempt)
    read = read_registry(config, transport, ['--attempt', attempt], ['read', attempt])
    if read['status'] != 'complete':
        return Outcome(inconclusive_result({'attempt': attempt}, read['status']))
    document = read['document']
    result = resolved_entry(selected_entry(document, attempt, config), document)
    notes = ()
    if reconcile:
        (result,), notes = with_resolved_events([result], config, transport)
    return check_outcome(result, notes)


def status_all(args, config, transport, *, reconcile):
    """Every open attempt through one reader call; each resolved on its own so one contract violation hides nothing."""
    read = read_registry(config, transport, ['--open'], ['read-all'])
    if read['status'] != 'complete':
        return Outcome([inconclusive_result(None, read['status'])])
    document = read['document']
    results = []
    for entry in document['attempts']:
        if entry['record'] is not None and not controller_matches(entry['record'], config):
            results.append(inconclusive_result(entry['record'], None, reason='controller cannot query another principal/cluster'))
        else:
            results.append(resolved_entry(entry, document))
    notes = ()
    if document['registry'].get('truncated'):
        notes = (f'open attempts truncated at {MAX_OPEN}; resolve some and run again',)
    if reconcile:
        results, event_notes = with_resolved_events(results, config, transport)
        notes = (*notes, *event_notes)
    return check_outcome(results, notes)


# ---------------------------------------------------------------- event writer (ack / abandon)

def event_precheck(kind, entry, result):
    """Cheap refusals from the last read; the event writer re-checks under the registry lock with fresh sacct."""
    resolution = result['resolution']
    if entry['record'] is None or resolution == 'error':
        raise SafetyError(f'attempt record cannot be used ({result.get("reason")}); repair the registry entry before any event')
    if kind == 'ack':
        if entry['record']['spec']['partition_profile']['preemptible']:
            raise SafetyError('acknowledgement applies to non-preemptible partitions only; this profile is preemptible')
        if resolution != 'unexpected_preemption':
            raise SafetyError(f'attempt is not in unexpected_preemption (resolution {resolution}); nothing to acknowledge')
    elif resolution != 'abandonable':
        raise SafetyError(f'attempt is not abandonable (resolution {resolution}); only an attempt without accounting rows after '
                          f'{ABANDON_SECONDS} s and without a known job id can be abandoned')


def event_reply(stdout, attempt):
    try:
        replies = {item[0]: item[1:] for item in parse_event_reply(stdout)}
    except RegistryError:
        return 'unknown', 'malformed_reply'
    return replies.get(attempt, ('unknown', 'malformed_reply'))


def event_operation(args, config, transport):
    from sherlock_kit import run_remote
    kind = 'ack' if args.acknowledge_preemption else 'abandon'
    flag = '--acknowledge-preemption' if kind == 'ack' else '--abandon'
    if not args.attempt:
        raise SafetyError(f'{flag} requires --attempt; it changes exactly one investigated attempt')
    attempt = checked_attempt_id(args.attempt)
    read = read_registry(config, transport, ['--attempt', attempt], ['read', attempt])
    # Ownership and the cheap pre-checks come from this read; without it nothing is sent (a failed read holds the cadence sentinel).
    if read['status'] != 'complete':
        raise SafetyError(f'{flag} needs a fresh registry read of the attempt before it writes; the last read is {read["status"]}')
    entry = selected_entry(read['document'], attempt, config)
    event_precheck(kind, entry, resolved_entry(entry, read['document']))
    note = args.note or ''
    reply = run_remote(transport, program_argv('event', config['registry_root'], kind, canonical({'note': note}), attempt), mutation=True)
    base = {'operation': kind, 'attempt': entry['record'], 'note': note, 'event': None, 'transport': reply.status, 'scientific_validation': 'unverified'}
    hint = 'shk status --attempt ' + attempt
    if not reply.dispatched or reply.status == 'not_sent':
        # Cooldown or ssh never started: nothing reached the registry, so no mutation is in doubt.
        return Outcome({**base, 'outcome': 'not_sent'}, 1, (stderr_tail(reply.stderr),) if reply.stderr.strip() else ())
    if reply.status != 'complete':
        return Outcome({**base, 'outcome': 'unknown', 'hint': hint}, 2, (hint,))
    outcome, value = event_reply(reply.stdout, attempt)
    if outcome == 'refused':
        raise SafetyError(f'registry refused {flag}: {value}')
    if outcome != 'written':
        return Outcome({**base, 'outcome': 'unknown', 'detail': value, 'hint': hint}, 2, (hint,))
    return Outcome({**base, 'outcome': 'written', 'event': value}, 0, forget_reads(config, transport, [attempt]))


# ---------------------------------------------------------------- occupancy

def squeue_argv(partition):
    return ['env', 'LC_ALL=C', 'squeue', '-h', '-p', partition, '-O', ','.join(f'{name}:{width}' for name, width in SQUEUE_COLUMNS)]


def parse_squeue(stdout):
    """Fixed-width squeue -O rows; tres-alloc is empty for pending jobs, so whitespace splitting is unsafe."""
    rows = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        item, offset = {}, 0
        for name, width in SQUEUE_COLUMNS:
            item[name] = line[offset:offset + width].strip()
            offset += width
        rows.append(item)
    return rows


def gpu_count(tres):
    """GPUs in one TRES string: Slurm reports gres/gpu=N beside gres/gpu:TYPE=N, so the two are never added."""
    generic, typed = 0, 0
    for part in tres.split(',') if tres else ():
        key, _, value = part.partition('=')
        if key != 'gres/gpu' and not key.startswith('gres/gpu:'):
            continue
        if not COUNT.fullmatch(value):
            raise SafetyError('malformed GPU occupancy accounting')
        if key == 'gres/gpu':
            generic += int(value)
        else:
            typed += int(value)
    return max(generic, typed)


def occupancy_summary(rows):
    """Per-user running/pending jobs and running GPUs; other states are not occupancy."""
    users, totals = {}, {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 0}
    for item in rows:
        if item['State'] in RUNNING_STATES:
            delta = {'running_jobs': 1, 'running_gpus': gpu_count(item['tres-alloc']), 'pending_jobs': 0}
        elif item['State'] in PENDING_STATES:
            delta = {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 1}
        else:
            continue
        user = users.setdefault(item['UserName'], {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 0})
        for key, value in delta.items():
            user[key] += value
            totals[key] += value
    return users, totals


def occupancy(config, transport, partition):
    """Read-only occupancy of one packaged partition to inform courtesy; it gates nothing."""
    from sherlock_kit import query_cache_path, run_remote
    profile = dict(packaged_profile(partition))
    argv = squeue_argv(partition)
    key = canonical([config['cluster'], config['principal'], transport.control_host, 'squeue', partition])

    def query():
        result = run_remote(transport, argv)
        if result.status != 'complete':
            return {'status': result.status, 'rows': []}
        return {'status': 'complete', 'rows': parse_squeue(result.stdout)}

    result = cached_query(query_cache_path(transport), key, query)
    users, totals = occupancy_summary(result.get('rows', []))
    report = {'partition': partition, 'profile': profile, 'users': users, 'totals': totals, 'transport': result.get('status')}
    return Outcome(report, 0 if report['transport'] == 'complete' else 1)


# ---------------------------------------------------------------- submit

def installed_for_admission(spec):
    from sherlock_kit import _pin_matches, policy_identity
    identity = policy_identity()
    if identity['install_mode'] != 'frozen':
        raise SafetyError('production admission requires a frozen installed toolkit')
    if spec.policy_digest != identity['policy_sha256']:
        raise SafetyError('new attempt policy differs from installed policy')
    pin = os.environ.get('SHERLOCK_KIT_PIN')
    # One pin rule, shared with doctor: partitions_sha256 is compared only when the pin advertises it.
    if pin and not _pin_matches(identity, json.loads(Path(pin).read_text())):
        raise SafetyError('advertised/installed revision mismatch; admission blocked')
    return identity


def submit_record(attempt, frozen):
    """The record body the registry runner checks; it stamps created/created_on/principal_uid/program_sha256 itself."""
    return {'schema_version': 1, 'kind': 'record', 'attempt': attempt, 'key': task_key(frozen), 'job_name': 'shk-' + attempt,
            'spec': frozen, 'sbatch_options': sbatch_options(attempt, frozen)}


def submit_outcome(attempt, frozen, config, reply):
    """D3 mapping of one runner reply; a refusal discards the id and becomes a SafetyError."""
    base = {'operation': 'submit', 'attempt': {'id': attempt, 'spec': frozen, 'registry_root': config['registry_root']},
            'transport': reply.status, 'job_id': None, 'reason': None}
    hint = 'shk status --attempt ' + attempt
    tail = stderr_tail(reply.stderr)
    if not reply.dispatched or reply.status == 'not_sent':
        # Cooldown or ssh never started: nothing reached Slurm and no record exists, so a status hint would dead-end.
        return Outcome({**base, 'resolution': 'not_sent', 'recorded': False, 'reason': tail or 'transport not started'}, 1)
    if reply.status != 'complete':
        return Outcome({**base, 'resolution': 'unknown', 'recorded': None, 'hint': hint}, 2, (hint, *(('remote stderr: ' + tail,) if tail else ())))
    outcome, value = parse_submit_reply(attempt, reply.stdout)
    if outcome == 'submitted':
        return Outcome({**base, 'resolution': 'submitted', 'job_id': value, 'recorded': True})
    if outcome == 'not_sent':
        return Outcome({**base, 'resolution': 'not_sent', 'reason': value, 'recorded': True}, 1)
    if outcome == 'refused':
        raise refusal_error(value)
    recorded = None if value == 'malformed_reply' else True
    return Outcome({**base, 'resolution': 'unknown', 'reason': value, 'recorded': recorded, 'hint': hint}, 2, (hint, *(('remote stderr: ' + tail,) if tail else ())))


def submit_operation(args, config, transport):
    from sherlock_kit import run_remote
    spec = AttemptSpec(**json.loads(Path(args.spec).read_text()))
    spec.checked()
    if (spec.principal, spec.cluster) != controller_identity(config):
        raise SafetyError('spec principal/cluster differs from private controller config')
    if not args.apply:
        return Outcome({'operation': 'preview', 'spec_digest': digest(spec.__dict__), 'resources': spec.checked()})
    if not spec.remote_run_directory:
        raise SafetyError('production submission requires an explicit isolated remote_run_directory')
    identity = installed_for_admission(spec)
    spec = replace(spec, toolkit_revision=identity['code_revision'])
    # Discover the authenticated principal before anything is frozen or claimed.
    probe = run_remote(transport, ['id', '-un'])
    if probe.status != 'complete' or probe.stdout.strip() != spec.principal:
        raise SafetyError('authenticated principal not established before admission')
    attempt = uuid4().hex
    frozen = frozen_record(spec, grant=config.get('grant'), advertised_policy=identity['policy_sha256'])
    argv = program_argv('submit', config['registry_root'], canonical(submit_record(attempt, frozen)), program_sha256())
    if len(shlex.join(argv)) > REMOTE_ARGV_LIMIT:
        raise SafetyError(f'remote submission argv exceeds {REMOTE_ARGV_LIMIT} characters; shorten the spec')
    return submit_outcome(attempt, frozen, config, run_remote(transport, argv, mutation=True))


# ---------------------------------------------------------------- fetch

def checked_remote_scope(args, config):
    """The verified control/data namespace root that both the manifest and the source lie under."""
    roots = config.get('remote_roots', {})
    if roots.get('control') != roots.get('data') or not roots.get('namespace_verified'):
        raise SafetyError('control/data filesystem namespace mapping must be explicitly verified')
    remote_root = roots.get('control', '')
    for remote_path in (args.source_root, args.manifest, remote_root):
        if not remote_path.startswith('/') or any(c in remote_path for c in '\x00\n\r'):
            raise SafetyError('resolved absolute authorized remote roots required')
        if any(part == '..' for part in Path(remote_path).parts):
            raise SafetyError('remote path traversal refused')
    Path(args.source_root).relative_to(Path(remote_root))
    Path(args.manifest).relative_to(Path(remote_root))
    return remote_root


def checked_destination(args, config):
    from sherlock_artifacts import no_symlink_ancestors
    allowed = config.get('fetch_root')
    if not allowed or not Path(allowed).is_absolute():
        raise SafetyError('private configuration must declare authorized fetch_root')
    destination = no_symlink_ancestors(Path(args.destination))
    if not destination.resolve().relative_to(Path(allowed).resolve()).parts:
        raise SafetyError('destination must be a strict descendant of authorized fetch_root')
    return destination


def frozen_validator(args, config, spec):
    """The workload validator frozen at admission, loaded from an authorized immutable root."""
    from sherlock_artifacts import no_symlink_ancestors
    validator_path, validator_digest, validator_function = (spec.get(key) for key in ('validator_path', 'validator_digest', 'validator_function'))
    if not all((validator_path, validator_digest, validator_function)):
        raise SafetyError('attempt must freeze a workload validator path/digest/function')
    if args.validator and args.validator != validator_function:
        raise SafetyError('validator function differs from admitted workload contract')
    if args.validator_sha256 and args.validator_sha256 != validator_digest:
        raise SafetyError('validator digest differs from admitted workload contract')
    module_path = no_symlink_ancestors(Path(validator_path))
    validator_roots = config.get('validator_roots', [])
    if not any(module_path.is_relative_to(Path(root).resolve()) for root in validator_roots if Path(root).is_absolute()):
        raise SafetyError('validator path outside authorized immutable workload roots')
    return load_validator(module_path, validator_digest, validator_function), digest({'sha256': validator_digest, 'function': validator_function})


def control_manifest(args, config, transport, remote_root):
    """``fetch-manifest`` on the login node: the attempt record and the canonical-path-checked manifest."""
    from sherlock_kit import run_remote
    argv = program_argv('fetch-manifest', config['registry_root'], args.attempt, remote_root, args.source_root, args.manifest)
    result = run_remote(transport, argv)
    if result.status != 'complete':
        raise SafetyError('control manifest unavailable; no unverified transfer')
    return parse_fetch_reply(result.stdout)


def local_fetch(args, config, destination):
    """Recovery from the attempt sidecar alone: no registry, no manifest read, no transfer."""
    try:
        sidecar = read_attempt_sidecar(destination)
    except FileNotFoundError:
        raise SafetyError('no attempt sidecar for local recovery; run a remote fetch first') from None
    if sidecar['attempt'] != args.attempt:
        raise SafetyError('attempt sidecar belongs to another attempt; local recovery refused')
    producer = sidecar['producer']
    if controller_identity(config) != (producer['principal'], producer['cluster']):
        raise SafetyError('fetch principal/cluster differs from recorded attempt')
    validator, validator_identity = frozen_validator(args, config, sidecar)
    if not destination.is_dir():
        raise SafetyError('local recovery requires an already promoted destination')
    manifest = sidecar['manifest']

    def never(stage, selected):
        raise SafetyError('local recovery never transfers')

    return fetch_bundle(manifest, destination, never, source_manifest=lambda: manifest, validator=validator,
                        validator_digest=validator_identity, expected_identity=producer, attempt_record=sidecar)


def fetch_operation(args, config, transport):
    from sherlock_kit import data_transfer
    checked_attempt_id(args.attempt)
    remote_root = checked_remote_scope(args, config)
    destination = checked_destination(args, config)
    if args.local:
        return Outcome(local_fetch(args, config, destination))
    record, manifest = control_manifest(args, config, transport, remote_root)
    if record.get('attempt') != args.attempt:
        raise SafetyError('control record names another attempt')
    if not controller_matches(record, config):
        raise SafetyError('fetch principal/cluster differs from recorded attempt')
    spec = record['spec']
    validator, validator_identity = frozen_validator(args, config, spec)
    expected = {key: spec[key] for key in IDENTITY_KEYS}
    expected['attempt'] = args.attempt
    checked_manifest(manifest, expected_identity=expected)
    sidecar = attempt_record(args.attempt, expected, spec, manifest)
    source = args.source_root
    # The data endpoint is addressed inside data_transfer, which shares the control cooldown.
    return Outcome(fetch_bundle(manifest, destination, lambda stage, selected: data_transfer(transport, source, stage, selected),
                                source_manifest=lambda: control_manifest(args, config, transport, remote_root)[1],
                                validator=validator, validator_digest=validator_identity, expected_identity=expected, attempt_record=sidecar))


# ---------------------------------------------------------------- entry point

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    submit = sub.add_parser('submit', help='Preview or explicitly apply a typed immutable submission through the registry on Sherlock')
    submit.add_argument('--config', required=True)
    submit.add_argument('--spec', required=True)
    submit.add_argument('--apply', action='store_true')
    for name in ('status', 'reconcile'):
        check = sub.add_parser(name, help='Resolve attempts from the registry and Slurm accounting through one bounded read')
        check.add_argument('--config', required=True)
        selection = check.add_mutually_exclusive_group(required=True)
        selection.add_argument('--attempt')
        selection.add_argument('--all', action='store_true', help='Every open attempt of the registry, through one bounded query')
        if name == 'reconcile':
            event = check.add_mutually_exclusive_group()
            event.add_argument('--acknowledge-preemption', action='store_true',
                               help='Waive one investigated preemption on a non-preemptible partition; requires --attempt')
            event.add_argument('--abandon', action='store_true',
                               help='Close one attempt that stayed absent from accounting for 900 s without a known job id; requires --attempt')
            check.add_argument('--note', help='Operator note stored with --acknowledge-preemption or --abandon')
        else:
            check.set_defaults(acknowledge_preemption=False, abandon=False, note=None)
    fetch = sub.add_parser('fetch', help='Manifest-bound rsync transaction with a pinned validator')
    fetch.add_argument('--config', required=True)
    fetch.add_argument('--attempt', required=True)
    fetch.add_argument('--manifest', required=True)
    fetch.add_argument('--source-root', required=True)
    fetch.add_argument('--destination', required=True)
    fetch.add_argument('--local', action='store_true', help='Recover the promoted bundle from its attempt sidecar without remote contact')
    fetch.add_argument('--validator', help='Optional assertion of frozen validator function name')
    fetch.add_argument('--validator-sha256', help='Optional assertion of frozen validator digest')
    usage = sub.add_parser('occupancy', help='Read-only per-user occupancy of one packaged partition; informs courtesy, gates nothing')
    usage.add_argument('--config', required=True)
    usage.add_argument('--partition', required=True)
    return parser


def check_operation(args, config, transport):
    if args.acknowledge_preemption or args.abandon:
        return event_operation(args, config, transport)
    if args.note is not None:
        raise SafetyError('--note requires --acknowledge-preemption or --abandon')
    reconcile = args.operation == 'reconcile'
    if args.all:
        return status_all(args, config, transport, reconcile=reconcile)
    return status_one(args, config, transport, reconcile=reconcile)


OPERATIONS = {'submit': submit_operation, 'status': check_operation, 'reconcile': check_operation, 'fetch': fetch_operation,
              'occupancy': lambda args, config, transport: occupancy(config, transport, args.partition)}


def main(argv=None):
    from sherlock_kit import TransportConfig
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = private_config(args.config)
        transport = TransportConfig(**transport_options(config))
        outcome = OPERATIONS[args.operation](args, config, transport)
    except (SafetyError, ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'shk: {exc}\n')
    print(json.dumps(outcome.result, sort_keys=True, indent=2))
    for note in outcome.notes:
        sys.stderr.write('shk: ' + note + '\n')
    return outcome.code


if __name__ == '__main__':
    raise SystemExit(main())
