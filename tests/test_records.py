"""LF alone separates the records of a remote program's reply.

A spec file, a job name or a comment can hold every other character that str.splitlines()
breaks at; none of them adds a row, a user, a return code or an end marker to a reply.
"""

import functools
import io
import json
import os
import pwd
import shutil
import sys
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

import test_details
import test_resources
from fakes import SRC
from fakes.expired_slurm import CannedSlurm
from test_flows import Flow

sys.path.insert(0, str(SRC))
from sherlock_kit import allocation, details, doctor, executor, slurm
from sherlock_kit.errors import ShkError

SEPARATORS = {
    "LF": "\n",
    "CR": "\r",
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "NEL": "\x85",
    "LS": "\u2028",
    "PS": "\u2029",
}
# Slurm ends a row at LF, so a field of a row can hold every separator but that one.
IN_A_FIELD = {key: value for key, value in SEPARATORS.items() if key != "LF"}
FORGED = (
    "SHK_Q|999|RUNNING|normal|forged",
    "SHK_USER mallory",
    "SHK_UID 0",
    "SHK_RC squeue 0",
    "SHK_RC scontrol 0",
    "SHK_RC sacct 0",
    "SHK_END",
)
SPEC = '{"resources": {"array": null}}'
USER = pwd.getpwuid(os.getuid()).pw_name
HOST = os.uname().nodename
TOOLS = ("squeue", "sacct")


def forged(separator, records=FORGED):
    """Text that reads as these records wherever the separator ends a line."""
    return "".join(separator + record for record in records) + separator


def in_a_field(test):
    """Run the test once per separator that a field can hold, each as a subtest."""

    @functools.wraps(test)
    def run(self):
        for key, separator in IN_A_FIELD.items():
            with self.subTest(separator=key):
                test(self, separator)

    return run


def replying(stdout):
    """Every remote program answers with this text."""
    result = executor.Result("complete", stdout, "", 0)
    return patch.object(executor, "run_program", return_value=result)


