# Documentation

These guides describe the supported toolkit and its operating constraints.

| Guide | Purpose |
| --- | --- |
| [Getting started and CLI reference](../README.md) | Installation, commands, configuration, JSON results and exit codes |
| [GPU campaign scheduling](campaign-throughput.md) | Compatible GPUs, soft preferences, partitions, resumable bundles and parallel work |
| [Allocation proofs](allocation.md) | Job, array element, process and step identity inside an allocation |
| [CPU thread budgets](cpu-threads.md) | Numerical library thread defaults on the host and in containers |
| [Sherlock policy](sherlock-policy.md) | Site-rule provenance, owner preferences and toolkit policy choices |
| [Design](design.md) | Runtime components, contracts, state resolution and accepted limitations |
| [Contributing](../CONTRIBUTING.md) | Development setup, offline tests and isolated packaging checks |
| [Changelog](../CHANGELOG.md) | Release history |

## Compatibility evidence

[Observed Sherlock and Slurm behavior](validation/observed-slurm.md) records
live observations underlying parser, accounting and allocation behavior, with
explicit validation limits. It is supporting evidence, not a deployment plan,
current cluster inventory or a guarantee that the current release was tested
live. Current resources and site policy must be discovered as the operating
guides require.

Implementation plans and worker handoffs belong in task storage as described in
[the repository instructions](../AGENTS.md). Completed implementation history
is available in Git; maintained conclusions belong in the relevant guide.
