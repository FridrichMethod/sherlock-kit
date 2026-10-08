"""Minimal CLI for explicit typed Slurm admission, bounded reconciliation and fetch."""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import time
from types import ModuleType

from sherlock_artifacts import checked_manifest, fetch_bundle, rsync_transfer, verify_bundle
from sherlock_orchestration import AttemptSpec, Coordinator, SafetyError, TERMINAL, canonical, digest


FIELDS = ('JobID', 'JobIDRaw', 'User', 'JobName', 'State', 'ElapsedRaw', 'AllocCPUS', 'AllocTRES', 'Submit', 'Start', 'End', 'Restarts', 'ExitCode', 'Cluster', 'DBIndex')


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
    required = {'schema_version', 'state_root', 'principal', 'cluster', 'limits', 'transport'}
    if not isinstance(value, dict) or not required <= value.keys() or value['schema_version'] != 1:
        raise SafetyError('private config schema mismatch')
    if not Path(value['state_root']).is_absolute():
        raise SafetyError('absolute shared state_root required')
    return value


def epoch(text):
    if not text or text in {'Unknown', 'None', 'N/A'}:
        return None
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def parse_accounting(stdout, attempt):
    """Scheduler token/user/cluster/time must match before enriching frozen identity.

    Array/step/restart accounting is deliberately inconclusive for this CLI profile;
    library allocation accounting can consume explicit complete adapter evidence.
    """
    spec = attempt['spec']
    records = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        columns = line.split('|')
        if len(columns) == len(FIELDS) + 1 and not columns[-1]:
            columns.pop()
        if len(columns) != len(FIELDS):
            raise SafetyError('malformed bounded accounting response')
        item = dict(zip(FIELDS, columns))
        job_id = item['JobIDRaw']
        if '_' in item['JobID'] or '[' in item['JobID']:
            raise SafetyError('array profile requires an explicit accounting adapter')
        if '.' in job_id:
            continue
        if item['JobName'] != 'shk-' + attempt['id']:
            continue
        if item['User'] != spec['principal'] or item['Cluster'] != spec['cluster']:
            raise SafetyError('scheduler ownership/cluster conflict')
        submitted = epoch(item['Submit'])
        if submitted is None or submitted < attempt['created'] - 5:
            raise SafetyError('scheduler submission time absent/stale')
        state = item['State'].split()[0].rstrip('+')
        if '_' in job_id:
            raise SafetyError('array profile requires an explicit accounting adapter')
        if not re.fullmatch('[1-9][0-9]*', job_id):
            raise SafetyError('invalid scheduler allocation identity')
        if attempt['job_id'] and attempt['job_id'] != job_id:
            raise SafetyError('scheduler allocation ID conflicts with acknowledgement')
        try:
            elapsed, cpus, restarts = (int(item[key]) for key in ('ElapsedRaw', 'AllocCPUS', 'Restarts'))
            tres = {}
            for part in item['AllocTRES'].split(',') if item['AllocTRES'] else ():
                if '=' not in part:
                    raise ValueError('malformed TRES')
                key, value = part.split('=', 1)
                if key in tres:
                    raise ValueError('duplicate TRES')
                tres[key] = value
            gpus = int(tres.get('gres/gpu', '0'))
        except ValueError as exc:
            raise SafetyError('malformed allocation accounting') from exc
        if min(elapsed, cpus, restarts, gpus) < 0:
            raise SafetyError('negative allocation accounting')
        if restarts:
            raise SafetyError('restart history requires a complete workload accounting adapter')
        start, end = epoch(item['Start']), epoch(item['End'])
        if not re.fullmatch('[1-9][0-9]*', item['DBIndex']):
            raise SafetyError('scheduler database identity missing')
        if start is not None and start < submitted - 5:
            raise SafetyError('scheduler allocation start predates submission')
        if end is not None and start is not None and end < start:
            raise SafetyError('scheduler allocation time conflict')
        complete = state in TERMINAL and end is not None and (start is not None or (elapsed == 0 and cpus == 0))
        gpu_fields = {key: value for key, value in tres.items() if key.startswith('gres/gpu:')}
        try:
            typed = sum(int(value) for value in gpu_fields.values())
            generic = int(tres['gres/gpu']) if 'gres/gpu' in tres else None
        except ValueError as exc:
            raise SafetyError('malformed GPU allocation accounting') from exc
        if generic is not None and gpu_fields and generic != typed:
            raise SafetyError('generic/typed GPU accounting conflicts')
        gpus = generic if generic is not None else typed
        if spec['resources']['gpus'] > 0 and generic is None and not gpu_fields and start is not None:
            complete = False
        if cpus != spec['resources']['cpus'] * spec['resources']['tasks'] and start is not None:
            raise SafetyError('allocated CPU resources differ from frozen request')
        # PREEMPTED/requeued snapshots cannot release admission or certify full cost.
        records.append({**{key: spec[key] for key in ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest')},
                        'attempt': attempt['id'], 'job_id': job_id, 'submitted_at': submitted,
                        'state': state, 'exit_code': item['ExitCode'], 'kind': 'scheduler',
                        'cost_known': complete, 'accounting_complete': complete,
                        'cpu_seconds': elapsed * cpus, 'gpu_seconds': elapsed * gpus,
                        'db_index': item['DBIndex'], 'restarts': restarts})
    return records


def status(coordinator, attempt_id, config, transport, *, remote=True):
    from sherlock_kit import run_remote
    attempt = coordinator.get(attempt_id)
    if config['principal'] != attempt['spec']['principal'] or config['cluster'] != attempt['spec']['cluster']:
        raise SafetyError('controller cannot query another principal/cluster')
    if not remote:
        return {'attempt': attempt, 'resolution': 'local_only', 'scientific_validation': 'unverified'}
    # Query IDs when known; only the current principal in a bounded submit interval
    # when acknowledgement was lost. No broad historical accounting scan.
    lower = datetime.fromtimestamp(attempt['created'] - 5, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    argv = ['env', 'TZ=UTC', 'LC_ALL=C', 'SLURM_TIME_FORMAT=standard', 'sacct', '-n', '-P', '--local', '--allocations', '--user=' + config['principal'], '--duplicates', '--starttime=' + lower,
            '--endtime=now', '--format=' + ','.join(field + '%128' if field in {'JobName', 'User', 'Cluster'} else field for field in FIELDS)]
    if attempt['job_id']:
        argv += ['--jobs=' + attempt['job_id']]
    else:
        argv += ['--name=shk-' + attempt_id]
    key = canonical([config['cluster'], config['principal'], transport.control_host, argv])
    def query():
        result = run_remote(transport, argv)
        if result.status != 'complete':
            return {'status': result.status, 'records': []}
        return {'status': 'complete', 'records': parse_accounting(result.stdout, attempt)}
    result = coordinator.cached_query(key, query)
    if result.get('status') != 'complete':
        return {'attempt': coordinator.get(attempt_id), 'resolution': 'inconclusive', 'transport': result.get('status'), 'scientific_validation': 'unverified'}
    return coordinator.reconcile(attempt_id, result['records'])


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
    from sherlock_kit import policy_identity
    identity = policy_identity()
    if identity['install_mode'] != 'frozen':
        raise SafetyError('production admission requires a frozen installed toolkit')
    if spec.policy_digest != identity['policy_sha256']:
        raise SafetyError('new attempt policy differs from installed policy')
    pin = os.environ.get('SHERLOCK_KIT_PIN')
    if pin:
        advertised = json.loads(Path(pin).read_text())
        if any(advertised.get(key) != identity[key] for key in ('schema_version', 'code_revision', 'policy_sha256')):
            raise SafetyError('advertised/installed revision mismatch; admission blocked')
    return identity


def main(argv=None):
    from sherlock_kit import TransportConfig, _ssh_prefix, run_remote
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    submit = sub.add_parser('submit', help='Preview or explicitly apply a typed immutable submission')
    submit.add_argument('--config', required=True)
    submit.add_argument('--spec', required=True)
    submit.add_argument('--apply', action='store_true')
    for name in ('status', 'reconcile'):
        check = sub.add_parser(name, help='Bounded identity-based scheduler evidence reconciliation')
        check.add_argument('--config', required=True)
        check.add_argument('--attempt', required=True)
        check.add_argument('--local', action='store_true')
    fetch = sub.add_parser('fetch', help='Manifest-bound rsync transaction with a pinned validator')
    fetch.add_argument('--config', required=True)
    fetch.add_argument('--attempt', required=True)
    fetch.add_argument('--manifest', required=True)
    fetch.add_argument('--source-root', required=True)
    fetch.add_argument('--destination', required=True)
    fetch.add_argument('--local', action='store_true', help='Recover matching promoted bundle from durable manifest without remote contact')
    fetch.add_argument('--validator', help='Optional assertion of frozen validator function name')
    fetch.add_argument('--validator-sha256', help='Optional assertion of frozen validator digest')
    args = parser.parse_args(argv)
    try:
        config = private_config(args.config)
        transport = TransportConfig(**config['transport'])
        coordinator = Coordinator(Path(config['state_root']))
        if args.operation == 'submit':
            spec = AttemptSpec(**json.loads(Path(args.spec).read_text()))
            spec.checked()
            if (spec.principal, spec.cluster) != (config['principal'], config['cluster']):
                raise SafetyError('spec principal/cluster differs from private controller config')
            if not args.apply:
                print(json.dumps({'operation': 'preview', 'spec_digest': digest(spec.__dict__), 'resources': spec.checked()}, indent=2))
                return 0
            identity = installed_for_admission(spec)
            spec = replace(spec, toolkit_revision=identity['code_revision'])
            # Discover authenticated principal without holding the admission lock.
            probe = run_remote(transport, ['id', '-un'])
            if probe.status != 'complete' or probe.stdout.strip() != spec.principal:
                raise SafetyError('authenticated principal not established before admission')
            admitted = coordinator.admit(spec, config['limits'], grant=config.get('grant'), advertised_policy=identity['policy_sha256'])
            result = coordinator.dispatch(admitted['id'], lambda command: run_remote(transport, command, mutation=True))
        elif args.operation in {'status', 'reconcile'}:
            result = status(coordinator, args.attempt, config, transport, remote=not args.local)
        else:
            from sherlock_artifacts import no_symlink_ancestors
            attempt = coordinator.get(args.attempt)
            spec = attempt['spec']
            if (spec['principal'], spec['cluster']) != (config['principal'], config['cluster']):
                raise SafetyError('fetch principal/cluster differs from recorded attempt')
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
            allowed = config.get('fetch_root')
            if not allowed or not Path(allowed).is_absolute():
                raise SafetyError('private configuration must declare authorized fetch_root')
            destination = no_symlink_ancestors(Path(args.destination))
            relative_destination = destination.resolve().relative_to(Path(allowed).resolve())
            if not relative_destination.parts:
                raise SafetyError('destination must be a strict descendant of authorized fetch_root')
            validator_path = spec.get('validator_path')
            validator_digest = spec.get('validator_digest')
            validator_function = spec.get('validator_function')
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
            validator = load_validator(module_path, validator_digest, validator_function)
            expected = {key: spec[key] for key in ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest')}
            expected['attempt'] = args.attempt
            manifest = coordinator.pinned_manifest(args.attempt) if args.local else read_manifest(transport, args.manifest, args.source_root, remote_root)
            checked_manifest(manifest, expected_identity=expected)
            coordinator.pin_manifest(args.attempt, manifest)
            if args.local and not destination.is_dir():
                raise SafetyError('local recovery requires an already promoted destination')
            source = args.source_root
            ssh_command = shlex.join(_ssh_prefix(transport))
            result = fetch_bundle(manifest, destination,
                lambda stage, m: rsync_transfer(transport.data_host + ':' + source, stage, m, ssh_command=ssh_command),
                source_manifest=(lambda: manifest) if args.local else (lambda: read_manifest(transport, args.manifest, args.source_root, remote_root)), validator=validator,
                validator_digest=digest({'sha256': validator_digest, 'function': validator_function}), expected_identity=expected)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (SafetyError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'shk: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
