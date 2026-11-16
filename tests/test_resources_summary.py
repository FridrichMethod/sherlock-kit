"""`shk resources --summary` through the real resources.sh against a fake `scontrol show nodes`."""

import sys
from unittest.mock import patch

import test_resources
from fakes import SRC
from test_flows import Flow
from test_properties import BOUNDED

sys.path.insert(0, str(SRC))
from hypothesis import given
from hypothesis import strategies as st

from sherlock_kit import executor, node_resources

node = test_resources.node
STOCK_FEATURES = "ActiveFeatures=IB:EDR,CPU_MNF:INTEL,NO_GPU "
COUNTS = ("nodes", "schedulable_nodes", "countable_nodes", "unschedulable_nodes", "unknown_nodes")
CAPACITY = ("cpus", "memory_mb", "gpus")
NULL = {"configured": None, "allocated": None, "free": None}
RULES = {
    "snapshot": "Snapshot heuristic at queried_at: configured minus allocated capacity. It does "
    "not model fairshare, QOS or partition limits, reservations, preemption or pending work, "
    "reserves nothing and predicts no start time.",
    "countable": "A node is countable when its State has none of the flags listed in "
    "provenance.schedulable and its configured and allocated CPUs, memory and GPUs are all "
    "known. free and slots count countable nodes only; configured and allocated also count "
    "unschedulable nodes. A node with a missing fact is in unknown_nodes and in no sum: beside "
    "a nonzero unknown_nodes the numbers are lower bounds. A value with nothing known behind it "
    "is null, never 0: configured and allocated in a group without a known node, free and "
    "slots in a group without a countable node unless all its nodes are known unschedulable "
    "nodes.",
    "slots": "Per countable node: the smallest of free CPUs, free memory and, when the shape "
    "asks for GPUs, free GPUs, each divided by the shape's amount and rounded down; summed over "
    "the group.",
    "overlap": "A node that lists several partitions is in each of their groups, and by_sku and "
    "by_class split the same nodes in two ways: never add partition groups or the two "
    "breakdowns together. union counts every node once. Within one breakdown a node is in "
    "exactly one entry, the one for its exact set of feature values.",
}
# 0.7.1 reply keys (schema_version and ok come from the renderer) and node record keys.
PLAIN_KEYS = [
    "schema_version",
    "ok",
    "complete",
    "malformed",
    "executor",
    "queried_at",
    "next_check_after",
    "provenance",
    "nodes",
]
NODE_KEYS = [
    "name",
    "state",
    "schedulable",
    "partitions",
    "features",
    "free_cpus",
    "free_memory_mb",
    "free_gpus",
    "configured_cpus",
    "allocated_cpus",
    "configured_memory_mb",
    "allocated_memory_mb",
    "configured_gpus",
    "allocated_gpus",
    "configured_tres",
    "allocated_tres",
    "raw",
]
GROUP_KEYS = [*COUNTS, *CAPACITY, "slots"]
SHAPE = "s:cpus=4,mem=16G"
NO_ALLOCATION = "CPUEfctv=20 CPUTot=20"  # CPUAlloc missing: allocated CPUs unknown
POOL = ("gpu", "owners", "stat", "normal")
DEFECTS = (None, None, "CPUAlloc", "State", "malformed")


def featured(line, features):
    """The line with these ActiveFeatures instead of the stock CPU node's."""
    assert STOCK_FEATURES in line
    return line.replace(STOCK_FEATURES, f"ActiveFeatures={features} ")


def gpu_node(name, state, partitions, features, *, gpus=4, used=0, cpus=None):
    line = node(
        name,
        state,
        partitions,
        cpus=cpus or "CPUAlloc=0 CPUEfctv=20 CPUTot=20",
        cfg=f"cpu=20,mem=125G,billing=51,gres/gpu={gpus}",
        alloc=f"cpu=0,mem=0,gres/gpu={used}",
    )
    return featured(line, features)


def stateless(line, state):
    """The line without its State field."""
    assert f"State={state} " in line
    return line.replace(f"State={state} ", "")


def entry_keys(entries, kind):
    return [entry[kind] for entry in entries]


