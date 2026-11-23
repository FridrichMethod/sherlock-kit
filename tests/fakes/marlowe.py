"""Marlowe-shaped Slurm 25.05.2 text, with synthetic ids, names, hosts and paths.

The shapes follow what was read on Marlowe: ClusterName ``slurm``; 31 identical nodes ``n01``
to ``n31`` with 112 CPUs, untyped GRES ``Gres=gpu:8(S:0-1)`` (no GPU model anywhere), the one
feature ``location=local`` and no CoreSpecCount, MemSpecLimit or GresUsed field; jobs on
partition ``preempt`` with the base project account and QOS ``normal``; Comment kept in
accounting (AccountingStoreFlags=job_comment).

- ``job_record(job, name, ...)``: one ``scontrol show job --oneliner`` record in the field order
  Slurm 25.05.2 printed for a running batch job, with Comment (absent there: the job had none)
  after WorkDir and TresPerJob before TresPerTask, where the Slurm formatter puts them;
- ``node(name, state, ...)``: one ``scontrol show nodes --oneliner`` line;
- ``queue(...)`` and ``account(...)``: one details.sh squeue and sacct row (field lists for
  ``CannedSlurm``), and ``tres(cpus, gpus)`` the TRES string of such a job;
- ``pidinfo(job)`` and ``listpids(job, *rows)``: ``scontrol pidinfo`` and ``scontrol listpids
  JOBID`` as read in a Marlowe job (docs/validation/observed-marlowe.md, "shk inside a batch
  job"): pidinfo's two lines, and listpids' header, the batch step's rows and the extern row; the
  column widths follow the printf formats of the Slurm 25.05.2 source (src/scontrol/info_job.c).

A running CPU-only job's record and AllocTRES were read on Marlowe; an array element's record
(ArrayJobId, ArrayTaskId) and a GPU job's AllocTRES follow the Slurm source, not a record read
there.
"""

import os
import pwd

from fakes import MARLOWE_ACCOUNT

USER = pwd.getpwuid(os.getuid()).pw_name
UID = os.getuid()
SCRATCH = "/scratch/m000000/USER/demo"
SUBMIT, START, END = "2026-11-21T17:05:08", "2026-11-21T17:05:34", "2026-11-21T17:15:34"


def tres(cpus=1, gpus=0):
    """A Marlowe job's ReqTRES or AllocTRES (8 GB per GPU job CPU, 1 GB per CPU job CPU)."""
    memory = f"{cpus * 8}G" if gpus else f"{cpus}G"
    return f"cpu={cpus},mem={memory},node=1,billing={cpus}" + (f",gres/gpu={gpus}" if gpus else "")


def job_record(
    job,
    name,
    *,
    state="RUNNING",
    array=None,
    task=None,
    token=None,
    cpus=1,
    gpus=0,
    held=False,
    **changes,
):
    """One 25.05.2 oneliner record; held is a user hold, and a value of None leaves a key out."""
    started = state not in ("PENDING", "REQUEUE_HOLD")
    run = f"{SCRATCH}/runs/{name}"
    fields = {
        "JobId": job,
        "ArrayJobId": array,
        "ArrayTaskId": task,
        "JobName": name,
        "UserId": f"{USER}({UID})",
        "GroupId": f"{USER}(5000)",
        "MCS_label": "N/A",
        "Priority": "0" if held else "1007039",
        "Nice": "0",
        "Account": MARLOWE_ACCOUNT,
        "QOS": "normal",
        "WCKey": "*",
        "JobState": state,
        "Reason": "JobHeldUser" if held else ("None" if started else "Priority"),
        "Dependency": "(null)",
        "Requeue": "1",
        "Restarts": "0",
        "BatchFlag": "1",
        "Reboot": "0",
        "ExitCode": "0:0",
        "RunTime": "00:00:03" if started else "00:00:00",
        "TimeLimit": "00:10:00",
        "TimeMin": "N/A",
        "SubmitTime": SUBMIT,
        "EligibleTime": "Unknown" if held else SUBMIT,
        "AccrueTime": "Unknown" if held else SUBMIT,
        "StartTime": START if started else "Unknown",
        "EndTime": END if started else "Unknown",
        "Deadline": "N/A",
        "PreemptEligibleTime": "2026-11-21T17:10:34" if started else None,
        "PreemptTime": "None" if started else None,
        "SuspendTime": "None",
        "SecsPreSuspend": "0",
        "LastSchedEval": START,
        "Scheduler": "Backfill" if started else "Main",
        "Partition": "preempt",
        "AllocNode:Sid": "login-01:4242",
        "ReqNodeList": "(null)",
        "ExcNodeList": "(null)",
        "NodeList": "n31" if started else "(null)",
        "BatchHost": "n31" if started else None,
        "NumNodes": "1",
        "NumCPUs": str(cpus),
        "NumTasks": "1",
        "CPUs/Task": str(cpus),
        "ReqB:S:C:T": "0:0:*:*",
        "ReqTRES": tres(cpus, gpus),
        "AllocTRES": tres(cpus, gpus) if started else "(null)",
        "Socks/Node": "*",
        "NtasksPerN:B:S:C": "0:0:*:*",
        "CoreSpec": "*",
        "MinCPUsNode": str(cpus),
        "MinMemoryNode": f"{cpus * 8}G" if gpus else f"{cpus}G",
        "MinTmpDiskNode": "0",
        "Features": "(null)",
        "DelayBoot": "00:00:00",
        "OverSubscribe": "OK",
        "Contiguous": "0",
        "Licenses": "(null)",
        "LicensesAlloc": "(null)",
        "Network": "(null)",
        "Command": f"{run}/.shk/{name}.{job}.sh",
        "WorkDir": run,
        "Comment": token,
        "StdErr": f"{run}/slurm-{job}.err",
        "StdIn": "/dev/null",
        "StdOut": f"{run}/slurm-{job}.out",
        "TresPerJob": f"gres/gpu:{gpus}" if gpus else None,
        "TresPerTask": f"cpu={cpus}",
        **changes,
    }
    return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


