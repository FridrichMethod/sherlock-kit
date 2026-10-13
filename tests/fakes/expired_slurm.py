"""Slurm controller expiry fixture: live queue and durable allocation accounting differ."""

import json
import os
import sys
from pathlib import Path


def main(tool, args):
    path = Path(os.environ["FAKE_EXPIRY_STATE"])
    state = json.loads(path.read_text())
    state.setdefault("calls", []).append({"tool": tool, "argv": args})
    path.write_text(json.dumps(state))
    if tool == "squeue":
        if state.get("queue_rc"):
            print("slurm_load_jobs error: Unable to contact slurm controller", file=sys.stderr)
            return state["queue_rc"]
        jobs = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--jobs=")), "")
        live = {row.split("|")[0].split("_")[0] for row in state["queue"]}
        if jobs and any(job.split("_")[0] not in live for job in jobs.split(",")):
            print("slurm_load_jobs error: Invalid job id specified", file=sys.stderr)
            return 1
        print("\n".join(state["queue"]))
    elif tool == "sacct":
        print("\n".join(state["accounting"]))
    elif tool == "scontrol":
        job = args[2]
        if job not in state["jobs"]:
            print("Invalid job id specified", file=sys.stderr)
            return 1
        print(state["jobs"][job])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
