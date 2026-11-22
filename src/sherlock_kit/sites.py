r"""Packaged sites: the stable facts that set one Slurm cluster apart from another.

SHERLOCK and MARLOWE are frozen literals and SITES maps each name to its record; site(name)
looks one up. A record holds no limit, duration, price or node shape, only facts that do not
change with demand:

- the default OpenSSH aliases of the control and the data host (one login-node alias on Marlowe,
  where rsync goes through the login nodes);
- the Slurm client environment of the remote programs: None where the clients are on the login
  PATH (Sherlock), else the directory appended to PATH and the SLURM_CONF path (Marlowe);
- the account rule: None where no account is set (Sherlock), else a pattern a configured account
  must fullmatch (Marlowe base project accounts, marlowe-<letters><digits>);
- on_cluster(hostname, env): whether this machine is part of the cluster. It chooses the local
  executor for an already selected site and never selects a site. Sherlock: a login node
  (^sh\d+-ln\d+$), SLURM_CLUSTER_NAME=sherlock, or an sh<N>- node inside a job. Marlowe: its
  Slurm configuration file exists here (hostnames and ClusterName are vendor defaults there);
- the site instructions file that doctor checks, or None;
- whether submit runs sbatch --test-only before staging;
- the packaged partition table, the rules files by kind and the skill name.

A reply from a site other than Sherlock carries reply_fields(site), {"site": NAME}, and the shk
commands its hints and exit 5 follow-up name carry selector(site), ["--site", NAME]; Sherlock
adds nothing to either, so its replies and texts are those of a kit without sites.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class SlurmEnvironment:
    """Where the Slurm clients are when the login PATH lacks them: appended to PATH, SLURM_CONF."""

    bin: str
    conf: str


@dataclass(frozen=True)
class Site:
    """The packaged facts of one cluster; see the module docstring for each field."""

    name: str
    display: str
    control_host: str
    data_host: str
    slurm: SlurmEnvironment | None
    account: re.Pattern[str] | None
    on_cluster: Callable[[str, Mapping[str, str]], bool]
    instructions: str | None
    test_before_submit: bool
    partitions: str
    rules: Mapping[str, str]
    skill: str


def _on_sherlock(hostname: str, env: Mapping[str, str]) -> bool:
    """A login node, SLURM_CLUSTER_NAME=sherlock, or an sh<N>- compute node inside a job."""
    short = hostname.split(".")[0].lower()
    return bool(
        re.fullmatch(r"sh\d+-ln\d+", short)
        or env.get("SLURM_CLUSTER_NAME") == "sherlock"
        or (re.match(r"sh\d+-", short) and env.get("SLURM_JOB_ID"))
    )


MARLOWE_SLURM = SlurmEnvironment(
    bin="/cm/shared/apps/slurm/current/bin",
    conf="/cm/shared/apps/slurm/var/etc/slurm/slurm.conf",
)


def _on_marlowe(_hostname: str, _env: Mapping[str, str]) -> bool:
    """Marlowe's Slurm configuration file is a file on this machine."""
    return os.path.isfile(MARLOWE_SLURM.conf)


SHERLOCK = Site(
    name="sherlock",
    display="Sherlock",
    control_host="sherlock-plain",
    data_host="sherlock-dtn",
    slurm=None,
    account=None,
    on_cluster=_on_sherlock,
    instructions="/etc/agents/AGENTS.md",
    test_before_submit=False,
    partitions="partitions.json",
    rules=MappingProxyType({"workstation": "workstation.md", "cluster": "cluster.md"}),
    skill="sherlock-kit",
)
MARLOWE = Site(
    name="marlowe",
    display="Marlowe",
    control_host="marlowe-plain",
    data_host="marlowe-plain",
    slurm=MARLOWE_SLURM,
    account=re.compile(r"marlowe-[a-z]+[0-9]+"),
    on_cluster=_on_marlowe,
    instructions=None,
    test_before_submit=True,
    partitions="partitions-marlowe.json",
    rules=MappingProxyType({
        "workstation": "marlowe-workstation.md",
        "cluster": "marlowe-cluster.md",
    }),
    skill="sherlock-kit-marlowe",
)
SITES: dict[str, Site] = {SHERLOCK.name: SHERLOCK, MARLOWE.name: MARLOWE}


def site(name: str) -> Site:
    """The packaged site of this name (a name that config or argparse already checked)."""
    return SITES[name]


def selector(found: Site) -> list[str]:
    """The global options that select found: none for Sherlock, ["--site", NAME] otherwise."""
    return [] if found.name == SHERLOCK.name else ["--site", found.name]


def command(found: Site) -> str:
    """The shk command prefix that hints name: "shk", or "shk --site NAME" off Sherlock."""
    return shlex.join(["shk", *selector(found)])


def reply_fields(found: Site) -> dict[str, str]:
    """The reply fields a site adds beside "executor": none for Sherlock, {"site": NAME} otherwise."""
    return {} if found.name == SHERLOCK.name else {"site": found.name}
