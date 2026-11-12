"""Bounded, deterministic properties of the existing pure parsers and reducers."""

import string
import sys
import unittest
from collections import Counter

from fakes import SRC

# Fakes must scrub inherited scheduler state before Hypothesis or project imports.
sys.path.insert(0, str(SRC))
from hypothesis import example, given, settings
from hypothesis import strategies as st

from sherlock_kit import allocation, config, details, slurm, spec, transfer
from sherlock_kit.errors import ShkError

BOUNDED = settings(max_examples=100, derandomize=True, database=None, deadline=None)
SMALL_COUNT = st.integers(min_value=0, max_value=32)
PROJECTS = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(string.ascii_lowercase + string.digits),
    st.text(string.ascii_lowercase + string.digits + "-", max_size=23),
)
UNICODE_TEXT = st.text(min_size=1, max_size=128)
COMPONENTS = st.text(string.ascii_lowercase + string.digits + "_-", min_size=1, max_size=12)
PATHS = st.lists(COMPONENTS, min_size=1, max_size=4).map("/".join)
DIGESTS = st.text(string.hexdigits, min_size=64, max_size=64)
MANIFESTS = st.dictionaries(PATHS, DIGESTS, min_size=1, max_size=32)
KEYS = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(string.ascii_letters),
    st.text(string.ascii_letters + string.digits + "_/:", max_size=15),
)
VALUES = st.text("".join(chr(i) for i in range(32, 127) if chr(i) != "="), max_size=128).map(
    str.strip
)
FIELDS = st.dictionaries(KEYS, VALUES, min_size=1, max_size=8)
BASE = 123
NAME = "shk.demo.train-a.0123456789"
PARTITION = "owners"
TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT")


def manifest_text(entries):
    return "".join(f"{digest}  {name}\n" for name, digest in entries)


def field_text(fields):
    return " ".join(f"{key}={value}" for key, value in fields.items())


@st.composite
def array_pairs(draw):
    count = draw(st.integers(min_value=2, max_value=32))
    return count, draw(st.one_of(st.none(), st.integers(min_value=1, max_value=count)))


@st.composite
def ranges(draw):
    low = draw(SMALL_COUNT)
    return low, draw(st.integers(min_value=low, max_value=32))


@st.composite
def accounting(draw):
    """At most 32 rows: one array, 2..8 tasks, 1..4 distinct attempts per task.

    Submission keys are strictly increasing, all attempts have distinct identities,
    and restart history is contiguous. Partition/name/base are fixed, so permutation
    never changes first-row metadata or resolves a tie between conflicting facts.
    """
    count = draw(st.integers(min_value=2, max_value=8))
    rows, totals, final_states = [], [0, 0], []
    for task in range(count):
        attempts = draw(st.integers(min_value=1, max_value=4))
        for restart in range(attempts):
            elapsed = draw(st.integers(min_value=0, max_value=3600))
            cpus = draw(st.integers(min_value=1, max_value=16))
            gpus = draw(st.integers(min_value=0, max_value=8))
            state = draw(st.sampled_from(TERMINAL_STATES))
            if restart == attempts - 1:
                final_states.append(state)
            totals[0] += elapsed * cpus
            totals[1] += elapsed * gpus
            submitted = f"2026-10-10T12:00:{len(rows):02d}"
            exit_code = "0:0" if state == "COMPLETED" else "1:0"
            rows.append(
                f"{BASE}_{task}|{state}|{PARTITION}|{elapsed}|{cpus}|"
                f"cpu={cpus},gres/gpu={gpus}|{submitted}|{restart}|{exit_code}|{NAME}"
            )
    return count, rows, tuple(totals), final_states


def resolve_accounting(lines, count=None):
    parsed = slurm.parse_rows([], lines, lambda name: name == NAME)
    if parsed.malformed or parsed.truncated:
        raise AssertionError("generated accounting rows must parse completely")
    frozen = None if count is None else slurm.SpecFile(BASE, count)
    return slurm.resolve_name(parsed.by_name[NAME], newest_spec=frozen)