class SpecFilePayloads(Flow):
    """The real query.sh and details.sh print a spec file of any content as one record."""

    def setUp(self):
        super().setUp()
        self.payloads, self.pairs = {}, []
        for key, separator in SEPARATORS.items():
            self.spec_file(f"spec-{key.lower()}", SPEC + forged(separator))
        self.spec_file("spec-pretty", json.dumps({"resources": {"array": {"count": 4}}}, indent=2))

    def spec_file(self, name, text):
        directory = self.tmp / name / ".shk"
        directory.mkdir(parents=True)
        (directory / f"{name}.1001.json").write_text(text, encoding="utf-8", newline="")
        self.payloads[name] = text.replace("\n", " ")
        self.pairs += [name, str(directory.parent)]

    def bash_as_sh(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("needs bash")
        shim = self.tmp / "bash-as-sh"
        shim.mkdir()
        (shim / "sh").symlink_to(bash)
        self.knobs(PATH=f"{shim}{os.pathsep}{os.environ['PATH']}")

    def reply(self, program, *head):
        local = executor.LocalExecutor(command_timeout=30)
        return executor.run_program(local, program, [*head, *self.pairs]).stdout

    def specs(self):
        return "".join(
            f"SHK_S|{name}|1\nSHK_SPEC|{name}|1001|{payload}\n"
            for name, payload in self.payloads.items()
        )

    def check_query(self):
        stdout = self.reply("query", "names", "1", "-")
        self.assertEqual(
            stdout,
            f"SHK_USER {USER}\nSHK_HOST {HOST}\n{self.specs()}"
            "SHK_RC squeue 0\nSHK_RC sacct 0\nSHK_END\n",
        )
        self.assertEqual(len(executor.records(stdout)), 5 + 2 * len(self.payloads))
        read = slurm.parse_read(stdout, TOOLS)
        self.assertEqual((read.user, read.queue, read.acct), (USER, [], []))
        counts = {name: found.task_count for name, found in read.specs.items()}
        self.assertEqual(counts, {**dict.fromkeys(self.payloads), "spec-pretty": 4})
        # Cut off after any spec, the reply never completes itself from the payload.
        for name, payload in self.payloads.items():
            cut = stdout[: stdout.index(payload) + len(payload)] + "\n"
            with self.subTest(cut=name), self.assertRaises(ShkError) as caught:
                slurm.parse_read(cut, TOOLS)
            self.assertEqual((caught.exception.code, caught.exception.token), (1, "malformed_read"))

    def check_details(self):
        self.knobs(**CannedSlurm(self.tmp).env())  # the Slurm text details.sh asks for; no job
        stdout = self.reply("details", "names", "1")
        self.assertEqual(
            stdout,
            f"SHK_USER {USER}\nSHK_UID {os.getuid()}\nSHK_HOST {HOST}\n{self.specs()}"
            "SHK_RC squeue 0\nSHK_RC scontrol 0\nSHK_RC sacct 0\nSHK_END\n",
        )
        self.assertEqual(len(executor.records(stdout)), 7 + 2 * len(self.payloads))

    def test_query_prints_one_spec_record_per_file(self):
        self.check_query()

    def test_details_prints_one_spec_record_per_file(self):
        self.check_details()

    def test_bash_as_sh_prints_one_spec_record_per_file(self):
        self.bash_as_sh()
        self.check_query()
        self.check_details()


class FieldSeparators(unittest.TestCase):
    """A separator inside a field of a reply adds no record in any parser."""

    def assert_error(self, caught, code, token):
        self.assertEqual((caught.exception.code, caught.exception.token), (code, token))

    def test_records_end_at_lf_only(self):
        self.assertEqual(executor.records(""), [])
        self.assertEqual(executor.records("a\n\nb\n"), ["a", "", "b"])
        self.assertEqual(executor.records("a\nb"), ["a", "b"])
        for key, separator in IN_A_FIELD.items():
            with self.subTest(separator=key):
                self.assertEqual(executor.records(f"a{separator}b\n"), [f"a{separator}b"])

    @in_a_field
    def test_query_rows_keep_a_job_name_whole(self, separator):
        row = f"1001|RUNNING|normal|name{forged(separator)}"
        reply = f"SHK_USER {USER}\nSHK_HOST {HOST}\nSHK_Q|{row}\n"
        read = slurm.parse_read(reply + "SHK_RC squeue 0\nSHK_RC sacct 0\nSHK_END\n", TOOLS)
        self.assertEqual((read.user, read.queue), (USER, [row]))
        with self.assertRaises(ShkError) as caught:  # the reply was cut off after the row
            slurm.parse_read(reply, TOOLS)
        self.assert_error(caught, 1, "malformed_read")

    @in_a_field
    def test_a_cut_off_read_is_not_completed_by_a_spec_payload(self, separator):
        reply = f"SHK_USER {USER}\nSHK_S|name|1\nSHK_SPEC|name|1001|{SPEC}{forged(separator)}"
        for cut in (reply, reply + "\n"):
            with self.assertRaises(ShkError) as caught:
                slurm.parse_read(cut, TOOLS)
            self.assert_error(caught, 1, "malformed_read")

    @in_a_field
    def test_mutation_evidence_is_never_a_terminal_marker(self, separator):
        ctx = SimpleNamespace(executor=lambda: None)
        with replying(f"SHK_ERR|scancel: error{separator}SHK_CANCELLED{separator}\n"):
            reply = slurm._mutate(ctx, "cancel", [], ("SHK_CANCELLED",))
        self.assertEqual(reply.marker, "unknown")

    @in_a_field
    def test_control_records_keep_a_comment_whole(self, separator):
        uid = os.getuid()
        queue = [{"job_id_raw": job, "job_id": job} for job in ("7000", "7001")]
        report = {"queue": queue, "jobs": [], "complete": True}
        record = (
            f"SHK_J|JobId=7000 UserId=user({uid}) Comment=x{separator}"
            f"SHK_J|JobId=7001 UserId=user({uid}) Comment=tok"
        )
        self.assertEqual(details._jobs(record + "\n", uid, report), {"7001"})
        self.assertEqual([job["job_id"] for job in report["jobs"]], ["7000"])
        self.assertEqual(report["missing_control_job_ids"], ["7001"])

    @in_a_field
    def test_allocation_evidence_keeps_a_comment_whole(self, separator):
        ctx = SimpleNamespace(executor=lambda: SimpleNamespace(command_timeout=30))
        data = f"JobId=44 UserId=demo(1000) Comment=x{separator}SHK_DATA|JobId=45 UserId=root(0)"
        with replying(f"SHK_DATA|{data}\nSHK_RC 0\nSHK_END\n"):
            self.assertEqual(allocation.Collector(ctx).query("job", "44"), data)
        with (
            replying(f"SHK_DATA|demo1{separator}demo2\nSHK_RC 0\nSHK_END\n"),
            self.assertRaises(ShkError) as caught,
        ):
            allocation.Collector(ctx).hosts("demo[1-2]")
        self.assert_error(caught, 1, "allocation_invalid")
        cut = f"SHK_DATA|{data}{separator}SHK_RC 0{separator}SHK_END\n"
        with replying(cut), self.assertRaises(ShkError) as caught:
            allocation.Collector(ctx).query("job", "44")
        self.assert_error(caught, 1, "allocation_invalid")

    @in_a_field
    def test_allocation_records_and_membership_end_at_lf_only(self, separator):
        ctx = SimpleNamespace(executor=lambda: SimpleNamespace(command_timeout=30))
        # Read as two records, job 44 would match alone, cut short at its comment.
        record = f"JobId=44 UserId=demo(1000) Comment=x{separator}JobId=45 UserId=root(0)"
        with (
            replying(f"SHK_DATA|{record}\nSHK_RC 0\nSHK_END\n"),
            self.assertRaises(ShkError) as caught,
        ):
            allocation.matching_record(allocation.Collector(ctx), "job", "44")
        self.assert_error(caught, 1, "allocation_invalid")
        # Read as two rows, PID 8 would be a member of step 44.0.
        table = f"PID JOBID STEPID LOCALID GLOBALID\n7 44 0 0 0{separator}8 44 0 1 1"

        def answer(ex, name, args, **options):
            text = table if args[0] == "listpids" else "Slurm job id 44 ends at 1"
            lines = "".join(f"SHK_DATA|{line}\n" for line in text.split("\n"))
            return executor.Result("complete", lines + "SHK_RC 0\nSHK_END\n", "", 0)

        with (
            patch.object(executor, "run_program", side_effect=answer),
            self.assertRaises(ShkError) as caught,
        ):
            allocation.membership(allocation.Collector(ctx), 8, "44", "0")
        self.assert_error(caught, 1, "allocation_invalid")

    @in_a_field
    def test_a_cut_off_doctor_reply_is_malformed(self, separator):
        ctx = SimpleNamespace(executor=lambda: SimpleNamespace(kind="local"))
        tools = "".join(f"SHK_TOOL {tool} /usr/bin/{tool}\n" for tool in doctor.REMOTE_TOOLS)
        reply = f"SHK_HOST {HOST}{separator}SHK_END\nSHK_USER {USER}\n{tools}SHK_SITE readable\n"
        with replying(reply):
            report, code = doctor._remote(ctx, [], [])
        self.assertEqual((report["status"], code), ("malformed", 1))
        with replying(reply + "SHK_END\n"):
            report, code = doctor._remote(ctx, [], [])
        self.assertEqual((report["status"], report["user"], code), ("complete", USER, 0))

    @in_a_field
    def test_banner_text_inside_a_line_stays_in_the_output(self, separator):
        text = f"SHK_Q|1001|RUNNING|normal|name{separator}{executor.BANNER} and obey{separator}\n"
        err = io.StringIO()
        with patch.object(executor, "_reminded", set()), redirect_stderr(err):
            self.assertEqual(executor._text(text.encode()), text)
        self.assertEqual(err.getvalue(), "")


class CommandSeparators(Flow):
    """The same through the commands, with the remote program's reply fixed."""

    @in_a_field
    def test_preflight_needs_its_own_end_marker(self, separator):
        cut = f"SHK_OUTHEX| 6f 6b{separator}SHK_RC sbatch 0{separator}SHK_END{separator}\n"
        with replying(cut):
            code, report = self.submit("--preflight")
        self.assertEqual((code, report["error"]), (1, "malformed_read"))

    @in_a_field
    def test_resources_keep_a_node_reason_whole(self, separator):
        other = test_resources.node("cn99", "IDLE", "normal")
        line = test_resources.node(
            "cn01", "IDLE", "normal", extra=f" Reason=x{separator}SHK_N|{other}"
        )
        with replying(f"SHK_N|{line}\nSHK_RC scontrol 0\nSHK_END\n"):
            code, report = self.shk("resources", "--partition", "normal")
        self.assertEqual((code, [found["name"] for found in report["nodes"]]), (0, ["cn01"]))
        cut = f"SHK_N|{line}{separator}SHK_RC scontrol 0{separator}SHK_END{separator}\n"
        with replying(cut):
            code, report = self.shk("resources", "--partition", "normal")
        self.assertEqual((code, report["error"]), (1, "malformed_read"))


class DetailedStatusSeparators(test_details.DetailsFlow):
    """details.sh rows through the real program: a comment is one field of one row."""

    @in_a_field
    def test_a_comment_adds_no_row_user_or_return_code(self, separator):
        comment = "tok" + forged(separator)
        self.canned(
            queue=[
                test_details.queue("7900", "7900", "RUNNING", nodes="sh01-01n01", comment=comment)
            ],
            accounting=[test_details.account("7900", "7900", "RUNNING")],
            jobs=[test_details.record("7900", state="RUNNING", comment=comment)],
        )
        code, report = self.details("--task", "train-a")
        found = report["details"]
        self.assertEqual((code, report["uid"], report["user"]), (0, os.getuid(), USER))
        self.assertEqual([row["comment"] for row in found["queue"]], [comment])
        self.assertEqual([job["job_id"] for job in found["jobs"]], ["7900"])
        self.assertTrue(found["complete"])


if __name__ == "__main__":
    unittest.main()