def values(group):
    """Every capacity value and slot of a group, as (field, path)."""
    for kind in CAPACITY:
        for side in ("configured", "allocated", "free"):
            yield side, (kind, side)
    for name in group["slots"]:
        yield "slots", ("slots", name)


def lookup(group, path):
    return group[path[0]][path[1]]


@st.composite
def node_lines(draw, name):
    """One node line (in POOL partitions) with random capacity, features and one defect."""
    member = draw(st.lists(st.sampled_from(POOL), min_size=1, max_size=3, unique=True))
    state = draw(st.sampled_from(("IDLE", "MIXED", "ALLOCATED", "IDLE+DRAIN", "DOWN+DRAIN")))
    cpus = draw(st.integers(min_value=1, max_value=64))
    used_cpus = draw(st.integers(min_value=0, max_value=cpus))
    mem = draw(st.integers(min_value=1, max_value=512))
    used_mem = draw(st.integers(min_value=0, max_value=mem))
    gpus = draw(st.integers(min_value=0, max_value=8))
    used_gpus = draw(st.integers(min_value=0, max_value=gpus))
    skus = draw(st.lists(st.sampled_from(("A100", "V100", "H100_SXM5")), max_size=2, unique=True))
    classes = draw(st.lists(st.sampled_from(("SH3_G4", "SH4_G8")), max_size=2, unique=True))
    defect = draw(st.sampled_from(DEFECTS))
    line = node(
        name,
        state,
        ",".join(member),
        cpus=(f"CPUAlloc={used_cpus} " if defect != "CPUAlloc" else "")
        + f"CPUEfctv={cpus} CPUTot={cpus}",
        cfg=f"cpu={cpus},mem={mem}G,gres/gpu={'x' if defect == 'malformed' else gpus}",
        alloc=f"cpu={used_cpus},mem={used_mem}G,gres/gpu={used_gpus}",
    )
    features = [*(f"GPU_SKU:{v}" for v in skus), "IB:HDR", *(f"CLASS:{v}" for v in classes)]
    line = featured(line, ",".join(features))
    return member, stateless(line, state) if defect == "State" else line


@st.composite
def reads(draw):
    """Node lines, requested partitions (repeats allowed), --by values and shapes."""
    count = draw(st.integers(min_value=0, max_value=12))
    nodes = [draw(node_lines(f"cn{i:02d}")) for i in range(count)]
    requested = draw(st.lists(st.sampled_from(POOL), min_size=1, max_size=4))
    by = draw(st.lists(st.sampled_from(("sku", "class")), max_size=3))
    amounts = st.tuples(
        st.integers(min_value=1, max_value=16),
        st.integers(min_value=1, max_value=64),
        st.integers(min_value=0, max_value=2),
    )
    shapes = [
        f"s{i}:cpus={cpus},mem={mem}G,gpus={gpus}"
        for i, (cpus, mem, gpus) in enumerate(draw(st.lists(amounts, max_size=2)))
    ]
    return nodes, requested, by, shapes