class ParserProperties(unittest.TestCase):
    @BOUNDED
    @given(project=PROJECTS, campaign=UNICODE_TEXT, task=UNICODE_TEXT)
    @example(project="demo", campaign="default", task="train-a")
    def test_job_names_are_deterministic_and_bounded(self, project, campaign, task):
        self.assertEqual(config.parse({"project": project}).project, project)
        name = spec.job_name(project, campaign, task)
        self.assertEqual(name, spec.job_name(project, campaign, task))
        self.assertIsNotNone(spec.NAME.fullmatch(name))
        self.assertLessEqual(len(name), 60)

    def test_existing_golden_name_and_large_array_limit(self):
        self.assertEqual(spec.job_name("demo", "default", "train-a"), "shk.demo.train-a.c04a9f67e5")
        self.assertEqual(spec.array("1000%1000"), spec.Array(1000, 1000))

    @BOUNDED
    @given(amount=st.integers(min_value=1, max_value=4096), lower=st.booleans())
    def test_memory_gigabytes_are_1024_megabytes(self, amount, lower):
        g, m = ("g", "m") if lower else ("G", "M")
        self.assertEqual(spec.memory_mb(f"{amount}{g}"), spec.memory_mb(f"{amount * 1024}{m}"))
        self.assertEqual(spec.memory_mb(f"{amount}{g}"), amount * 1024)

    @BOUNDED
    @given(seconds=st.integers(min_value=1, max_value=7 * 24 * 3600))
    def test_positive_clock_duration_rounds_up(self, seconds):
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds_part = divmod(remainder, 60)
        self.assertEqual(
            spec.minutes(f"{hours}:{minutes:02d}:{seconds_part:02d}"), (seconds + 59) // 60
        )

    @BOUNDED
    @given(pair=array_pairs())
    def test_array_throttle_bounds(self, pair):
        count, throttle = pair
        text = str(count) + ("" if throttle is None else f"%{throttle}")
        self.assertEqual(spec.array(text), spec.Array(count, throttle))

    @BOUNDED
    @given(
        amount=st.integers(min_value=1, max_value=32), count=st.integers(min_value=2, max_value=32)
    )
    def test_explicit_resource_grammar_violations(self, amount, count):
        invalid = (
            (spec.memory_mb, f"{amount}T"),
            (spec.memory_mb, f"{amount}.5G"),
            (spec.memory_mb, f"-{amount}M"),
            (spec.minutes, f"{amount}:60:00"),
            (spec.minutes, f"{amount}:00:60"),
            (spec.minutes, f"{amount}d"),
            (spec.array, f"{count}%0"),
            (spec.array, f"{count}%{count + 1}"),
            (spec.array, f"{count}%"),
        )
        for parse, text in invalid:
            with self.assertRaises(ShkError) as caught:
                parse(text)
            self.assertEqual((caught.exception.code, caught.exception.token), (1, "spec_invalid"))

    @BOUNDED
    @given(entries=MANIFESTS, data=st.data())
    def test_manifest_order_and_hex_normalization(self, entries, data):
        expected = {path: digest.lower() for path, digest in entries.items()}
        shuffled = data.draw(st.permutations(list(entries.items())))
        self.assertEqual(transfer.parse_manifest(manifest_text(shuffled)), expected)
        normalized = manifest_text(expected.items())
        self.assertEqual(transfer.parse_manifest(normalized), expected)
        self.assertEqual(transfer.parse_manifest(manifest_text(entries.items())), expected)

    @BOUNDED
    @given(path=PATHS, digest=DIGESTS, other=DIGESTS)
    def test_manifest_duplicate_entries_fail(self, path, digest, other):
        with self.assertRaises(ShkError) as caught:
            transfer.parse_manifest(manifest_text([(path, digest), (path, other)]))
        self.assertEqual((caught.exception.code, caught.exception.token), (1, "manifest_invalid"))

    @BOUNDED
    @given(
        prefix=COMPONENTS,
        suffix=COMPONENTS,
        forbidden=st.sampled_from(("", ".", "..", ".shk", ".shk-private")),
        digest=DIGESTS,
    )
    def test_manifest_forbidden_components_fail(self, prefix, suffix, forbidden, digest):
        for path in (f"{prefix}/{forbidden}/{suffix}", f"/{prefix}", f"{prefix}/SHA256SUMS"):
            with self.assertRaises(ShkError) as caught:
                transfer.parse_manifest(manifest_text([(path, digest)]))
            self.assertEqual(caught.exception.token, "manifest_invalid")

    @BOUNDED
    @given(
        spans=st.lists(ranges(), min_size=1, max_size=16),
        throttle=st.integers(min_value=1, max_value=32),
    )
    def test_aggregate_ids_are_sorted_unions(self, spans, throttle):
        parts = [str(low) if low == high else f"{low}-{high}" for low, high in spans]
        # Repeat a range explicitly so overlapping membership is always exercised.
        expression = ",".join([*parts, parts[0]])
        expected = sorted({index for low, high in spans for index in range(low, high + 1)})
        self.assertEqual(slurm.job_id(f"{BASE}_[{expression}]"), (BASE, None, expected))
        self.assertEqual(slurm.job_id(f"{BASE}_[{expression}%{throttle}]"), (BASE, None, expected))

    @BOUNDED
    @given(task=SMALL_COUNT)
    def test_single_and_explicit_array_task_ids_stay_distinct(self, task):
        self.assertEqual(slurm.job_id(str(BASE)), (BASE, None, None))
        self.assertEqual(slurm.job_id(f"{BASE}_{task}"), (BASE, task, None))

    @BOUNDED
    @given(
        shares=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=4),
        extra=SMALL_COUNT,
    )
    def test_gpu_totals_and_typed_shares_are_not_double_counted(self, shares, extra):
        typed = ",".join(f"gres/gpu:type{i}={share}" for i, share in enumerate(shares))
        total = sum(shares) + extra
        text = f"cpu=4,gres/gpu={total},{typed}"
        self.assertEqual(slurm.gpus(text), total)
        self.assertEqual(allocation.tres_counts(text), (4, total))
        self.assertEqual(slurm.gpus(typed), sum(shares))
        self.assertEqual(allocation.tres_counts(typed), (None, sum(shares)))

    @BOUNDED
    @given(generic=SMALL_COUNT, excess=st.integers(min_value=1, max_value=32))
    def test_gpu_contradiction_has_distinct_parser_contracts(self, generic, excess):
        text = f"gres/gpu={generic},gres/gpu:a100={generic + excess}"
        self.assertEqual(slurm.gpus(text), generic + excess)
        with self.assertRaises(ShkError) as caught:
            allocation.tres_counts(text)
        self.assertEqual(caught.exception.token, "allocation_invalid")

    @BOUNDED
    @given(first=SMALL_COUNT, second=SMALL_COUNT)
    def test_duplicate_gpu_evidence_has_distinct_parser_contracts(self, first, second):
        for key in ("gres/gpu", "gres/gpu:a100"):
            text = f"{key}={first},{key}={second}"
            self.assertEqual(slurm.gpus(text), first + second)
            with self.assertRaises(ShkError) as caught:
                allocation.tres_counts(text)
            self.assertEqual(caught.exception.token, "allocation_invalid")

    @BOUNDED
    @given(fields=FIELDS)
    @example(fields={"JobId": "123", "Comment": "two interior spaces here", "TRES/gpu:a100": "4"})
    def test_fields_round_trip_unambiguous_printable_values(self, fields):
        raw = field_text(fields)
        self.assertEqual(details.fields(raw), fields)
        self.assertEqual(allocation.strict_fields(raw), fields)

    @BOUNDED
    @given(fields=FIELDS, data=st.data())
    def test_strict_fields_reject_duplicate_keys(self, fields, data):
        key = data.draw(st.sampled_from(list(fields)))
        with self.assertRaises(ShkError) as caught:
            allocation.strict_fields(f"{field_text(fields)} {key}=duplicate")
        self.assertEqual(caught.exception.token, "allocation_invalid")

    @BOUNDED
    @given(
        fields=FIELDS, control=st.sampled_from([chr(i) for i in (*range(32), 127)]), data=st.data()
    )
    def test_strict_fields_reject_injected_controls(self, fields, control, data):
        raw = field_text(fields)
        position = data.draw(st.integers(min_value=0, max_value=len(raw)))
        with self.assertRaises(ShkError) as caught:
            allocation.strict_fields(raw[:position] + control + raw[position:])
        self.assertEqual(caught.exception.token, "allocation_invalid")


