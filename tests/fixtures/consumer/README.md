# Synthetic consumer's first day

This fixture uses no credentials, remote resources, or scientific data. Both agents
receive the exact `policy_projection()` block through their global entry points;
client selection/precedence is checked separately by the delivery acceptance gate.

1. Install the reviewed frozen pin; compare `shk policy --identity` with its metadata.
2. Run local `shk doctor`; missing integration stays unverified, not silently active.
3. Explicit remote diagnostics reuse an already authenticated master. They cannot
   submit jobs and do not execute shells on a DTN.
4. Read `workload.json` as an illustrative consumer contract, not a submission or
   borrowed-access authorization. The source/runtime/input/output and validator
   fields must be real identities before a live adopter is accepted.
5. Tests use `fake_ssh.py` to simulate response loss, authentication, and literal
   argument transport. An unknown mutation must never trigger another dispatch.

This fixture proves the foundation contract, not a live pilot or scientific result.
