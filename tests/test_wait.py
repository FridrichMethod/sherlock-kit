"""shk wait: the real query.sh against the fake Slurm, with a fake clock and a fake sleep.

The fake sleep records each delay wait asks for, advances the clock by it and then applies the
next scripted change to the fake Slurm state (or to the environment), so the next read sees what
a real wait would see one delay later. Every run also checks what each wait guarantees: only
squeue and sacct run (one of each per read, nothing reaches the tripwire, so no sbatch, scancel
or scontrol), nothing is printed before the end and nothing on stderr, no file is written, and
consecutive reads are at least 60 s apart.
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta

from fakes import SRC, install_fake_ssh, tripwire_hits
from test_flows import NOW, Flow
from test_marlowe_submit import NAME as MARLOWE_NAME
from test_marlowe_submit import OnMarlowe

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, slurm, spec, wait


def iso(seconds):
    """The reply time format, seconds after NOW."""
    return (NOW + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class WaitFlow(Flow):
    site = None
    modules = (slurm, wait)

    def setUp(self):
        super().setUp()
        self.now = NOW
        self.sleeps = []
        self.changes = []

    def name(self, task="a"):
        return spec.job_name("demo", "default", task)

    def sleep(self, seconds):
        self.assertEqual((self.out.getvalue(), self.err.getvalue()), ("", ""))
        self.assertLess(len(self.sleeps), 50, "the wait does not end")  # fail instead of hanging
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        change = self.changes.pop(0) if self.changes else None
        if change is not None:
            change()

    def context(self):
        return cli.Context(
            project_dir=self.tmp,
            executor=self.executor,
            site=self.site,
            env={},
            hostname="lab-ubuntu",
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    def shk(self, *argv):
        self.out, self.err = io.StringIO(), io.StringIO()
        with redirect_stdout(self.out), redirect_stderr(self.err):
            code = cli.main(list(argv), modules=list(self.modules), context=self.context())
        return code, json.loads(self.out.getvalue())

    def files(self):
        """Every path under the test directory, with the bytes of each file (no Slurm or ssh log)."""
        logs = {self.slurm.path, self.tmp / "ssh-calls.jsonl"}
        return {
            path: path.read_bytes() if path.is_file() else None
            for path in self.tmp.rglob("*")
            if path not in logs
        }

    def wait(self, *argv, changes=()):
        """shk wait with one change applied per sleep, checked against what every wait keeps."""
        self.sleeps, self.changes = [], list(changes)
        before, hits, calls = self.files(), len(tripwire_hits()), len(self.slurm.calls())
        code, reply = self.shk("wait", *argv)
        self.assertEqual(self.err.getvalue(), "")
        self.assertEqual(self.changes, [], "every scripted change must happen")
        self.assertEqual(self.files(), before)
        self.assertEqual(len(tripwire_hits()), hits)
        tools = [call["tool"] for call in self.slurm.calls()[calls:]]
        self.assertLessEqual(set(tools), {"squeue", "sacct"})
        self.assertTrue(all(seconds >= 60 for seconds in self.sleeps), self.sleeps)
        if "wait" in reply:
            reads = reply["wait"]["reads"]
            self.assertEqual(tools, ["squeue", "sacct"] * reads)
            self.assertEqual(len(self.sleeps), reads - 1)
            self.assertEqual(list(reply)[-1], "wait")
        return code, reply

    def assert_outcome(self, reply, outcome, names=(), reads=1):
        found = reply["wait"]
        self.assertEqual(
            (found["outcome"], found["names"], found["reads"]), (outcome, list(names), reads), reply
        )

    def assert_status_reply(self, code, reply, *selection):
        """Without "wait", the reply is what shk status prints for the selection at that moment."""
        status = self.shk("status", *selection)
        self.assertEqual((code, list(reply.items())[:-1]), (status[0], list(status[1].items())))


class WaitOutcomes(WaitFlow):
    def test_a_selection_without_a_live_name_settles_at_the_first_read(self):
        self.slurm.add(self.name("done"), "COMPLETED")
        self.slurm.add(self.name("old"), "FAILED")
        selection = ("--task", "done", "--task", "old", "--task", "never")
        code, reply = self.wait(*selection, "--timeout", "1h")
        self.assertEqual(code, 0)
        self.assertEqual(
            reply["wait"],
            {
                "outcome": "settled",
                "names": [],
                "reads": 1,
                "started_at": iso(0),
                "timeout_at": iso(3600),
            },
        )
        self.assertEqual([entry["state"] for entry in reply["names"]], ["done", "failed", "new"])
        self.assertEqual(self.sleeps, [])
        self.assert_status_reply(code, reply, *selection)

    def test_it_settles_once_no_name_is_live(self):
        job = self.slurm.add(self.name(), "PENDING")
        changes = [lambda: self.slurm.set(job, "RUNNING"), lambda: self.slurm.set(job, "COMPLETED")]
        code, reply = self.wait("--task", "a", "--timeout", "2h", changes=changes)
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "settled", reads=3)
        self.assertEqual(self.sleeps, [300, 300])
        self.assertEqual(
            (reply["names"][0]["state"], reply["queried_at"], reply["executor"]),
            ("done", iso(600), "local"),
        )
        self.assertEqual(
            (reply["wait"]["started_at"], reply["wait"]["timeout_at"]), (iso(0), iso(7200))
        )
        self.assert_status_reply(code, reply, "--task", "a")

    def test_a_failure_after_live_ends_the_wait_while_another_name_is_live(self):
        first = self.slurm.add(self.name("a"), "RUNNING")
        self.slurm.add(self.name("b"), "RUNNING")
        selection = ("--task", "a", "--task", "b")
        changes = [lambda: self.slurm.set(first, "FAILED")]
        code, reply = self.wait(*selection, "--timeout", "2h", changes=changes)
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "failed", [self.name("a")], reads=2)
        self.assertEqual([entry["state"] for entry in reply["names"]], ["failed", "live"])
        self.assert_status_reply(code, reply, *selection)

    def test_with_all_an_older_failure_never_counts_and_a_watched_one_does(self):
        self.slurm.add(self.name("old"), "FAILED")
        watched = self.slurm.add(self.name("new"), "RUNNING")
        changes = [lambda: self.slurm.set(watched, "COMPLETED")]
        code, reply = self.wait("--all", "--timeout", "2h", changes=changes)
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "settled", reads=2)
        states = {entry["name"]: entry["state"] for entry in reply["names"]}
        self.assertEqual(states, {self.name("old"): "failed", self.name("new"): "done"})
        later = self.slurm.add(self.name("later"), "RUNNING")
        changes = [lambda: self.slurm.set(later, "FAILED")]
        code, reply = self.wait("--all", "--timeout", "2h", changes=changes)
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "failed", [self.name("later")], reads=2)
        self.assert_status_reply(code, reply, "--all")

    def test_an_anomaly_ends_the_wait_with_exit_6(self):
        first = self.slurm.add(self.name("a"), "RUNNING")
        self.slurm.add(self.name("b"), "RUNNING")
        selection = ("--task", "a", "--task", "b")
        changes = [lambda: self.slurm.requeue(first)]  # preempted on normal, which never preempts
        code, reply = self.wait(*selection, "--timeout", "2h", changes=changes)
        self.assertEqual((code, reply["ok"]), (6, True))
        self.assert_outcome(reply, "anomaly", [self.name("a")], reads=2)
        anomalies = [entry["current"]["anomalies"] for entry in reply["names"]]
        self.assertEqual(anomalies, [["unexpected_preemption"], []])
        self.assert_status_reply(code, reply, *selection)

    def test_max_pending_ends_a_wait_on_a_name_that_never_starts(self):
        self.slurm.add(self.name(), "PENDING")
        code, reply = self.wait(
            "--task", "a", "--timeout", "4h", "--max-pending", "20m", changes=[None, None, None]
        )
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "max_pending", [self.name()], reads=4)
        self.assertEqual(self.sleeps, [300, 600, 1200])  # 2100 s idle, the first read at least 20m

    def test_a_running_task_resets_max_pending(self):
        job = self.slurm.add(self.name(), "PENDING", partition="owners")  # requeue is expected
        changes = [lambda: self.slurm.set(job, "RUNNING"), lambda: self.slurm.requeue(job)]
        code, reply = self.wait(
            "--task",
            "a",
            "--timeout",
            "4h",
            "--max-pending",
            "10m",
            changes=[*changes, None, None],
        )
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "max_pending", [self.name()], reads=5)
        # Without the reset the read at 600 s would end it: idle since 0. Idle since 600 instead.
        self.assertEqual(self.sleeps, [300, 300, 300, 600])
        self.assertEqual(reply["queried_at"], iso(1500))
        self.assertEqual(reply["names"][0]["current"]["anomalies"], [])

    def test_timeout_reads_once_more_at_the_deadline(self):
        self.slurm.add(self.name(), "PENDING")
        code, reply = self.wait("--task", "a", "--timeout", "12m", changes=[None, None])
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "timeout", reads=3)
        self.assertEqual(self.sleeps, [300, 420])
        self.assertEqual((reply["queried_at"], reply["wait"]["timeout_at"]), (iso(720), iso(720)))
        self.assertEqual(reply["names"][0]["state"], "live")

    def test_less_than_60_s_before_the_deadline_is_a_timeout_without_another_read(self):
        self.slurm.add(self.name(), "PENDING")
        code, reply = self.wait("--task", "a", "--timeout", "0:05:30", changes=[None])
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "timeout", reads=2)
        self.assertEqual(self.sleeps, [300])
        self.assertEqual((reply["queried_at"], reply["wait"]["timeout_at"]), (iso(300), iso(330)))


class WaitCadence(WaitFlow):
    def test_reads_are_at_least_60_s_apart(self):
        self.slurm.add(self.name(), "PENDING")
        for timeout, sleeps in (("1m", [60]), ("0:02:30", [60, 90]), ("0:02:59", [60, 119])):
            with self.subTest(timeout=timeout):
                code, reply = self.wait(
                    "--task",
                    "a",
                    "--timeout",
                    timeout,
                    "--interval",
                    "1m",
                    changes=[None] * len(sleeps),
                )
                self.assertEqual(code, 0)
                self.assert_outcome(reply, "timeout", reads=len(sleeps) + 1)
                self.assertEqual(self.sleeps, sleeps)

    def test_the_delay_doubles_to_30_min_and_a_running_task_resets_it(self):
        job = self.slurm.add(self.name(), "PENDING")
        changes = [None] * 4 + [
            lambda: self.slurm.set(job, "RUNNING"),
            None,  # still running, nothing changed: no backoff while a task runs
            lambda: self.slurm.set(job, "COMPLETED"),
        ]
        code, reply = self.wait("--task", "a", "--timeout", "4h", changes=changes)
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "settled", reads=8)
        self.assertEqual(self.sleeps, [300, 600, 1200, 1800, 1800, 300, 300])

    def test_a_change_without_a_running_task_resets_the_delay(self):
        job = self.slurm.add(self.name(), "PENDING", "PENDING")
        changes = [
            None,
            lambda: self.slurm.set(job, "COMPLETED", None),
            None,
            lambda: self.slurm.set(job, None, "COMPLETED"),
        ]
        code, reply = self.wait("--task", "a", "--timeout", "4h", changes=changes)
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "settled", reads=5)
        self.assertEqual(self.sleeps, [300, 600, 300, 600])

    def test_an_interval_above_30_min_is_the_delay_and_its_cap(self):
        self.slurm.add(self.name(), "PENDING")
        code, reply = self.wait(
            "--task", "a", "--timeout", "4h", "--interval", "45m", changes=[None] * 6
        )
        self.assertEqual(code, 0)
        self.assert_outcome(reply, "timeout", reads=7)
        self.assertEqual(self.sleeps, [2700] * 5 + [900])


class WaitArguments(WaitFlow):
    def test_durations_are_checked_before_any_read(self):
        cases = (
            ("--timeout", "0:00:59"),
            ("--timeout", "0m"),
            ("--timeout", "169h"),
            ("--timeout", "168:00:01"),
            ("--timeout", "2d"),
            ("--timeout", "90"),
            ("--timeout", "1h30m"),
            ("--timeout", ""),
            ("--timeout", "99999999999999h"),
            ("--timeout", "99999999999:00:00"),
            ("--interval", "0:00:59"),
            ("--interval", "30s"),
            ("--max-pending", "0:00:30"),
            ("--max-pending", "1.5h"),
            ("--max-pending", ""),
        )
        for option, value in cases:
            with self.subTest(option=option, value=value):
                given = {"--timeout": "1h", option: value}
                code, reply = self.wait(
                    "--task", "a", *(word for pair in given.items() for word in pair)
                )
                self.assertEqual((code, reply["error"]), (1, "spec_invalid"))
                self.assertTrue(
                    reply["message"].startswith(f"{option} {value!r}: use 2h, 90m or H:MM:SS")
                )
        self.assertEqual(self.slurm.calls(), [])

    def test_timeout_is_required(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main(["wait", "--task", "a"], modules=list(self.modules), context=self.context())
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.slurm.calls(), [])

    def test_bounds_are_inclusive_and_the_interval_has_no_upper_bound(self):
        self.slurm.add(self.name(), "COMPLETED")
        cases = (
            (("--timeout", "1m"), 60),
            (("--timeout", "168h"), 168 * 3600),
            (("--timeout", "168:00:00", "--interval", "1m", "--max-pending", "1m"), 168 * 3600),
            (("--timeout", "1h", "--interval", "1000h", "--max-pending", "1000h"), 3600),
            (
                ("--timeout", "1h", "--interval", "9" * 20 + "h", "--max-pending", "9" * 20 + "m"),
                3600,
            ),
        )
        for argv, seconds in cases:
            with self.subTest(argv=argv):
                code, reply = self.wait("--task", "a", *argv)
                self.assertEqual(code, 0)
                self.assert_outcome(reply, "settled")
                started = reply["wait"]["started_at"]
                self.assertEqual(reply["wait"]["timeout_at"], iso(seconds))
                self.assertEqual(started, iso(0))

    def test_the_selection_is_checked_as_status_checks_it(self):
        cases = (
            ("--name", "not-a-shk-name"),
            ("--task", "a", "--since", "0"),
            ("--all", "--since", "32"),
            ("--task", "a", "--campaign", "x", "--campaign", "y", "--task", "b", "--campaign", "z"),
            ("--task", ""),
        )
        for selection in cases:
            with self.subTest(selection=selection):
                code, reply = self.wait(*selection, "--timeout", "1h")
                self.assertEqual((code, reply), self.shk("status", *selection))
                self.assertEqual((code, reply["error"]), (1, "spec_invalid"))
        self.assertEqual(self.slurm.calls(), [])
        status, waits = self.options("status"), self.options("wait")
        self.assertEqual(waits - status, {"--timeout", "--interval", "--max-pending"})
        self.assertEqual(status - waits, {"--job-id", "--details"})

    def options(self, command):
        parser = cli.build_parser(list(self.modules))
        commands = next(action for action in parser._actions if action.dest == "command")
        return {
            option
            for action in commands.choices[command]._actions
            for option in action.option_strings
        }


class WaitErrors(WaitFlow):
    def test_a_malformed_read_ends_the_wait_with_its_error(self):
        broken = self.tmp / "broken"
        broken.mkdir()
        (broken / "squeue").write_text("#!/bin/sh\necho squeue: error: unreachable >&2\nexit 1\n")
        (broken / "squeue").chmod(0o755)
        self.slurm.add(self.name(), "RUNNING")

        def fail_squeue():
            os.environ["PATH"] = f"{broken}{os.pathsep}{os.environ['PATH']}"

        code, reply = self.wait("--task", "a", "--timeout", "1h", changes=[fail_squeue])
        self.assertEqual((code, reply["ok"], reply["error"]), (1, False, "malformed_read"))
        self.assertNotIn("wait", reply)
        self.assertEqual(self.sleeps, [300])
        self.assertEqual(
            [call["tool"] for call in self.slurm.calls()], ["squeue", "sacct", "sacct"]
        )


class SshWait(WaitFlow):
    executor = "ssh"

    def setUp(self):
        super().setUp()
        self.fake = install_fake_ssh(self.tmp)
        self.knobs(**self.fake.env(FAKE_SSH_BANNER="none"))

    def dials(self):
        return [call[-2] for call in self.fake.calls() if "-O" not in call]

    def test_a_wait_through_the_fake_ssh(self):
        job = self.slurm.add(self.name(), "RUNNING")
        changes = [lambda: self.slurm.set(job, "COMPLETED")]
        code, reply = self.wait("--task", "a", "--timeout", "1h", changes=changes)
        self.assertEqual((code, reply["executor"]), (0, "ssh"))
        self.assert_outcome(reply, "settled", reads=2)
        self.assertEqual(self.dials(), ["sherlock-plain"] * 2)

    def test_an_unreachable_read_ends_the_wait_with_exit_4(self):
        self.slurm.add(self.name(), "RUNNING")
        changes = [lambda: os.environ.update(FAKE_SSH_MODE="disconnect")]
        code, reply = self.wait("--task", "a", "--timeout", "1h", changes=changes)
        self.assertEqual((code, reply["error"]), (4, "unavailable"))
        self.assertNotIn("wait", reply)
        self.assertEqual(self.sleeps, [300])
        self.assertEqual(self.dials(), ["sherlock-plain"] * 2)


class MarloweWait(OnMarlowe, WaitFlow):
    """One Marlowe wait from a workstation: query.sh behind the site environment, over ssh."""

    executor = "ssh"
    site = "marlowe"
    modules = (slurm, wait)
    shk = WaitFlow.shk

    def setUp(self):
        super().setUp()
        self.fake = install_fake_ssh(self.tmp)
        self.knobs(**self.fake.env(FAKE_SSH_BANNER="none"))

    def test_a_marlowe_wait_settles_through_marlowe_plain(self):
        job = self.slurm.add(MARLOWE_NAME, "PENDING", partition="preempt")
        changes = [lambda: self.slurm.set(job, "RUNNING"), lambda: self.slurm.requeue(job)]
        changes += [lambda: self.slurm.set(job, "COMPLETED")]
        code, reply = self.wait("--task", "train-a", "--timeout", "1h", changes=changes)
        self.assertEqual((code, reply["executor"], reply["site"]), (0, "ssh", "marlowe"))
        self.assertEqual(list(reply)[list(reply).index("executor") + 1], "site")
        self.assert_outcome(reply, "settled", reads=4)
        self.assertEqual(reply["names"][0]["current"]["anomalies"], [])  # preempt requeues
        dials = [call[-2] for call in self.fake.calls() if "-O" not in call]
        self.assertEqual(dials, ["marlowe-plain"] * 4)
        self.assert_status_reply(code, reply, "--task", "train-a")


if __name__ == "__main__":
    unittest.main()
