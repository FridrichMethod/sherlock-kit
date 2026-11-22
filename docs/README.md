# Documentation

These guides describe the supported toolkit and its operating constraints.

| Guide | Purpose |
| --- | --- |
| [Overview](../README.md) | What `shk` does, a short install and a quick start |
| [Installation and connection](install.md) | Install forms, environments on Sherlock and inside jobs, SSH control masters, Marlowe setup |
| [Command reference](cli.md) | Every command and its JSON reply, configuration and exit codes |
| [GPU campaign scheduling](campaign-throughput.md) | Compatible GPUs, soft preferences, partitions, resumable bundles and parallel work |
| [Allocation proofs](allocation.md) | Job, array element, process and step identity inside an allocation, and GPU SKU candidates |
| [CPU thread budgets](cpu-threads.md) | Numerical library thread defaults on the host and in containers |
| [Sherlock policy](sherlock-policy.md) | Site-rule provenance, owner preferences and toolkit policy choices |
| [Marlowe policy](marlowe-policy.md) | Provenance of the Marlowe rules: documentation pages and their retrieval date, owner choices and toolkit choices |
| [Design](design.md) | Runtime components, sites, contracts, state resolution and accepted limitations |
| [Contributing](../CONTRIBUTING.md) | Development setup, offline tests and isolated packaging checks |
| [Changelog](../CHANGELOG.md) | Release history |

## Compatibility evidence

[Observed Sherlock and Slurm behavior](validation/observed-slurm.md) records
live observations underlying parser, accounting and allocation behavior, and
the source-confirmed Slurm facts that are not observed live, with explicit
validation limits. It is supporting evidence, not a deployment plan,
current cluster inventory or a guarantee that the current release was tested
live. Current resources and site policy must be discovered as the operating
guides require.

[Observed Marlowe and Slurm behavior](validation/observed-marlowe.md) records
what was seen live on Marlowe (Slurm 25.05.2), the client environment, the
submission verdicts, queue waits and preemption and `shk`'s own commands
there, with the same limits: observations of one day, not site guarantees.

Implementation plans, worker handoffs and check logs are not part of these
guides; [the repository instructions](../AGENTS.md) say where they are kept.
Maintained conclusions belong in the relevant guide.
