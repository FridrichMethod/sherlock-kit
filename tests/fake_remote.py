"""A ``run_remote`` substitute that executes the registry program locally.

The CLI ships ``sherlock_registry`` to the login node as ``python3 -c STUB``. This
side effect recognises that shape and runs ``[sys.executable, '-c', *argv[2:]]``
against the test's temporary ``registry_root`` with fake ``sbatch``/``sacct``/
``squeue`` scripts on PATH, driven by ``SHK_FAKE_*`` environment variables exactly
as ``tests/test_registry.py`` drives them. The principal probe and the occupancy
``squeue`` query get canned replies. Every call is recorded as ``(argv, mutation)``
and any subcommand can be made to fail at the transport layer.
"""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sherlock_commands import main
from sherlock_kit import RemoteResult
from sherlock_orchestration import frozen_record, sbatch_options
from sherlock_registry import FIELDS, task_key
from test_orchestration import spec as base_spec

PRINCIPAL = pwd.getpwuid(os.getuid()).pw_name
CLUSTER = 'sherlock'
CONTROL_HOST = 'sherlock-plain'
PROBE = ['id', '-un']
SQUEUE_FORMAT = 'UserName:64,State:24,tres-alloc:128,TimeUsed:24,TimeLimit:24'
PREEMPTION_MESSAGE = 'unexpected preemption on a non-preemptible partition; investigate, then `reconcile --acknowledge-preemption`'
CREATED = 1791414000  # 2026-10-07T23:00:00Z
SCRIPT = b'#!/bin/bash -l\necho task "$SLURM_ARRAY_TASK_ID"\n'
FAKE_TOOL = '''#!@PYTHON@
import json, os, sys, time
tool = os.path.basename(sys.argv[0])
prefix = "SHK_FAKE_" + tool.upper() + "_"
data = sys.stdin.buffer.read() if tool == "sbatch" else b""
record = os.environ.get("SHK_FAKE_RECORD")
if record:
    entry = {"tool": tool, "argv": sys.argv[1:], "stdin": data.decode("latin-1"),
             "sbatch_env": sorted(k for k in os.environ if k.startswith("SBATCH_"))}
    with open(record, "a") as stream:
        stream.write(json.dumps(entry) + "\\n")
sys.stdout.write(os.environ.get(prefix + "STDOUT", ""))
sys.stderr.write(os.environ.get(prefix + "STDERR", ""))
sys.stdout.flush()
sys.stderr.flush()
if os.environ.get(prefix + "SLEEP"):
    time.sleep(float(os.environ[prefix + "SLEEP"]))
sys.exit(int(os.environ.get(prefix + "RC", "0")))
'''


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def hex32(n):
    return f'{n:032x}'


def complete(stdout):
    return RemoteResult('complete', stdout=stdout, returncode=0, dispatched=True)


def sacct_line(attempt_id, created=CREATED, **overrides):
    values = {'JobID': '123', 'JobIDRaw': '123', 'User': PRINCIPAL, 'JobName': 'shk-' + attempt_id, 'State': 'COMPLETED', 'ElapsedRaw': '60', 'AllocCPUS': '1',
              'AllocTRES': 'cpu=1,mem=1024M,node=1', 'Submit': stamp(created), 'Start': stamp(created + 1), 'End': stamp(created + 61), 'Restarts': '0',
              'ExitCode': '0:0', 'Cluster': CLUSTER, 'DBIndex': '42'}
    values.update(overrides)
    return '|'.join(values[field] for field in FIELDS)


def task_line(attempt_id, index, db_index, created=CREATED, job='500', **overrides):
    """One started array task ``job_index`` with its own DBIndex."""
    return sacct_line(attempt_id, created, JobID=f'{job}_{index}', JobIDRaw=str(int(job) + index), DBIndex=str(db_index), **overrides)


def pending_aggregate(attempt_id, indices, created=CREATED, db_index='99', job='500', **overrides):
    """The single ``job_[a-b%t]`` row sacct prints for tasks that have not started."""
    unknown = {'State': 'PENDING', 'Start': 'Unknown', 'End': 'Unknown', 'ElapsedRaw': '0', 'AllocCPUS': '0', 'AllocTRES': ''}
    return sacct_line(attempt_id, created, JobID=f'{job}_[{indices}]', JobIDRaw=job, DBIndex=db_index, **{**unknown, **overrides})


def squeue_line(user, state, tres, used='1:00', limit='2:00:00'):
    """squeue -O pads every column to its declared width; tres-alloc is empty for pending jobs."""
    return f'{user:<64}{state:<24}{tres:<128}{used:<24}{limit:<24}'


def spec(**overrides):
    """tests/test_orchestration.spec with the real user as principal, so the runner's pwd check passes."""
    return base_spec(**{'principal': PRINCIPAL, **overrides})


def frozen(**overrides):
    return frozen_record(spec(**overrides))


def stamped_record(attempt_id, frozen_spec, created=CREATED, **extra):
    """A record as the runner leaves it on Sherlock; ``created`` is backdated by the caller."""
    record = {'schema_version': 1, 'kind': 'record', 'attempt': attempt_id, 'key': task_key(frozen_spec), 'job_name': 'shk-' + attempt_id,
              'spec': frozen_spec, 'sbatch_options': sbatch_options(attempt_id, frozen_spec),
              'created': created, 'created_on': 'login01', 'principal_uid': os.getuid(), 'program_sha256': '0' * 64}
    record.update(extra)
    return record


