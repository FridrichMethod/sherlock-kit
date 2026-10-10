import dataclasses
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import sherlock_orchestration
from sherlock_orchestration import (AttemptSpec, BASE_TERMINAL, PREEMPTION_STATES, SUBMIT_TIME_TOLERANCE_SECONDS, SafetyError, canonical,
                                    checked_grant, digest, frozen_record, resources_checked, sbatch_options, terminal_states)
from sherlock_partitions import partition_profile, partitions_sha256

D = 'a' * 64
ATTEMPT = '1' * 32
GRANT = {'grantee': 'fixture', 'evidence_reference': 'private-evidence', 'valid_from': 1, 'valid_until': 100, 'scope': 'shared', 'partitions': ['btrippe']}


def spec(**kwargs):
    data = dict(project='consumer', campaign='new-pilot', task='test', cluster='sherlock', principal='fixture', resource_scope='shared', code_digest=D, input_digest=D, runtime_digest=D, policy_digest=D, resources={'partition': 'normal', 'cpus': 1, 'gpus': 0, 'tasks': 1, 'memory_mb': 1024, 'walltime_seconds': 600}, remote_script='/synthetic/releases/frozen/job.sh', script_digest=D)
    data.update(kwargs)
    return AttemptSpec(**data)


def resources(partition='normal', **overrides):
    return {**spec().resources, 'partition': partition, **overrides}


def frozen(attempt_spec, **profile_overrides):
    """A frozen record whose packaged profile is overridden for frozen-profile tests."""
    record = frozen_record(attempt_spec, grant=GRANT, now=50)
    return {**record, 'partition_profile': {**record['partition_profile'], **profile_overrides}}


