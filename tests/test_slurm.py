"""Row parsing and per-name resolution: pure functions over squeue and sacct text."""
import sys
import unittest

from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import slurm  # noqa: E402
from sherlock_kit.errors import ShkError  # noqa: E402

N = "shk.demo.train-a.0123456789"
READ = "\n".join([
    "banner from a login shell",
    "SHK_USER zyli",
    "SHK_HOST sh02-ln04.stanford.edu",
    "SHK_NOW 1",
    "SHK_Q|1|PENDING|None|normal|n",
    "SHK_A|x",
    "SHK_S|n|2",
    'SHK_SPEC|n|7|{"resources":{"array":{"count":4,"throttle":null}}}',
    "SHK_SPEC|m|8|{truncated",
    'SHK_SPEC|s|9|{"resources":{"array":null}}',
    "SHK_O|owners|row",
    "SHK_RC squeue 0",
    "SHK_RC sacct 0",
    "SHK_END",
]) + "\n"


def q(job, state="PENDING", reason="None", partition="normal", name=N):
    return f"{job}|{state}|{reason}|{partition}|{name}"


def a(job, state="COMPLETED", restart=0, elapsed=60, cpus=2, tres="cpu=2,mem=4G,node=1",
      exit="0:0", partition="normal", name=N):
    return (f"{job}|{state}|{partition}|{elapsed}|{cpus}|{tres}|2026-10-10T12:00:00|{restart}"
            f"|{exit}|{name}")


def resolve(queue=(), acct=(), spec_files=0, spec=None):
    rows = slurm.parse_rows(list(queue), list(acct), lambda name: True)
    newest = slurm.SpecFile(*spec) if spec else None
    return slurm.resolve_name(rows.by_name.get(N, []), spec_files, newest, rows.malformed[N])


def current(*args, **kwargs):
    return resolve(*args, **kwargs)["current"]


