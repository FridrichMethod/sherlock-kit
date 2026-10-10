"""The registry program: resolution core, registry files, runner/reader/event writer, remote stub."""
import ast
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import pwd
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import sherlock_registry as registry
from sherlock_registry import (ABANDON_SECONDS, BASE_TERMINAL, FIELDS, FINISHED_STATES, LOCK_TIMEOUT_SECONDS, MAX_OPEN, PREEMPTION_STATES,
                               SUBMIT_TIME_TOLERANCE_SECONDS, RegistryError, canonical, digest, epoch, expand_indices, gpu_allocation, main,
                               normalise_rows, parse_event_reply, parse_fetch_reply, parse_job_id, parse_read, parse_rows, parse_submit_reply,
                               parse_tres, program_argv, program_sha256, program_source, released, replace_marker, resolve, sacct_argv, task_key,
                               terminal_states, write_once)

D = 'a' * 64
CREATED = 1791414000  # 2026-10-07T23:00:00Z
NOW = CREATED + 100
PRINCIPAL = pwd.getpwuid(os.getuid()).pw_name
NORMAL = {'preemptible': False, 'requeue': False, 'borrowed': False, 'gpus_allowed': False, 'courtesy': ''}
OWNERS = {'preemptible': True, 'requeue': True, 'borrowed': False, 'gpus_allowed': True, 'courtesy': 'Preemptible.'}
SCRIPT = b'#!/bin/bash -l\necho task "$SLURM_ARRAY_TASK_ID"\n'
FAKE_TOOL = '''#!@PYTHON@
import json, os, sys, time
tool = os.path.basename(sys.argv[0])
prefix = "SHK_FAKE_" + tool.upper() + "_"
data = sys.stdin.buffer.read() if tool == "sbatch" else b""
record = os.environ.get("SHK_FAKE_RECORD")
if record:
    expected = [p for p in os.environ.get("SHK_FAKE_EXPECT_FILES", "").split(":") if p]
    entry = {"tool": tool, "argv": sys.argv[1:], "stdin": data.decode("latin-1"),
             "sbatch_env": sorted(k for k in os.environ if k.startswith("SBATCH_")),
             "present": {p: os.path.exists(p) for p in expected}}
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


def spec(**overrides):
    data = dict(project='consumer', campaign='pilot', task='test', cluster='sherlock', principal=PRINCIPAL, resource_scope='shared',
                code_digest=D, input_digest=D, runtime_digest=D, policy_digest=D,
                resources={'partition': 'normal', 'cpus': 1, 'gpus': 0, 'tasks': 1, 'memory_mb': 1024, 'walltime_seconds': 600, 'requeue': False},
                remote_script='/synthetic/releases/frozen/job.sh', script_digest=hashlib.sha256(SCRIPT).hexdigest(), parent_attempt=None,
                validator_path=None, validator_digest=None, validator_function=None, toolkit_revision=None, remote_run_directory='/synthetic/runs/one',
                partition_profile=NORMAL, partitions_sha256=D, grant=None, schema_version=1)
    data.update(overrides)
    return data


def owners_spec(**overrides):
    base = spec(**{key: value for key, value in overrides.items() if key != 'resources'})
    base['resources'] = {**base['resources'], 'partition': 'owners', 'requeue': True, **overrides.get('resources', {})}
    base['partition_profile'] = OWNERS
    return base


def array_spec(count=4, throttle=None, **overrides):
    base = spec(**overrides)
    array = {'count': count} if throttle is None else {'count': count, 'throttle': throttle}
    base['resources'] = {**base['resources'], 'array': array}
    return base


def options(attempt, body):
    resources = body['resources']
    argv = ['sbatch', '--parsable', '--job-name=shk-' + attempt, '--comment=shk:' + attempt, '--partition=' + resources['partition'],
            '--cpus-per-task=1', '--no-requeue', '--ntasks=1', '--mem=1024M', '--time=10']
    if 'array' in resources:
        argv.append('--array=0-' + str(resources['array']['count'] - 1))
    return argv


def unstamped(attempt, body, **extra):
    record = {'schema_version': 1, 'kind': 'record', 'attempt': attempt, 'key': task_key(body), 'job_name': 'shk-' + attempt,
              'spec': body, 'sbatch_options': options(attempt, body)}
    record.update(extra)
    return record


def record(attempt, body, created=CREATED, **extra):
    return unstamped(attempt, body, created=created, created_on='login01', principal_uid=os.getuid(), program_sha256='0' * 64, **extra)


def event(kind, name=None, **body):
    return {'name': name or kind + '.json', 'kind': kind, 'body': body}


def submitted(job_id='123', cluster=None):
    return event('submitted', job_id=job_id, cluster=cluster, submitted_at=CREATED + 1, sbatch={'returncode': 0, 'stdout': job_id or '', 'stderr': ''})


def sacct_line(attempt, created=CREATED, **overrides):
    values = {'JobID': '123', 'JobIDRaw': '123', 'User': PRINCIPAL, 'JobName': 'shk-' + attempt, 'State': 'COMPLETED', 'ElapsedRaw': '60',
              'AllocCPUS': '1', 'AllocTRES': 'cpu=1,mem=1024M,node=1', 'Submit': stamp(created), 'Start': stamp(created + 1),
              'End': stamp(created + 61), 'Restarts': '0', 'ExitCode': '0:0', 'Cluster': 'sherlock', 'DBIndex': '42'}
    values.update(overrides)
    return '|'.join(values[field] for field in FIELDS)


def task_line(attempt, index, db_index, **overrides):
    return sacct_line(attempt, JobID=f'500_{index}', JobIDRaw=str(500 + index), DBIndex=str(db_index), **overrides)


def pending_aggregate(attempt, indices, db_index='99', **overrides):
    unknown = {'State': 'PENDING', 'Start': 'Unknown', 'End': 'Unknown', 'ElapsedRaw': '0', 'AllocCPUS': '0', 'AllocTRES': ''}
    return sacct_line(attempt, JobID=f'500_[{indices}]', JobIDRaw='500', DBIndex=db_index, **{**unknown, **overrides})


def rows(*lines):
    return parse_rows('\n'.join(lines) + '\n')


def concurrent_submit(root, record_text, env, start, queue):
    os.environ.update(env)
    start.wait()
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(['submit', root, record_text, '0' * 64])
    queue.put((code, out.getvalue().strip()))


class GrammarTests(unittest.TestCase):
    def test_job_id_grammar_plain_task_aggregate_and_malformed(self):
        self.assertEqual(parse_job_id('123'), ('123', None, None))
        self.assertEqual(parse_job_id('123_7'), ('123', 7, None))
        self.assertEqual(parse_job_id('123_[2-3]'), ('123', None, [2, 3]))
        self.assertEqual(parse_job_id('123_[0,2-4,9%2]'), ('123', None, [0, 2, 3, 4, 9]))
        self.assertEqual(parse_job_id('123_[5]'), ('123', None, [5]))
        for text in ('0', '', '123.batch', '123_2.0', '123_', '123_[]', '123_[1-]', '123_[a]', 'x123', '123_[2-1]', '123_[1,,2]', ' 123'):
            with self.subTest(text=text), self.assertRaisesRegex(RegistryError, 'invalid_job_id'):
                parse_job_id(text)
        self.assertEqual(expand_indices('0-3'), [0, 1, 2, 3])
        self.assertEqual(expand_indices('7,1-2,1'), [1, 2, 7])
        with self.assertRaises(RegistryError):
            expand_indices('3-1')

    def test_parse_rows_shapes(self):
        with self.assertRaisesRegex(RegistryError, 'malformed'):
            parse_rows('a|b|c\n')
        self.assertEqual(parse_rows(''), [])
        self.assertEqual(parse_rows(sacct_line('x') + '|\n\n')[0]['JobName'], 'shk-x')
        self.assertEqual(len(FIELDS), 15)

    def test_tres_gpu_epoch_and_sacct_argv(self):
        self.assertEqual(parse_tres('cpu=1,gres/gpu=2,gres/gpu:h100=2'), {'cpu': '1', 'gres/gpu': '2', 'gres/gpu:h100': '2'})
        self.assertEqual(parse_tres(''), {})
        for text in ('cpu=1,cpu=2', 'cpu=1,bad'):
            with self.subTest(text=text), self.assertRaises(RegistryError):
                parse_tres(text)
        self.assertEqual(gpu_allocation({'gres/gpu:h100': '1'}), 1)
        self.assertEqual(gpu_allocation({'gres/gpu': '1', 'gres/gpu:h100': '1'}), 1)
        self.assertIsNone(gpu_allocation({'cpu': '1'}))
        for tres in ({'gres/gpu': '2', 'gres/gpu:h100': '1'}, {'gres/gpu': 'x'}, {'gres/gpu:h100': 'x'}):
            with self.subTest(tres=tres), self.assertRaises(RegistryError):
                gpu_allocation(tres)
        self.assertEqual(epoch('2026-10-07T23:00:00'), CREATED)
        self.assertIsNone(epoch('Unknown'))
        self.assertIsNone(epoch(''))
        argv = sacct_argv(PRINCIPAL, CREATED, '--name=shk-a,shk-b')
        self.assertEqual(argv[:5], ['env', 'TZ=UTC', 'LC_ALL=C', 'SLURM_TIME_FORMAT=standard', 'sacct'])
        self.assertIn('--starttime=' + stamp(CREATED - SUBMIT_TIME_TOLERANCE_SECONDS), argv)
        self.assertIn('--user=' + PRINCIPAL, argv)
        self.assertIn('--duplicates', argv)
        self.assertIn('--allocations', argv)
        self.assertEqual(argv[-1], '--name=shk-a,shk-b')
        self.assertEqual(argv[-2], '--format=' + ','.join(f + '%128' if f in {'JobName', 'User', 'Cluster'} else f for f in FIELDS))
        self.assertNotIn('--jobs', ' '.join(argv))

    def test_duplicated_constants_match_orchestration_contract(self):
        self.assertEqual(BASE_TERMINAL, {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL', 'OUT_OF_MEMORY', 'BOOT_FAIL', 'DEADLINE'})
        self.assertEqual(PREEMPTION_STATES, {'PREEMPTED', 'REQUEUED'})
        self.assertEqual(FINISHED_STATES, BASE_TERMINAL | PREEMPTION_STATES)
        self.assertEqual((SUBMIT_TIME_TOLERANCE_SECONDS, ABANDON_SECONDS, MAX_OPEN, LOCK_TIMEOUT_SECONDS), (300, 900, 500, 20))
        self.assertEqual(terminal_states({'requeue': False}, OWNERS), BASE_TERMINAL | {'PREEMPTED'})
        self.assertEqual(terminal_states({'requeue': True}, OWNERS), BASE_TERMINAL)
        self.assertEqual(terminal_states({'requeue': False}, NORMAL), BASE_TERMINAL)
        self.assertEqual(canonical({'b': 1, 'a': [1, 2]}), '{"a":[1,2],"b":1}')
        self.assertEqual(digest({'a': 1}), hashlib.sha256(b'{"a":1}').hexdigest())
        self.assertEqual(task_key(spec()), digest(['consumer', 'pilot', 'test']))


class NormaliseTests(unittest.TestCase):
    def setUp(self):
        self.id = hex32(1)
        self.record = record(self.id, spec())

    def normalised(self, **fields):
        return normalise_rows(self.record, rows(sacct_line(self.id, **fields)))

    def resolved(self, *lines, events=None, body=None, now=NOW):
        return resolve(body or self.record, [submitted()] if events is None else events, rows(*lines), now)

    def test_actual_sacct_fallback_fields_and_identity(self):
        row = self.normalised()[0]
        self.assertTrue(row['complete'])
        self.assertEqual((row['cpu_seconds'], row['gpu_seconds'], row['restart']), (60, 0, 0))
        self.assertEqual((row['db_index'], row['start'], row['job'], row['task'], row['indices']), ('42', CREATED + 1, '123', 0, None))
        result = self.resolved(sacct_line(self.id))
        self.assertEqual((result['resolution'], result['job_id'], result['tasks']['terminal'], result['tasks']['incomplete_history']), ('terminal', '123', 1, []))
        refusals = {'ownership_conflict': ({'User': 'other'}, {'Cluster': 'other'}), 'db_index_missing': ({'DBIndex': ''},),
                    'start_predates_submit': ({'Start': '2026-10-07T22:59:00'},), 'time_conflict': ({'End': '2026-10-07T22:59:59'},),
                    'malformed_accounting': ({'AllocTRES': 'cpu=1,cpu=2'}, {'AllocTRES': 'cpu=1,bad'}, {'State': ''}, {'ElapsedRaw': 'x'}),
                    'shape_conflict': ({'JobID': '123_0', 'JobIDRaw': '900'}, {'JobID': '123_[0-1]'}),
                    'stale_submit': ({'Submit': stamp(CREATED - SUBMIT_TIME_TOLERANCE_SECONDS - 1)}, {'Submit': 'Unknown'}),
                    'allocated_cpus_differ': ({'AllocCPUS': '2'},), 'negative_accounting': ({'ElapsedRaw': '-1'},),
                    'invalid_allocation_identity': ({'JobIDRaw': '0'},)}
        for reason, cases in refusals.items():
            for fields in cases:
                with self.subTest(fields=fields):
                    with self.assertRaisesRegex(RegistryError, reason):
                        self.normalised(**fields)
                    result = self.resolved(sacct_line(self.id, **fields))
                    self.assertEqual((result['resolution'], result['reason']), ('error', reason))
        # Restart counts are parsed, not refused: resolve decides whether a restart is an anomaly.
        restarted = self.normalised(Restarts='1')[0]
        self.assertEqual(restarted['restart'], 1)
        result = self.resolved(sacct_line(self.id, Restarts='1'))
        self.assertEqual((result['resolution'], result['tasks']['incomplete_history']), ('unexpected_preemption', [0]))
        self.assertIn('restart_gap', result['anomalies'])
        early = self.normalised(Submit=stamp(CREATED - SUBMIT_TIME_TOLERANCE_SECONDS + 1), Start=stamp(CREATED - SUBMIT_TIME_TOLERANCE_SECONDS + 2))[0]
        self.assertEqual(early['state'], 'COMPLETED')
        conflict = self.resolved(sacct_line(self.id), events=[submitted(job_id='999')])
        self.assertEqual((conflict['resolution'], conflict['reason']), ('error', 'job_id_conflict'))
        self.assertFalse(released(conflict))
        self.assertEqual(normalise_rows(self.record, rows(sacct_line('other'), sacct_line(self.id, JobID='123.batch', JobIDRaw='123.batch'))), [])

    def test_non_completed_sacct_row_shapes(self):
        unknown = {'Start': 'Unknown', 'End': 'Unknown', 'ElapsedRaw': '0', 'AllocCPUS': '0', 'AllocTRES': ''}
        cases = {'preempted': ({'State': 'PREEMPTED'}, 'PREEMPTED', True),
                 'boot_fail': ({'State': 'BOOT_FAIL'}, 'BOOT_FAIL', True),
                 'node_fail': ({'State': 'NODE_FAIL'}, 'NODE_FAIL', True),
                 'requeued': ({'State': 'REQUEUED'}, 'REQUEUED', True),
                 'requeued_open': ({'State': 'REQUEUED', 'End': 'Unknown'}, 'REQUEUED', False),
                 'cancelled_by': ({'State': 'CANCELLED by 1234'}, 'CANCELLED', True),
                 'completed_plus': ({'State': 'COMPLETED+'}, 'COMPLETED', True),
                 'pending': ({'State': 'PENDING', **unknown}, 'PENDING', False),
                 'running': ({'State': 'RUNNING', 'End': 'Unknown'}, 'RUNNING', False),
                 'completing': ({'State': 'COMPLETING', 'End': 'Unknown'}, 'COMPLETING', False),
                 'cancelled_before_start': ({'State': 'CANCELLED by 1234', **unknown, 'End': stamp(CREATED + 5)}, 'CANCELLED', True)}
        for name, (fields, state, final) in cases.items():
            with self.subTest(shape=name):
                row = self.normalised(**fields)[0]
                self.assertEqual((row['state'], row['restart']), (state, 0))
                self.assertIs(row['complete'], final)
                self.assertEqual(row['start'], None if fields.get('Start') == 'Unknown' else CREATED + 1)
                result = self.resolved(sacct_line(self.id, **fields))
                self.assertEqual(result['tasks']['by_state'], {state: 1})
                self.assertEqual(result['cost']['known'], final and state in BASE_TERMINAL)
        self.assertEqual(self.normalised(**{**unknown, 'State': 'CANCELLED by 1', 'End': stamp(CREATED + 5)})[0]['cpu_seconds'], 0)

    def test_gpu_tres_typed_generic_and_missing(self):
        body = spec(resources={**spec()['resources'], 'gpus': 1})
        self.record = record(self.id, body)
        for tres in ('cpu=1,gres/gpu:h100=1', 'cpu=1,gres/gpu=1,gres/gpu:h100=1'):
            row = self.normalised(AllocTRES=tres)[0]
            self.assertTrue(row['complete'])
            self.assertEqual(row['gpu_seconds'], 60)
        self.assertEqual(self.resolved(sacct_line(self.id, AllocTRES='cpu=1,gres/gpu:h100=1'))['cost'], {'cpu_seconds': 60, 'gpu_seconds': 60, 'known': True})
        with self.assertRaisesRegex(RegistryError, 'gpu_accounting_conflict'):
            self.normalised(AllocTRES='cpu=1,gres/gpu=2,gres/gpu:h100=1')
        for tres in ('cpu=1,gres/gpu=x', 'cpu=1,gres/gpu:h100=x', 'cpu=1,gres/gpu=-1'):
            with self.subTest(tres=tres), self.assertRaises(RegistryError):
                self.normalised(AllocTRES=tres)
        # A started GPU job without GPU TRES is terminal but its cost is not known.
        self.assertFalse(self.normalised(AllocTRES='cpu=1')[0]['complete'])
        result = self.resolved(sacct_line(self.id, AllocTRES='cpu=1'))
        self.assertEqual((result['resolution'], result['cost']['known']), ('terminal', False))

    def test_duplicate_rows_collapse_or_conflict(self):
        same = sacct_line(self.id)
        self.assertEqual(len(normalise_rows(self.record, rows(same, same))), 1)
        with self.assertRaisesRegex(RegistryError, 'duplicate_accounting_conflicts'):
            normalise_rows(self.record, rows(same, sacct_line(self.id, ElapsedRaw='61')))
        self.assertEqual(self.resolved(same, sacct_line(self.id, ElapsedRaw='61'))['reason'], 'duplicate_accounting_conflicts')

    def test_array_shape_and_index_bounds(self):
        body = record(self.id, array_spec(count=2))
        with self.assertRaisesRegex(RegistryError, 'shape_conflict'):
            normalise_rows(body, rows(sacct_line(self.id)))
        with self.assertRaisesRegex(RegistryError, 'array_index_outside_frozen_count'):
            normalise_rows(body, rows(task_line(self.id, 2, 50)))
        with self.assertRaisesRegex(RegistryError, 'array_index_outside_frozen_count'):
            normalise_rows(body, rows(pending_aggregate(self.id, '1-2')))
        tasks = normalise_rows(body, rows(task_line(self.id, 1, 50), pending_aggregate(self.id, '0')))
        self.assertEqual(sorted(((row['task'], row['indices']) for row in tasks), key=str), [(1, None), (None, [0])])
        aggregate = [row for row in tasks if row['indices']][0]
        self.assertEqual((aggregate['complete'], aggregate['cpu_seconds']), (False, 0))
        cancelled = normalise_rows(body, rows(pending_aggregate(self.id, '0-1', State='CANCELLED by 1', End=stamp(CREATED + 5))))[0]
        self.assertEqual((cancelled['complete'], cancelled['cpu_seconds'], cancelled['state']), (True, 0, 'CANCELLED'))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.id = hex32(2)
        self.record = record(self.id, spec())

    def resolved(self, *lines, events=None, body=None, now=NOW):
        return resolve(body or self.record, [submitted()] if events is None else events, rows(*lines), now)

    def test_event_precedence(self):
        not_sent = event('not_sent', at=CREATED + 1, reason='sbatch_unavailable', detail='x')
        result = self.resolved(events=[not_sent])
        self.assertEqual((result['resolution'], result['job_id'], result['tasks']['missing']), ('not_sent', None, 1))
        self.assertTrue(released(result))
        self.assertEqual(self.resolved(sacct_line(self.id), events=[not_sent])['reason'], 'rows_for_not_sent_attempt')
        abandoned = event('abandoned', at=CREATED + 1000, age_seconds=1000, checks={}, note='')
        result = self.resolved(events=[submitted(job_id=None), abandoned])
        self.assertEqual(result['resolution'], 'abandoned')
        self.assertTrue(released(result))
        self.assertEqual(self.resolved(sacct_line(self.id), events=[submitted(), abandoned])['reason'], 'rows_for_abandoned_attempt')
        self.assertEqual(self.resolved(events=[submitted(), submitted()])['reason'], 'duplicate_event')
        self.assertEqual(self.resolved(events=[event('bogus')])['reason'], 'unknown_event_kind')
        self.assertEqual(self.resolved(events=[{'name': 'submitted.json', 'kind': 'submitted', 'body': []}])['reason'], 'malformed_event:submitted')
        self.assertEqual(self.resolved(events=[submitted(job_id='abc')])['reason'], 'malformed_event:submitted')
        resolved_event = event('resolved', at=NOW, resolution='terminal', job_id='123', tasks={}, cost={}, anomalies=[], rows_sha256=D)
        running = self.resolved(sacct_line(self.id, State='RUNNING', End='Unknown'), events=[submitted(), resolved_event])
        self.assertEqual(running['resolution'], 'identified')
        self.assertIn('reopened_after_resolved', running['anomalies'])
        terminal = self.resolved(sacct_line(self.id), events=[submitted(), resolved_event])
        self.assertEqual((terminal['resolution'], terminal['anomalies']), ('terminal', []))
        # A record the reader could not trust becomes an isolated error, never an exception.
        self.assertEqual(resolve({'attempt': self.id, 'spec': None}, [], [], NOW)['reason'], 'malformed_record')

    def test_inconclusive_versus_abandonable(self):
        unknown = [submitted(job_id=None)]
        self.assertEqual(self.resolved(events=unknown, now=CREATED + ABANDON_SECONDS - 1)['resolution'], 'inconclusive')
        result = self.resolved(events=unknown, now=CREATED + ABANDON_SECONDS)
        self.assertEqual((result['resolution'], result['job_id']), ('abandonable', None))
        self.assertFalse(released(result))
        self.assertEqual(self.resolved(events=[], now=CREATED + ABANDON_SECONDS)['resolution'], 'abandonable')
        known = self.resolved(events=[submitted()], now=CREATED + 10 * ABANDON_SECONDS)
        self.assertEqual((known['resolution'], known['job_id'], known['tasks']['missing']), ('inconclusive', '123', 1))
        self.assertFalse(released(known))

    def test_single_job_resolutions_and_flags(self):
        result = self.resolved(sacct_line(self.id, State='RUNNING', End='Unknown'))
        self.assertEqual((result['resolution'], result['tasks']['running'], result['cost']), ('identified', 1, {'cpu_seconds': 0, 'gpu_seconds': 0, 'known': False}))
        pending = self.resolved(sacct_line(self.id, State='PENDING', Start='Unknown', End='Unknown', ElapsedRaw='0', AllocCPUS='0', AllocTRES=''))
        self.assertEqual((pending['resolution'], pending['tasks']['pending']), ('identified', 1))
        failed = self.resolved(sacct_line(self.id, State='FAILED', ExitCode='1:0'))
        self.assertEqual((failed['resolution'], failed['cost']), ('terminal', {'cpu_seconds': 60, 'gpu_seconds': 0, 'known': True}))
        self.assertTrue(released(failed))
        self.assertFalse(released(self.resolved(sacct_line(self.id))))
        unrecorded = self.resolved(sacct_line(self.id), events=[])
        self.assertEqual((unrecorded['resolution'], unrecorded['job_id'], unrecorded['anomalies']), ('terminal', '123', ['submitted_unrecorded']))
        suffix = self.resolved(sacct_line(self.id), events=[submitted(job_id=None, cluster='other')])
        self.assertEqual(suffix['anomalies'], ['cluster_suffix_mismatch'])
        self.assertEqual(self.resolved(sacct_line(self.id), sacct_line(self.id, JobID='124', JobIDRaw='124', DBIndex='43'))['reason'], 'multiple_scheduler_identities')
        self.assertEqual(self.resolved(sacct_line(self.id), events=[submitted(job_id='124')])['reason'], 'job_id_conflict')
        self.assertEqual(self.resolved(sacct_line(self.id), events=[submitted(job_id=None)])['job_id'], '123')
        self.assertEqual(set(result), {'attempt', 'resolution', 'job_id', 'tasks', 'cost', 'anomalies', 'reason'})
        self.assertEqual(set(result['tasks']), {'count', 'terminal', 'running', 'pending', 'missing', 'by_state', 'incomplete_history'})

    def test_restart_history_and_cost_on_owners(self):
        self.record = record(self.id, owners_spec())
        first = sacct_line(self.id, State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 31))
        second = sacct_line(self.id, State='COMPLETED', Restarts='1', Start=stamp(CREATED + 100), End=stamp(CREATED + 160), DBIndex='43')
        result = self.resolved(first, second)
        self.assertEqual((result['resolution'], result['cost'], result['anomalies']), ('terminal', {'cpu_seconds': 90, 'gpu_seconds': 0, 'known': True}, []))
        self.assertEqual(result['tasks']['by_state'], {'COMPLETED': 1})
        gap = sacct_line(self.id, State='COMPLETED', Restarts='2', Start=stamp(CREATED + 100), End=stamp(CREATED + 160), DBIndex='44')
        result = self.resolved(first, gap)
        self.assertEqual((result['resolution'], result['tasks']['running'], result['tasks']['incomplete_history'], result['cost']['known']), ('identified', 1, [0], False))
        self.assertIn('restart_gap', result['anomalies'])
        requeued = self.resolved(sacct_line(self.id, State='REQUEUED', ElapsedRaw='30', End=stamp(CREATED + 31)))
        self.assertEqual((requeued['resolution'], requeued['tasks']['pending'], requeued['anomalies']), ('identified', 1, []))
        preempted_open = self.resolved(sacct_line(self.id, State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 31)))
        self.assertEqual((preempted_open['resolution'], preempted_open['tasks']['pending']), ('identified', 1))
        no_requeue = record(self.id, owners_spec(resources={'requeue': False}))
        final = self.resolved(sacct_line(self.id, State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 31)), body=no_requeue)
        self.assertEqual((final['resolution'], final['cost']['known']), ('terminal', True))
        self.assertTrue(released(final))
        informational = self.resolved(first.replace('PREEMPTED', 'REQUEUED'), second, body=no_requeue)
        self.assertEqual((informational['resolution'], informational['anomalies']), ('terminal', ['requeued_without_requeue']))

    def test_preemption_anomaly_and_waiver_on_normal(self):
        restarted = sacct_line(self.id, State='RUNNING', Restarts='1', End='Unknown')
        history = sacct_line(self.id, State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 31))
        result = self.resolved(history, restarted.replace('|42', '|43'))
        self.assertEqual((result['resolution'], result['tasks']['running']), ('unexpected_preemption', 1))
        self.assertEqual(result['anomalies'], ['preempted_on_non_preemptible', 'restart_on_non_preemptible'])
        self.assertFalse(released(result))
        preempted = self.resolved(history)
        self.assertEqual((preempted['resolution'], preempted['anomalies'], preempted['tasks']['pending']), ('unexpected_preemption', ['preempted_on_non_preemptible'], 1))
        ack = event('ack', name='ack-1791414100.json', at=NOW, job_id='123', waived={'0': {'restart': 0, 'state': 'PREEMPTED'}}, note='', observation={})
        waived = self.resolved(history, events=[submitted(), ack])
        self.assertEqual((waived['resolution'], waived['tasks']['terminal'], waived['anomalies']), ('terminal', 1, ['acknowledged_preemption']))
        self.assertEqual((waived['cost'], waived['tasks']['by_state']), ({'cpu_seconds': 30, 'gpu_seconds': 0, 'known': True}, {'PREEMPTED': 1}))
        self.assertTrue(released(waived))
        # A waiver for an older restart does not cover a newer one.
        again = self.resolved(history, restarted.replace('|42', '|43'), events=[submitted(), ack])
        self.assertEqual(again['resolution'], 'unexpected_preemption')
        covering = event('ack', name='ack-1791414101.json', at=NOW, job_id='123', waived={'0': {'restart': 1, 'state': 'RUNNING'}}, note='', observation={})
        covered = self.resolved(history, restarted.replace('|42', '|43'), events=[submitted(), ack, covering])
        self.assertEqual((covered['resolution'], covered['anomalies']), ('identified', ['acknowledged_preemption']))
        self.assertEqual(self.resolved(history, events=[submitted(), event('ack', waived={'0': {'restart': 'x'}})])['reason'], 'malformed_event:ack')

    def test_array_resolutions(self):
        self.record = record(self.id, array_spec(count=4))
        done = [task_line(self.id, i, 50 + i) for i in range(4)]
        self.resolved = lambda *lines, events=[submitted(job_id='500')], body=None, now=NOW: resolve(body or self.record, events, rows(*lines), now)
        result = self.resolved(*done)
        self.assertEqual((result['resolution'], result['tasks']['terminal'], result['cost']), ('terminal', 4, {'cpu_seconds': 240, 'gpu_seconds': 0, 'known': True}))
        self.assertEqual((result['job_id'], result['tasks']['by_state']), ('500', {'COMPLETED': 4}))
        self.assertFalse(released(result))
        partial = self.resolved(*done[:2], task_line(self.id, 2, 52, State='RUNNING', End='Unknown'), pending_aggregate(self.id, '3'))
        self.assertEqual(partial['tasks'], {'count': 4, 'terminal': 2, 'running': 1, 'pending': 1, 'missing': 0, 'by_state': {'COMPLETED': 2, 'PENDING': 1, 'RUNNING': 1}, 'incomplete_history': []})
        self.assertEqual((partial['resolution'], partial['cost']), ('identified', {'cpu_seconds': 120, 'gpu_seconds': 0, 'known': False}))
        missing = self.resolved(*done[:3])
        self.assertEqual((missing['resolution'], missing['tasks']['missing'], missing['cost']['known']), ('identified', 1, False))
        aggregate = self.resolved(*done[:2], pending_aggregate(self.id, '2-3%2'))
        self.assertEqual((aggregate['tasks']['pending'], aggregate['tasks']['terminal']), (2, 2))
        cancelled = self.resolved(*done[:2], pending_aggregate(self.id, '2-3', State='CANCELLED by 1', End=stamp(CREATED + 5)))
        self.assertEqual((cancelled['resolution'], cancelled['tasks']['by_state'], cancelled['cost']), ('terminal', {'CANCELLED': 2, 'COMPLETED': 2}, {'cpu_seconds': 120, 'gpu_seconds': 0, 'known': True}))
        overlap = self.resolved(*done, pending_aggregate(self.id, '3'))
        self.assertEqual((overlap['resolution'], overlap['anomalies'], overlap['tasks']['terminal']), ('terminal', ['aggregate_overlap'], 4))
        requeued_record = record(self.id, {**owners_spec(), 'resources': {**owners_spec()['resources'], 'array': {'count': 4}}})
        first = task_line(self.id, 1, 51, State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 31))
        second = task_line(self.id, 1, 61, State='COMPLETED', Restarts='1', Start=stamp(CREATED + 100), End=stamp(CREATED + 160))
        history = self.resolved(done[0], first, second, done[2], done[3], body=requeued_record)
        self.assertEqual((history['resolution'], history['cost'], history['anomalies']), ('terminal', {'cpu_seconds': 270, 'gpu_seconds': 0, 'known': True}, []))
        pending_requeue = self.resolved(done[0], first, done[2], done[3], body=requeued_record)
        self.assertEqual((pending_requeue['resolution'], pending_requeue['tasks']['pending'], pending_requeue['cost']['known']), ('identified', 1, False))
        anomaly = self.resolved(done[0], first, second, done[2], done[3])
        self.assertEqual((anomaly['resolution'], anomaly['anomalies']), ('unexpected_preemption', ['preempted_on_non_preemptible', 'restart_on_non_preemptible']))
        ack = event('ack', waived={'1': {'restart': 1, 'state': 'COMPLETED'}}, at=NOW, job_id='500', note='', observation={})
        self.assertEqual(self.resolved(done[0], first, second, done[2], done[3], events=[submitted(job_id='500'), ack])['resolution'], 'terminal')
        self.assertEqual(self.resolved(*done, events=[submitted(job_id='123')])['reason'], 'job_id_conflict')

    def test_group_conflicts_and_highest_db_index_state(self):
        conflict = self.resolved(sacct_line(self.id), sacct_line(self.id, State='FAILED', DBIndex='43'))
        self.assertEqual((conflict['resolution'], conflict['reason']), ('error', 'terminal_state_conflict'))
        costs = self.resolved(sacct_line(self.id), sacct_line(self.id, ElapsedRaw='61', End=stamp(CREATED + 62), DBIndex='43'))
        self.assertEqual((costs['resolution'], costs['reason']), ('error', 'accounting_conflicts'))
        queued = sacct_line(self.id, State='PENDING', Start='Unknown', End='Unknown', ElapsedRaw='0', AllocCPUS='0', AllocTRES='')
        mixed = self.resolved(queued, sacct_line(self.id, State='RUNNING', End='Unknown', DBIndex='43'))
        self.assertEqual((mixed['resolution'], mixed['tasks']['running'], mixed['tasks']['pending'], mixed['tasks']['by_state']), ('identified', 1, 0, {'RUNNING': 1}))
        reversed_order = self.resolved(sacct_line(self.id, State='RUNNING', End='Unknown'), sacct_line(self.id, **{'State': 'PENDING', 'Start': 'Unknown', 'End': 'Unknown', 'ElapsedRaw': '0', 'AllocCPUS': '0', 'AllocTRES': '', 'DBIndex': '43'}))
        self.assertEqual((reversed_order['tasks']['running'], reversed_order['tasks']['pending']), (0, 1))

    def test_released_predicate(self):
        for resolution, by_state, expected in (('not_sent', {}, True), ('abandoned', {}, True), ('terminal', {'FAILED': 2}, True), ('terminal', {'COMPLETED': 1, 'FAILED': 1}, False),
                                               ('identified', {}, False), ('inconclusive', {}, False), ('abandonable', {}, False), ('unexpected_preemption', {}, False), ('error', {}, False)):
            with self.subTest(resolution=resolution):
                self.assertIs(released({'resolution': resolution, 'tasks': {'by_state': by_state}}), expected)


class ProgramTests(unittest.TestCase):
    def test_program_text_is_small_stdlib_only_and_compiles(self):
        source = program_source()
        self.assertLess(len(source), 100_000)
        self.assertEqual(program_sha256(), hashlib.sha256(source).hexdigest())
        tree = compile(source, 'sherlock_registry', 'exec', ast.PyCF_ONLY_AST)
        compile(source, 'sherlock_registry', 'exec')
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
        self.assertTrue(imported)
        self.assertTrue(imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names))
        self.assertFalse(any(name.startswith('sherlock_') for name in imported))
        self.assertIn(b"if __name__ == '__main__':\n    sys.exit(main(sys.argv[1:]))", source)
        argv = program_argv('read', '/synthetic/registry', '--open')
        self.assertEqual((argv[0], argv[1], argv[3:]), ('python3', '-c', ['read', '/synthetic/registry', '--open']))
        self.assertRegex(argv[2], r"^import base64,sys,zlib;exec\(compile\(zlib\.decompress\(base64\.b64decode\('[A-Za-z0-9+/=]+'\)\),'sherlock_registry','exec'\)\)$")
        self.assertLess(len(shlex.join(argv).encode()), 100_000)
        # Only the fixed stub text carries shell-special characters; the payload never inflates under quoting.
        self.assertLess(len(shlex.join(argv)) - sum(len(a) for a in argv), 100)

    def test_stub_round_trip_reads_an_empty_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = str(Path(temp).resolve() / 'registry')
            result = subprocess.run([sys.executable, *program_argv('read', root, '--open')[1:]], capture_output=True, text=True, timeout=60)
            self.assertEqual((result.returncode, result.stderr), (0, ''))
            document = parse_read(result.stdout)
            self.assertEqual((document['schema_version'], document['registry']['exists'], document['attempts'], document['sacct']), (1, False, [], None))
            os.mkdir(root, 0o700)
            result = subprocess.run([sys.executable, *program_argv('read', root, '--open')[1:]], capture_output=True, text=True, timeout=60)
            document = parse_read(result.stdout)
            self.assertEqual((document['registry']['exists'], document['registry']['open_count'], document['attempts']), (True, 0, []))
            result = subprocess.run([sys.executable, *program_argv('bogus')[1:]], capture_output=True, text=True, timeout=60)
            self.assertEqual((result.returncode, result.stdout), (1, ''))
            self.assertIn('unknown subcommand', result.stderr)

    def test_reply_parsers(self):
        attempt = hex32(9)
        self.assertEqual(parse_submit_reply(attempt, f'SHK_SUBMITTED:{attempt}:123\n'), ('submitted', '123'))
        self.assertEqual(parse_submit_reply(attempt, f'SHK_NOT_SENT:{attempt}:sbatch_unavailable\n'), ('not_sent', 'sbatch_unavailable'))
        self.assertEqual(parse_submit_reply(attempt, f'SHK_REFUSED:{attempt}:duplicate_logical_task:{hex32(8)}'), ('refused', 'duplicate_logical_task:' + hex32(8)))
        self.assertEqual(parse_submit_reply(attempt, f'SHK_UNKNOWN:{attempt}:1\n'), ('unknown', '1'))
        for text in ('', '123\n', f'SHK_SUBMITTED:{hex32(8)}:123\n', f'SHK_SUBMITTED:{attempt}:123\nextra\n', f'SHK_SUBMITTED:{attempt}:abc\n', f'SHK_BOGUS:{attempt}:1\n'):
            with self.subTest(text=text):
                self.assertEqual(parse_submit_reply(attempt, text), ('unknown', 'malformed_reply'))
        self.assertEqual(parse_event_reply(f'SHK_EVENT_WRITTEN:{attempt}:abandoned.json\nSHK_EVENT_REFUSED:{hex32(8)}:not_applicable:terminal\nSHK_EVENT_UNKNOWN:{hex32(7)}:disk full\n'),
                         [(attempt, 'written', 'abandoned.json'), (hex32(8), 'refused', 'not_applicable:terminal'), (hex32(7), 'unknown', 'disk full')])
        with self.assertRaises(RegistryError):
            parse_event_reply('garbage\n')
        with self.assertRaises(RegistryError):
            parse_read('{"schema_version": 2}')
        with self.assertRaises(RegistryError):
            parse_read('[]')
        self.assertEqual(parse_fetch_reply('{"record": {"attempt": "x"}, "manifest": {"files": []}}'), ({'attempt': 'x'}, {'files': []}))
        with self.assertRaises(RegistryError):
            parse_fetch_reply('{"record": 1}')


class RegistryCase(unittest.TestCase):
    """A temporary registry, workload and fake Slurm binaries on PATH."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'registry'
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        for tool in ('sbatch', 'sacct', 'squeue'):
            path = self.bin / tool
            path.write_text(FAKE_TOOL.replace('@PYTHON@', sys.executable))
            path.chmod(0o700)
        self.calls = self.base / 'calls.jsonl'
        self.run_dir = self.base / 'run'
        self.run_dir.mkdir()
        self.script = self.base / 'job.sh'
        self.script.write_bytes(SCRIPT)
        self.counter = 0

    def fake_env(self, **fake):
        env = {'PATH': str(self.bin) + os.pathsep + os.environ.get('PATH', ''), 'SHK_FAKE_RECORD': str(self.calls), 'SBATCH_ACCOUNT': 'leak', 'SBATCH_PARTITION': 'leak'}
        env.update({'SHK_FAKE_' + key.upper(): str(value) for key, value in fake.items()})
        return env

    def run_main(self, argv, **fake):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.fake_env(**fake)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def spec(self, **overrides):
        return spec(**{'remote_script': str(self.script), 'remote_run_directory': str(self.run_dir), **overrides})

    def new_id(self):
        self.counter += 1
        return hex32(0xa000 + self.counter)

    def submit(self, body=None, attempt=None, record_text=None, program='0' * 64, **fake):
        attempt = attempt or self.new_id()
        body = body or self.spec()
        text = record_text or json.dumps(unstamped(attempt, body))
        fake.setdefault('sbatch_stdout', '777\n')
        code, out, err = self.run_main(['submit', str(self.root), text, program], **fake)
        return attempt, code, out, err

    def calls_for(self, tool):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines() if json.loads(line)['tool'] == tool]

    def attempt_dir(self, attempt):
        return self.root / 'attempts' / attempt

    def files(self, attempt):
        return sorted(p.name for p in self.attempt_dir(attempt).iterdir()) if self.attempt_dir(attempt).exists() else None

    def load(self, attempt, name):
        return json.loads((self.attempt_dir(attempt) / name).read_text())

    def marker(self, body):
        path = self.root / 'tasks' / task_key(body)
        return path.read_text().strip() if path.exists() else None

    def seed(self, attempt, body, created=None, events=(), marker=True):
        """A durable attempt as the runner would have left it (events as (name, body) pairs)."""
        for name in ('', 'attempts', 'tasks'):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        directory = self.attempt_dir(attempt)
        directory.mkdir(mode=0o700)
        stamped = record(attempt, body, created=time.time() - 10 if created is None else created)
        write_once(directory / 'record.json', (json.dumps(stamped, sort_keys=True) + '\n').encode())
        for name, payload in events:
            write_once(directory / name, (json.dumps(payload, sort_keys=True) + '\n').encode())
        if marker:
            replace_marker(self.root / 'tasks' / task_key(body), (attempt + '\n').encode())
        return stamped

    def read(self, *selection, **fake):
        code, out, err = self.run_main(['read', str(self.root), *selection], **fake)
        self.assertEqual((code, err), (0, ''))
        return parse_read(out)


class RunnerTests(RegistryCase):
    def test_submit_writes_record_marker_submitted_and_prints_line(self):
        body = self.spec()
        attempt = self.new_id()
        expected = ':'.join([str(self.attempt_dir(attempt) / 'record.json'), str(self.root / 'tasks' / task_key(body))])
        attempt, code, out, err = self.submit(body, attempt=attempt, program='f' * 64, sbatch_stdout='4242;sherlock\n', sbatch_stderr='sbatch: note\n', expect_files=expected)
        self.assertEqual((code, out), (0, f'SHK_SUBMITTED:{attempt}:4242\n'))
        self.assertIn('sbatch: note', err)
        self.assertEqual(parse_submit_reply(attempt, out), ('submitted', '4242'))
        self.assertEqual(self.files(attempt), ['record.json', 'submitted.json'])
        stored = self.load(attempt, 'record.json')
        self.assertEqual((stored['attempt'], stored['key'], stored['job_name'], stored['program_sha256'], stored['principal_uid'], stored['spec']), (attempt, task_key(body), 'shk-' + attempt, 'f' * 64, os.getuid(), body))
        self.assertLess(abs(stored['created'] - time.time()), 60)
        self.assertTrue(stored['created_on'])
        submitted_body = self.load(attempt, 'submitted.json')
        self.assertEqual((submitted_body['job_id'], submitted_body['cluster'], submitted_body['sbatch']['returncode'], submitted_body['sbatch']['stdout']), ('4242', 'sherlock', 0, '4242;sherlock\n'))
        self.assertEqual(self.marker(body), attempt)
        call, = self.calls_for('sbatch')
        self.assertEqual(call['argv'], options(attempt, body)[1:])
        self.assertEqual(call['stdin'].encode('latin-1'), SCRIPT)
        self.assertEqual(call['sbatch_env'], [])
        self.assertEqual(call['present'], {path: True for path in expected.split(':')})
        for path in (self.root, self.root / 'attempts', self.root / 'tasks', self.attempt_dir(attempt)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)
        for name in ('record.json', 'submitted.json'):
            self.assertEqual(stat.S_IMODE((self.attempt_dir(attempt) / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'tasks' / task_key(body)).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / '.lock').stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.attempt_dir(attempt).glob('.*')], [])
        self.assertEqual(self.calls_for('sacct'), [])

    def test_submit_refusals_before_any_write(self):
        body = self.spec()
        attempt = self.new_id()
        good = unstamped(attempt, body)
        invalid = {'not_json': 'nope', 'list': '[]', 'kind': json.dumps({**good, 'kind': 'x'}), 'attempt': json.dumps({**good, 'attempt': 'xyz'}),
                   'key': json.dumps({**good, 'key': D}), 'job_name': json.dumps({**good, 'job_name': 'shk-other'}),
                   'no_job_name_option': json.dumps({**good, 'sbatch_options': ['sbatch', '--parsable']}),
                   'not_sbatch': json.dumps({**good, 'sbatch_options': ['srun'] + good['sbatch_options'][1:]}),
                   'no_parsable': json.dumps({**good, 'sbatch_options': [good['sbatch_options'][0]] + good['sbatch_options'][2:]}),
                   'array_option_without_array': json.dumps({**good, 'sbatch_options': good['sbatch_options'] + ['--array=0-3']}),
                   'array_without_option': json.dumps(unstamped(attempt, self.spec(resources={**body['resources'], 'array': {'count': 4}}), sbatch_options=good['sbatch_options'])),
                   'bad_array': json.dumps(unstamped(attempt, self.spec(resources={**body['resources'], 'array': {'count': 1}}))),
                   'bad_profile': json.dumps(unstamped(attempt, self.spec(partition_profile={'preemptible': 'no'}))),
                   'prestamped': json.dumps({**good, 'created': 1}), 'bad_parent': json.dumps(unstamped(attempt, self.spec(parent_attempt='zz')))}
        for name, text in invalid.items():
            with self.subTest(case=name):
                expected = attempt if name not in {'not_json', 'list', 'attempt'} else '-'
                _, code, out, _ = self.submit(record_text=text)
                self.assertEqual((code, out), (0, f'SHK_REFUSED:{expected}:record_invalid\n'))
        self.assertFalse(self.root.exists())
        _, code, out, _ = self.submit(self.spec(principal='nobody-else'))
        self.assertEqual(out.split(':')[2].strip(), 'principal_mismatch')
        self.assertFalse(self.root.exists())
        cases = {'run_directory_invalid': (self.spec(remote_run_directory=str(self.base / 'missing')),),
                 'digest_mismatch': (self.spec(script_digest='b' * 64),),
                 'script_unreadable': (self.spec(remote_script=str(self.base / 'nope.sh')),)}
        link = self.base / 'link'
        link.symlink_to(self.run_dir, target_is_directory=True)
        cases['run_directory_invalid'] += (self.spec(remote_run_directory=str(link)), self.spec(remote_run_directory=None))
        directive = self.base / 'directive.sh'
        directive.write_bytes(b'#!/bin/bash\n  #SBATCH --partition=owners\n')
        cases['digest_mismatch'] += (self.spec(remote_script=str(directive), script_digest=hashlib.sha256(directive.read_bytes()).hexdigest()),)
        for reason, bodies in cases.items():
            for body in bodies:
                with self.subTest(reason=reason, body=body):
                    attempt, code, out, _ = self.submit(body)
                    self.assertEqual((code, out), (0, f'SHK_REFUSED:{attempt}:{reason}\n'))
                    self.assertEqual(parse_submit_reply(attempt, out), ('refused', reason))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['attempts', 'tasks'])
        self.assertEqual(list((self.root / 'attempts').iterdir()), [])
        self.assertEqual(list((self.root / 'tasks').iterdir()), [])
        self.assertFalse(self.calls.exists())

    def test_submit_unknown_on_nonzero_rc_or_foreign_cluster(self):
        attempt, code, out, err = self.submit(sbatch_stdout='', sbatch_stderr='sbatch: error: Batch job submission failed\n', sbatch_rc=1)
        self.assertEqual((code, out), (0, f'SHK_UNKNOWN:{attempt}:1\n'))
        self.assertIn('Batch job submission failed', err)
        body = self.load(attempt, 'submitted.json')
        self.assertEqual((body['job_id'], body['cluster'], body['sbatch']['returncode'], body['sbatch']['stderr']), (None, None, 1, 'sbatch: error: Batch job submission failed\n'))
        self.assertEqual(self.marker(self.spec()), attempt)
        other = self.spec(task='other')
        attempt, code, out, _ = self.submit(other, sbatch_stdout='77;elsewhere\n')
        self.assertEqual(out, f'SHK_UNKNOWN:{attempt}:0\n')
        self.assertEqual((self.load(attempt, 'submitted.json')['job_id'], self.load(attempt, 'submitted.json')['cluster']), (None, 'elsewhere'))
        attempt, code, out, _ = self.submit(self.spec(task='third'), sbatch_stdout='garbage\n')
        self.assertEqual(out, f'SHK_UNKNOWN:{attempt}:0\n')
        self.assertIsNone(self.load(attempt, 'submitted.json')['job_id'])

    def test_submit_without_sbatch_is_not_sent(self):
        for tool in ('sbatch',):
            (self.bin / tool).unlink()
        body = self.spec()
        attempt = self.new_id()
        out = io.StringIO()
        with patch.dict(os.environ, {**self.fake_env(), 'PATH': str(self.bin)}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(['submit', str(self.root), json.dumps(unstamped(attempt, body)), '0' * 64])
        self.assertEqual((code, out.getvalue()), (0, f'SHK_NOT_SENT:{attempt}:sbatch_unavailable\n'))
        self.assertEqual(self.files(attempt), ['not_sent.json', 'record.json'])
        not_sent = self.load(attempt, 'not_sent.json')
        self.assertEqual(not_sent['reason'], 'sbatch_unavailable')
        self.assertTrue(not_sent['detail'])
        self.assertEqual(self.marker(body), attempt)
        # The marker names this attempt, so a retry must link it as parent; not_sent is released without sacct.
        (self.bin / 'sbatch').write_text(FAKE_TOOL.replace('@PYTHON@', sys.executable))
        (self.bin / 'sbatch').chmod(0o700)
        retry, code, out, _ = self.submit(self.spec(parent_attempt=attempt), sbatch_stdout='55\n')
        self.assertEqual(out, f'SHK_SUBMITTED:{retry}:55\n')
        self.assertEqual(self.marker(body), retry)
        self.assertEqual(len(self.calls_for('sacct')), 1)

    def test_lock_file_symlink_or_directory_is_registry_unsafe(self):
        for name in ('', 'attempts', 'tasks'):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        decoy = self.base / 'decoy'
        decoy.write_text('')
        (self.root / '.lock').symlink_to(decoy)
        attempt, code, out, err = self.submit()
        self.assertEqual((code, out, err), (0, f'SHK_REFUSED:{attempt}:registry_unsafe\n', ''))
        self.assertEqual(parse_submit_reply(attempt, out), ('refused', 'registry_unsafe'))
        (self.root / '.lock').unlink()
        (self.root / '.lock').mkdir(mode=0o700)
        attempt, code, out, err = self.submit()
        self.assertEqual((code, out, err), (0, f'SHK_REFUSED:{attempt}:registry_unsafe\n', ''))
        code, out, err = self.run_main(['event', str(self.root), 'abandon', '{}', attempt])
        self.assertEqual((code, out, err), (0, f'SHK_EVENT_REFUSED:{attempt}:registry_unsafe\n', ''))
        self.assertEqual(list((self.root / 'attempts').iterdir()), [])
        self.assertEqual(list((self.root / 'tasks').iterdir()), [])
        self.assertFalse(self.calls.exists())

    def test_unwritable_attempts_directory_is_record_write_failed(self):
        for name in ('', 'attempts', 'tasks'):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        attempts = self.root / 'attempts'
        attempts.chmod(0o500)
        self.addCleanup(attempts.chmod, 0o700)
        body = self.spec()
        attempt, code, out, err = self.submit(body)
        self.assertEqual((code, out, err), (0, f'SHK_REFUSED:{attempt}:record_write_failed\n', ''))
        self.assertEqual(parse_submit_reply(attempt, out), ('refused', 'record_write_failed'))
        self.assertIsNone(self.files(attempt))
        self.assertIsNone(self.marker(body))
        self.assertFalse(self.calls.exists())

    def test_null_array_is_a_single_job(self):
        body = self.spec()
        body['resources'] = {**body['resources'], 'array': None}
        attempt = self.new_id()
        text = json.dumps({**unstamped(attempt, self.spec()), 'spec': body})
        attempt, code, out, _ = self.submit(attempt=attempt, record_text=text, sbatch_stdout='91\n')
        self.assertEqual((code, out), (0, f'SHK_SUBMITTED:{attempt}:91\n'))
        stored = self.load(attempt, 'record.json')
        self.assertIsNone(stored['spec']['resources']['array'])
        row = sacct_line(attempt, int(stored['created']), JobID='91', JobIDRaw='91')
        self.assertEqual(resolve(stored, [submitted('91')], rows(row), time.time())['resolution'], 'terminal')

    def test_claim_failure_is_not_sent(self):
        original = registry.write_once
        body = self.spec()
        marker = self.root / 'tasks' / task_key(body)

        def failing_with(error):
            def failing(path, data):
                if Path(path) == marker:
                    raise error
                return original(path, data)
            return failing

        for error, needle in ((FileExistsError(17, 'File exists'), 'appeared'), (OSError(28, 'No space left on device'), 'No space left')):
            with self.subTest(error=type(error).__name__), patch.object(registry, 'write_once', failing_with(error)):
                attempt, code, out, _ = self.submit(body)
                self.assertEqual((code, out), (0, f'SHK_NOT_SENT:{attempt}:claim_failed\n'))
                self.assertEqual(parse_submit_reply(attempt, out), ('not_sent', 'claim_failed'))
                self.assertEqual(self.files(attempt), ['not_sent.json', 'record.json'])
                not_sent = self.load(attempt, 'not_sent.json')
                self.assertEqual(not_sent['reason'], 'claim_failed')
                self.assertIn(needle, not_sent['detail'])
                self.assertIsNone(self.marker(body))
                self.assertEqual(self.calls_for('sbatch'), [])

    def test_submit_registry_busy_writes_nothing(self):
        for name in ('', 'attempts', 'tasks'):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        lock = os.open(self.root / '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with patch.object(registry, 'LOCK_TIMEOUT_SECONDS', 0.2):
            started = time.monotonic()
            attempt, code, out, _ = self.submit()
        self.assertEqual((code, out), (0, f'SHK_REFUSED:{attempt}:registry_busy\n'))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(list((self.root / 'attempts').iterdir()), [])
        self.assertEqual(list((self.root / 'tasks').iterdir()), [])
        self.assertFalse(self.calls.exists())
        fcntl.flock(lock, fcntl.LOCK_UN)
        attempt, code, out, _ = self.submit()
        self.assertEqual(out, f'SHK_SUBMITTED:{attempt}:777\n')

    def test_duplicate_and_parent_rules(self):
        body = self.spec()
        first, _, out, _ = self.submit(body)
        self.assertEqual(out, f'SHK_SUBMITTED:{first}:777\n')
        duplicate, _, out, _ = self.submit(body)
        self.assertEqual(out, f'SHK_REFUSED:{duplicate}:duplicate_logical_task:{first}\n')
        self.assertIsNone(self.files(duplicate))
        stranger, _, out, _ = self.submit(self.spec(task='fresh', parent_attempt=first))
        self.assertEqual(out, f'SHK_REFUSED:{stranger}:parent_unknown\n')
        wrong, _, out, _ = self.submit(self.spec(parent_attempt=hex32(5)))
        self.assertEqual(out, f'SHK_REFUSED:{wrong}:parent_mismatch:{first}\n')
        retry_body = self.spec(parent_attempt=first)
        checks = {'parent_not_released:inconclusive': {'sacct_stdout': ''},
                  'parent_not_released:identified': {'sacct_stdout': sacct_line(first, time.time(), State='RUNNING', End='Unknown', JobID='777', JobIDRaw='777') + '\n'},
                  'parent_not_released:unexpected_preemption': {'sacct_stdout': sacct_line(first, time.time(), State='RUNNING', End='Unknown', Restarts='1', JobID='777', JobIDRaw='777') + '\n'},
                  'parent_not_released:terminal': {'sacct_stdout': sacct_line(first, time.time(), JobID='777', JobIDRaw='777') + '\n'},
                  'parent_unverifiable': {'sacct_stdout': '', 'sacct_rc': 1},
                  'parent_conflict:ownership_conflict': {'sacct_stdout': sacct_line(first, time.time(), User='someone', JobID='777', JobIDRaw='777') + '\n'}}
        for reason, fake in checks.items():
            with self.subTest(reason=reason):
                before = len(self.calls_for('sacct'))
                retry, code, out, _ = self.submit(retry_body, **fake)
                self.assertEqual((code, out), (0, f'SHK_REFUSED:{retry}:{reason}\n'))
                self.assertIsNone(self.files(retry))
                self.assertEqual(self.marker(body), first)
                call = self.calls_for('sacct')[before]
                self.assertEqual(call['argv'], sacct_argv(PRINCIPAL, self.load(first, 'record.json')['created'], '--name=shk-' + first)[5:])
        failed = sacct_line(first, time.time(), State='FAILED', ExitCode='1:0', JobID='777', JobIDRaw='777') + '\n'
        retry, _, out, _ = self.submit(retry_body, sacct_stdout=failed, sbatch_stdout='778\n')
        self.assertEqual(out, f'SHK_SUBMITTED:{retry}:778\n')
        self.assertEqual(self.marker(body), retry)
        self.assertEqual(self.files(retry), ['record.json', 'submitted.json'])
        self.assertEqual(self.load(retry, 'record.json')['spec']['parent_attempt'], first)
        stale, _, out, _ = self.submit(retry_body, sacct_stdout=failed)
        self.assertEqual(out, f'SHK_REFUSED:{stale}:parent_mismatch:{retry}\n')
        # A vanished parent record is a conflict, never a release.
        orphan_body = self.spec(task='orphan')
        orphan, _, out, _ = self.submit(orphan_body)
        (self.attempt_dir(orphan) / 'submitted.json').unlink()
        (self.attempt_dir(orphan) / 'record.json').unlink()
        child, _, out, _ = self.submit(self.spec(task='orphan', parent_attempt=orphan))
        self.assertEqual(out, f'SHK_REFUSED:{child}:parent_conflict:missing_record\n')

    def test_registry_root_refusals(self):
        self.root.mkdir(mode=0o750)
        attempt, code, out, _ = self.submit()
        self.assertEqual((code, out), (0, f'SHK_REFUSED:{attempt}:registry_unsafe\n'))
        self.assertEqual(list(self.root.iterdir()), [])
        self.root.chmod(0o700)
        link = self.base / 'via-link'
        link.symlink_to(self.root, target_is_directory=True)
        code, out, _ = self.run_main(['submit', str(link), json.dumps(unstamped(self.new_id(), self.spec())), '0' * 64])
        self.assertIn(':registry_unsafe\n', out)
        _, out, _ = self.run_main(['submit', 'relative/registry', json.dumps(unstamped(self.new_id(), self.spec())), '0' * 64])
        self.assertIn(':registry_unsafe\n', out)
        self.assertEqual(list(self.root.iterdir()), [])
        code, out, err = self.run_main(['submit', str(self.root)])
        self.assertEqual((code, out), (1, ''))
        self.assertIn('usage', err)

    def test_existing_attempt_directory_refused(self):
        body = self.spec()
        attempt = self.new_id()
        for name in ('', 'attempts', 'tasks'):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        self.attempt_dir(attempt).mkdir(mode=0o700)
        attempt, code, out, _ = self.submit(body, attempt=attempt)
        self.assertEqual(out, f'SHK_REFUSED:{attempt}:attempt_exists\n')
        self.assertIsNone(self.marker(body))
        self.assertEqual(self.files(attempt), [])
        self.assertFalse(self.calls.exists())

    def test_submitted_write_failure_still_prints_job_id(self):
        original = registry.write_once

        def failing(path, data):
            if Path(path).name == 'submitted.json':
                raise OSError(28, 'No space left on device')
            return original(path, data)

        with patch.object(registry, 'write_once', failing):
            attempt, code, out, err = self.submit(sbatch_stdout='31\n')
        self.assertEqual((code, out), (0, f'SHK_SUBMITTED:{attempt}:31\n'))
        self.assertIn('submitted.json unrecorded', err)
        self.assertEqual(self.files(attempt), ['record.json'])
        self.assertEqual(self.marker(self.spec()), attempt)

    def test_write_once_and_replace_marker_never_expose_partial_files(self):
        target = self.base / 'once.json'
        write_once(target, b'first')
        self.assertEqual(target.read_bytes(), b'first')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            write_once(target, b'second')
        self.assertEqual(target.read_bytes(), b'first')
        self.assertEqual([p.name for p in self.base.glob('.once.json.*')], [])
        link = self.base / 'link.json'
        link.symlink_to(self.base / 'elsewhere.json')
        with self.assertRaises(OSError):
            write_once(link, b'x')
        self.assertFalse((self.base / 'elsewhere.json').exists())
        marker = self.base / 'marker'
        replace_marker(marker, b'a\n')
        replace_marker(marker, b'b\n')
        self.assertEqual(marker.read_bytes(), b'b\n')
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.base.glob('.marker.*')], [])
        with patch.object(os, 'link', side_effect=OSError(5, 'io')):
            with self.assertRaises(OSError):
                write_once(self.base / 'never.json', b'x')
        self.assertEqual(sorted(p.name for p in self.base.iterdir() if p.name.startswith('.')), [])

    def test_three_concurrent_runners_yield_one_submission(self):
        body = self.spec()
        context = multiprocessing.get_context('fork')
        start, queue = context.Event(), context.Queue()
        ids = [self.new_id() for _ in range(3)]
        env = self.fake_env(sbatch_stdout='900\n')
        workers = [context.Process(target=concurrent_submit, args=(str(self.root), json.dumps(unstamped(attempt, body)), env, start, queue)) for attempt in ids]
        for worker in workers:
            worker.start()
        start.set()
        answers = [queue.get(timeout=60) for _ in workers]
        for worker in workers:
            worker.join(30)
            self.assertEqual(worker.exitcode, 0)
        lines = sorted(line for _, line in answers)
        self.assertEqual([code for code, _ in answers], [0, 0, 0])
        submitted_lines = [line for line in lines if line.startswith('SHK_SUBMITTED:')]
        self.assertEqual(len(submitted_lines), 1)
        winner = submitted_lines[0].split(':')[1]
        self.assertIn(winner, ids)
        refused = [line for line in lines if line.startswith('SHK_REFUSED:')]
        self.assertEqual(sorted(line.split(':', 2)[2] for line in refused), ['duplicate_logical_task:' + winner] * 2)
        self.assertEqual(self.marker(body), winner)
        self.assertEqual(sorted(p.name for p in (self.root / 'attempts').iterdir()), [winner])
        self.assertEqual(len(self.calls_for('sbatch')), 1)


class ReaderTests(RegistryCase):
    def test_open_listing_errors_and_one_sacct_call(self):
        body = self.spec()
        open_a = self.seed(hex32(0xa1), body, created=CREATED + 20, events=[('submitted.json', {'job_id': '1', 'cluster': None, 'submitted_at': CREATED + 21, 'sbatch': {}})])
        self.seed(hex32(0xa2), self.spec(task='b'), created=CREATED + 10, events=[('not_sent.json', {'at': 1, 'reason': 'claim_failed', 'detail': ''})])
        self.seed(hex32(0xa3), self.spec(task='c'), created=CREATED + 30, events=[('resolved.json', {'at': 1})])
        self.seed(hex32(0xa4), self.spec(task='d'), created=CREATED + 5, events=[('ack-1791414100.json', {'at': 1, 'waived': {}})])
        self.seed(hex32(0xa5), self.spec(task='e'), created=CREATED + 40, events=[('submitted.json', {'pad': 'x' * (64 * 1024)})])
        self.seed(hex32(0xa6), self.spec(task='f'), created=CREATED + 50, events=[('abandoned.json', {'at': 1})])
        (self.attempt_dir(hex32(0xa6)) / 'abandoned.json').unlink()
        (self.attempt_dir(hex32(0xa6)) / 'record.json').write_text('{not json')
        (self.attempt_dir(hex32(0xa6)) / 'stray.txt').write_text('x')
        (self.attempt_dir(hex32(0xa6)) / '.record.json.deadbeef.tmp').write_text('partial')
        (self.root / 'attempts' / 'not-an-attempt').mkdir()
        rows_text = sacct_line(hex32(0xa1), CREATED + 20, JobID='1', JobIDRaw='1') + '\n'
        document = self.read('--open', sacct_stdout=rows_text)
        self.assertEqual(document['registry'], {'exists': True, 'root': str(self.root), 'open_count': 4, 'truncated': False})
        self.assertEqual([entry['attempt'] for entry in document['attempts']], [hex32(0xa4), hex32(0xa1), hex32(0xa5), hex32(0xa6)])
        by_id = {entry['attempt']: entry for entry in document['attempts']}
        self.assertEqual(by_id[hex32(0xa1)]['record'], open_a)
        self.assertEqual(by_id[hex32(0xa1)]['events'], [{'name': 'submitted.json', 'kind': 'submitted', 'body': {'job_id': '1', 'cluster': None, 'submitted_at': CREATED + 21, 'sbatch': {}}}])
        self.assertEqual((by_id[hex32(0xa1)]['marker'], by_id[hex32(0xa1)]['errors'], by_id[hex32(0xa1)]['exists']), (hex32(0xa1), [], True))
        self.assertEqual(by_id[hex32(0xa4)]['events'][0]['kind'], 'ack')
        self.assertEqual((by_id[hex32(0xa5)]['events'], by_id[hex32(0xa5)]['errors']), ([], ['submitted.json:oversized']))
        self.assertEqual((by_id[hex32(0xa6)]['record'], by_id[hex32(0xa6)]['errors']), (None, ['record.json:malformed', 'unexpected_file:stray.txt']))
        self.assertEqual(set(document) - {'schema_version', 'now', 'host', 'registry', 'attempts', 'sacct'}, set())
        sacct = document['sacct']
        self.assertEqual((sacct['status'], sacct['returncode'], sacct['row_count'], sacct['truncated'], sacct['stderr_tail']), ('complete', 0, 1, False, ''))
        self.assertEqual(sacct['rows'], parse_rows(rows_text))
        self.assertEqual(sacct['argv'][-1], '--name=' + ','.join('shk-' + i for i in (hex32(0xa4), hex32(0xa1), hex32(0xa5))))
        self.assertIn('--starttime=' + stamp(CREATED + 5 - SUBMIT_TIME_TOLERANCE_SECONDS), sacct['argv'])
        self.assertEqual(len(self.calls_for('sacct')), 1)
        self.assertEqual(self.calls_for('sacct')[0]['argv'], sacct['argv'][5:])
        self.assertEqual(resolve(open_a, by_id[hex32(0xa1)]['events'], sacct['rows'], time.time())['resolution'], 'terminal')
        explicit = self.read('--attempt', hex32(0xa2), hex32(0xa9), 'zzz', sacct_stdout='')
        self.assertEqual([(e['attempt'], e['exists'], e['errors']) for e in explicit['attempts']], [(hex32(0xa2), True, []), (hex32(0xa9), False, ['attempt_missing']), ('zzz', False, ['invalid_attempt_id'])])
        self.assertEqual(explicit['attempts'][0]['events'][0]['kind'], 'not_sent')
        self.assertEqual(explicit['sacct']['argv'][-1], '--name=shk-' + hex32(0xa2))
        self.assertEqual(len(self.calls_for('sacct')), 2)

    def test_reader_without_open_attempts_queries_nothing(self):
        self.seed(hex32(0xb1), self.spec(), events=[('not_sent.json', {'at': 1, 'reason': 'claim_failed', 'detail': ''})])
        document = self.read('--open')
        self.assertEqual((document['attempts'], document['sacct'], document['registry']['open_count']), ([], None, 0))
        self.assertFalse(self.calls.exists())
        document = self.read('--attempt', hex32(0xb9))
        self.assertEqual((document['attempts'][0]['exists'], document['sacct']), (False, None))
        self.assertFalse(self.calls.exists())
        code, out, err = self.run_main(['read', str(self.root)])
        self.assertEqual((code, out), (1, ''))
        self.assertIn('usage', err)

    def test_reader_cap_and_sacct_failures_are_reported(self):
        for index in range(3):
            self.seed(hex32(0xc0 + index), self.spec(task=f't{index}'), created=CREATED + index)
        with patch.object(registry, 'MAX_OPEN', 2):
            document = self.read('--open', sacct_stdout='', sacct_rc=1, sacct_stderr='sacct: error: slurmdbd down\n')
        self.assertEqual((document['registry']['open_count'], document['registry']['truncated']), (3, True))
        self.assertEqual([e['attempt'] for e in document['attempts']], [hex32(0xc0), hex32(0xc1)])
        self.assertEqual((document['sacct']['status'], document['sacct']['returncode'], document['sacct']['rows']), ('failed', 1, []))
        self.assertIn('slurmdbd down', document['sacct']['stderr_tail'])
        malformed = self.read('--open', sacct_stdout='a|b\n')
        self.assertEqual((malformed['sacct']['status'], malformed['sacct']['rows']), ('malformed', []))
        with patch.object(registry, 'MAX_ROWS', 1):
            capped = self.read('--open', sacct_stdout=sacct_line(hex32(0xc0)) + '\n' + sacct_line(hex32(0xc1)) + '\n')
        self.assertEqual((capped['sacct']['row_count'], capped['sacct']['truncated']), (1, True))
        gone = self.read('--open', sacct_stdout='', sacct_rc=0)
        self.assertEqual(gone['sacct']['status'], 'complete')
        with patch.object(registry, 'QUERY_TIMEOUT_SECONDS', 0.3):
            slow = self.read('--open', sacct_stdout='', sacct_sleep='5')
        self.assertEqual(slow['sacct']['status'], 'timeout')

    def test_reader_principal_cluster_disagreement(self):
        self.seed(hex32(0xd0), self.spec(), created=CREATED)
        self.seed(hex32(0xd1), self.spec(task='x', cluster='other'), created=CREATED + 1)
        document = self.read('--open', sacct_stdout='')
        self.assertEqual([e['errors'] for e in document['attempts']], [[], ['principal_cluster_disagrees']])
        self.assertEqual(document['sacct']['argv'][-1], '--name=shk-' + hex32(0xd0))


    def test_unlistable_attempt_directory_is_isolated(self):
        good, bad = hex32(0xd5), hex32(0xd6)
        stored = self.seed(good, self.spec(), created=CREATED, events=[('submitted.json', {'job_id': '1', 'cluster': None, 'submitted_at': CREATED, 'sbatch': {}})])
        self.seed(bad, self.spec(task='dark'), created=CREATED + 1)
        self.attempt_dir(bad).chmod(0)
        self.addCleanup(self.attempt_dir(bad).chmod, 0o700)
        document = self.read('--open', sacct_stdout=sacct_line(good, JobID='1', JobIDRaw='1') + '\n')
        self.assertEqual(document['registry']['open_count'], 2)
        by_id = {entry['attempt']: entry for entry in document['attempts']}
        self.assertEqual((by_id[good]['record'], by_id[good]['errors']), (stored, []))
        self.assertEqual((by_id[bad]['exists'], by_id[bad]['record'], by_id[bad]['events']), (True, None, []))
        self.assertTrue(any(error.startswith('listing:') for error in by_id[bad]['errors']), by_id[bad]['errors'])
        self.assertEqual(document['sacct']['argv'][-1], '--name=shk-' + good)
        self.assertEqual(len(self.calls_for('sacct')), 1)
        self.assertEqual(resolve(stored, by_id[good]['events'], document['sacct']['rows'], time.time())['resolution'], 'terminal')


class EventTests(RegistryCase):
    def event(self, kind, *ids, note='', **fake):
        code, out, err = self.run_main(['event', str(self.root), kind, json.dumps({'note': note}), *ids], **fake)
        self.assertEqual((code, err), (0, ''))
        return parse_event_reply(out)

    def test_ack_preconditions_and_per_task_waiver(self):
        attempt = hex32(0xe1)
        body = self.spec()
        self.seed(attempt, body, created=CREATED, events=[('submitted.json', {'job_id': '123', 'cluster': None, 'submitted_at': CREATED, 'sbatch': {}})])
        restarted = sacct_line(attempt, State='RUNNING', Restarts='1', End='Unknown') + '\n'
        self.assertEqual(self.event('ack', attempt, sacct_stdout=sacct_line(attempt) + '\n'), [(attempt, 'refused', 'not_applicable:terminal')])
        self.assertEqual(self.event('ack', attempt, sacct_rc=1), [(attempt, 'refused', 'unverifiable')])
        self.assertEqual(self.event('ack', attempt, hex32(0xe2), sacct_stdout=restarted), [(attempt, 'refused', 'invalid_request'), (hex32(0xe2), 'refused', 'invalid_request')])
        self.assertEqual(self.event('ack', hex32(0xe9), sacct_stdout=restarted), [(hex32(0xe9), 'refused', 'unknown_attempt')])
        self.assertEqual(self.files(attempt), ['record.json', 'submitted.json'])
        written = self.event('ack', attempt, note='investigated: node drained', sacct_stdout=restarted)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][:2], (attempt, 'written'))
        name = written[0][2]
        self.assertRegex(name, r'^ack-[0-9]+\.json$')
        ack = self.load(attempt, name)
        self.assertEqual((ack['job_id'], ack['waived'], ack['note']), ('123', {'0': {'restart': 1, 'state': 'RUNNING'}}, 'investigated: node drained'))
        self.assertEqual(ack['observation']['tasks']['running'], 1)
        self.assertEqual(ack['observation']['rows_sha256'], digest(parse_rows(restarted)))
        document = self.read('--attempt', attempt, sacct_stdout=restarted)
        entry = document['attempts'][0]
        result = resolve(entry['record'], entry['events'], document['sacct']['rows'], time.time())
        self.assertEqual((result['resolution'], result['anomalies']), ('identified', ['acknowledged_preemption', 'restart_gap']))
        self.assertEqual(self.event('ack', attempt, sacct_stdout=restarted), [(attempt, 'refused', 'not_applicable:identified')])
        owners = hex32(0xe3)
        self.seed(owners, owners_spec(task='o'), created=CREATED)
        self.assertEqual(self.event('ack', owners, sacct_stdout=sacct_line(owners, State='RUNNING', Restarts='1', End='Unknown') + '\n'), [(owners, 'refused', 'preemptible_profile')])
        array_id = hex32(0xe4)
        self.seed(array_id, array_spec(count=3, task='arr'), created=CREATED)
        lines = [task_line(array_id, 0, 70), task_line(array_id, 1, 71, State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 31)), task_line(array_id, 2, 72, State='RUNNING', Restarts='2', End='Unknown')]
        written = self.event('ack', array_id, sacct_stdout='\n'.join(lines) + '\n')
        waived = self.load(array_id, written[0][2])['waived']
        self.assertEqual(waived, {'1': {'restart': 0, 'state': 'PREEMPTED'}, '2': {'restart': 2, 'state': 'RUNNING'}})

    def test_abandon_refusals_and_success(self):
        fresh, old = hex32(0xf1), hex32(0xf2)
        body = self.spec()
        self.seed(fresh, body, created=time.time() - 10, events=[('submitted.json', {'job_id': None, 'cluster': None, 'submitted_at': 1, 'sbatch': {}})])
        self.seed(old, self.spec(task='old'), created=time.time() - 2 * ABANDON_SECONDS, events=[('submitted.json', {'job_id': None, 'cluster': None, 'submitted_at': 1, 'sbatch': {}})])
        self.assertEqual(self.event('abandon', fresh, sacct_stdout=''), [(fresh, 'refused', 'not_applicable:inconclusive')])
        self.assertEqual(self.event('abandon', old, sacct_stdout=sacct_line(old, time.time() - 100, State='RUNNING', End='Unknown') + '\n'), [(old, 'refused', 'not_applicable:identified')])
        self.assertEqual(self.event('abandon', old, sacct_stdout='', squeue_stdout='123\n'), [(old, 'refused', 'squeue_shows_job')])
        self.assertEqual(self.event('abandon', old, sacct_stdout='', sacct_rc=1), [(old, 'refused', 'unverifiable')])
        self.assertEqual(self.event('abandon', old, sacct_stdout='', squeue_rc=1), [(old, 'refused', 'unverifiable')])
        self.assertEqual(self.files(old), ['record.json', 'submitted.json'])
        self.assertEqual(self.event('abandon', old, note='lost reply', sacct_stdout='', squeue_stdout=''), [(old, 'written', 'abandoned.json')])
        abandoned = self.load(old, 'abandoned.json')
        self.assertGreaterEqual(abandoned['age_seconds'], ABANDON_SECONDS)
        self.assertEqual((abandoned['checks']['sacct_rows'], abandoned['checks']['squeue_rows'], abandoned['note']), (0, 0, 'lost reply'))
        self.assertEqual(abandoned['checks']['squeue_argv'], ['env', 'LC_ALL=C', 'squeue', '-h', '--name=shk-' + old, '-o', '%i'])
        self.assertEqual(abandoned['checks']['sacct_argv'][-1], '--name=shk-' + old)
        self.assertEqual(self.event('abandon', old, sacct_stdout=''), [(old, 'refused', 'already_present')])
        document = self.read('--attempt', old, sacct_stdout='')
        entry = document['attempts'][0]
        result = resolve(entry['record'], entry['events'], document['sacct']['rows'], time.time())
        self.assertEqual((result['resolution'], released(result)), ('abandoned', True))
        self.assertEqual([e['attempt'] for e in self.read('--open', sacct_stdout='')['attempts']], [fresh])
        retry, _, out, _ = self.submit(self.spec(task='old', parent_attempt=old), sacct_stdout='', sbatch_stdout='88\n')
        self.assertEqual(out, f'SHK_SUBMITTED:{retry}:88\n')

    def test_resolved_only_for_fresh_terminal_and_busy(self):
        done, running = hex32(0xf5), hex32(0xf6)
        self.seed(done, self.spec(), created=CREATED, events=[('submitted.json', {'job_id': '123', 'cluster': None, 'submitted_at': CREATED, 'sbatch': {}})])
        self.seed(running, self.spec(task='r'), created=CREATED, events=[('submitted.json', {'job_id': '124', 'cluster': None, 'submitted_at': CREATED, 'sbatch': {}})])
        stdout = sacct_line(done) + '\n' + sacct_line(running, JobID='124', JobIDRaw='124', DBIndex='43', State='RUNNING', End='Unknown') + '\n'
        self.assertEqual(self.event('resolved', done, running, hex32(0xf9), sacct_stdout=stdout),
                         [(done, 'written', 'resolved.json'), (running, 'refused', 'not_applicable:identified'), (hex32(0xf9), 'refused', 'unknown_attempt')])
        self.assertEqual(len(self.calls_for('sacct')), 1)
        self.assertEqual(self.calls_for('sacct')[0]['argv'][-1], f'--name=shk-{done},shk-{running}')
        resolved = self.load(done, 'resolved.json')
        self.assertEqual((resolved['resolution'], resolved['job_id'], resolved['tasks']['terminal'], resolved['cost'], resolved['anomalies']), ('terminal', '123', 1, {'cpu_seconds': 60, 'gpu_seconds': 0, 'known': True}, []))
        self.assertEqual(resolved['rows_sha256'], digest(parse_rows(sacct_line(done) + '\n')))
        self.assertEqual(self.event('resolved', done, sacct_stdout=stdout), [(done, 'refused', 'already_present')])
        self.assertEqual([e['attempt'] for e in self.read('--open', sacct_stdout=stdout)['attempts']], [running])
        lock = os.open(self.root / '.lock', os.O_RDWR)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with patch.object(registry, 'LOCK_TIMEOUT_SECONDS', 0.1):
            self.assertEqual(self.event('resolved', running, sacct_stdout=stdout), [(running, 'refused', 'registry_busy')])
        fcntl.flock(lock, fcntl.LOCK_UN)
        code, out, err = self.run_main(['event', str(self.root), 'bogus', '{}', done])
        self.assertEqual((code, out), (1, ''))
        self.assertIn('unknown event kind', err)


    def test_event_write_failure_is_unknown(self):
        old = hex32(0xf7)
        self.seed(old, self.spec(), created=time.time() - 2 * ABANDON_SECONDS)
        original = registry.write_once

        def failing(path, data):
            if Path(path).name == 'abandoned.json':
                raise OSError(28, 'No space left on device')
            return original(path, data)

        with patch.object(registry, 'write_once', failing):
            replies = self.event('abandon', old, sacct_stdout='', squeue_stdout='')
        self.assertEqual((len(replies), replies[0][:2]), (1, (old, 'unknown')))
        self.assertIn('No space left', replies[0][2])
        self.assertEqual(self.files(old), ['record.json'])
        self.assertEqual(self.event('abandon', old, sacct_stdout='', squeue_stdout=''), [(old, 'written', 'abandoned.json')])

    def test_event_on_absent_registry_creates_nothing(self):
        attempt = hex32(0xf8)
        self.assertEqual(self.event('abandon', attempt, sacct_stdout=''), [(attempt, 'refused', 'unknown_attempt')])
        self.assertFalse(self.root.exists())
        self.assertFalse(self.calls.exists())
        self.root.mkdir(mode=0o750)
        self.assertEqual(self.event('abandon', attempt, sacct_stdout=''), [(attempt, 'refused', 'registry_unsafe')])
        self.assertEqual(list(self.root.iterdir()), [])


class FetchManifestTests(RegistryCase):
    def test_fetch_manifest_checks_paths_and_returns_record(self):
        attempt = hex32(0xaa)
        stamped = self.seed(attempt, self.spec())
        allowed = self.base / 'authorized'
        source = allowed / 'bundle'
        source.mkdir(parents=True)
        (source / 'result.json').write_text('42\n')
        manifest = allowed / 'manifest.json'
        payload = {'schema_version': 1, 'files': [{'path': 'result.json', 'size': 3, 'sha256': D}]}
        manifest.write_text(json.dumps(payload))
        code, out, err = self.run_main(['fetch-manifest', str(self.root), attempt, str(allowed), str(source), str(manifest)])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(parse_fetch_reply(out), (stamped, payload))
        code, out, err = self.run_main(['fetch-manifest', str(self.root), hex32(0xab), str(allowed), str(source), str(manifest)])
        self.assertEqual((code, out), (1, ''))
        self.assertTrue(err)
        outside = self.base / 'outside.json'
        outside.write_text(json.dumps(payload))
        linked = allowed / 'linked.json'
        linked.symlink_to(outside)
        failures = {'symlinked_manifest': (allowed, source, linked), 'outside_scope': (allowed, source, outside), 'missing_file': (allowed, allowed, manifest),
                    'relative': (Path('authorized'), source, manifest)}
        for name, (scope, src, path) in failures.items():
            with self.subTest(case=name):
                code, out, _ = self.run_main(['fetch-manifest', str(self.root), attempt, str(scope), str(src), str(path)])
                self.assertEqual((code, out), (1, ''))
        (source / 'result.json').unlink()
        (source / 'result.json').symlink_to(outside)
        code, out, _ = self.run_main(['fetch-manifest', str(self.root), attempt, str(allowed), str(source), str(manifest)])
        self.assertEqual((code, out), (1, ''))
        manifest.write_text(json.dumps({'files': [{'path': '../escape'}]}))
        code, out, _ = self.run_main(['fetch-manifest', str(self.root), attempt, str(allowed), str(source), str(manifest)])
        self.assertEqual((code, out), (1, ''))
        manifest.write_bytes(b'x' * (4 * 1024 * 1024 + 1))
        code, out, _ = self.run_main(['fetch-manifest', str(self.root), attempt, str(allowed), str(source), str(manifest)])
        self.assertEqual((code, out), (1, ''))


if __name__ == '__main__':
    unittest.main()
