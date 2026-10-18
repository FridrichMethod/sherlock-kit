"""`shk resources` through the real resources.sh against a fake `scontrol show nodes`."""

import os
import shlex
import sys

from fakes import SRC
from test_flows import Flow

sys.path.insert(0, str(SRC))
from sherlock_kit import executor


def node(
    name,
    state,
    partitions,
    *,
    cpus="CPUAlloc=0 CPUEfctv=20 CPUTot=20",
    cfg="cpu=20,mem=125G,billing=51",
    alloc="",
    extra="",
):
    """One `scontrol show nodes --oneliner` line in the Slurm 25.11.8 layout (synthetic host)."""
    return (
        f"NodeName={name} Arch=x86_64 CpuBind=cores CoresPerSocket=10  {cpus} CPULoad=0.01 "
        "AvailableFeatures=IB:EDR,CPU_MNF:INTEL,NO_GPU ActiveFeatures=IB:EDR,CPU_MNF:INTEL,NO_GPU "
        f"Gres=(null) NodeAddr={name} NodeHostName={name} Version=25.11.8 "
        "OS=Linux 5.14.0-1.el9.x86_64 #1 SMP Thu Sep 10 04:02:55 UTC 2026  RealMemory=128000 "
        f"AllocMem=0 FreeMem=116599 Sockets=2 Boards=1 State={state} ThreadsPerCore=1 "
        f"TmpDisk=200000 Weight=142091 Owner=N/A MCS_label=N/A Partitions={partitions}  "
        "BootTime=2026-10-07T14:32:22 SlurmdStartTime=2026-10-16T22:59:38 "
        "LastBusyTime=2026-10-18T09:42:33 ResumeAfterTime=None "
        f"CfgTRES={cfg} AllocTRES={alloc} CurrentWatts=368 AveWatts=887{extra}"
    )


class ResourcesFlows(Flow):
    def scontrol(self, *lines, rc=0, stderr=""):
        """A fake scontrol that answers only `show nodes --oneliner`, with these lines."""
        directory = self.tmp / "tools"
        directory.mkdir(exist_ok=True)
        text = self.tmp / "nodes.txt"
        text.write_text("".join(f"{line}\n" for line in lines))
        script = directory / "scontrol"
        script.write_text(
            "#!/bin/sh\n"
            '[ "$*" = "show nodes --oneliner" ] || { echo "unexpected: scontrol $*" >&2; exit 97; }\n'
            f"cat {shlex.quote(str(text))}\n"
            f"printf '%s' {shlex.quote(stderr)} >&2\n"
            f"exit {rc}\n"
        )
        script.chmod(0o755)
        self.knobs(PATH=f"{directory}{os.pathsep}{os.environ['PATH']}")

    def names(self, report):
        return [found["name"] for found in report["nodes"]]

    def test_failed_scontrol_read_is_malformed_with_its_diagnostics(self):
        error = "slurm_load_node error: Unable to contact slurm controller (connect failure)\n"
        self.scontrol(rc=1, stderr=error)
        code, report = self.shk("resources", "--partition", "gpu")
        self.assertEqual((code, report["ok"], report["error"]), (1, False, "malformed_read"))
        self.assertIn("scontrol rc=1", report["message"])
        self.assertIn("Unable to contact slurm controller", report["message"])
        self.assertIn("retry once after 60 s", report["hint"])
        # A failed read never passes for capacity evidence, even with some lines printed.
        self.scontrol(node("cn01", "IDLE", "gpu"), rc=1, stderr=error)
        code, report = self.shk("resources", "--partition", "gpu")
        self.assertEqual((code, report["error"]), (1, "malformed_read"))

    def test_remote_program_ships_only_the_requested_partitions(self):
        self.scontrol(
            node("cn01", "MIXED", "stat,owners"),
            node("cn02", "IDLE", "normal"),
            node("cn03", "IDLE", "gpu,owners"),
        )
        local = executor.LocalExecutor(command_timeout=30)
        reply = executor.run_program(local, "resources", ["gpu", "stat"])
        shipped = [
            line.split()[0] for line in reply.stdout.splitlines() if line.startswith("SHK_N|")
        ]
        self.assertEqual(shipped, ["SHK_N|NodeName=cn01", "SHK_N|NodeName=cn03"])
        self.assertEqual(reply.stdout.splitlines()[-2:], ["SHK_RC scontrol 0", "SHK_END"])

    def test_schedulable_follows_state_flags_and_free_capacity_stays_arithmetic(self):
        states = {
            "MIXED": True,
            "ALLOCATED": True,
            "IDLE": True,
            "MIXED+PLANNED": True,
            "IDLE+PLANNED": True,
            "MIXED+COMPLETING": True,
            "IDLE+DRAIN": False,
            "MIXED+DRAIN": False,
            "DOWN+NOT_RESPONDING": False,
            "DOWN+DRAIN+NOT_RESPONDING": False,
            "ALLOCATED+NOT_RESPONDING": False,
            "IDLE+COMPLETING+NOT_RESPONDING": False,
            "IDLE+COMPLETING+DRAIN+NOT_RESPONDING": False,
            "DOWN+DRAIN+INVALID_REG": False,
            "IDLE+MAINTENANCE+RESERVED": False,
            "IDLE+RESERVED": False,
            "IDLE+POWERED_DOWN": False,
            "IDLE+POWERING_DOWN": False,
            "IDLE+REBOOT_REQUESTED": False,
            "MIXED+REBOOT_ISSUED": False,
            "IDLE+FAIL": False,
            "FUTURE": False,
            "UNKNOWN": False,
        }
        self.scontrol(
            *(node(f"cn{i:02d}", state, "owners") for i, state in enumerate(states)),
            node("cn99", "IDLE+DRAIN", "owners", cfg="cpu=20,mem=125G,billing=111,gres/gpu=4"),
        )
        code, report = self.shk("resources", "--partition", "owners")
        self.assertEqual(code, 0)
        found = {entry["state"]: entry["schedulable"] for entry in report["nodes"][:-1]}
        self.assertEqual(found, states)
        drained = report["nodes"][-1]
        self.assertEqual(
            (drained["schedulable"], drained["free_cpus"], drained["free_gpus"]), (False, 20, 4)
        )
        self.assertTrue(report["complete"])

    def test_missing_state_is_unknown_schedulability_and_incomplete(self):
        self.scontrol(node("cn01", "IDLE", "normal").replace("State=IDLE ", ""))
        code, report = self.shk("resources", "--partition", "normal")
        self.assertEqual(code, 0)
        self.assertIsNone(report["nodes"][0]["schedulable"])
        self.assertFalse(report["complete"])

    def test_verdict_and_cadence_precede_nodes_and_provenance_is_printed_once(self):
        self.scontrol(node("cn01", "IDLE", "normal"), node("cn02", "IDLE", "normal"))
        code, report = self.shk("resources", "--partition", "normal")
        self.assertEqual(code, 0)
        keys = list(report)
        self.assertEqual(keys[-1], "nodes")
        for key in ("complete", "malformed", "next_check_after", "provenance"):
            self.assertLess(keys.index(key), keys.index("nodes"))
        self.assertEqual(report["provenance"]["memory"], "CfgTRES minus AllocTRES")
        self.assertIn("DRAIN", report["provenance"]["schedulable"])
        self.assertTrue(all("provenance" not in entry for entry in report["nodes"]))
        self.assertTrue(all("CfgTRES=" in entry["raw"] for entry in report["nodes"]))
