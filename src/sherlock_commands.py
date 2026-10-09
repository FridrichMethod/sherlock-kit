"""Minimal CLI for explicit typed Slurm admission, bounded reconciliation, partition occupancy and fetch."""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import ModuleType

from sherlock_artifacts import checked_manifest, fetch_bundle
from sherlock_orchestration import (AttemptSpec, BASE_TERMINAL, Coordinator, IDENTITY_KEYS, PREEMPTION_STATES, SUBMIT_TIME_TOLERANCE_SECONDS,
                                    SafetyError, canonical, digest, packaged_profile)


FIELDS = ('JobID', 'JobIDRaw', 'User', 'JobName', 'State', 'ElapsedRaw', 'AllocCPUS', 'AllocTRES', 'Submit', 'Start', 'End', 'Restarts', 'ExitCode', 'Cluster', 'DBIndex')
REQUIRED_CONFIG = frozenset({'schema_version', 'state_root', 'principal', 'cluster', 'limits', 'transport'})
OPTIONAL_CONFIG = frozenset({'fetch_root', 'validator_roots', 'remote_roots', 'grant'})
# A row in one of these states has left its allocation; with an End time its accounting is final for that restart.
FINISHED_STATES = BASE_TERMINAL | PREEMPTION_STATES
JOB_ID = re.compile('[1-9][0-9]*')
COUNT = re.compile('[0-9]+')
SQUEUE_COLUMNS = (('UserName', 64), ('State', 24), ('tres-alloc', 128), ('TimeUsed', 24), ('TimeLimit', 24))
RUNNING_STATES = frozenset({'RUNNING', 'COMPLETING'})
PENDING_STATES = frozenset({'PENDING'})
PREEMPTION_MESSAGE = 'unexpected preemption on a non-preemptible partition; investigate, then `reconcile --acknowledge-preemption`'


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
    if not Path(value['state_root']).is_absolute():
        raise SafetyError('absolute shared state_root required')
    return value


def epoch(text):
    if not text or text in {'Unknown', 'None', 'N/A'}:
        return None
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def parse_rows(stdout):
    """Raw bounded sacct rows, one dict per allocation line; the shape is checked, identity is not."""
    rows = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        columns = line.split('|')
        if len(columns) == len(FIELDS) + 1 and not columns[-1]:
            columns.pop()
        if len(columns) != len(FIELDS):
            raise SafetyError('malformed bounded accounting response')
        rows.append(dict(zip(FIELDS, columns)))
    return rows


def parse_tres(text):
    tres = {}
    for part in text.split(',') if text else ():
        if '=' not in part:
            raise SafetyError('malformed allocation accounting')
        key, value = part.split('=', 1)
        if key in tres:
            raise SafetyError('malformed allocation accounting')
        tres[key] = value
    return tres


def gpu_allocation(tres):
    """Allocated GPUs from generic and typed TRES, or None when neither is reported; a disagreement is refused."""
    try:
        typed = [int(value) for key, value in tres.items() if key.startswith('gres/gpu:')]
        generic = int(tres['gres/gpu']) if 'gres/gpu' in tres else None
    except ValueError as exc:
        raise SafetyError('malformed GPU allocation accounting') from exc
    if generic is not None and typed and generic != sum(typed):
        raise SafetyError('generic/typed GPU accounting conflicts')
    if generic is None and not typed:
        return None
    return generic if generic is not None else sum(typed)