def submitted_event(job_id='123', cluster=None, at=CREATED + 1):
    return 'submitted.json', {'job_id': job_id, 'cluster': cluster, 'submitted_at': at, 'sbatch': {'returncode': 0, 'stdout': (job_id or '') + '\n', 'stderr': ''}}


def _private_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def seed_attempt(registry_root, record, events=(), marker=True):
    """Write an attempt's registry files directly (0700/0600) so tests control ``created``.

    ``events`` are ``(file name, body)`` pairs. The task marker names this attempt
    unless ``marker`` is False.
    """
    root = Path(registry_root)
    for name in ('', 'attempts', 'tasks'):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    directory = root / 'attempts' / record['attempt']
    directory.mkdir(mode=0o700)
    _private_file(directory / 'record.json', (json.dumps(record, sort_keys=True) + '\n').encode())
    for name, body in events:
        _private_file(directory / name, (json.dumps(body, sort_keys=True) + '\n').encode())
    if marker:
        _private_file(root / 'tasks' / record['key'], (record['attempt'] + '\n').encode())
    return record


class FakeRemote:
    """Callable side effect for ``patch('sherlock_kit.run_remote', side_effect=FakeRemote(...))``."""

    def __init__(self, base, *, squeue_stdout=''):
        self.base = Path(base).resolve()
        self.bin = self.base / 'fake-bin'
        self.bin.mkdir(mode=0o700, exist_ok=True)
        for tool in ('sbatch', 'sacct', 'squeue'):
            path = self.bin / tool
            path.write_text(FAKE_TOOL.replace('@PYTHON@', sys.executable))
            path.chmod(0o700)
        self.tool_calls_path = self.base / 'fake-tool-calls.jsonl'
        self.calls = []
        self.fake = {'sbatch_stdout': '777\n'}
        self.squeue_stdout = squeue_stdout
        self.failures = {}

    def fail(self, subcommand, result):
        """Make the next calls of one registry subcommand (or 'probe'/'squeue') return ``result`` or raise it."""
        self.failures[subcommand] = result

    def remove_tool(self, tool):
        (self.bin / tool).unlink()

    def tool_calls(self, tool):
        if not self.tool_calls_path.exists():
            return []
        return [json.loads(line) for line in self.tool_calls_path.read_text().splitlines() if json.loads(line)['tool'] == tool]

    def program_calls(self, subcommand=None):
        calls = [argv for argv, _ in self.calls if argv[:2] == ['python3', '-c']]
        return [argv for argv in calls if subcommand is None or argv[3] == subcommand]

    def _environment(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith('SHK_FAKE_')}
        env.update({'PATH': str(self.bin) + os.pathsep + env.get('PATH', ''), 'SHK_FAKE_RECORD': str(self.tool_calls_path)})
        env.update({'SHK_FAKE_' + key.upper(): str(value) for key, value in self.fake.items()})
        return env

    def _injected(self, name):
        failure = self.failures.get(name)
        if isinstance(failure, BaseException):
            raise failure
        return failure

    def __call__(self, transport, argv, mutation=False):
        argv = list(argv)
        self.calls.append((argv, mutation))
        if argv == PROBE:
            return self._injected('probe') or complete(PRINCIPAL + '\n')
        if argv[:3] == ['env', 'LC_ALL=C', 'squeue']:
            return self._injected('squeue') or complete(self.squeue_stdout)
        if argv[:2] != ['python3', '-c'] or len(argv) < 4:
            raise AssertionError('unexpected remote command: ' + repr(argv[:4]))
        injected = self._injected(argv[3])
        if injected is not None:
            return injected
        process = subprocess.run([sys.executable, '-c', *argv[2:]], capture_output=True, text=True, env=self._environment(), timeout=120)
        if process.returncode == 0:
            status = 'complete'
        else:
            status = 'unknown' if mutation else 'failed'
        return RemoteResult(status, process.stdout, process.stderr, process.returncode, dispatched=True)


_CONFIGS = iter(range(1, 10 ** 6))


def config_file(directory, registry_root, state_dir, **extra):
    """A private 0600 config with an explicit local state root; ``extra`` overrides or adds keys."""
    path = Path(directory) / f'config-{next(_CONFIGS)}.json'
    payload = {'schema_version': 1, 'registry_root': str(registry_root), 'principal': PRINCIPAL, 'cluster': CLUSTER,
               'transport': {'backoff_file': str(Path(state_dir) / 'auth-backoff.json')}}
    payload.update(extra)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return str(path)


def run_main(arguments, remote):
    """main() in-process with ``remote`` as run_remote; an exception instance fails on any network use."""
    with patch('sherlock_kit.run_remote', side_effect=remote), patch('sys.stdout', new_callable=io.StringIO) as out, patch('sys.stderr', new_callable=io.StringIO) as err:
        try:
            code = main(arguments)
        except SystemExit as stop:
            code = stop.code
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def cache_path(state_dir):
    return Path(state_dir) / 'query-cache.json'


def query_cache(state_dir):
    path = cache_path(state_dir)
    return json.loads(path.read_text())['entries'] if path.exists() else {}


def query_keys(state_dir):
    return sorted(query_cache(state_dir))


def expire_queries(state_dir):
    """Age every cached query past the cadence without touching the wall clock."""
    path = cache_path(state_dir)
    document = json.loads(path.read_text())
    for entry in document['entries'].values():
        entry['observed'] = entry['observed'] - 3600
    path.write_text(json.dumps(document))
    path.chmod(0o600)


def now():
    return time.time()