class AccountingProperties(unittest.TestCase):
    @BOUNDED
    @given(case=accounting(), data=st.data())
    def test_order_and_exact_duplicates_preserve_semantic_summary(self, case, data):
        count, rows, totals, final_states = case
        baseline = resolve_accounting(rows, count)
        permuted = list(data.draw(st.permutations(rows)))
        self.assertEqual(resolve_accounting(permuted, count), baseline)
        # Exact duplicates preserve every fact; conflicting tied records are excluded.
        self.assertEqual(resolve_accounting(permuted + permuted, count), baseline)
        current = baseline["current"]
        self.assertEqual(
            current["cost"], {"cpu_seconds": totals[0], "gpu_seconds": totals[1], "final": True}
        )
        self.assertEqual(current["tasks"]["by_state"], dict(Counter(final_states)))
        self.assertEqual(current["tasks"]["count"], count)
        self.assertEqual(current["tasks"]["missing"], 0)
        self.assertEqual(
            baseline["state"],
            "done" if all(state == "COMPLETED" for state in final_states) else "failed",
        )

    @BOUNDED
    @given(case=accounting(), data=st.data())
    def test_missing_entire_task_prevents_final_cost_with_frozen_count(self, case, data):
        count, rows, _, _ = case
        removed = data.draw(st.integers(min_value=0, max_value=count - 1))
        remaining = [line for line in rows if not line.startswith(f"{BASE}_{removed}|")]
        current = resolve_accounting(remaining, count)["current"]
        self.assertEqual(current["tasks"]["count"], count)
        self.assertEqual(current["tasks"]["missing"], 1)
        self.assertFalse(current["cost"]["final"])

    @BOUNDED
    @given(case=accounting())
    def test_noncontiguous_tasks_without_spec_have_unknown_count_and_nonfinal_cost(self, case):
        _, rows, _, _ = case
        # Keeping 1..N-1 guarantees a noncontiguous set relative to the 0-based layout.
        remaining = [line for line in rows if not line.startswith(f"{BASE}_0|")]
        current = resolve_accounting(remaining)["current"]
        self.assertIsNone(current["array"]["count"])
        self.assertIsNone(current["tasks"]["count"])
        self.assertIsNone(current["tasks"]["missing"])
        self.assertFalse(current["cost"]["final"])


if __name__ == "__main__":
    unittest.main()