def accounting_record(attempt, item):
    """One sacct row of this attempt as scheduler evidence; identity, ownership and time must agree first."""
    spec = attempt['spec']
    if item['User'] != spec['principal'] or item['Cluster'] != spec['cluster']:
        raise SafetyError('scheduler ownership/cluster conflict')
    submitted = epoch(item['Submit'])
    if submitted is None or submitted < attempt['created'] - SUBMIT_TIME_TOLERANCE_SECONDS:
        raise SafetyError('scheduler submission time absent/stale')
    job_id = item['JobIDRaw']
    if '_' in job_id:
        raise SafetyError('array profile requires an explicit accounting adapter')
    if not JOB_ID.fullmatch(job_id):
        raise SafetyError('invalid scheduler allocation identity')
    if attempt['job_id'] and attempt['job_id'] != job_id:
        raise SafetyError('scheduler allocation ID conflicts with acknowledgement')
    words = item['State'].split()
    if not words:
        raise SafetyError('malformed allocation accounting')
    state = words[0].rstrip('+')
    try:
        elapsed, cpus, restart = (int(item[key]) for key in ('ElapsedRaw', 'AllocCPUS', 'Restarts'))
    except ValueError as exc:
        raise SafetyError('malformed allocation accounting') from exc
    gpus = gpu_allocation(parse_tres(item['AllocTRES']))
    if min(elapsed, cpus, restart, 0 if gpus is None else gpus) < 0:
        raise SafetyError('negative allocation accounting')
    start, end = epoch(item['Start']), epoch(item['End'])
    if not JOB_ID.fullmatch(item['DBIndex']):
        raise SafetyError('scheduler database identity missing')
    if start is not None and start < submitted - 5:
        raise SafetyError('scheduler allocation start predates submission')
    if end is not None and start is not None and end < start:
        raise SafetyError('scheduler allocation time conflict')
    if cpus != spec['resources']['cpus'] * spec['resources']['tasks'] and start is not None:
        raise SafetyError('allocated CPU resources differ from frozen request')
    # A finished restart (including a preempted or requeued one) with an End time is fully accounted;
    # whether a restart is acceptable is decided by reconcile against the frozen profile.
    complete = state in FINISHED_STATES and end is not None and (start is not None or (elapsed == 0 and cpus == 0))
    if spec['resources']['gpus'] > 0 and gpus is None and start is not None:
        complete = False
    return {**{key: spec[key] for key in IDENTITY_KEYS},
            'attempt': attempt['id'], 'job_id': job_id, 'submitted_at': submitted,
            'state': state, 'exit_code': item['ExitCode'], 'kind': 'scheduler',
            'cost_known': complete, 'accounting_complete': complete,
            'cpu_seconds': elapsed * cpus, 'gpu_seconds': elapsed * (gpus or 0),
            'db_index': item['DBIndex'], 'start_time': start, 'restart': restart}


def accounting_records(attempt, rows):
    """Scheduler evidence for one attempt from already parsed rows; steps and other job names are skipped.

    restart_history_complete is True for a job only when the response shows every restart 0..max.
    """
    records = []
    for item in rows:
        if '_' in item['JobID'] or '[' in item['JobID']:
            raise SafetyError('array profile requires an explicit accounting adapter')
        if '.' in item['JobIDRaw'] or item['JobName'] != 'shk-' + attempt['id']:
            continue
        records.append(accounting_record(attempt, item))
    restarts = {}
    for record in records:
        restarts.setdefault(record['job_id'], set()).add(record['restart'])
    return [{**record, 'restart_history_complete': restarts[record['job_id']] == set(range(max(restarts[record['job_id']]) + 1))} for record in records]


def parse_accounting(stdout, attempt):
    """Scheduler token/user/cluster/time must match before enriching frozen identity."""
    return accounting_records(attempt, parse_rows(stdout))