class SummaryFlows(Flow):
    scontrol = test_resources.ResourcesFlows.scontrol  # the fake only, not its tests

    def summary(self, *partitions, extra=()):
        """Run resources --summary once; assert exactly one resources.sh call and exit 0."""
        argv = [word for name in partitions for word in ("--partition", name)]
        with patch.object(executor, "run_program", wraps=executor.run_program) as run:
            code, report = self.shk("resources", *argv, "--summary", *extra)
        self.assertEqual(code, 0, report)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[1:], ("resources", sorted(set(partitions))))
        return report

    def refused(self, *argv):
        """Run resources with these options; it must fail offline, before any remote call."""
        with patch.object(executor, "run_program", wraps=executor.run_program) as run:
            code, report = self.shk("resources", *argv)
        run.assert_not_called()
        return code, report

    def assert_split(self, group, entries, kind):
        """The entries of one breakdown split their group as contract 2.A states."""
        keys = entry_keys(entries, kind)
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len({tuple(key) for key in keys}), len(keys))
        for count in COUNTS:
            self.assertEqual(sum(entry[count] for entry in entries), group[count], count)
        for entry in entries:
            self.assertEqual(entry[COUNTS[0]], sum(entry[count] for count in COUNTS[2:]))
            self.assertEqual(list(entry), [kind, *GROUP_KEYS])
        for field, path in values(group):
            total, parts = lookup(group, path), [lookup(entry, path) for entry in entries]
            if total is not None:
                self.assertEqual(sum(part for part in parts if part is not None), total, path)
                continue
            for entry, part in zip(entries, parts):
                self.assertIn(part, (None, 0), path)
                if part == 0:  # a known 0: free or slots of known unschedulable nodes only
                    self.assertIn(field, ("free", "slots"))
                    self.assertEqual(entry["nodes"], entry["unschedulable_nodes"])

    def test_unchanged_without_summary_the_reply_keeps_its_0_7_1_keys(self):
        self.scontrol(node("cn01", "IDLE", "gpu"), node("cn02", "IDLE+DRAIN", "gpu"))
        code, plain = self.shk("resources", "--partition", "gpu")
        self.assertEqual((code, list(plain)), (0, PLAIN_KEYS))
        self.assertEqual([list(found) for found in plain["nodes"]], [NODE_KEYS, NODE_KEYS])
        summary = self.summary("gpu")
        # The summary mode replaces nodes by summary and keeps every other key as it was.
        self.assertEqual(list(summary), [*PLAIN_KEYS[:-1], "summary"])
        self.assertEqual(
            {key: value for key, value in summary.items() if key != "summary"},
            {key: value for key, value in plain.items() if key != "nodes"},
        )

    def test_by_or_shape_without_summary_is_spec_invalid_before_any_read(self):
        self.scontrol(node("cn01", "IDLE", "gpu"))
        for argv, named in (
            (("--by", "sku"), "--by"),
            (("--shape", SHAPE), "--shape"),
            (("--shape", SHAPE, "--by", "class"), "--by and --shape"),
        ):
            with self.subTest(argv=argv):
                code, report = self.refused("--partition", "gpu", *argv)
                self.assertEqual((code, report["ok"], report["error"]), (1, False, "spec_invalid"))
                self.assertEqual(report["message"], f"{named}: only with --summary")

    def test_bad_shapes_are_spec_invalid_offline(self):
        self.scontrol(node("cn01", "IDLE", "gpu"))
        bad = (
            "",
            "cpus=1,mem=1G",
            ":cpus=1,mem=1G",
            "-a:cpus=1,mem=1G",
            ".a:cpus=1,mem=1G",
            "a b:cpus=1,mem=1G",
            "a/b:cpus=1,mem=1G",
            "\u00e9:cpus=1,mem=1G",
            "a" * 33 + ":cpus=1,mem=1G",
            "a:",
            "a:cpus",
            "a:cpus=1,mem=1G,",
            "a:cpus=1,mem=1G,disk=1",
            "a:CPUS=1,mem=1G",
            "a:cpus=1,cpus=2,mem=1G",
            "a:cpus=1,mem=1G,mem=2G",
            "a:cpus=1,mem=1G,gpus=1,gpus=1",
            "a:mem=1G",
            "a:cpus=1",
            "a:gpus=1",
            "a:cpus=0,mem=1G",
            "a:cpus=01,mem=1G",
            "a:cpus=-1,mem=1G",
            "a:cpus=,mem=1G",
            "a:cpus=1.5,mem=1G",
            "a:cpus=\u0661,mem=1G",
            "a:cpus=1,mem=0G",
            "a:cpus=1,mem=16",
            "a:cpus=1,mem=16T",
            "a:cpus=1,mem=1.5G",
            "a:cpus=1,mem=16 G",
            "a:cpus=1,mem=",
            "a:cpus=1,mem=1G,gpus=01",
            "a:cpus=1,mem=1G,gpus=-1",
            "a:cpus=1,mem=1G,gpus=",
            "a:cpus=1,mem=1G,gpus=x",
            "a:cpus=1,mem=1G:x",
        )
        for shape in bad:
            with self.subTest(shape=shape):
                # --shape=VALUE: argparse would take a value that starts with "-" for an option.
                code, report = self.refused("--partition", "gpu", "--summary", f"--shape={shape}")
                self.assertEqual((code, report["error"]), (1, "spec_invalid"))
                self.assertTrue(report["message"].startswith(f"--shape {shape!r}: "))
                self.assertIn("NAME:cpus=N,mem=SIZE[,gpus=N]", report["hint"])
        # A repeated NAME is refused even when the amounts differ.
        code, report = self.refused(
            "--partition", "gpu", "--summary", "--shape", SHAPE, "--shape", "s:cpus=8,mem=8G"
        )
        self.assertEqual((code, report["error"]), (1, "spec_invalid"))
        self.assertEqual(report["message"], "--shape 's:cpus=8,mem=8G': NAME 's' repeated")

    def test_good_shapes_take_keys_in_any_order_and_the_submit_mem_grammar(self):
        self.scontrol(node("cn01", "IDLE", "gpu"))
        longest = "A" + "b._-9" * 6 + "z"
        self.assertEqual(len(longest), 32)
        shapes = (
            f"{longest}:cpus=1,mem=16G",
            "x.1:mem=16000M,gpus=0,cpus=8",
            "Y_2:gpus=2,mem=16g,cpus=1",
            "9:cpus=12,gpus=1,mem=1m",
        )
        report = self.summary("gpu", extra=[word for s in shapes for word in ("--shape", s)])
        self.assertEqual(
            report["summary"]["shapes"],
            {
                longest: {"cpus": 1, "memory_mb": 16384, "gpus": 0},
                "x.1": {"cpus": 8, "memory_mb": 16000, "gpus": 0},
                "Y_2": {"cpus": 1, "memory_mb": 16384, "gpus": 2},
                "9": {"cpus": 12, "memory_mb": 1, "gpus": 1},
            },
        )
        self.assertEqual(list(report["summary"]["shapes"]), [longest, "x.1", "Y_2", "9"])
        self.assertEqual(
            [list(shape) for shape in report["summary"]["shapes"].values()],
            [["cpus", "memory_mb", "gpus"]] * 4,
        )

    def test_offline_checks_run_partitions_then_summary_then_each_shape_in_order(self):
        self.scontrol(node("cn01", "IDLE", "gpu"))
        code, report = self.refused("--partition", "gpux", "--by", "sku", "--shape", "-")
        self.assertEqual((code, report["error"]), (3, "unknown_partition"))
        code, report = self.refused("--partition", "gpu", "--shape", "-")
        self.assertEqual((code, report["message"]), (1, "--shape: only with --summary"))
        code, report = self.refused(
            "--partition", "gpu", "--summary", "--shape", SHAPE, "--shape", "b:", "--shape", "-"
        )
        self.assertEqual(code, 1)
        self.assertTrue(report["message"].startswith("--shape 'b:': "))

    def test_by_takes_sku_or_class_and_another_value_is_a_usage_error(self):
        self.scontrol(node("cn01", "IDLE", "gpu"))
        for value in ("gpu", "SKU", "classes", ""):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as caught:
                    self.shk("resources", "--partition", "gpu", "--summary", "--by", value)
                self.assertEqual(caught.exception.code, 2)

    def test_summary_reply_key_order_rules_and_unchanged_provenance(self):
        self.scontrol(node("cn01", "IDLE", "gpu,owners"))
        report = self.summary("owners", "gpu", "owners")
        self.assertEqual(
            list(report),
            [
                "schema_version",
                "ok",
                "complete",
                "malformed",
                "executor",
                "queried_at",
                "next_check_after",
                "provenance",
                "summary",
            ],
        )
        self.assertNotIn("nodes", report)
        self.assertEqual(report["provenance"], node_resources.PROVENANCE)
        self.assertEqual(report["provenance"]["memory"], "CfgTRES minus AllocTRES")
        summary = report["summary"]
        self.assertEqual(
            list(summary), ["rules", "shapes", "by", "overlapping_nodes", "union", "partitions"]
        )
        self.assertEqual(summary["rules"], RULES)
        self.assertEqual(list(summary["rules"]), ["snapshot", "countable", "slots", "overlap"])
        self.assertEqual((summary["shapes"], summary["by"]), ({}, []))
        self.assertEqual(list(summary["partitions"]), ["owners", "gpu"])
        for group in (summary["union"], *summary["partitions"].values()):
            self.assertEqual(list(group), GROUP_KEYS)
            self.assertEqual(group["slots"], {})
            for kind in CAPACITY:
                self.assertEqual(list(group[kind]), ["configured", "allocated", "free"])

    def test_node_classes_are_counted_and_nodes_is_their_sum(self):
        self.scontrol(
            node("cn01", "MIXED", "owners"),
            node("cn02", "IDLE+DRAIN", "owners"),
            node("cn03", "IDLE", "owners", cpus=NO_ALLOCATION),
            stateless(node("cn04", "IDLE", "owners"), "IDLE"),
            node("cn05", "DOWN+NOT_RESPONDING", "owners", cpus=NO_ALLOCATION),
            node("cn06", "ALLOCATED", "owners"),
        )
        group = self.summary("owners")["summary"]["union"]
        self.assertEqual(
            [group[count] for count in COUNTS],
            [6, 3, 2, 1, 3],  # schedulable: cn01 and cn06 countable, cn03 unknown
        )
        self.assertEqual(group["nodes"], sum(group[count] for count in COUNTS[2:]))

    def test_free_counts_countable_nodes_and_configured_also_unschedulable_ones(self):
        self.scontrol(
            node(
                "cn01",
                "MIXED",
                "owners",
                cpus="CPUAlloc=4 CPUEfctv=20 CPUTot=20",
                alloc="cpu=4,mem=8G",
            ),
            node(
                "cn02",
                "IDLE+DRAIN",
                "owners",
                cpus="CPUAlloc=2 CPUEfctv=20 CPUTot=20",
                alloc="cpu=2,mem=4G",
            ),
            node("cn03", "IDLE", "owners", cpus=NO_ALLOCATION),
        )
        report = self.summary("owners", extra=("--shape", SHAPE))
        union = report["summary"]["union"]
        self.assertEqual(
            (union["cpus"], union["memory_mb"], union["gpus"]),
            (
                {"configured": 40, "allocated": 6, "free": 16},
                {"configured": 256000, "allocated": 12288, "free": 119808},
                {"configured": 0, "allocated": 0, "free": 0},
            ),
        )
        self.assertEqual(union["slots"], {"s": 4})
        self.assertEqual(union["unknown_nodes"], 1)
        self.assertFalse(report["complete"])  # with an unknown node the sums are lower bounds
        self.assertEqual(report["summary"]["partitions"]["owners"], union)

    def test_a_group_of_unknown_nodes_only_has_null_sums_and_slots_never_0(self):
        self.scontrol(
            node("cn01", "IDLE", "gpu", cpus=NO_ALLOCATION),
            stateless(node("cn02", "IDLE", "gpu"), "IDLE"),
            node("cn03", "IDLE", "normal"),
        )
        report = self.summary("gpu", "normal", extra=("--shape", SHAPE))
        group = report["summary"]["partitions"]["gpu"]
        self.assertEqual([group[count] for count in COUNTS], [2, 1, 0, 0, 2])
        self.assertEqual([group[kind] for kind in CAPACITY], [NULL] * 3)
        self.assertEqual(group["slots"], {"s": None})
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["partitions"]["normal"]["slots"], {"s": 5})

    def test_a_drained_known_node_beside_an_unknown_node_has_configured_and_null_free(self):
        self.scontrol(
            node("cn01", "IDLE+DRAIN", "bioe"), node("cn02", "IDLE", "bioe", cpus=NO_ALLOCATION)
        )
        report = self.summary("bioe", extra=("--shape", SHAPE))
        group = report["summary"]["partitions"]["bioe"]
        self.assertEqual(
            [group[kind] for kind in CAPACITY],
            [
                {"configured": 20, "allocated": 0, "free": None},
                {"configured": 128000, "allocated": 0, "free": None},
                {"configured": 0, "allocated": 0, "free": None},
            ],
        )
        self.assertEqual(group["slots"], {"s": None})
        self.assertFalse(report["complete"])

    def test_known_drained_nodes_only_have_free_0_and_slots_0(self):
        self.scontrol(
            node("cn01", "IDLE+DRAIN", "stat"),
            node("cn02", "DOWN+NOT_RESPONDING", "stat", cfg="cpu=20,mem=125G,gres/gpu=4"),
        )
        report = self.summary("stat", extra=("--shape", SHAPE, "--shape", "g:cpus=1,mem=1G,gpus=1"))
        group = report["summary"]["union"]
        self.assertEqual(
            [group[kind] for kind in CAPACITY],
            [
                {"configured": 40, "allocated": 0, "free": 0},
                {"configured": 256000, "allocated": 0, "free": 0},
                {"configured": 4, "allocated": 0, "free": 0},
            ],
        )
        self.assertEqual(group["slots"], {"s": 0, "g": 0})
        self.assertEqual([group[count] for count in COUNTS], [2, 0, 0, 2, 0])
        self.assertTrue(report["complete"])

    def test_a_requested_partition_that_no_line_names_has_nodes_0_and_null_sums(self):
        self.scontrol(node("cn01", "IDLE", "gpu"))
        report = self.summary("gpu", "normal", extra=("--shape", SHAPE, "--by", "sku"))
        group = report["summary"]["partitions"]["normal"]
        self.assertEqual([group[count] for count in COUNTS], [0] * 5)
        self.assertEqual([group[kind] for kind in CAPACITY], [NULL] * 3)
        self.assertEqual((group["slots"], group["by_sku"]), ({"s": None}, []))
        # The union of an empty read: the read does not show any capacity.
        self.scontrol()
        report = self.summary("normal", extra=("--shape", SHAPE))
        union = report["summary"]["union"]
        self.assertEqual([union[count] for count in COUNTS], [0] * 5)
        self.assertEqual(
            ([union[kind] for kind in CAPACITY], union["slots"]), ([NULL] * 3, {"s": None})
        )
        self.assertEqual((report["complete"], report["malformed"]), (False, []))

    def test_a_partition_whose_only_line_is_malformed_is_one_unknown_node(self):
        bad = featured(
            node("cn01", "IDLE", "owners,stat", cfg="cpu=20,mem=125G,gres/gpu=x"),
            "GPU_SKU:V100_PCIE,CLASS:SH3_G4",
        )
        self.scontrol(bad, node("cn02", "IDLE", "gpu"))
        code, plain = self.shk(
            "resources", "--partition", "owners", "--partition", "stat", "--partition", "gpu"
        )
        self.assertEqual((code, plain["malformed"], len(plain["nodes"])), (0, [bad], 1))
        report = self.summary("owners", "stat", "gpu", extra=("--shape", SHAPE, "--by", "sku"))
        self.assertEqual((report["complete"], report["malformed"]), (False, plain["malformed"]))
        summary = report["summary"]
        for name in ("owners", "stat"):
            group = summary["partitions"][name]
            # State=IDLE is in the line, but every fact of a malformed line is null.
            self.assertEqual([group[count] for count in COUNTS], [1, 0, 0, 0, 1])
            self.assertEqual([group[kind] for kind in CAPACITY], [NULL] * 3)
            self.assertEqual(group["slots"], {"s": None})
            self.assertEqual(entry_keys(group["by_sku"], "sku"), [["V100_PCIE"]])
        self.assertEqual(summary["overlapping_nodes"], 1)
        union = summary["union"]
        self.assertEqual([union[count] for count in COUNTS], [2, 1, 1, 0, 1])
        self.assertEqual(entry_keys(union["by_sku"], "sku"), [[], ["V100_PCIE"]])
        self.assertEqual(union["by_sku"][1]["unknown_nodes"], 1)

    def test_slots_take_the_tightest_resource_per_node_rounded_down(self):
        self.scontrol(
            node(
                "cnA",
                "MIXED",
                "normal",
                cpus="CPUAlloc=5 CPUEfctv=20 CPUTot=20",
                alloc="cpu=5,mem=5G",
            ),
            node(
                "cnB",
                "MIXED",
                "gpu",
                cpus="CPUAlloc=4 CPUEfctv=32 CPUTot=32",
                cfg="cpu=32,mem=250G,billing=100,gres/gpu=4",
                alloc="cpu=4,mem=4G,gres/gpu=3",
            ),
            node(
                "cnC",
                "IDLE",
                "bigmem",
                cpus="CPUAlloc=0 CPUEfctv=64 CPUTot=64",
                cfg="cpu=64,mem=100G",
            ),
            node("cnD", "IDLE+DRAIN", "owners", cfg="cpu=20,mem=125G,gres/gpu=8"),
        )
        shapes = (
            "cpu8:cpus=8,mem=8G",
            "mem64:mem=64G,cpus=1",
            "gpu1:cpus=4,gpus=1,mem=16G",
            "gpu2:gpus=2,cpus=1,mem=1G",
        )
        report = self.summary(
            "normal", "gpu", "bigmem", "owners", extra=[w for s in shapes for w in ("--shape", s)]
        )
        summary = report["summary"]
        slots = {name: group["slots"] for name, group in summary["partitions"].items()}
        self.assertEqual(
            slots,
            {
                # free 15 CPUs, 120 GiB: CPU-bound for cpu8 (15 // 8), memory-bound for mem64.
                "normal": {"cpu8": 1, "mem64": 1, "gpu1": 0, "gpu2": 0},
                # free 28 CPUs, 246 GiB, 1 GPU: cpu8 has no GPU term; GPU-bound for gpu1, gpu2.
                "gpu": {"cpu8": 3, "mem64": 3, "gpu1": 1, "gpu2": 0},
                # free 64 CPUs, 100 GiB, no GPU.
                "bigmem": {"cpu8": 8, "mem64": 1, "gpu1": 0, "gpu2": 0},
                # one known drained node: a known 0.
                "owners": {"cpu8": 0, "mem64": 0, "gpu1": 0, "gpu2": 0},
            },
        )
        self.assertEqual(summary["union"]["slots"], {"cpu8": 12, "mem64": 5, "gpu1": 1, "gpu2": 0})
        self.assertEqual(list(summary["union"]["slots"]), ["cpu8", "mem64", "gpu1", "gpu2"])
        self.assertEqual(summary["union"]["gpus"], {"configured": 12, "allocated": 3, "free": 1})

    def test_overlap_a_node_in_two_requested_partitions_is_in_both_and_once_in_union(self):
        self.scontrol(
            node("cn01", "IDLE", "gpu,owners"),
            node("cn02", "IDLE", "gpu"),
            node("cn03", "IDLE", "owners"),
            node("cn04", "IDLE", "bioe,owners"),
            node("cn05", "IDLE", "normal"),
        )
        summary = self.summary("owners", "gpu", "gpu")["summary"]
        self.assertEqual(list(summary["partitions"]), ["owners", "gpu"])
        self.assertEqual(
            [summary["partitions"][name]["nodes"] for name in ("owners", "gpu")], [3, 2]
        )
        self.assertEqual(summary["union"]["nodes"], 4)
        self.assertEqual(summary["union"]["cpus"]["configured"], 80)
        self.assertEqual(summary["partitions"]["owners"]["cpus"]["configured"], 60)
        self.assertEqual(summary["partitions"]["gpu"]["cpus"]["configured"], 40)
        # bioe is not requested, so cn04 lists only one requested partition.
        self.assertEqual(summary["overlapping_nodes"], 1)

    def breakdown_read(self):
        self.scontrol(
            gpu_node(
                "cn01", "MIXED", "gpu", "GPU_SKU:A100_SXM4,CLASS:SH3_G4FP64,GPU_GEN:AMP", used=1
            ),
            gpu_node("cn02", "IDLE", "gpu", "CLASS:SH3_G4FP64,GPU_SKU:V100S_PCIE"),
            gpu_node(
                "cn03",
                "MIXED",
                "gpu",
                "GPU_SKU:V100_PCIE,GPU_SKU:A100_SXM4,CLASS:SH3_G8,CLASS:SH3_G4FP64",
                gpus=8,
                used=2,
            ),
            featured(node("cn04", "IDLE", "gpu"), "CPU_MNF:INTEL,CLASS:SH3_CBASE"),
            featured(node("cn05", "IDLE", "gpu"), "(null)"),
            gpu_node("cn06", "IDLE+DRAIN", "gpu", "GPU_SKU:A100_SXM4,CLASS:SH3_G4FP64"),
            gpu_node("cn07", "IDLE", "gpu", "GPU_SKU:A100_SXM4", cpus=NO_ALLOCATION),
        )

    def test_by_sku_puts_each_node_in_the_entry_for_its_exact_set_of_values(self):
        self.breakdown_read()
        shape = "g:cpus=1,mem=1G,gpus=1"
        union = self.summary("gpu", extra=("--by", "sku", "--shape", shape))["summary"]["union"]
        entries = union["by_sku"]
        self.assertEqual(
            entry_keys(entries, "sku"),
            [[], ["A100_SXM4"], ["A100_SXM4", "V100_PCIE"], ["V100S_PCIE"]],
        )
        self.assertEqual([entry["nodes"] for entry in entries], [2, 3, 1, 1])
        single, double = entries[1], entries[2]
        # cn01 countable, cn06 drained, cn07 unknown; cn03's eight GPUs are only in its entry.
        self.assertEqual([single[count] for count in COUNTS], [3, 2, 1, 1, 1])
        self.assertEqual(single["gpus"], {"configured": 8, "allocated": 1, "free": 3})
        self.assertEqual(single["slots"], {"g": 3})
        self.assertEqual(double["gpus"], {"configured": 8, "allocated": 2, "free": 6})
        self.assertEqual(entries[0]["gpus"], {"configured": 0, "allocated": 0, "free": 0})
        self.assertEqual(entries[3]["gpus"], {"configured": 4, "allocated": 0, "free": 4})
        self.assertEqual(union["gpus"], {"configured": 20, "allocated": 3, "free": 13})
        self.assertEqual(union["slots"], {"g": 13})
        self.assert_split(union, entries, "sku")

    def test_by_class_splits_the_same_nodes_by_their_class_features(self):
        self.breakdown_read()
        union = self.summary("gpu", extra=("--by", "class"))["summary"]["union"]
        self.assertNotIn("by_sku", union)
        entries = union["by_class"]
        self.assertEqual(
            entry_keys(entries, "class"),
            [[], ["SH3_CBASE"], ["SH3_G4FP64"], ["SH3_G4FP64", "SH3_G8"]],
        )
        self.assertEqual([entry["nodes"] for entry in entries], [2, 1, 3, 1])  # cn05, cn07
        # cn03's eight GPUs are only in its two-value entry, in no single-class entry.
        self.assertEqual(entries[2]["gpus"], {"configured": 12, "allocated": 1, "free": 7})
        self.assertEqual(entries[3]["gpus"], {"configured": 8, "allocated": 2, "free": 6})
        self.assert_split(union, entries, "class")

    def test_by_keeps_the_order_given_without_repeats_and_follows_the_group_fields(self):
        self.breakdown_read()
        summary = self.summary("gpu", extra=("--by", "class", "--by", "sku", "--by", "class"))
        summary = summary["summary"]
        self.assertEqual(summary["by"], ["class", "sku"])
        for group in (summary["union"], summary["partitions"]["gpu"]):
            self.assertEqual(list(group), [*GROUP_KEYS, "by_class", "by_sku"])
        summary = self.summary("gpu", extra=("--by", "sku"))["summary"]
        self.assertEqual((summary["by"], list(summary["union"])[-1]), (["sku"], "by_sku"))
        summary = self.summary("gpu")["summary"]
        self.assertEqual((summary["by"], list(summary["union"])), ([], GROUP_KEYS))

    @BOUNDED
    @given(reads())
    def test_property_breakdowns_split_their_group_and_union_counts_every_shipped_line(self, read):
        nodes, requested, by, shapes = read
        self.scontrol(*(line for _, line in nodes))
        extra = [word for kind in by for word in ("--by", kind)]
        extra += [word for shape in shapes for word in ("--shape", shape)]
        summary = self.summary(*requested, extra=extra)["summary"]
        shipped = [member for member, _ in nodes if set(member) & set(requested)]
        self.assertEqual(summary["union"]["nodes"], len(shipped))
        self.assertEqual(list(summary["partitions"]), list(dict.fromkeys(requested)))
        for name, group in summary["partitions"].items():
            self.assertEqual(group["nodes"], sum(name in member for member in shipped))
        for group in (summary["union"], *summary["partitions"].values()):
            self.assertEqual(group["nodes"], sum(group[count] for count in COUNTS[2:]))
            for kind in dict.fromkeys(by):
                self.assert_split(group, group[f"by_{kind}"], kind)