class ParsingTests(unittest.TestCase):
    def test_job_id_grammar(self):
        self.assertEqual(slurm.job_id("123"), (123, None, None))
        self.assertEqual(slurm.job_id("123_7"), (123, 7, None))
        self.assertEqual(slurm.job_id("123_[2-3%2]"), (123, None, [2, 3]))
        self.assertEqual(slurm.job_id("123_[0,2-4,9,2%2]"), (123, None, [0, 2, 3, 4, 9]))
        for text in ("0", "", "123.batch", "123_2.0", "123_", "123_[]", "123_[1-]", "123_[a]",
                     "x123", "123_[2-1]", "123_[1,,2]", " 123", "123+0", "123_[0-1000]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                slurm.job_id(text)

    def test_gpus_from_tres(self):
        cases = {"": 0, "N/A": 0, "cpu=4,mem=32G,node=1": 0, "cpu=4,gres/gpu=2": 2,
                 "gres/gpu:h100=1": 1, "gres/gpu:a100=1,gres/gpu:h100=2": 3,
                 "cpu=4,gres/gpu=2,gres/gpu:h100=2": 2,
                 "gres/gpu=3,gres/gpu:h100=1": 3}  # untyped GPUs beside typed ones: the total wins
        self.assertEqual({tres: slurm.gpus(tres) for tres in cases}, cases)
        for bad in ("gres/gpu=x", "gres/gpu:h100=", "gres/gpu=-1", "gres/gpu"):
            with self.subTest(tres=bad), self.assertRaises(ValueError):
                slurm.gpus(bad)

    def test_read_is_complete_only_with_end_and_every_tool(self):
        read = slurm.parse_read(READ, ("squeue", "sacct"))
        self.assertEqual((read.user, read.host), ("zyli", "sh02-ln04.stanford.edu"))
        self.assertEqual((read.queue, read.acct), (["1|PENDING|None|normal|n"], ["x"]))
        self.assertEqual((read.occupancy, read.spec_files), ({"owners": ["row"]}, {"n": 2}))
        self.assertEqual(read.specs, {"n": slurm.SpecFile(7, 4), "m": slurm.SpecFile(8, None),
                                      "s": slurm.SpecFile(9, 1)})
        broken = ((READ.replace("SHK_END\n", ""), ("squeue",)),
                  (READ.replace("sacct 0", "sacct 1"), ("sacct",)),
                  (READ, ("occupancy",)),
                  ("SHK_RC squeue 0\nSHK_RC sacct 0\n", ("squeue",)))
        for text, tools in broken:
            with self.subTest(tools=tools), self.assertRaises(ShkError) as caught:
                slurm.parse_read(text, tools)
            self.assertEqual((caught.exception.code, caught.exception.token), (1, "malformed_read"))

    def test_rows_attribution_filter_and_truncation(self):
        rows = slurm.parse_rows([q("5", name="odd|name"), "short"],
                                [a("x1"), a("1", name="other"), a("2")], lambda n: n != "other")
        self.assertEqual(sorted(rows.by_name), ["odd|name", N])
        self.assertEqual(([r.base for r in rows.by_name[N]], rows.by_name["odd|name"][0].state),
                         ([2], "PENDING"))
        self.assertEqual((rows.malformed[N], rows.malformed[None], rows.truncated), (1, 1, False))
        many = [a(str(i), name="shk.foo-bar.t.0123456789") for i in range(1, slurm.MAX_ROWS + 1)]
        rows = slurm.parse_rows([], [*many, a("9", name="shk.foo.t.1")],
                                lambda name: name.startswith("shk.foo."))
        self.assertEqual((rows.by_name, rows.truncated), ({}, True))

    def test_occupancy_fixed_width_rows(self):
        def row(user, state, tres):
            return f"{user:<64}{state:<24}{tres:<128}{'1:00':<24}{'2:00:00':<24}"
        users, totals = slurm.occupancy_summary([
            row("alice", "RUNNING", "cpu=4,gres/gpu=2,gres/gpu:h100=2"),
            row("alice", "PENDING", ""),
            row("bob", "RUNNING", "cpu=8,gres/gpu:h100=1"), row("bob", "COMPLETING", "gres/gpu=1"),
            row("carol", "SUSPENDED", "cpu=1,gres/gpu=4"), row("dave", "PENDING", "N/A")])
        self.assertEqual(users, {"alice": {"running_jobs": 1, "running_gpus": 2, "pending_jobs": 1},
                                 "bob": {"running_jobs": 2, "running_gpus": 2, "pending_jobs": 0},
                                 "dave": {"running_jobs": 0, "running_gpus": 0, "pending_jobs": 1}})
        self.assertEqual(totals, {"running_jobs": 3, "running_gpus": 4, "pending_jobs": 2})
        with self.assertRaises(ShkError) as caught:
            slurm.occupancy_summary([row("erin", "RUNNING", "gres/gpu=x")])
        self.assertEqual(caught.exception.token, "malformed_read")


class ResolveTests(unittest.TestCase):
    def test_new_stale_and_single_job_states(self):
        self.assertEqual(resolve(), {"state": "new", "current": None, "history": [],
                                     "spec_files": 0})
        self.assertEqual(resolve(spec_files=2)["state"], "stale")
        live = resolve([q("100", "PENDING", "Priority")])
        tasks = live["current"]["tasks"]
        self.assertEqual((live["state"], live["current"]["job_id"], tasks["pending"]),
                         ("live", "100", 1))
        self.assertIsNone(live["current"]["array"])
        done = current([q("100", "COMPLETED")], [a("100")])
        self.assertEqual((done["tasks"]["completed"], done["exit_codes"], done["anomalies"]),
                         (1, {"0:0": 1}, []))
        self.assertEqual((done["max_restart"], done["cost"]),
                         (0, {"cpu_seconds": 120, "gpu_seconds": 0, "final": True}))
        failed = resolve(acct=[a("100", "FAILED", exit="1:0")])
        self.assertEqual((failed["state"], failed["current"]["tasks"]["failed"]), ("failed", 1))
        self.assertEqual(failed["current"]["exit_codes"], {"1:0": 1})
        cancelled = resolve(acct=[a("100", "CANCELLED by 123", elapsed=0)])
        self.assertEqual(cancelled["state"], "failed")
        self.assertEqual(resolve(acct=[a("100", "RUNNING")])["state"], "live")
        squeue_wins = current([q("100", "RUNNING")], [a("100", "PENDING", elapsed=0)])
        self.assertEqual(squeue_wins["tasks"]["running"], 1)
        self.assertEqual(current(acct=[a("100"), a("100")])["cost"]["cpu_seconds"], 120)

    def test_newest_job_is_current_and_older_jobs_are_history(self):
        result = resolve(acct=[a("100", "FAILED", exit="1:0"), a("200")])
        self.assertEqual((result["state"], result["current"]["job_id"]), ("done", "200"))
        history = [{"job_id": "100", "state": "failed", "submitted": "2026-10-10T12:00:00Z"}]
        self.assertEqual(result["history"], history)
        older_live = resolve([q("100", "RUNNING")], [a("100", "RUNNING"), a("200", "FAILED")])
        self.assertEqual((older_live["state"], older_live["current"]["job_id"]), ("live", "200"))
        self.assertEqual(older_live["current"]["anomalies"], [])
        both = resolve([q("100", "RUNNING"), q("200", "PENDING")])
        self.assertEqual((both["state"], both["current"]["anomalies"]),
                         ("live", ["duplicate_live"]))

    def test_arrays_throttle_aggregates_and_counts(self):
        live = current([q("300_1", "RUNNING"), q("300_[2-3%2]", "PENDING", "JobArrayTaskLimit")],
                       [a("300_0"), a("300_1", "RUNNING")], spec=(300, 4))
        tasks = live["tasks"]
        keys = ("count", "completed", "failed", "running", "pending", "missing")
        self.assertEqual([tasks[key] for key in keys], [4, 1, 0, 1, 2, 0])
        self.assertEqual(live["array"], {"count": 4, "count_known": True})
        self.assertEqual(tasks["by_state"], {"COMPLETED": 1, "PENDING": 2, "RUNNING": 1})
        self.assertFalse(live["cost"]["final"])
        inferred = resolve(acct=[a(f"300_{i}") for i in range(3)])
        self.assertEqual((inferred["state"], inferred["current"]["array"]),
                         ("done", {"count": 3, "count_known": False}))
        short = resolve(acct=[a(f"300_{i}") for i in range(3)], spec=(300, 4))
        self.assertEqual((short["state"], short["current"]["tasks"]["missing"]), ("failed", 1))
        other_spec = current(acct=[a("300_0"), a("300_1")], spec=(299, 4))
        self.assertEqual(other_spec["array"], {"count": 2, "count_known": False})
        cancelled = a("300_[0-1]", "CANCELLED", elapsed=0, cpus=0, tres="")
        explicit = current(acct=[cancelled, a("300_0")])
        self.assertEqual((explicit["tasks"]["by_state"], explicit["cost"]["cpu_seconds"]),
                         ({"CANCELLED": 1, "COMPLETED": 1}, 120))

    def test_restarts_cost_and_anomalies(self):
        owners = resolve(acct=[a("500", "PREEMPTED", elapsed=30, partition="owners"),
                               a("500", restart=1, partition="owners")])
        self.assertEqual((owners["state"], owners["current"]["max_restart"]), ("done", 1))
        self.assertEqual((owners["current"]["cost"]["cpu_seconds"], owners["current"]["anomalies"]),
                         (180, []))
        normal = resolve([q("500", "RUNNING")],
                         [a("500", "PREEMPTED", elapsed=30), a("500", "RUNNING", restart=1)])
        self.assertEqual((normal["state"], normal["current"]["anomalies"]),
                         ("live", ["unexpected_preemption"]))
        gpu = current(acct=[a("500", "PREEMPTED", partition="gpu", tres="cpu=2,gres/gpu=1",
                              exit="0:15")])
        self.assertEqual((gpu["anomalies"], gpu["cost"]["gpu_seconds"], gpu["tasks"]["failed"]),
                         (["unexpected_preemption"], 60, 1))
        gap = current(acct=[a("500", restart=2, partition="owners")])["tasks"]
        self.assertEqual(gap["incomplete_history"], [0])
        self.assertEqual(current(acct=[a("500", partition="mystery")])["anomalies"],
                         ["unknown_partition"])
        self.assertEqual(current(acct=[a("500"), a("500", elapsed="x")])["anomalies"],
                         ["malformed_rows"])


if __name__ == "__main__":
    unittest.main()
