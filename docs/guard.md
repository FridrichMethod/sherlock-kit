# Optional guard and thin adapters

The guard is provisional until an adopter and both actual clients pass acceptance.
`shk guard --client claude|codex` reads a bounded JSON event from stdin and returns
exit 0. A proven denial emits `hookSpecificOutput` with `hookEventName=PreToolUse`,
`permissionDecision=deny`, and a useful reason. Other commands produce no decision.
Unknown never means safe, approved, or policy compliant. There are no network calls,
state writes, locks, authentication attempts, command rewrites, or automatic retries.

Recognized violations are intentionally narrow:

- A simple direct `ssh` command targeting a literal Sherlock alias/domain, running
  `watch` of `squeue`, `sacct`, `sstat`, or `sinfo` with an explicit interval
  below 60 seconds; or local `watch` around that direct Sherlock SSH query.
- Direct invocation of the explicitly designated purge-evasion helper basenames
  `sherlock-scratch-purge-refresh` and `sherlock-scratch-purge-keepalive`, including
  a direct interpreter invocation or a recognized Sherlock SSH invocation.
  These names are a prohibited catalog, not shipped helper programs. No existing
  legacy helper identity was established during reference inspection. The pure
  API may receive additional reviewed helper basenames; do not infer a catalog
  from filenames, user paths, or arbitrary `touch`/`cp` commands.

Compound shells, Python programs, shell substitutions, continued `write_stdin`
sessions, aliases/functions, customized unknown SSH aliases, alternate polling
wrappers, and indirect helper invocations may bypass these patterns. Even a
60-second `watch` is unknown here: the full site policy still prohibits `watch`
and shell polling loops. Existing legacy wrappers are not certified by this guard.
Without an explicit interval, `watch` cadence is unknown: installed procps-ng
4.0.4 documentation confirms that `WATCH_INTERVAL` can override its default.
Typed orchestration must enforce its own requests, grants, uncertainty, and paths.

## One registration owner

Dotfiles installs the frozen package, projects global policy separately, preserves
existing registrations, and owns the single opt-in hook registration per client.
The thin Claude plugin has only its manifest and namespaced skill; it registers
no hooks. Codex receives the same skill contract through its skill discovery path.
Both call the installed `shk`, rather than bundling another orchestration engine.

Suggested Claude handler in the existing settings merge:

```json
{"matcher":"^Bash$","hooks":[{"type":"command","command":"shk guard --client claude","timeout":5}]}
```

Suggested Codex inline configuration, preserving all existing handler arrays:

```toml
[[hooks.PreToolUse]]
matcher = "^Bash$"
[[hooks.PreToolUse.hooks]]
type = "command"
command = "shk guard --client codex"
timeout = 5
```

Resolve `shk` to the reviewed frozen installation before registration. Use a
synchronous handler; an asynchronous hook cannot block. Do not register both an
inline handler and a separate `hooks.json` handler for the same guard.

## Trust and acceptance evidence

Checked 2026-10-07 against Codex 0.161.0 and Claude 2.1.293. Codex discovers
`hooks.json` beside active config layers or inline `[hooks]`, not `hooks.toml`.
Use `/hooks` to review and trust the current definition hash; changed definitions
are skipped until trusted. Do not bypass trust. Its generated local app-server
schema exposes `hooks/list`: `currentHash`, `enabled`, `trustStatus`, `sourcePath`,
and per-layer errors/warnings. Trust states include untrusted, trusted, modified,
and managed. Discovery alone does not establish active enforcement.

Codex normalizes unified exec hooks to `Bash` and `tool_input.command`; the adapter
also accepts direct fixture input `exec_command`/`cmd`. Later `write_stdin` does
not invoke PreToolUse again. Never emit `ask`: current Codex rejects it and proceeds.
Missing, skipped, malformed, failed, or timed-out hooks must remain inactive in
the acceptance report; a nonzero exit is not a portable fail-closed strategy.
Malformed/oversized guard input exits 0 with diagnostic stderr and no decision.

Before activation, prove installed revision and projection consistency, effective
registration and current trust, a negative blocking smoke test using a fake SSH
executable in a temporary consumer, and normal useful commands through both actual
clients. No live Sherlock command or real credential is needed. Verify skill
discovery separately. A unit JSON denial is not an actual-client blocking test.

Sources: [Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Claude hooks](https://code.claude.com/docs/en/hooks),
[Claude plugin manifest](https://code.claude.com/docs/en/plugins-reference),
[Codex skill discovery](https://learn.chatgpt.com/docs/build-skills).