def sacct_argv(principal, created, selector):
    """Bounded accounting query: this principal, from the admission window, one job-name or job-id selector."""
    lower = datetime.fromtimestamp(created - SUBMIT_TIME_TOLERANCE_SECONDS, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    return ['env', 'TZ=UTC', 'LC_ALL=C', 'SLURM_TIME_FORMAT=standard', 'sacct', '-n', '-P', '--local', '--allocations',
            '--user=' + principal, '--duplicates', '--starttime=' + lower, '--endtime=now',
            '--format=' + ','.join(field + '%128' if field in {'JobName', 'User', 'Cluster'} else field for field in FIELDS), selector]


def scheduler_rows(coordinator, transport, key, argv):
    """One scheduler query per cache key and cadence; rows are cached raw and matched per attempt afterwards."""
    from sherlock_kit import run_remote
    def query():
        result = run_remote(transport, argv)
        if result.status != 'complete':
            return {'status': result.status, 'rows': []}
        return {'status': 'complete', 'rows': parse_rows(result.stdout)}
    return coordinator.cached_query(key, query)


def controller_matches(attempt, config):
    return (config['principal'], config['cluster']) == (attempt['spec']['principal'], attempt['spec']['cluster'])


def controller_owns(attempt, config):
    if not controller_matches(attempt, config):
        raise SafetyError('controller cannot query another principal/cluster')


def local_result(attempt):
    return {'attempt': attempt, 'resolution': 'local_only', 'scientific_validation': 'unverified'}


def inconclusive_result(attempt, transport_status):
    return {'attempt': attempt, 'resolution': 'inconclusive', 'transport': transport_status, 'scientific_validation': 'unverified'}


def status(coordinator, attempt_id, config, transport, *, remote=True):
    attempt = coordinator.get(attempt_id)
    controller_owns(attempt, config)
    if not remote:
        return local_result(attempt)
    # Query the ID when known; only the current principal in a bounded submit interval
    # when acknowledgement was lost. The cache key is the attempt, not the selector.
    selector = '--jobs=' + attempt['job_id'] if attempt['job_id'] else '--name=shk-' + attempt_id
    key = canonical([config['cluster'], config['principal'], transport.control_host, 'sacct', attempt_id])
    result = scheduler_rows(coordinator, transport, key, sacct_argv(config['principal'], attempt['created'], selector))
    if result.get('status') != 'complete':
        return inconclusive_result(coordinator.get(attempt_id), result.get('status'))
    return coordinator.reconcile(attempt_id, accounting_records(attempt, result['rows']))


def reconciled_or_error(coordinator, attempt, rows):
    """Reconcile one attempt of a batch; its contract violation is reported, never allowed to hide the others."""
    try:
        return coordinator.reconcile(attempt['id'], accounting_records(attempt, rows))
    except SafetyError as exc:
        return {'attempt': coordinator.get(attempt['id']), 'resolution': 'error', 'error': str(exc), 'scientific_validation': 'unverified'}


def status_all(coordinator, config, transport, *, remote=True):
    """One bounded query for every reserved submitted/unknown attempt, each reconciled on its own.

    sacct ANDs its filters, so one --name list selects all attempts; every job is named shk-<attempt>.
    """
    pending = coordinator.unresolved()
    owned = [attempt for attempt in pending if controller_matches(attempt, config)]
    foreign = [{**inconclusive_result(attempt, None), 'reason': 'controller cannot query another principal/cluster'}
               for attempt in pending if not controller_matches(attempt, config)]
    if not remote:
        return [local_result(attempt) for attempt in owned] + foreign
    if not owned:
        return foreign
    selector = '--name=' + ','.join('shk-' + attempt['id'] for attempt in owned)
    key = canonical([config['cluster'], config['principal'], transport.control_host, 'sacct-all'])
    result = scheduler_rows(coordinator, transport, key, sacct_argv(config['principal'], min(attempt['created'] for attempt in owned), selector))
    if result.get('status') != 'complete':
        return [inconclusive_result(attempt, result.get('status')) for attempt in owned] + foreign
    return [reconciled_or_error(coordinator, attempt, result['rows']) for attempt in owned] + foreign


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


def occupancy(coordinator, config, transport, partition):
    """Read-only occupancy of one packaged partition to inform courtesy; it gates nothing."""
    from sherlock_kit import run_remote
    profile = dict(packaged_profile(partition))
    argv = squeue_argv(partition)
    key = canonical([config['cluster'], config['principal'], transport.control_host, 'squeue', partition])
    def query():
        result = run_remote(transport, argv)
        if result.status != 'complete':
            return {'status': result.status, 'rows': []}
        return {'status': 'complete', 'rows': parse_squeue(result.stdout)}
    result = coordinator.cached_query(key, query)
    users, totals = occupancy_summary(result.get('rows', []))
    return {'partition': partition, 'profile': profile, 'users': users, 'totals': totals, 'transport': result.get('status')}


REMOTE_MANIFEST = '''import json, os, pathlib, stat, sys
root, source, manifest = map(pathlib.Path, sys.argv[1:])
for path in (root, source, manifest):
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("symlink/noncanonical remote artifact path")
    path.relative_to(root)
if not root.is_dir() or not source.is_dir():
    raise ValueError("remote artifact roots must be directories")
fd = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW)
with os.fdopen(fd, "rb") as stream:
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        raise ValueError("manifest must be regular")
    payload = stream.read(4 * 1024 * 1024 + 1)
if len(payload) > 4 * 1024 * 1024:
    raise ValueError("manifest metadata ceiling")
value = json.loads(payload)
for item in value.get("files", []):
    name = item.get("path", "")
    relative = pathlib.PurePosixPath(name)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("manifest path escape")
    path = source / name
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("symlink/noncanonical artifact file")
    path.relative_to(root)
sys.stdout.buffer.write(payload)
'''


def read_manifest(transport, path, source, root):
    from sherlock_kit import run_remote
    if not path.startswith('/') or any(c in path for c in '\x00\n\r'):
        raise SafetyError('resolved absolute manifest path required')
    result = run_remote(transport, ['python3', '-c', REMOTE_MANIFEST, root, source, path])
    if result.status != 'complete':
        raise SafetyError('control manifest unavailable; no unverified transfer')
    manifest = json.loads(result.stdout)
    checked_manifest(manifest)
    return manifest


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


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    submit = sub.add_parser('submit', help='Preview or explicitly apply a typed immutable submission')
    submit.add_argument('--config', required=True)
    submit.add_argument('--spec', required=True)
    submit.add_argument('--apply', action='store_true')
    for name in ('status', 'reconcile'):
        check = sub.add_parser(name, help='Bounded identity-based scheduler evidence reconciliation')
        check.add_argument('--config', required=True)
        selection = check.add_mutually_exclusive_group(required=True)
        selection.add_argument('--attempt')
        selection.add_argument('--all', action='store_true', help='Every reserved attempt still submitted/unknown, through one bounded query')
        check.add_argument('--local', action='store_true')
        if name == 'reconcile':
            check.add_argument('--acknowledge-preemption', action='store_true',
                               help='Release one investigated preemption on a non-preemptible partition; requires --attempt, no remote query')
        else:
            check.set_defaults(acknowledge_preemption=False)
    fetch = sub.add_parser('fetch', help='Manifest-bound rsync transaction with a pinned validator')
    fetch.add_argument('--config', required=True)
    fetch.add_argument('--attempt', required=True)
    fetch.add_argument('--manifest', required=True)
    fetch.add_argument('--source-root', required=True)
    fetch.add_argument('--destination', required=True)
    fetch.add_argument('--local', action='store_true', help='Recover matching promoted bundle from durable manifest without remote contact')
    fetch.add_argument('--validator', help='Optional assertion of frozen validator function name')
    fetch.add_argument('--validator-sha256', help='Optional assertion of frozen validator digest')
    usage = sub.add_parser('occupancy', help='Read-only per-user occupancy of one packaged partition; informs courtesy, gates nothing')
    usage.add_argument('--config', required=True)
    usage.add_argument('--partition', required=True)
    return parser


def transport_options(config):
    options = {**config['transport']}
    if options.get('backoff_file') is None and 'SHERLOCK_KIT_STATE_ROOT' not in os.environ:
        options['backoff_file'] = str(Path(config['state_root']) / 'auth-backoff.json')
    return options


def submit_operation(args, config, transport, coordinator):
    from sherlock_kit import run_remote
    spec = AttemptSpec(**json.loads(Path(args.spec).read_text()))
    spec.checked()
    if (spec.principal, spec.cluster) != (config['principal'], config['cluster']):
        raise SafetyError('spec principal/cluster differs from private controller config')
    if not args.apply:
        return {'operation': 'preview', 'spec_digest': digest(spec.__dict__), 'resources': spec.checked()}
    if not spec.remote_run_directory:
        raise SafetyError('production submission requires an explicit isolated remote_run_directory')
    identity = installed_for_admission(spec)
    spec = replace(spec, toolkit_revision=identity['code_revision'])
    # Discover authenticated principal without holding the admission lock.
    probe = run_remote(transport, ['id', '-un'])
    if probe.status != 'complete' or probe.stdout.strip() != spec.principal:
        raise SafetyError('authenticated principal not established before admission')
    admitted = coordinator.admit(spec, config['limits'], grant=config.get('grant'), advertised_policy=identity['policy_sha256'])
    return coordinator.dispatch(admitted['id'], lambda command: run_remote(transport, command, mutation=True))


def check_operation(args, config, transport, coordinator):
    if args.acknowledge_preemption:
        if not args.attempt:
            raise SafetyError('--acknowledge-preemption requires --attempt; it releases exactly one investigated attempt')
        controller_owns(coordinator.get(args.attempt), config)
        return coordinator.acknowledge_preemption(args.attempt)
    if args.all:
        return status_all(coordinator, config, transport, remote=not args.local)
    return status(coordinator, args.attempt, config, transport, remote=not args.local)


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


def fetch_operation(args, config, transport, coordinator):
    from sherlock_kit import data_transfer
    attempt = coordinator.get(args.attempt)
    spec = attempt['spec']
    if not controller_matches(attempt, config):
        raise SafetyError('fetch principal/cluster differs from recorded attempt')
    remote_root = checked_remote_scope(args, config)
    destination = checked_destination(args, config)
    validator, validator_identity = frozen_validator(args, config, spec)
    expected = {key: spec[key] for key in IDENTITY_KEYS}
    expected['attempt'] = args.attempt
    manifest = coordinator.pinned_manifest(args.attempt) if args.local else read_manifest(transport, args.manifest, args.source_root, remote_root)
    checked_manifest(manifest, expected_identity=expected)
    coordinator.pin_manifest(args.attempt, manifest)
    if args.local and not destination.is_dir():
        raise SafetyError('local recovery requires an already promoted destination')
    # The data endpoint is addressed inside data_transfer, which shares the control cooldown.
    source = args.source_root
    return fetch_bundle(manifest, destination, lambda stage, m: data_transfer(transport, source, stage, m),
                        source_manifest=(lambda: manifest) if args.local else (lambda: read_manifest(transport, args.manifest, args.source_root, remote_root)),
                        validator=validator, validator_digest=validator_identity, expected_identity=expected)


def unexpected_preemption(result):
    items = result if isinstance(result, list) else [result]
    return any(isinstance(item, dict) and item.get('resolution') == 'unexpected_preemption' for item in items)


def failed_reconciliations(result):
    items = result if isinstance(result, list) else [result]
    return sum(1 for item in items if isinstance(item, dict) and item.get('resolution') == 'error')


def main(argv=None):
    from sherlock_kit import TransportConfig
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = private_config(args.config)
        transport = TransportConfig(**transport_options(config))
        coordinator = Coordinator(Path(config['state_root']))
        code = 0
        if args.operation == 'submit':
            result = submit_operation(args, config, transport, coordinator)
        elif args.operation == 'occupancy':
            result = occupancy(coordinator, config, transport, args.partition)
            code = 0 if result['transport'] == 'complete' else 1
        elif args.operation == 'fetch':
            result = fetch_operation(args, config, transport, coordinator)
        else:
            result = check_operation(args, config, transport, coordinator)
        print(json.dumps(result, sort_keys=True, indent=2))
        if unexpected_preemption(result):
            parser.exit(2, 'shk: ' + PREEMPTION_MESSAGE + '\n')
        failed = failed_reconciliations(result)
        if failed:
            parser.exit(2, f'shk: {failed} attempt(s) could not be reconciled; see the "error" entries\n')
        return code
    except (SafetyError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'shk: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