class ConstantTests(unittest.TestCase):
    def test_duplicated_constants_have_the_registry_values(self):
        self.assertEqual(SUBMIT_TIME_TOLERANCE_SECONDS, 300)
        self.assertEqual(BASE_TERMINAL, {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL', 'OUT_OF_MEMORY', 'BOOT_FAIL', 'DEADLINE'})
        self.assertEqual(PREEMPTION_STATES, {'PREEMPTED', 'REQUEUED'})
        self.assertEqual(sherlock_orchestration.IDENTITY_KEYS, ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest'))
        self.assertEqual(sherlock_orchestration.PROFILE_FLAGS, ('preemptible', 'requeue', 'borrowed', 'gpus_allowed'))

    def test_terminal_states_follow_profile_and_requeue(self):
        owners, normal = partition_profile('owners'), partition_profile('normal')
        self.assertEqual(terminal_states({'requeue': False}, owners), BASE_TERMINAL | {'PREEMPTED'})
        self.assertEqual(terminal_states({'requeue': True}, owners), BASE_TERMINAL)
        self.assertEqual(terminal_states({'requeue': False}, normal), BASE_TERMINAL)
        self.assertEqual(terminal_states({}, normal), BASE_TERMINAL)

    def test_canonical_and_digest_are_order_independent(self):
        self.assertEqual(canonical({'b': 1, 'a': [1, 2]}), '{"a":[1,2],"b":1}')
        self.assertEqual(digest({'b': 1, 'a': [1, 2]}), digest({'a': [1, 2], 'b': 1}))
        self.assertRegex(digest([]), '^[0-9a-f]{64}$')
        with self.assertRaises(ValueError):
            canonical({'x': float('nan')})

    def test_local_ledger_mechanisms_are_gone(self):
        for name in ('Coordinator', 'submission_argv', 'scheduler_cost', 'LEGACY_PROFILE', 'frozen_profile', 'matching_evidence', 'observe',
                     'recorded_cost', 'cost_floor', 'budget_charge', 'REMOTE_MANIFEST', 'sqlite3', 'socket', 'uuid', 'shlex', 'TERMINAL'):
            with self.subTest(name=name):
                self.assertFalse(hasattr(sherlock_orchestration, name))


class ResourceGrammarTests(unittest.TestCase):
    def test_site_resources_and_constraint_signal_grammar(self):
        r = resources_checked({**spec().resources, 'constraint': 'CPU_GEN1|CPU_GEN2', 'signal': 'B:USR1@60', 'walltime_seconds': 601})
        self.assertEqual((r['constraint'], r['signal'], r['walltime_seconds'], r['requeue']), ('CPU_GEN1|CPU_GEN2', 'B:USR1@60', 660, False))
        for wrong in ({'account': 'group'}, {'exclude': 'node'}, {'cpus': True}, {'cpus': 0}, {'memory_mb': '1024'}, {'constraint': 'cpu; false'},
                      {'signal': 'B:USR1@60\n'}, {'tasks': 0}, {'gpus': -1}, {'partition': 'bad name'}):
            with self.subTest(wrong=wrong), self.assertRaises(SafetyError):
                resources_checked({**spec().resources, **wrong})

    def test_checked_resources_do_not_alias_the_input(self):
        original = resources(array={'count': 4})
        snapshot = canonical(original)
        checked = resources_checked(original)
        self.assertEqual(canonical(original), snapshot)
        self.assertIsNot(checked['array'], original['array'])

    def test_unknown_partition_refused_offline(self):
        with self.assertRaisesRegex(SafetyError, 'unknown partition'):
            resources_checked(resources('nonexistent'))
        with self.assertRaisesRegex(SafetyError, 'unknown partition'):
            frozen_record(spec(resources=resources('nonexistent')))

    def test_gpus_follow_the_profile(self):
        with self.assertRaisesRegex(SafetyError, 'GPU'):
            resources_checked(resources('normal', gpus=1))
        for partition in ('owners', 'btrippe'):
            with self.subTest(partition=partition):
                self.assertEqual(resources_checked(resources(partition, gpus=1))['gpus'], 1)

    def test_requeue_follows_the_profile(self):
        for partition in ('normal', 'btrippe'):
            with self.subTest(partition=partition):
                with self.assertRaisesRegex(SafetyError, 'requeue'):
                    resources_checked(resources(partition, requeue=True))
                self.assertIs(resources_checked(resources(partition))['requeue'], False)
        self.assertIs(resources_checked(resources('owners'))['requeue'], True)
        self.assertIs(resources_checked(resources('owners', requeue=False))['requeue'], False)
        with self.assertRaisesRegex(SafetyError, 'boolean'):
            resources_checked(resources('owners', requeue='yes'))

    def test_malformed_profile_is_refused(self):
        with self.assertRaisesRegex(SafetyError, 'profile'):
            resources_checked(resources(), {'preemptible': False})
        with self.assertRaisesRegex(SafetyError, 'profile'):
            resources_checked(resources(), {**dict(partition_profile('normal')), 'requeue': 1})

    def test_array_grammar_accepts_count_and_optional_throttle(self):
        self.assertEqual(resources_checked(resources(array={'count': 2}))['array'], {'count': 2})
        self.assertEqual(resources_checked(resources(array={'count': 1000, 'throttle': 1000}))['array'], {'count': 1000, 'throttle': 1000})
        self.assertEqual(resources_checked(resources(array={'count': 4, 'throttle': 1}))['array'], {'count': 4, 'throttle': 1})
        self.assertNotIn('array', resources_checked(resources()))

    def test_array_grammar_refusals(self):
        cases = {'count 1': {'count': 1}, 'count 1001': {'count': 1001}, 'count 0': {'count': 0}, 'count bool': {'count': True},
                 'count str': {'count': '4'}, 'count float': {'count': 4.0}, 'throttle 0': {'count': 4, 'throttle': 0},
                 'throttle above count': {'count': 4, 'throttle': 5}, 'throttle bool': {'count': 4, 'throttle': True},
                 'throttle str': {'count': 4, 'throttle': '2'}, 'extra key': {'count': 4, 'step': 1}, 'missing count': {'throttle': 2},
                 'empty': {}, 'list': [4], 'int': 4, 'none': None}
        for label, array in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(SafetyError, 'array'):
                resources_checked(resources(array=array))


class AttemptSpecTests(unittest.TestCase):
    def test_checked_returns_checked_resources(self):
        self.assertEqual(spec().checked(), resources_checked(spec().resources))

    def test_identity_digest_and_path_validation(self):
        cases = (dict(project='bad name'), dict(cluster=''), dict(code_digest='A' * 64), dict(script_digest='a' * 63), dict(remote_script='relative/job.sh'),
                 dict(remote_script='/synthetic/job\n.sh'), dict(toolkit_revision='main'), dict(remote_run_directory='relative'),
                 dict(remote_run_directory='/run/../escape'), dict(remote_run_directory='/run/%j'),
                 dict(validator_path='/v.py'), dict(validator_path='/v.py', validator_digest=D, validator_function='1bad'),
                 dict(validator_path='v.py', validator_digest=D, validator_function='check'))
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(SafetyError):
                spec(**overrides).checked()
        complete = spec(validator_path='/v.py', validator_digest=D, validator_function='check', toolkit_revision='0' * 40, remote_run_directory='/scratch/run')
        self.assertEqual(complete.checked()['partition'], 'normal')


class GrantTests(unittest.TestCase):
    def test_checked_grant_ignores_legacy_limits_and_checks_identity_scope_and_validity(self):
        checked_grant({**GRANT, 'limits': {'cpus': 1}}, spec(), 'btrippe', 50)
        checked_grant(GRANT, spec(), 'btrippe', 1)
        for label, grant, now in (('missing', None, 50), ('not mapping', ['btrippe'], 50), ('grantee', {**GRANT, 'grantee': 'other'}, 50),
                                  ('evidence', {**GRANT, 'evidence_reference': ''}, 50), ('no evidence', {k: v for k, v in GRANT.items() if k != 'evidence_reference'}, 50),
                                  ('before', GRANT, 0), ('expired', GRANT, 100), ('no validity', {k: v for k, v in GRANT.items() if k not in ('valid_from', 'valid_until')}, 50),
                                  ('partition', {**GRANT, 'partitions': ['owners']}, 50), ('scope', {**GRANT, 'scope': 'gpu'}, 50)):
            with self.subTest(label=label), self.assertRaises(SafetyError):
                checked_grant(grant, spec(), 'btrippe', now)


class FrozenRecordTests(unittest.TestCase):
    def test_frozen_record_contents_for_a_consumer_partition(self):
        record = frozen_record(spec(), grant=GRANT, now=50)
        expected = {**dataclasses.asdict(spec()), 'resources': resources_checked(spec().resources), 'schema_version': 1, 'grant': None,
                    'partition_profile': dict(partition_profile('normal')), 'partitions_sha256': partitions_sha256()}
        self.assertEqual(record, expected)
        self.assertIs(record['resources']['requeue'], False)
        self.assertNotIn('array', record['resources'])
        self.assertIsInstance(record['partition_profile'], dict)
        canonical(record)

    def test_frozen_record_carries_array_and_rounded_walltime(self):
        record = frozen_record(spec(resources=resources(array={'count': 4, 'throttle': 2}, walltime_seconds=61)))
        self.assertEqual(record['resources']['array'], {'count': 4, 'throttle': 2})
        self.assertEqual(record['resources']['walltime_seconds'], 120)

    def test_grant_recorded_only_when_the_profile_is_borrowed(self):
        borrowed = spec(task='borrowed', resources=resources('btrippe'))
        with self.assertRaisesRegex(SafetyError, 'grant'):
            frozen_record(borrowed, now=50)
        with self.assertRaisesRegex(SafetyError, 'partition/scope'):
            frozen_record(borrowed, grant={**GRANT, 'partitions': ['owners']}, now=50)
        with self.assertRaisesRegex(SafetyError, 'inactive|expired'):
            frozen_record(borrowed, grant=GRANT, now=100)
        legacy = {**GRANT, 'limits': {'cpus': 1}}
        record = frozen_record(borrowed, grant=legacy, now=50)
        self.assertEqual(record['grant'], legacy)
        self.assertTrue(record['partition_profile']['borrowed'])
        self.assertIsNone(frozen_record(spec(resources=resources('owners')), grant=GRANT, now=50)['grant'])

    def test_grant_validity_defaults_to_the_wall_clock(self):
        borrowed = spec(resources=resources('btrippe'))
        self.assertEqual(frozen_record(borrowed, grant={**GRANT, 'valid_until': time.time() + 3600})['grant']['grantee'], 'fixture')
        with self.assertRaisesRegex(SafetyError, 'inactive|expired'):
            frozen_record(borrowed, grant=GRANT)

    def test_policy_mismatch_blocks_admission(self):
        with self.assertRaisesRegex(SafetyError, 'mismatch'):
            frozen_record(spec(), advertised_policy='b' * 64)
        self.assertEqual(frozen_record(spec(), advertised_policy=D)['policy_digest'], D)

    def test_spec_problems_are_refused_before_any_profile_lookup(self):
        with self.assertRaisesRegex(SafetyError, 'identity field'):
            frozen_record(spec(project='bad name'))
        with self.assertRaisesRegex(SafetyError, 'GPU'):
            frozen_record(spec(resources=resources('normal', gpus=1)))


class SbatchOptionsTests(unittest.TestCase):
    def test_normal_partition_option_list_is_exact(self):
        record = frozen_record(spec(remote_run_directory='/scratch/run', resources=resources(constraint='CPU_GEN1|CPU_GEN2', signal='B:USR1@60')))
        self.assertEqual(sbatch_options(ATTEMPT, record), [
            'sbatch', '--parsable', '--job-name=shk-' + ATTEMPT, '--comment=shk:' + ATTEMPT, '--partition=normal', '--cpus-per-task=1',
            '--no-requeue', '--ntasks=1', '--mem=1024M', '--time=10', '--chdir=/scratch/run', '--output=/scratch/run/slurm-%j.out',
            '--error=/scratch/run/slurm-%j.err', '--constraint=CPU_GEN1|CPU_GEN2', '--signal=B:USR1@60'])

    def test_run_directory_and_gpu_flags_are_omitted_when_absent(self):
        options = sbatch_options(ATTEMPT, frozen_record(spec()))
        self.assertFalse([option for option in options if option.startswith(('--chdir', '--output', '--error', '--array', '--constraint', '--signal'))])
        self.assertNotIn('-G', options)

    def test_owners_emits_requeue_and_gpus(self):
        owners = sbatch_options(ATTEMPT, frozen_record(spec(resources=resources('owners', gpus=1, tasks=2, cpus=4))))
        self.assertIn('--requeue', owners)
        self.assertIn('--open-mode=append', owners)
        self.assertNotIn('--no-requeue', owners)
        self.assertEqual(owners[owners.index('-G') + 1], '1')
        self.assertIn('--ntasks=2', owners)
        self.assertIn('--cpus-per-task=4', owners)
        self.assertIn('--no-requeue', sbatch_options(ATTEMPT, frozen_record(spec(resources=resources('owners', requeue=False)))))

    def test_btrippe_is_borrowed_without_requeue(self):
        options = sbatch_options(ATTEMPT, frozen_record(spec(resources=resources('btrippe', gpus=1)), grant=GRANT, now=50))
        self.assertIn('--partition=btrippe', options)
        self.assertIn('--no-requeue', options)
        self.assertEqual(options[options.index('-G') + 1], '1')

    def test_array_adds_range_after_partition_and_task_output_patterns(self):
        throttled = frozen_record(spec(remote_run_directory='/scratch/run', resources=resources(array={'count': 4, 'throttle': 2})))
        options = sbatch_options(ATTEMPT, throttled)
        self.assertEqual(options[options.index('--partition=normal') + 1], '--array=0-3%2')
        self.assertIn('--output=/scratch/run/slurm-%A_%a.out', options)
        self.assertIn('--error=/scratch/run/slurm-%A_%a.err', options)
        self.assertFalse([option for option in options if '%j' in option])
        self.assertEqual(options[2:4], ['--job-name=shk-' + ATTEMPT, '--comment=shk:' + ATTEMPT])
        plain = sbatch_options(ATTEMPT, frozen_record(spec(resources=resources(array={'count': 1000}))))
        self.assertIn('--array=0-999', plain)
        self.assertEqual(sum(option.startswith('--array') for option in plain), 1)

    def test_options_read_only_the_frozen_profile(self):
        record = frozen(spec(), gpus_allowed=True)
        record['resources']['gpus'] = 1
        self.assertIn('-G', sbatch_options(ATTEMPT, record))
        with self.assertRaisesRegex(SafetyError, 'profile'):
            sbatch_options(ATTEMPT, {**record, 'partition_profile': {'preemptible': False}})
        with self.assertRaisesRegex(SafetyError, 'requeue'):
            sbatch_options(ATTEMPT, {**record, 'resources': {**record['resources'], 'requeue': True}})

    def test_legacy_frozen_spec_is_refused(self):
        legacy = {**dataclasses.asdict(spec()), 'resources': dict(spec().resources)}
        with self.assertRaisesRegex(SafetyError, 'partition_profile'):
            sbatch_options(ATTEMPT, legacy)
        record = frozen_record(spec())
        del record['resources']['requeue']
        with self.assertRaisesRegex(SafetyError, 'partition_profile/requeue'):
            sbatch_options(ATTEMPT, record)

    def test_malformed_attempt_or_record_is_refused(self):
        record = frozen_record(spec())
        for attempt in ('', 'A' * 32, '1' * 31, '1' * 33, 'shk-' + ATTEMPT):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(SafetyError, 'attempt'):
                sbatch_options(attempt, record)
        with self.assertRaisesRegex(SafetyError, 'frozen'):
            sbatch_options(ATTEMPT, [record])
        with self.assertRaisesRegex(SafetyError, 'array'):
            sbatch_options(ATTEMPT, {**record, 'resources': {**record['resources'], 'array': {'count': 1}}})


if __name__ == '__main__':
    unittest.main()