def node(name, state="MIXED", *, cpus=0, memory_mb=0, gpus=0, partitions="hero,batch,preempt"):
    """One 25.05.2 oneliner node line with cpus, memory_mb and gpus allocated."""
    allocated = f"cpu={cpus},mem={memory_mb}M,gres/gpu={gpus}" if cpus else ""
    return (
        f"NodeName={name} Arch=x86_64 CoresPerSocket=56 CPUAlloc={cpus} CPUEfctv=112 CPUTot=112 "
        "CPULoad=10.63 AvailableFeatures=location=local ActiveFeatures=location=local "
        f"Gres=gpu:8(S:0-1) NodeAddr={name} NodeHostName={name} Version=25.05.2 "
        "OS=Linux 5.15.0-1106-nvidia #116-Ubuntu SMP Tue Oct 14 10:00:00 UTC 2026  "
        f"RealMemory=1950000 AllocMem={memory_mb} FreeMem=1500000 Sockets=2 Boards=1 "
        f"State={state} ThreadsPerCore=1 TmpDisk=0 Weight=1 Owner=N/A MCS_label=N/A "
        f"Partitions={partitions}  BootTime=2026-11-01T08:00:00 "
        "SlurmdStartTime=2026-11-01T08:05:00 LastBusyTime=2026-11-21T17:00:00 "
        "ResumeAfterTime=None CfgTRES=cpu=112,mem=1950000M,billing=112,gres/gpu=8 "
        f"AllocTRES={allocated} CurrentWatts=0 AveWatts=0"
    )


def pidinfo(job, remaining=594):
    """scontrol pidinfo of a process of job: its end (END in ctime form) and remaining seconds."""
    return (
        f"Slurm JobId={job} ends at Sat Nov 21 17:15:34 2026\n"
        f"Job remaining time is {remaining} seconds"
    )


def listpids(job, *rows):
    """scontrol listpids JOB: the header, a row per (pid, step, local id, global id), the extern row.

    As read on Marlowe: the batch shell prints step "batch" with ids 0, each of its children
    "-" for both ids, and the job's extern step one row with PID -1 and ids 0.
    """
    header = f"{'PID':<8} {'JOBID':<8} {'STEPID':<8} {'LOCALID':<7} {'GLOBALID':<8}\n"
    return header + "".join(
        f"{pid:<8} {job:<8} {step:<8} {local:<7} {task:<8} \n"
        for pid, step, local, task in (*rows, ("-1", "extern", "0", "0"))
    )


def queue(job, physical, state, name, *, cpus=1, token=""):
    """One details.sh squeue row of a Marlowe job: pending rows have no node, start or end."""
    started = state != "PENDING"
    return [
        job,
        physical,
        state,
        "preempt",
        "0:03" if started else "0:00",
        str(cpus),
        SUBMIT,
        START if started else "N/A",
        END if started else "N/A",
        "n31" if started else "",
        str(UID),
        token,
        name,
    ]


def account(job, physical, state, name, *, cpus=1, gpus=0, elapsed=7, restarts=0, token=""):
    """One details.sh sacct row of a Marlowe job (Cluster slurm, the comment kept)."""
    started = state != "PENDING"
    return [
        job,
        physical,
        "slurm",
        f"1826843840000{physical}",
        state,
        "preempt",
        str(elapsed if started else 0),
        str(cpus if started else 0),
        tres(cpus, gpus) if started else "",
        SUBMIT,
        START if started else "Unknown",
        "Unknown" if state in ("PENDING", "RUNNING") else "2026-11-21T17:05:41",
        str(restarts),
        "0:0",
        "n31" if started else "None assigned",
        str(UID),
        token,
        name,
    ]
